#!/usr/bin/env python3
"""Anthropic Messages and OpenAI Chat Completions APIs served by Cursor's models.

Speaks Anthropic's and OpenAI's wire formats to clients and Cursor's private
agent.v1.AgentService/Run Connect/protobuf stream to the backend (see session.py).

Supported
  POST /v1/messages                streaming (SSE) and non-streaming
  POST /v1/messages/count_tokens
  POST /v1/chat/completions        streaming (SSE) and non-streaming
  GET  /v1/models                  every model the signed-in account can use
  GET  /login                      authorisation URL for a browser (PKCE) login
  system (str or blocks), multi-turn history, image blocks (base64),
  document/PDF blocks, tools -> native Cursor tool calls (tool_use / tool_result),
  extended thinking blocks, usage, stop_reason, x-api-key / bearer gate.

Approximated client-side (Cursor's protocol has no knob for them)
  stop_sequences  cut when the sequence appears (stop_reason stop_sequence)
  max_tokens      cut on a ~4 chars/token estimate (stop_reason max_tokens)
  tool_choice     expressed as an instruction, not enforced by the server

Not representable at all (accepted and ignored)
  temperature, top_p, top_k, prompt-caching controls, and the thinking block
  `signature`: Cursor never sends Anthropic's signature down, so "" is emitted.

Run:   PORT=8787 API_KEY=sk-local python -m cursor2api serve
Use:   ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_API_KEY=sk-local claude
"""
import base64, errno, hmac, json, os, struct, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth as auth_mod
from . import h2stream, models, openai_api
from .auth import AuthError
from .session import HOST, Session, split_client_type

BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
API_KEY = os.environ.get("API_KEY", "")          # empty = no auth
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-fable-5")
# Safety net only: a turn ends on turn_ended or when the stream closes. Long file
# writes and long reasoning go minutes without a frame, so this stays generous.
IDLE_STOP = float(os.environ.get("IDLE_STOP", "180"))
FIRST_TIMEOUT = float(os.environ.get("FIRST_TIMEOUT", "90"))  # upstream never answered
# No user-visible output for this long after the upstream started answering:
# control chatter and heartbeats must not keep a zero-output turn alive until
# the 900s hard timeout, because downstream used to log that as a success.
FIRST_OUTPUT_TIMEOUT = float(os.environ.get("FIRST_OUTPUT_TIMEOUT", "240"))
WEB = os.environ.get("CURSOR2API_WEB", "1") == "1"
# auto: think only when the caller asks for it or names a thinking variant. Cursor's
# defaults turn reasoning on for every model, which triples the time to first token.
THINKING = os.environ.get("CURSOR2API_THINKING", "auto")
PING = float(os.environ.get("PING_INTERVAL", "5"))   # SSE keepalive while upstream is quiet
DEBUG = bool(os.environ.get("DBG"))
# One structured line per turn. The service used to log nothing but its own
# restarts, so every incident had to be reconstructed from client transcripts.
LOG_TURNS = os.environ.get("CURSOR2API_LOG_TURNS", "1") == "1"
MAX_BODY = int(os.environ.get("CURSOR2API_MAX_BODY", str(64 * 1024 * 1024)))

# ---- live tool-call sessions ------------------------------------------------
# A turn that stops at tool_use leaves the Run stream open waiting for the
# result. Instead of closing it and replaying the whole history as text on the
# next request (which the model sometimes distrusts and re-runs), the session is
# parked here keyed by tool_use id; a follow-up request whose last user message
# is purely the matching tool_result(s) resumes the same live stream.
LIVE_TTL = float(os.environ.get("CURSOR2API_LIVE_TTL", "150"))
_live_sessions = {}      # tool_use_id -> {session, exec: (eid, esid), ts, model}
_live_lock = threading.Lock()


def _live_gc_locked():
    now = time.time()
    for key in [k for k, v in _live_sessions.items() if now - v["ts"] > LIVE_TTL]:
        entry = _live_sessions.pop(key)
        if not any(v["session"] is entry["session"] for v in _live_sessions.values()):
            try:
                entry["session"].close()
            except Exception:
                pass


def _live_gc_loop():
    """Reap expired parked sessions on a timer.

    Collection used to run only when a new request arrived, so an abandoned
    tool call held its H2 connection and reader thread until traffic resumed.
    """
    period = min(30.0, max(5.0, LIVE_TTL / 4))
    while True:
        time.sleep(period)
        try:
            with _live_lock:
                _live_gc_locked()
        except Exception:
            pass


def register_live(turn):
    """Park the open session after a tool_use turn. True when parked."""
    if turn.stop_reason != "tool_use" or not turn.pending or turn.session is None:
        return False
    with _live_lock:
        _live_gc_locked()
        for tu in turn.pending:
            eid, esid = tu.get("exec") or (None, None)
            _live_sessions[tu["id"]] = {"session": turn.session,
                                        "exec": (eid, esid),
                                        "ts": time.time(),
                                        "model": turn.model,
                                        "client_type": turn.client_type}
    return True


def claim_live(body, model, client_type):
    """[(entry, tool_result_block)] when the request purely answers a parked
    session's tool calls, else None. A parked stream keeps the usage pool it was
    opened against, so it may only be resumed by a request of the same identity."""
    messages = body.get("messages") or []
    if not messages or messages[-1].get("role") != "user":
        return None
    blocks = messages[-1].get("content")
    if not isinstance(blocks, list) or not blocks:
        return None
    if any(not (isinstance(b, dict) and b.get("type") == "tool_result") for b in blocks):
        return None
    with _live_lock:
        _live_gc_locked()
        session = None
        claimed = []
        for b in blocks:
            entry = _live_sessions.get(b.get("tool_use_id"))
            if entry is None or entry["model"] != model or \
                    entry.get("client_type") != client_type or \
                    (session is not None and entry["session"] is not session):
                return None
            session = entry["session"]
            claimed.append((entry, b))
        # A parked session waits for *all* of its tool results on one live
        # stream. Resuming with only a subset would leave the upstream blocked
        # on the missing ones (until FIRST_OUTPUT_TIMEOUT) and strand the
        # leftover parked ids pointing at a dead stream; replay fresh instead.
        wanted = {b.get("tool_use_id") for b in blocks}
        parked = {k for k, v in _live_sessions.items() if v["session"] is session}
        if wanted != parked:
            return None
        for b in blocks:
            _live_sessions.pop(b.get("tool_use_id"), None)
    return claimed


# Plain chat has no workspace and no caller tools, but Cursor still hands the model
# its coding-agent harness. This keeps answers from turning into repository work.
CHAT_PROMPT = (
    "You are answering a single API request in a plain conversation. There is no "
    "workspace, repository, project or user machine attached, and no tools are "
    "available: never call file, search, terminal, todo or task tools, never look "
    "for context, and never mention a workspace, codebase or your environment. "
    "Answer the user's message directly, as a general-purpose assistant, in the "
    "language the user used.")


# ---------------------------------------------------------------- conversion
def _blocks(content):
    return [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])


def image_size(data):
    """(width, height) from the file header; some models drop 0x0 attachments."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker, size = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + size
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h
    except Exception:
        pass
    return 1024, 1024


def render_history(messages):
    """Flatten Anthropic messages into (prompt_text, images, documents).

    Only the final user turn keeps its attachments as real protocol context; earlier
    turns are rendered as a transcript because Cursor starts a fresh conversation.
    """
    lines, images, docs = [], [], []
    last = len(messages) - 1
    for i, m in enumerate(messages):
        role = m.get("role", "user")
        chunks = []
        for b in _blocks(m.get("content")):
            t = b.get("type")
            if t == "text":
                chunks.append(b.get("text", ""))
            elif t == "thinking":
                continue
            elif t == "image":
                src = b.get("source", {})
                data = base64.b64decode(src.get("data", "")) if src.get("type") == "base64" else b""
                if data and i == last:
                    w, h = image_size(data)
                    images.append((data, src.get("media_type", "image/png"), w, h))
                else:
                    chunks.append("[image]")
            elif t == "document":
                src = b.get("source", {})
                if src.get("type") == "base64":
                    data = base64.b64decode(src.get("data", ""))
                    name = b.get("title") or ("document." + src.get("media_type", "application/pdf").split("/")[-1])
                    if i == last:
                        docs.append((data, name, src.get("media_type", "application/pdf")))
                    else:
                        chunks.append(f"[document {name}]")
                elif src.get("type") == "text":
                    chunks.append(src.get("data", ""))
            elif t == "tool_use":
                chunks.append(f"[called tool {b.get('name')} with {json.dumps(b.get('input', {}), ensure_ascii=False)}]")
            elif t == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in (c or []) if isinstance(x, dict))
                chunks.append(f"[tool result: {txt}]")
        body = "\n".join(x for x in chunks if x)
        if body:
            lines.append(("Human: " if role == "user" else "Assistant: ") + body)
    if len(lines) == 1 and lines[0].startswith("Human: "):
        prompt = lines[0][len("Human: "):]
    elif len(lines) == 1:
        prompt = lines[0]
    else:
        prompt = "\n\n".join(lines) + "\n\nAssistant:"
    return prompt, images, docs


def system_text(system):
    if not system:
        return None
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))


def tool_specs(tools):
    out = []
    for t in tools or []:
        if t.get("type", "custom") not in ("custom", "", None):
            continue                                  # server-side tool types: unsupported
        out.append({"name": t["name"], "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {"type": "object"})})
    return out


# Reasoning levels, weakest to strongest. Callers spell them in several ways and
# each model family publishes its own subset, so a request is clamped onto the
# nearest level the target model actually declares rather than sent verbatim
# (Cursor rejects a parameter value it does not know).
EFFORT_LADDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
EFFORT_ALIASES = {"minimal": "low", "default": "medium", "highest": "max"}
# Anthropic states the same intent as a token budget; these are the boundaries
# used to bucket it back onto the ladder.
# LiteLLM emits 1024/2048/4096 for the low/medium/high efforts a Codex client
# can select, so anything above the medium budget is carried through as the
# strongest level the target model publishes.
EFFORT_BUDGETS = ((1024, "low"), (2048, "medium"))


def nearest_effort(level, allowed):
    """The published level closest to the one asked for, or None."""
    if level in allowed:
        return level
    if level not in EFFORT_LADDER:
        return None
    want = EFFORT_LADDER.index(level)
    ranked = sorted((abs(EFFORT_LADDER.index(a) - want), a)
                    for a in allowed if a in EFFORT_LADDER)
    return ranked[0][1] if ranked else None


# --------------------------------------------------------------- live turns
class Turn:
    """Runs one assistant turn and yields normalized deltas."""

    def __init__(self, body):
        self.body = body
        self.client_type, self.model_in = split_client_type(body.get("model", DEFAULT_MODEL))
        self.model, self.model_params = models.resolve(self.model_in, DEFAULT_MODEL)
        self.model_options = models.options(self.model)
        self.tools = tool_specs(body.get("tools"))
        self.want_thinking = bool((body.get("thinking") or {}).get("type") == "enabled")
        self.stops = [s for s in (body.get("stop_sequences") or []) if s]
        try:
            self.max_tokens = int(body.get("max_tokens") or 0)
        except (TypeError, ValueError):
            self.max_tokens = 0
        self.chat = not self.tools
        self.session = None
        self.pending = []          # tool_use blocks emitted this turn
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.stop_reason = None
        self.stop_sequence = None
        self.text_so_far = ""
        self.sent_chars = 0        # chars actually sent upstream, for usage estimates
        self.think_chars = 0       # thinking chars received, for usage estimates
        self.resumed = False       # continued a parked stream instead of replaying
        self._usage_final = None   # usage_of() result, computed once per turn
        self.error = None          # upstream error text, for the turn log

    def _effort(self):
        """Reasoning level the caller asked for, in ladder terms, or None."""
        think = self.body.get("thinking") or {}
        raw = think.get("effort") or self.body.get("reasoning_effort") or ""
        raw = str(raw).strip().lower()
        if raw:
            return EFFORT_ALIASES.get(raw, raw)
        try:
            budget = int(think.get("budget_tokens"))
        except (TypeError, ValueError):
            return None
        for cutoff, name in EFFORT_BUDGETS:
            if budget <= cutoff:
                return name
        return "max"

    def _tune(self, images, docs):
        """Drop reasoning for turns that did not ask for it (time to first token)."""
        params = dict(self.model_params or {})
        # `thinking` is only a real parameter on the families that publish it;
        # composer and the grok line have no such id and reject one.
        if "thinking" in self.model_options or "thinking" in params:
            if THINKING == "off" or (THINKING == "auto" and not self.want_thinking
                                     and "think" not in (self.model_in or "").lower()):
                params["thinking"] = "false"
            elif self.want_thinking:
                params["thinking"] = "true"
        level = self._effort()
        if level and params.get("thinking") != "false":
            # claude/grok publish this as `effort`, the gpt and kimi families as
            # `reasoning`; without this the level was parsed and then dropped, so
            # low and high reached the backend as the same request.
            for pid in ("effort", "reasoning"):
                allowed = self.model_options.get(pid)
                if not allowed:
                    continue
                pick = nearest_effort(level, allowed)
                if pick:
                    params[pid] = pick
                break
        self.model_params = params
        self.chat = not self.tools and not images and not docs

    def start(self):
        prompt, images, docs = render_history(self.body.get("messages", []))
        sysmsg = system_text(self.body.get("system"))
        choice = self.body.get("tool_choice") or {}
        if choice.get("type") == "none":
            self.tools = []
        elif choice.get("type") == "any" and self.tools:
            sysmsg = ((sysmsg + "\n\n") if sysmsg else "") + \
                "You must answer by calling one of the provided tools."
        elif choice.get("type") == "tool" and choice.get("name") and self.tools:
            sysmsg = ((sysmsg + "\n\n") if sysmsg else "") + \
                "You must answer by calling the tool `%s`." % choice["name"]
        if self.tools:
            sysmsg = ((sysmsg + "\n\n") if sysmsg else "") + (
                "When a task needs one of the provided tools, call the tool directly "
                "instead of describing it. Your builtin file, search and shell tools "
                "are not connected to this machine and will fail; use only the "
                "provided tools, and use the paths given in the conversation rather "
                "than the workspace path you were told about. Earlier turns may "
                "appear as a transcript in which `[called tool NAME with ARGS]` "
                "followed by `[tool result: ...]` records a tool call that really "
                "executed on the user's machine; treat those results as real and "
                "current, and do not call a tool again merely to re-verify them.")
        self._tune(images, docs)
        if self.chat:
            sysmsg = ((sysmsg + "\n\n") if sysmsg else "") + CHAT_PROMPT
        self.sent_chars = (len(prompt) + len(sysmsg or "")
                           + (len(json.dumps(self.tools, ensure_ascii=False)) if self.tools else 0))
        self.session = Session(model=self.model, system=sysmsg, tools=self.tools,
                               web=WEB, model_params=self.model_params, debug=DEBUG,
                               chat=self.chat, client_type=self.client_type)
        self.session.start(prompt, images=images, documents=docs)

    def resume(self, claimed):
        """Continue a parked live session by answering its tool calls."""
        self.session = claimed[0][0]["session"]
        self.resumed = True
        results = []
        chars = 0
        for entry, block in claimed:
            eid, esid = entry["exec"]
            content = block.get("content")
            text = content if isinstance(content, str) else " ".join(
                x.get("text", "") for x in (content or [])
                if isinstance(x, dict) and x.get("type") == "text")
            chars += len(text)
            results.append((eid, esid, text, bool(block.get("is_error"))))
        self.sent_chars = max(1, chars)
        self.session.send_tool_results(results)

    def stream(self):
        """Yields ('thinking'|'text'|'tool_use'|'done'|'error', value)."""
        for kind, val in self.session.events(idle_stop=IDLE_STOP, hard_timeout=900,
                                             first_timeout=FIRST_TIMEOUT,
                                             first_output_timeout=FIRST_OUTPUT_TIMEOUT):
            if kind == "tick":
                yield "tick", None
            elif kind == "text":
                emit, reason = self._cut(val)
                if emit:
                    yield "text", emit
                if reason:
                    self.stop_reason = reason
                    break
            elif kind == "thinking":
                self.think_chars += len(val)
                if self.want_thinking:
                    yield "thinking", val
            elif kind == "web":
                yield "web", val
            elif kind == "tool_use":
                self.pending.append(val)
                self.stop_reason = "tool_use"
                yield "tool_use", val
                if not self.session.buffered():
                    return                 # hand control back to the API caller
                # More of this batch is already decoded: parallel calls share a
                # frame, and stopping here used to drop every call after the
                # first, leaving the agent waiting on a result that never came.
            elif kind == "turn_ended":
                self.usage.update({k: v for k, v in val.items()
                                   if k in ("input_tokens", "output_tokens",
                                            "cache_read_input_tokens",
                                            "cache_creation_input_tokens")})
            elif kind == "error":
                self.error = val
                yield "error", val
                return
            elif kind == "end":
                break
        yield "done", None

    def _cut(self, delta):
        """Applies stop_sequences / max_tokens locally: (text_to_emit, stop_reason)."""
        before = len(self.text_so_far)
        self.text_so_far += delta
        for s in self.stops:
            at = self.text_so_far.find(s)
            if at >= 0:
                self.stop_sequence = s
                self.text_so_far = self.text_so_far[:at]
                return self.text_so_far[before:], "stop_sequence"
        cap = self.max_tokens * 4          # rough estimate, no real tokenizer
        if cap and len(self.text_so_far) > cap:
            self.text_so_far = self.text_so_far[:cap]
            return self.text_so_far[before:], "max_tokens"
        return delta, None

    def close(self):
        if self.session:
            self.session.close()


# ------------------------------------------------------------------ HTTP I/O
def web_lines(ws):
    """Cursor's server-side web search -> plain text for the OpenAI shim."""
    out = ["\n[web] %s %s" % (r["title"], r["url"]) for r in ws.get("results", [])]
    if not out and ws.get("summary"):
        out.append("\n[web] " + ws["summary"])
    return "".join(out)


def web_blocks(ws):
    """Cursor's server-side web search -> Anthropic server_tool_use pair."""
    tid = ws.get("id") or "srvtoolu_" + uuid.uuid4().hex[:16]
    return [
        {"type": "server_tool_use", "id": tid, "name": "web_search",
         "input": {"query": ws.get("query", "")}},
        {"type": "web_search_tool_result", "tool_use_id": tid,
         "content": [{"type": "web_search_result", "title": r["title"], "url": r["url"],
                      "page_age": None, "encrypted_content": ""}
                     for r in ws.get("results", [])]},
    ]


def upstream_error(msg):
    """Maps Cursor's error codes onto Anthropic's error types.

    Returns (status, type, message); the message is rewritten when Cursor's raw
    payload is a protobuf-ish JSON blob the caller cannot act on.
    """
    m = msg or ""
    if "RATE_LIMIT" in m or "resource_exhausted" in m:
        return 429, "rate_limit_error", "Cursor rate limit reached, retry later"
    if "NOT_LOGGED_IN" in m or "unauthenticated" in m or "HTTP 401" in m:
        # A revoked token stays "valid" by its exp claim for up to an hour;
        # drop the cache so the next turn re-exchanges the API key or picks a
        # fresher credential instead of replaying the dead one.
        auth_mod.invalidate_cached()
        return 401, "authentication_error", "Cursor rejected the credentials"
    if "MODEL_BLOCKED" in m:
        return 403, "permission_error", (
            "this model is blocked for the account: enable it (and accept its data "
            "retention policy) in the Cursor dashboard")
    if "MODEL_NOT_AVAILABLE" in m or "MODEL_NOT_SUPPORTED" in m:
        return 400, "invalid_request_error", "model not available for the account"
    if "permission_denied" in m or "not allowed" in m:
        return 403, "permission_error", m
    return 502, "api_error", m


_cpt_lock = threading.Lock()
_cpt = [4.0]        # chars per prompt token, calibrated against real counters


def _calibrate_cpt(chars, tokens):
    """Learn the prompt chars-per-token ratio from turns Cursor counted cleanly."""
    if tokens < 1000 or chars < 4000:
        return
    ratio = chars / tokens
    if not 1.5 <= ratio <= 12.0:
        return
    with _cpt_lock:
        _cpt[0] = _cpt[0] * 0.7 + ratio * 0.3


def prompt_chars(turn):
    """Size of the prompt this API request carried, in characters."""
    body = turn.body if isinstance(turn.body, dict) else {}
    try:
        return sum(len(json.dumps(body.get(k) or d, ensure_ascii=False))
                   for k, d in (("system", ""), ("messages", []), ("tools", [])))
    except (TypeError, ValueError):
        return 0


def usage_of(turn):
    """Anthropic usage for one API request.

    Two Cursor behaviours have to be undone here. Cursor only reports real
    counters in the turn_ended frame, which never arrives for turns that stop
    at a tool_use (control goes back to the API caller while Cursor's turn
    stays open); those turns are estimated from characters. And when the frame
    does arrive it carries counters accumulated over Cursor's *whole* turn,
    which spans every parked sub-request, so reporting it verbatim told the
    caller its context window was nearly full after a few tool calls.

    The prompt sides are therefore clamped to the prompt this request actually
    carried, split across the cache counters so the three add up, and the
    estimates already billed on parked sub-requests are handed to the session
    so the next real frame can subtract them.
    """
    if turn._usage_final is not None:
        return dict(turn._usage_final)

    real_in = turn.usage.get("input_tokens", 0)
    real_out = turn.usage.get("output_tokens", 0)
    chars = prompt_chars(turn)
    if real_in and not turn.resumed and not turn.pending:
        _calibrate_cpt(chars, real_in)
    with _cpt_lock:
        cpt = _cpt[0]

    est_in = max(1, int(chars / cpt)) if chars else max(1, turn.sent_chars // 4)
    out_chars = (len(turn.text_so_far) + turn.think_chars
                 + sum(len(json.dumps(tu, ensure_ascii=False)) for tu in turn.pending))
    est_out = max(1, int(out_chars / cpt))

    total_in = min(real_in, est_in) if real_in else est_in
    read = min(turn.usage.get("cache_read_input_tokens", 0), total_in)
    created = min(turn.usage.get("cache_creation_input_tokens", 0), total_in - read)
    u = {"input_tokens": max(1, total_in - read - created),
         "output_tokens": real_out or est_out,
         "cache_read_input_tokens": read,
         "cache_creation_input_tokens": created}

    if not turn.usage and turn.session is not None:
        seen = turn.session._usage_est
        for k in ("input_tokens", "output_tokens"):
            seen[k] = seen.get(k, 0) + u[k]
    turn._usage_final = u
    return dict(u)


def log_turn(turn, status, started, parked=False):
    """One structured line per turn, so failures can be read off the log."""
    if not LOG_TURNS:
        return
    try:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "status": status, "model": turn.model, "requested": turn.model_in,
               "client_type": turn.client_type, "params": turn.model_params,
               "tools": len(turn.tools), "tool_calls": len(turn.pending),
               "resumed": turn.resumed, "parked": parked,
               "stop_reason": turn.stop_reason,
               "ms": int((time.time() - started) * 1000),
               "usage": usage_of(turn)}
        if turn.error:
            rec["error"] = str(turn.error)[:300]
        sys.stderr.write(json.dumps(rec, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cursor2api/1.0"
    timeout = float(os.environ.get("CURSOR2API_HTTP_IDLE", "300"))
    _headers_sent = False
    _stream_flavor = None

    def log_message(self, fmt, *a):
        if DEBUG:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    # -- helpers
    def _json(self, code, obj, headers=()):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, code, kind, message):
        headers = [("retry-after", "1")] if code == 429 else []
        self._json(code, {"type": "error",
                          "error": {"type": kind, "message": message}}, headers)

    def _finish_chunked(self):
        """Write the terminating chunk.

        Without it a keep-alive client blocks until its own timeout and the
        connection is left mid-body, so the next request on the socket reads
        the leftovers as a malformed request.
        """
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _fail(self, code, kind, message):
        """Report an error, honouring a stream whose headers already went out.

        Calling send_response() again after that writes a second status line
        into the chunked body, which the client sees as a corrupt frame.
        """
        if not self._headers_sent:
            return self._err(code, kind, message)
        try:
            if self._stream_flavor == "openai":
                raw = (b"data: " + json.dumps({"error": {"message": message,
                                                         "type": kind}}).encode()
                       + b"\n\n")
            else:
                raw = sse("error", {"type": "error",
                                    "error": {"type": kind, "message": message}})
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        self._finish_chunked()

    def _authed(self):
        if not API_KEY:
            return True
        auth = self.headers.get("authorization") or ""
        bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else auth
        for presented in (self.headers.get("x-api-key") or "", bearer):
            if presented and hmac.compare_digest(presented, API_KEY):
                return True
        return False

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
        if n > MAX_BODY:
            raise ValueError("request body of %d bytes exceeds the %d byte limit"
                             % (n, MAX_BODY))
        return json.loads(self.rfile.read(n) or b"{}")

    @staticmethod
    def _model_entry(name):
        """One model object carrying both SDKs' field names."""
        created = int(time.time())
        return {"type": "model", "object": "model", "id": name, "display_name": name,
                "created": created, "owned_by": "cursor",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))}

    # -- routes
    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") == "/v1/models":
            # One payload that satisfies both SDKs: Anthropic reads type/display_name/
            # created_at, OpenAI reads object/created/owned_by.
            data = [self._model_entry(name) for name in models.ids()]
            return self._json(200, {"object": "list", "data": data, "has_more": False,
                                    "first_id": data[0]["id"] if data else None,
                                    "last_id": data[-1]["id"] if data else None})
        if self.path.split("?")[0].startswith("/v1/models/"):
            name = self.path.split("?")[0][len("/v1/models/"):]
            if name in models.ids():
                return self._json(200, self._model_entry(name))
            return self._err(404, "not_found_error", "unknown model: " + name)
        if self.path.rstrip("/") in ("", "/", "/health"):
            return self._json(200, {"ok": True, "backend": "cursor agent.v1",
                                    "default_model": DEFAULT_MODEL})
        if self.path.rstrip("/") == "/login":
            # Headless authorisation: hand back a URL, keep polling in the background.
            return self._json(200, start_background_login())
        self._err(404, "not_found_error", "unknown route")

    def do_HEAD(self):
        # Claude Code probes the base URL with HEAD before its first request.
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        # A keep-alive connection reuses this handler instance for every request.
        self._headers_sent = False
        self._stream_flavor = None
        if not self._authed():
            return self._err(401, "authentication_error", "invalid x-api-key")
        route = self.path.split("?")[0].rstrip("/")
        try:
            body = self._body()
        except Exception as e:
            return self._err(400, "invalid_request_error", str(e))

        if route.startswith("/openai"):
            route = route[len("/openai"):]

        if route == "/v1/messages/count_tokens":
            chars = len(json.dumps(body.get("messages", []), ensure_ascii=False))
            chars += len(system_text(body.get("system")) or "")
            return self._json(200, {"input_tokens": max(1, chars // 4)})

        openai = route in ("/v1/chat/completions", "/chat/completions")
        if route != "/v1/messages" and not openai:
            return self._err(404, "not_found_error", "unknown route")

        if openai:
            model_in = body.get("model", DEFAULT_MODEL)
            body = openai_api.to_anthropic(body)
        if not body.get("messages"):
            return self._err(400, "invalid_request_error", "messages: required")

        turn = Turn(body)
        started = time.time()
        status = "ok"
        claimed = None
        try:
            claimed = claim_live(body, turn.model, turn.client_type)
            if claimed:
                turn.resume(claimed)
        except Exception:                          # dead stream: fresh replay
            if claimed:
                try:
                    claimed[0][0]["session"].close()
                except Exception:
                    pass
            turn.session = None
        try:
            if openai:
                turn.model_in = model_in
                if body.get("stream"):
                    self._stream_turn_openai(turn)
                else:
                    self._buffer_turn_openai(turn)
            elif body.get("stream"):
                self._stream_turn(turn)
            else:
                self._buffer_turn(turn)
        except (BrokenPipeError, ConnectionResetError):
            status = "client_gone"
        except AuthError as e:
            status = "auth_error"
            turn.error = str(e)
            self._fail(401, "authentication_error", str(e))
        except Exception as e:
            status = "exception"
            turn.error = f"{type(e).__name__}: {e}"
            if DEBUG:
                import traceback
                traceback.print_exc()
            try:
                self._fail(500, "api_error", f"{type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            parked = register_live(turn)
            if turn.error and status == "ok":
                status = "upstream_error"
            log_turn(turn, status, started, parked)
            if not parked:
                turn.close()

    # -- non-streaming
    def _buffer_turn(self, turn):
        if turn.session is None:
            turn.start()
        blocks, think, text, web = [], [], [], []
        err = None
        for kind, val in turn.stream():
            if kind == "thinking":
                think.append(val)
            elif kind == "text":
                text.append(val)
            elif kind == "web":
                web.extend(web_blocks(val))
            elif kind == "error":
                err = val
        if err:
            return self._err(*upstream_error(err))
        if think:
            blocks.append({"type": "thinking", "thinking": "".join(think), "signature": ""})
        blocks.extend(web)
        if text:
            blocks.append({"type": "text", "text": "".join(text)})
        for tu in turn.pending:
            blocks.append({"type": "tool_use", "id": tu["id"], "name": tu["name"],
                           "input": tu["input"]})
        self._json(200, {
            "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
            "model": turn.model_in, "content": blocks,
            "stop_reason": turn.stop_reason or "end_turn",
            "stop_sequence": turn.stop_sequence,
            "usage": usage_of(turn),
        })

    # -- streaming (Anthropic SSE)
    def _stream_turn(self, turn):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        self._headers_sent = True
        self._stream_flavor = "anthropic"
        mid = "msg_" + uuid.uuid4().hex[:24]

        def chunk(raw):
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.flush()

        chunk(sse("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": turn.model_in,
            "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}}))

        if turn.session is None:
            turn.start()
        idx = -1
        open_kind = None

        def close_block():
            nonlocal open_kind
            if open_kind == "thinking":
                chunk(sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                  "delta": {"type": "signature_delta", "signature": ""}}))
            if open_kind:
                chunk(sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
            open_kind = None

        last_ping = time.time()
        for kind, val in turn.stream():
            if kind == "tick":
                if time.time() - last_ping > PING:
                    last_ping = time.time()
                    chunk(sse("ping", {"type": "ping"}))
                continue
            if kind in ("thinking", "text"):
                if open_kind != kind:
                    close_block()
                    idx += 1
                    open_kind = kind
                    start = ({"type": "thinking", "thinking": "", "signature": ""}
                             if kind == "thinking" else {"type": "text", "text": ""})
                    chunk(sse("content_block_start", {"type": "content_block_start",
                                                     "index": idx, "content_block": start}))
                d = ({"type": "thinking_delta", "thinking": val} if kind == "thinking"
                     else {"type": "text_delta", "text": val})
                chunk(sse("content_block_delta", {"type": "content_block_delta",
                                                  "index": idx, "delta": d}))
            elif kind == "tool_use":
                close_block()
                idx += 1
                chunk(sse("content_block_start", {"type": "content_block_start", "index": idx,
                          "content_block": {"type": "tool_use", "id": val["id"],
                                            "name": val["name"], "input": {}}}))
                chunk(sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                          "delta": {"type": "input_json_delta",
                                    "partial_json": json.dumps(val["input"], ensure_ascii=False)}}))
                chunk(sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
                open_kind = None
            elif kind == "web":
                close_block()
                for b in web_blocks(val):
                    idx += 1
                    chunk(sse("content_block_start", {"type": "content_block_start",
                                                      "index": idx, "content_block": b}))
                    chunk(sse("content_block_stop", {"type": "content_block_stop",
                                                     "index": idx}))
            elif kind == "error":
                _, kind, text_err = upstream_error(val)
                chunk(sse("error", {"type": "error",
                                    "error": {"type": kind, "message": text_err}}))
                return self._finish_chunked()
            last_ping = time.time()

        close_block()
        chunk(sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": turn.stop_reason or "end_turn",
                      "stop_sequence": turn.stop_sequence},
            "usage": usage_of(turn)}))
        chunk(sse("message_stop", {"type": "message_stop"}))
        self._finish_chunked()

    # -- OpenAI chat completions
    def _buffer_turn_openai(self, turn):
        if turn.session is None:
            turn.start()
        think, text = [], []
        err = None
        for kind, val in turn.stream():
            if kind == "thinking":
                think.append(val)
            elif kind == "text":
                text.append(val)
            elif kind == "web":
                text.append(web_lines(val))
            elif kind == "error":
                err = val
        if err:
            return self._err(*upstream_error(err))
        self._json(200, openai_api.completion(
            turn.model_in, "".join(text), "".join(think), turn.pending,
            turn.stop_reason or "end_turn", usage_of(turn)))

    def _stream_turn_openai(self, turn):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        self._headers_sent = True
        self._stream_flavor = "openai"
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]

        def send(obj):
            raw = b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.flush()

        send(openai_api.chunk(cid, turn.model_in, {"role": "assistant", "content": ""}))
        if turn.session is None:
            turn.start()
        calls = 0
        last_ping = time.time()
        for kind, val in turn.stream():
            if kind == "tick":
                if time.time() - last_ping > PING:
                    last_ping = time.time()
                    raw = b": keepalive\n\n"          # SSE comment: ignored by clients
                    self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
                    self.wfile.flush()
                continue
            last_ping = time.time()
            if kind == "text":
                send(openai_api.chunk(cid, turn.model_in, {"content": val}))
            elif kind == "thinking":
                send(openai_api.chunk(cid, turn.model_in, {"reasoning_content": val}))
            elif kind == "web":
                line = web_lines(val)
                if line:
                    send(openai_api.chunk(cid, turn.model_in, {"content": line}))
            elif kind == "tool_use":
                send(openai_api.chunk(cid, turn.model_in,
                                      openai_api.tool_call_delta(calls, val)))
                calls += 1
            elif kind == "error":
                _, kind, text_err = upstream_error(val)
                send({"error": {"message": text_err, "type": kind}})
                return self._finish_chunked()
        send(openai_api.chunk(cid, turn.model_in, {},
                             openai_api.FINISH.get(turn.stop_reason or "end_turn", "stop"),
                             usage_of(turn)))
        raw = b"data: [DONE]\n\n"
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
        self._finish_chunked()


_login_flow = {}
_login_lock = threading.Lock()


def start_background_login():
    """Start (or report) a browser authorisation the caller can complete later."""
    with _login_lock:
        try:
            auth_mod.access_token()
            return {"authorized": True}
        except AuthError:
            pass
        if _login_flow.get("pending"):
            return {"authorized": False, "pending": True,
                    "login_url": _login_flow["loginUrl"]}
        flow = auth_mod.start_login()
        _login_flow.update(flow, pending=True)

    def wait():
        try:
            result = auth_mod.poll_login(flow["uuid"], flow["verifier"])
            if result:
                auth_mod.remember(result)
        finally:
            with _login_lock:
                _login_flow["pending"] = False

    threading.Thread(target=wait, daemon=True).start()
    return {"authorized": False, "pending": True, "login_url": flow["loginUrl"]}


def _background_warmup():
    """Keeps the access token fresh and a TLS/HTTP2 connection ready.

    Both are on the critical path of the first token, and both are cheap to keep
    warm: a token exchange is one HTTPS round trip, a Run stream needs a fresh
    HTTP/2 connection because Cursor closes it with the turn.
    """
    def loop():
        while True:
            try:
                auth_mod.access_token()
                h2stream.prewarm(HOST)
            except Exception:
                pass
            time.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


def main():
    try:
        auth_mod.access_token()
    except AuthError as e:
        # Interactive start: authorise now. Headless start: come up anyway and let
        # the operator authorise through GET /login.
        if os.environ.get("CURSOR2API_AUTO_LOGIN", "1") != "1":
            print(e, file=sys.stderr)
            return 1
        print(e, file=sys.stderr)
        if sys.stdin.isatty():
            try:
                auth_mod.login()
            except AuthError as e2:
                print(e2, file=sys.stderr)
                return 1
        else:
            flow = start_background_login()
            print("authorise this proxy at: %s" % flow.get("login_url", ""), flush=True)
    _background_warmup()
    threading.Thread(target=models.catalog, daemon=True).start()
    threading.Thread(target=_live_gc_loop, daemon=True).start()
    ThreadingHTTPServer.request_queue_size = 256
    # A supervised restart races the old process releasing the port.
    srv = None
    for attempt in range(15):
        try:
            srv = ThreadingHTTPServer((BIND, PORT), Handler)
            break
        except OSError as e:
            if e.errno != errno.EADDRINUSE or attempt == 14:
                raise
            time.sleep(1.0)
    srv.daemon_threads = True
    print(f"listening on http://{BIND}:{PORT} "
          f"(/v1/messages, /v1/chat/completions, /v1/models; "
          f"default model {DEFAULT_MODEL}, auth {'on' if API_KEY else 'off'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
