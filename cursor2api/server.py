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
import base64, json, os, struct, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth as auth_mod
from . import h2stream, models, openai_api
from .auth import AuthError
from .session import HOST, Session

BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
API_KEY = os.environ.get("API_KEY", "")          # empty = no auth
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-fable-5")
# Safety net only: a turn ends on turn_ended or when the stream closes. Long file
# writes and long reasoning go minutes without a frame, so this stays generous.
IDLE_STOP = float(os.environ.get("IDLE_STOP", "180"))
FIRST_TIMEOUT = float(os.environ.get("FIRST_TIMEOUT", "90"))  # upstream never answered
WEB = os.environ.get("CURSOR2API_WEB", "1") == "1"
# auto: think only when the caller asks for it or names a thinking variant. Cursor's
# defaults turn reasoning on for every model, which triples the time to first token.
THINKING = os.environ.get("CURSOR2API_THINKING", "auto")
PING = float(os.environ.get("PING_INTERVAL", "5"))   # SSE keepalive while upstream is quiet
DEBUG = bool(os.environ.get("DBG"))

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
    if len(lines) == 1:
        prompt = lines[0][len("Human: "):]
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


# --------------------------------------------------------------- live turns
class Turn:
    """Runs one assistant turn and yields normalized deltas."""

    def __init__(self, body):
        self.body = body
        self.model_in = body.get("model", DEFAULT_MODEL)
        self.model, self.model_params = models.resolve(self.model_in, DEFAULT_MODEL)
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

    def _tune(self, images, docs):
        """Drop reasoning for turns that did not ask for it (time to first token)."""
        params = dict(self.model_params or {})
        if THINKING == "off" or (THINKING == "auto" and not self.want_thinking
                                 and "think" not in (self.model_in or "").lower()
                                 and "thinking" in params):
            params["thinking"] = "false"
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
                "than the workspace path you were told about.")
        self._tune(images, docs)
        if self.chat:
            sysmsg = ((sysmsg + "\n\n") if sysmsg else "") + CHAT_PROMPT
        self.session = Session(model=self.model, system=sysmsg, tools=self.tools,
                               web=WEB, model_params=self.model_params, debug=DEBUG,
                               chat=self.chat)
        self.session.start(prompt, images=images, documents=docs)

    def stream(self):
        """Yields ('thinking'|'text'|'tool_use'|'done'|'error', value)."""
        for kind, val in self.session.events(idle_stop=IDLE_STOP, hard_timeout=900,
                                             first_timeout=FIRST_TIMEOUT):
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
                if self.want_thinking:
                    yield "thinking", val
            elif kind == "web":
                yield "web", val
            elif kind == "tool_use":
                self.pending.append(val)
                self.stop_reason = "tool_use"
                yield "tool_use", val
                return                     # hand control back to the API caller
            elif kind == "turn_ended":
                self.usage.update({k: v for k, v in val.items()
                                   if k in ("input_tokens", "output_tokens",
                                            "cache_read_input_tokens",
                                            "cache_creation_input_tokens")})
            elif kind == "error":
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


def usage_of(turn):
    """Anthropic usage, including the cache counters Cursor reports on turn end."""
    u = {"input_tokens": turn.usage.get("input_tokens", 0),
         "output_tokens": turn.usage.get("output_tokens", 0)}
    if not u["output_tokens"]:                  # turn cut short: estimate
        u["output_tokens"] = max(1, len(turn.text_so_far) // 4)
    for k in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        u[k] = turn.usage.get(k, 0)
    return u


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cursor2api/1.0"

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

    def _authed(self):
        if not API_KEY:
            return True
        key = self.headers.get("x-api-key") or ""
        auth = self.headers.get("authorization") or ""
        return key == API_KEY or auth.replace("Bearer ", "") == API_KEY

    def _body(self):
        n = int(self.headers.get("content-length") or 0)
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
            pass
        except AuthError as e:
            self._err(401, "authentication_error", str(e))
        except Exception as e:
            if DEBUG:
                import traceback
                traceback.print_exc()
            try:
                self._err(500, "api_error", f"{type(e).__name__}: {e}")
            except Exception:
                pass
        finally:
            turn.close()

    # -- non-streaming
    def _buffer_turn(self, turn):
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
        mid = "msg_" + uuid.uuid4().hex[:24]

        def chunk(raw):
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.flush()

        chunk(sse("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": turn.model_in,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}}))

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
                return
            last_ping = time.time()

        close_block()
        chunk(sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": turn.stop_reason or "end_turn",
                      "stop_sequence": turn.stop_sequence},
            "usage": usage_of(turn)}))
        chunk(sse("message_stop", {"type": "message_stop"}))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # -- OpenAI chat completions
    def _buffer_turn_openai(self, turn):
        turn.start()
        think, text = [], []
        err = None
        for kind, val in turn.stream():
            if kind == "thinking":
                think.append(val)
            elif kind == "text":
                text.append(val)
            elif kind == "web":
                text.append("".join("\n[web] %s %s" % (r["title"], r["url"])
                                    for r in val.get("results", [])))
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
        cid = "chatcmpl-" + uuid.uuid4().hex[:24]

        def send(obj):
            raw = b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
            self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
            self.wfile.flush()

        send(openai_api.chunk(cid, turn.model_in, {"role": "assistant", "content": ""}))
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
                for r in val.get("results", []):
                    send(openai_api.chunk(cid, turn.model_in, {
                        "content": "\n[web] %s %s" % (r["title"], r["url"])}))
            elif kind == "tool_use":
                send(openai_api.chunk(cid, turn.model_in,
                                      openai_api.tool_call_delta(calls, val)))
                calls += 1
            elif kind == "error":
                _, kind, text_err = upstream_error(val)
                send({"error": {"message": text_err, "type": kind}})
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
        send(openai_api.chunk(cid, turn.model_in, {},
                             openai_api.FINISH.get(turn.stop_reason or "end_turn", "stop"),
                             usage_of(turn)))
        raw = b"data: [DONE]\n\n"
        self.wfile.write(b"%x\r\n" % len(raw) + raw + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


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
    ThreadingHTTPServer.request_queue_size = 256
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print(f"listening on http://{BIND}:{PORT} "
          f"(/v1/messages, /v1/chat/completions, /v1/models; "
          f"default model {DEFAULT_MODEL}, auth {'on' if API_KEY else 'off'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
