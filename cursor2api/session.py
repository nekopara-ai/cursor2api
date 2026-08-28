"""Reusable single-turn/multi-turn session over Cursor's raw
agent.v1.AgentService/Run bidirectional Connect RPC.

Schema (field numbers extracted from the Cursor CLI bundle):

AgentRunRequest        2 action(ConversationAction)  4 mcp_tools(McpTools)
                       5 conversation_id  8 custom_system_prompt  9 requested_model
                       12 exclude_workspace_context  16 conversation_group_id
                       19 client_supports_inline_images  25 run_id
ConversationAction     1 user_message_action(UserMessageAction)
UserMessageAction      1 user_message(UserMessage)
UserMessage            1 text  2 message_id  3 selected_context  4 mode
SelectedContext        1 selected_images(SelectedImage)  25 selected_documents(SelectedDocument)
SelectedImage          8 data  2 uuid  3 path  4 dimension{1 w,2 h}  7 mime_type
SelectedDocument       8 data  2 uuid  3 filename  4 mime_type  7 path
RequestedModel         1 model_id  3 parameters{1 id,2 value}
McpTools               1 mcp_tools(McpToolDefinition)
McpToolDefinition      1 name  2 description  4 provider_identifier  5 tool_name
                       6 input_schema_json
RequestContext         4 env  7 tools  17 web_search_enabled  24 web_fetch_enabled
McpArgs                1 name  2 args(map)  3 tool_call_id  4 provider_identifier
                       5 tool_name  8 skip_approval
McpResult              1 success(McpSuccess{1 content(McpToolResultContentItem),2 is_error})
McpToolResultContentItem 1 text(McpTextContent{1 text})
"""
import json, os, re, tempfile, time, uuid
from . import sandbox
from .auth import access_token
from .pb import parse, emit, msg, get, getall, getvar, frame, deframe
from . import h2stream

HOST = "agentn.global.api5.cursor.sh"
PATH = "/agent.v1.AgentService/Run"
VERSION = os.environ.get("CURSOR_CLI_VERSION", "cli-2026.08.11-e8db854")

# Cursor picks the usage pool from the announced client identity, not from the
# model or the endpoint. "cli" draws on the plan's included/bonus pools; "sand"
# is Grok Bot (bundle id com.anysphere.sand), whose weekly pool is billed apart
# from the plan and is reported by DashboardService/GetSandUsageStatus.
# The version header still has to name the CLI build, because that is the
# transport this stream speaks; a sand version here is rejected outright.
CLIENT_TYPE = os.environ.get("CURSOR2API_CLIENT_TYPE", "cli")
CLIENT_TYPE_PREFIXES = {"sand/": "sand", "bot/": "sand", "grokbot/": "sand",
                        "cli/": "cli"}

# The API caller owns tool execution by default. Cursor still needs its MCP
# discovery control tool, but every executable tool exposed to the model must
# be one of the caller definitions carried through McpTools.
DEFAULT_TOOL_OWNER = os.environ.get("CURSOR2API_TOOL_OWNER", "caller")
CALLER_TOOL_ALLOWLIST = ("mcp_tool_call", "get_mcp_tools_tool_call")


def normalise_tool_owner(value):
    aliases = {"client": "caller", "codex": "caller", "legacy": "cursor"}
    raw = str(value or "caller").strip().lower()
    owner = aliases.get(raw, raw)
    if owner not in ("caller", "cursor"):
        raise ValueError("CURSOR2API_TOOL_OWNER must be 'caller' or 'cursor'")
    return owner


def split_client_type(name, default=None):
    """`sand/claude-opus-5` -> ("sand", "claude-opus-5"); plain names pass through."""
    text = (name or "").strip()
    for prefix, client_type in CLIENT_TYPE_PREFIXES.items():
        if text.lower().startswith(prefix):
            return client_type, text[len(prefix):].strip()
    return default or CLIENT_TYPE, text


# Workspace path announced to the agent. It is only meaningful for the sandbox, so
# it points there rather than leaking the directory the proxy happens to run in.
WS = os.environ.get("CURSOR_WS") or os.path.join(tempfile.gettempdir(), "cursor-sandbox")

# ---- model registry -------------------------------------------------------
def default_params(name):
    """Parameters for a model when the caller did not pick any.

    The account's own catalog is the only trustworthy source: parameter ids
    differ per family (claude/grok publish `effort`, gpt/kimi publish
    `reasoning`) and Cursor rejects ids a model does not declare. A second
    hand-written table here used to disagree with models.FALLBACK.
    """
    try:
        from . import models
        return dict(models.resolve(name, name)[1])
    except Exception:
        return {}


def requested_model(name, params=None):
    p = params if params is not None else default_params(name)
    toks = [(1, 2, name.encode())]
    for k, v in p.items():
        toks.append((3, 2, msg(f1=k, f2=str(v))))
    return emit(toks)


def selected_context(images=(), documents=()):
    """images: [(bytes, mime, w, h)]   documents: [(bytes, filename, mime)]"""
    toks = []
    for data, mime, w, h in images:
        toks.append((1, 2, msg(f8=data, f2=str(uuid.uuid4()), f3="image",
                               f4=msg(f1=int(w), f2=int(h)), f7=mime)))
    for data, fname, mime in documents:
        toks.append((25, 2, msg(f8=data, f2=str(uuid.uuid4()), f3=fname,
                                f4=mime, f7=fname)))
    return emit(toks)


# Cursor runs its own builtin tools under these names and rejects the whole
# request (provider error) when a client tool claims one of them. Clients such as
# Claude Code do exactly that (Read, Write, WebSearch, WebFetch), so colliding
# tools travel under a suffixed name and are mapped back on the way out.
BUILTIN_TOOLS = {"read", "write", "ls", "delete", "grep", "glob", "shell",
                 "web_search", "web_fetch"}


def _normalise(name):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower().replace("-", "_")


# ExecServerMessage field -> the caller's tool that does the same job. This is a
# fallback for legacy cursor-owned sessions and protocol control messages; normal
# API sessions filter model-visible Cursor tools before generation starts.
BUILTIN_EQUIVALENT = {
    2: ("shell", "bash", "terminal", "run_command"),
    3: ("write", "write_file", "create_file", "edit"),
    4: ("delete", "remove", "delete_file"),
    5: ("grep", "search", "ripgrep"),
    7: ("read", "read_file", "view", "cat"),
    8: ("ls", "list_dir", "glob", "list_files"),
    14: ("shell", "bash", "terminal", "run_command"),
    29: ("read", "read_file", "view", "cat"),
    52: ("shell", "bash", "terminal", "run_command"),
}


def wire_names(tools):
    """{tool name -> name to send}, avoiding Cursor's builtin tool names."""
    taken = {t["name"] for t in tools}
    out = {}
    for t in tools:
        wire = t["name"]
        while _normalise(wire) in BUILTIN_TOOLS or (wire != t["name"] and wire in taken):
            wire += "_"
        out[t["name"]] = wire
        taken.add(wire)
    return out


def mcp_tools(tools, wire=None):
    """tools: [{name, description, input_schema}] -> McpTools"""
    wire = wire or {}
    toks = []
    for t in tools:
        name = wire.get(t["name"], t["name"])
        toks.append((1, 2, msg(f1=name, f2=t.get("description", "")[:4000],
                               f4="anthropic-passthrough", f5=name,
                               f6=json.dumps(t.get("input_schema", {"type": "object"})))))
    return emit(toks)


class Session:
    """One open Run stream. Drives the required client-side exec replies."""

    def __init__(self, model="claude-fable-5", system=None, tools=None,
                 web=True, workspace=False, model_params=None, debug=False,
                 chat=False, client_type=None, tool_owner=None):
        self.model, self.system, self.tools = model, system, tools or []
        self.client_type = client_type or CLIENT_TYPE
        self.tool_owner = normalise_tool_owner(tool_owner or DEFAULT_TOOL_OWNER)
        # Caller-owned sessions receive search/fetch through MCP just like every
        # other function. The context flags are only meaningful in legacy mode.
        self.web = web and self.tool_owner == "cursor"
        self.workspace, self.debug = workspace, debug
        # Plain chat: no caller tools and no attachments, so nothing the agent's
        # builtin file/shell tools could do is of any use to the caller.
        self.chat = chat
        self.model_params = model_params
        self.conv = str(uuid.uuid4())
        self.rid = str(uuid.uuid4())
        self.conn = None
        self.buf = b""
        # Frames and the events they decode to live on the session, not in a
        # local variable inside events(): a caller that stops mid-batch (Turn
        # hands control back on a tool call) must not destroy the rest of the
        # batch along with the abandoned generator.
        self.pending_frames = []
        self.pending_events = []
        self.usage = {}
        self.tool_by_name = {t["name"]: t for t in self.tools}
        self.wire_name = wire_names(self.tools)
        self.name_of_wire = {v: k for k, v in self.wire_name.items()}
        self.by_norm = {_normalise(t["name"]): t["name"] for t in self.tools}
        # Same index without separators: models also spell `WebSearch` as
        # `websearch`, which normalises to itself and misses by_norm.
        self.by_flat = {k.replace("_", ""): v for k, v in self.by_norm.items()}
        # In legacy cursor-owned mode, turn off a builtin web tool when the caller
        # ships one with the same name. Caller-owned mode starts with both off.
        self.builtin_web = {"web_search": self.web, "web_fetch": self.web}
        for norm in self.by_norm:
            if norm in self.builtin_web:
                self.builtin_web[norm] = False
        self.blobs = {}                 # KV blob store the server writes through us
        self.last_activity = None      # last frame carrying interaction/exec content
        self.last_frame = None         # last server message of any kind, heartbeats too
        self.first_output = False      # any user-visible output or terminal event yet
        self.turn_ended = False
        self.attached = False          # uploads come back down as builtin write/read

    # ---- wire helpers -----------------------------------------------------
    def _run_request(self, text, images=(), documents=()):
        if self.system:
            # custom_system_prompt (field 8) is server-side gated ("unknown option
            # --system-prompt"), so the system prompt is prepended to the turn text.
            text = ("<system>\n" + self.system + "\n</system>\n\n" + text)
        um = {"f1": text, "f2": str(uuid.uuid4()), "f4": 1}
        sc = selected_context(images, documents)
        if sc:
            um["f3"] = sc
        run = {
            "f1": b"",                       # conversation_state (required, empty = new)
            "f2": msg(f1=msg(f1=msg(**um))),
            "f5": self.conv,
            "f9": requested_model(self.model, self.model_params),
            "f16": self.conv,
            "f25": self.rid,
            "f19": True,
            "f12": 0,
        }
        if self.tools:
            # McpTools{1: repeated McpToolDefinition}
            run["f4"] = mcp_tools(self.tools, self.wire_name)
        return msg(f1=msg(**run))

    def _context_reply(self, eid, esid):
        # Only attachment turns need a folder: uploads come back down as builtin
        # write calls. Announcing one otherwise makes the agent open every turn by
        # exploring a directory the caller knows nothing about.
        folders = [WS] if self.attached else []
        env = msg(f1="Linux 6.8", f2=folders, f3="/bin/bash", f5=False,
                  f7=WS + "/.terminals", f8=WS + "/.notes", f9=WS + "/.cnotes",
                  f10="UTC", f11=WS, f12=WS + "/.transcripts")
        ctx = {"f4": env}
        if self.web:
            if self.builtin_web["web_search"]:
                ctx["f17"] = True   # web_search_enabled
            if self.builtin_web["web_fetch"]:
                ctx["f24"] = True   # web_fetch_enabled
        inner = {"f10": msg(f1=msg(f1=msg(**ctx)))}
        if eid:
            inner["f1"] = eid
        if esid:
            inner["f15"] = esid
        return msg(f2=msg(**inner))

    def _send(self, payload):
        self.conn.send(frame(payload))

    def start(self, text, images=(), documents=()):
        tok = access_token()
        headers = {
            "authorization": "Bearer " + tok,
            "content-type": "application/connect+proto",
            "connect-protocol-version": "1",
            "connect-accept-encoding": "gzip",
            "user-agent": "connect-es/1.6.1",
            "x-cursor-client-type": self.client_type,
            "x-cursor-client-version": VERSION,
            "x-ghost-mode": "false",
            "x-request-id": self.rid,
            "x-original-request-id": self.rid,
        }
        if self.client_type == "sand":
            headers["x-sand-box-namespace"] = "prod"
        if self.tool_owner == "caller":
            headers["x-cursor-agent-allowed-tools"] = ",".join(
                CALLER_TOOL_ALLOWLIST)
        self.attached = bool(images or documents)
        self.conn = h2stream.acquire(HOST)
        self.conn.start(PATH, headers)
        self._send(self._run_request(text, images, documents))

    def send_tool_results(self, results):
        """results: [(exec_id_int, exec_id_str, text, is_error)] answered as McpResult"""
        self.turn_ended = False
        self.first_output = False      # first output of the next model step
        self.last_activity = self.last_frame = time.time()
        for eid, esid, text, is_error in results:
            content = msg(f1=msg(f1=str(text)))          # McpToolResultContentItem{text}
            success = msg(f1=content, f2=bool(is_error))  # McpSuccess
            inner = {"f11": msg(f1=success)}              # ExecClientMessage.mcp_result
            if eid:
                inner["f1"] = eid
            if esid:
                inner["f15"] = esid
            self._send(msg(f2=msg(**inner)))

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    # ---- event loop -------------------------------------------------------
    def events(self, idle_stop=180.0, hard_timeout=600.0, first_timeout=90.0,
               first_output_timeout=None):
        """Yields ('text'|'thinking'|'tool_use'|'web'|'tick'|'end'|'error', payload).

        A turn normally ends on turn_ended or when the stream closes; `idle_stop`
        is only a safety net for a connection that goes silent without either,
        and counts the server's heartbeats as signs of life, because a model that
        is writing a long file can stay quiet for minutes.

        'tick' fires while the upstream is quiet, so the HTTP layer can keep the
        caller's connection alive. A turn that never produces any event at all is
        cut after `first_timeout` instead of `hard_timeout`: a stalled upstream
        otherwise looks like a hung proxy for minutes. Once the upstream has
        answered, control chatter alone cannot reset `first_timeout`; if nothing
        yields a user-visible event for `first_output_timeout` the stream is
        abandoned with an explicit error and the H2 session is torn down, so the
        API caller falls through the normal error path instead of discovering a
        zero-output turn at `hard_timeout`. (first output = text/thinking/
        tool_use/web/dbg or a terminal event; control frames merely imply a
        minimal delay so the sandbox/context handshake can finish.)
        """
        t0 = time.time()
        # send_tool_results seeds last_activity when a new model step starts;
        # a fresh events() call (e.g. for the initial start()) clears it.
        if self.first_output:                      # finished previous step
            self.last_activity = self.last_frame = None
        else:
            self.last_frame = None
        while time.time() - t0 < hard_timeout:
            if self.buffered():
                for ev in self._drain():
                    yield ev
                    if ev[0] in ("end", "error"):
                        return
                continue
            try:
                kind, val = self.conn.events.get(timeout=0.5)
            except Exception:
                kind, val = None, None
            if (not self.first_output and first_output_timeout
                    and self.last_activity is not None
                    and time.time() - self.last_activity > first_output_timeout):
                # Control frames seeded the clock but no user-visible event came
                # out of them within the budget: abandon the turn loudly instead
                # of floating to hard_timeout with a zero-output "success".
                self.close()
                yield "error", f"upstream produced no output for {int(first_output_timeout)}s"
                return
            if kind is None:
                if self._idle_over(idle_stop):
                    yield "end", "idle"
                    return
                if self.last_activity is None and time.time() - t0 > first_timeout:
                    yield "error", "upstream did not respond"
                    return
                yield "tick", None
                continue
            if kind == "headers":
                if val.get(":status") != "200":
                    yield "error", f"HTTP {val.get(':status')}"
                    return
                continue
            if kind == "end":
                yield "end", kind
                return
            if kind in ("closed", "error"):
                # A reset, a GOAWAY or a dead socket is a truncated turn, not a
                # finished one. Sharing the "end" path meant the caller saw
                # HTTP 200 with whatever half answer had arrived.
                yield "error", "upstream stream %s: %s" % (kind, val)
                return
            frames, self.buf = deframe(self.buf + val)
            self.pending_frames.extend(frames)
            if self._idle_over(idle_stop) and not self.buffered():
                yield "end", "idle"
                return
        self.close()
        yield "error", f"turn exceeded hard timeout of {int(hard_timeout)}s"

    def buffered(self):
        """True while frames from an already-received batch are still queued."""
        return bool(self.pending_events or self.pending_frames)

    def _drain(self):
        """Yields the events of already-received frames, one buffered item at a time.

        Everything stays on the session between yields, so abandoning this
        generator (which is what handing a tool call back to the API caller
        does) only pauses the batch instead of dropping it.
        """
        while self.buffered():
            while self.pending_events:
                ev = self.pending_events.pop(0)
                if not self.first_output and (
                        ev[0] in ("text", "thinking", "tool_use", "web", "dbg")
                        or self.turn_ended):
                    self.first_output = True
                    self.last_activity = time.time()
                yield ev
            if self.turn_ended:
                yield "end", "turn_ended"
                return
            if not self.pending_frames:
                return
            flag, payload = self.pending_frames.pop(0)
            if flag & 0x02:
                err = _trailer_error(payload.decode("utf-8", "replace"))
                yield ("error", err) if err else ("end", "trailer")
                return
            try:
                self.pending_events = list(self._handle(payload))
            except Exception as e:
                # One undecodable frame must not abort the turn and leak the
                # stream; the rest of the batch is still worth reading.
                self.pending_events = ([("dbg", "undecodable frame: %r" % (e,))]
                                       if self.debug else [])

    def _redirect(self, field):
        """Text refusing a builtin tool in favour of the caller's own tools.

        The sandbox is a scratch directory that has nothing to do with the API
        caller's machine, so once the caller declares tools every builtin call is
        refused and the model is pointed at the equivalent caller tool.
        """
        if self.attached:
            return None
        if self.chat:
            return ("This tool is not available: there is no workspace, repository or "
                    "user machine in this conversation. Answer the user directly "
                    "instead of calling tools.")
        if not self.tools:
            return None
        for cand in BUILTIN_EQUIVALENT.get(field, ()):
            name = self.by_norm.get(cand)
            if name:
                return ("This tool is not connected to the user's machine. Call the "
                        "tool '%s' instead." % self.wire_name[name])
        return ("This tool is not connected to the user's machine. Use the provided "
                "tools (%s) instead." % ", ".join(sorted(self.wire_name.values())[:20]))

    def _reply_exec(self, replies, eid, esid):
        """Send ExecClientMessage replies tagged with the exec id they answer."""
        for rf, payload in replies:
            inner = {"f%d" % rf: payload}
            if eid:
                inner["f1"] = eid
            if esid:
                inner["f15"] = esid
            self._send(msg(f2=msg(**inner)))
        if any(rf == 14 for rf, _ in replies) and eid:
            # streamed execs must be closed: ExecClientControlMessage.stream_close
            self._send(msg(f5=msg(f1=msg(f1=eid))))

    def _builtin_tool(self, fields, ex, eid, esid, out):
        """Answer the agent's builtin file/shell tools from the sandbox."""
        candidates = sorted(fields - {1, 15, 19, 55})
        for fn in candidates:
            a = get(ex, fn)
            if a is None:
                continue
            try:
                done = sandbox.handle(fn, a, redirect=self._redirect(fn))
            except Exception as e:                 # never leave the stream unanswered
                done = [sandbox.refuse(fn, str(e))]
            if not done:
                # An unanswered ExecServerMessage stalls the stream for good, so an
                # unsupported builtin tool is refused rather than ignored.
                done = [sandbox.refuse(fn, "this tool is not available here")]
            self._reply_exec(done, eid, esid)
            if self.debug:
                out.append(("dbg", f"sandbox answered f{fn}"))
            return
        if candidates:
            # A request we cannot even name still has to be answered, or the
            # agent waits on it forever and the turn dies at idle_stop.
            self._reply_exec([sandbox.refuse(candidates[0],
                                             "this tool is not available here")],
                             eid, esid)
            if self.debug:
                out.append(("dbg", f"refused unhandled exec {candidates}"))

    def _caller_name(self, name):
        """Wire tool name -> the caller's own name for it.

        Exact wire names are the common case. Models also paraphrase the name:
        they drop the collision suffix, or fall back to Cursor's builtin spelling
        (`WebSearch_` -> `web_search`). Matching on the normalised form recovers
        those instead of handing the caller a tool it never declared.
        """
        if name in self.name_of_wire:
            return self.name_of_wire[name]
        if name in self.tool_by_name:
            return name
        for cand in (name, name.rstrip("_")):
            norm = _normalise(cand)
            hit = self.by_norm.get(norm) or self.by_flat.get(norm.replace("_", ""))
            if hit:
                return hit
        return name

    def _idle_over(self, idle_stop):
        """True when nothing at all has arrived for `idle_stop` seconds."""
        seen = self.last_frame or self.last_activity
        return seen is not None and time.time() - seen > idle_stop

    def _handle(self, sm):
        out = []
        self.last_frame = time.time()
        iu = get(sm, 1)
        if iu is not None:
            if {fn for fn, wt, v in parse(iu)} & {1, 4}:
                # text/thinking arrived; control frames (usage 14, heartbeat 13,
                # dbg-only oddities) do not count, so a one-way stream that only
                # talks to itself still hits first_timeout/first_output_timeout.
                self.last_activity = time.time()
            td = get(iu, 1)
            if td is not None and get(td, 1):
                out.append(("text", get(td, 1).decode("utf-8", "replace")))
            th = get(iu, 4)
            if th is not None and get(th, 1):
                out.append(("thinking", get(th, 1).decode("utf-8", "replace")))
            tc = get(iu, 3)                                  # tool_call_completed
            if tc is not None:
                ws = _web_search(get(tc, 2) or b"")
                if ws:
                    out.append(("web", ws))
            te = get(iu, 14)
            if te is not None:
                self.usage = {
                    "input_tokens": getvar(te, 1) or 0,
                    "output_tokens": getvar(te, 2) or 0,
                    "cache_read_input_tokens": getvar(te, 3) or 0,
                    "cache_creation_input_tokens": getvar(te, 4) or 0,
                    "reasoning_tokens": getvar(te, 5) or 0,
                }
                self.turn_ended = True
                out.append(("turn_ended", self.usage))
            if self.debug:
                for fn, wt, v in parse(iu):
                    if fn not in (1, 4, 13, 14):
                        out.append(("dbg", f"interaction f{fn}"))
            return out
        ex = get(sm, 2)
        if ex is not None:
            # Any exec-family message proves the agent is alive. Only mcp_args
            # leads to a caller-visible event, but message traffic alone must at
            # least seed the first-output clock so first_output_timeout can fire
            # once context/allowlist/sandbox chatter has died down without the
            # model ever producing text/thinking/tool output.
            first_exec = self.last_activity is None and not self.first_output
            eid = getvar(ex, 1) or 0
            esid = get(ex, 15)
            esid = esid.decode() if esid else None
            fields = {fn for fn, wt, v in parse(ex)}
            if self.debug:
                out.append(("dbg", f"exec {sorted(fields - {1, 15, 19})}"))
            if first_exec:
                self.last_activity = time.time()
            if 10 in fields:                      # request_context_args
                self._send(self._context_reply(eid or None, esid))
            elif 11 in fields:                    # mcp_args -> real tool call
                self.last_activity = time.time()
                a = get(ex, 11)
                name = (get(a, 5) or get(a, 1) or b"").decode()
                name = self._caller_name(name)
                tcid = (get(a, 3) or b"").decode()
                args = {}
                for kv in getall(a, 2):
                    k = (get(kv, 1) or b"").decode()
                    v = get(kv, 2)
                    args[k] = _mcp_value(v) if v is not None else None
                out.append(("tool_use", {"id": tcid or "toolu_" + uuid.uuid4().hex[:16],
                                         "name": name, "input": args,
                                         "exec": (eid or None, esid)}))
            else:
                key = next((k for k in (41, 42, 43) if k in fields), None)
                if key:                       # allowlist precheck -> allowlisted
                    self._reply_exec([(key, msg(f1=True))], eid, esid)
                else:
                    self._builtin_tool(fields, ex, eid, esid, out)
            return out
        kv = get(sm, 4)
        if kv is not None:
            # The server persists conversation blobs through the client and waits for
            # the ack before it runs the next model step.
            kid = getvar(kv, 1) or 0
            setb = get(kv, 3)
            getb = get(kv, 2)
            if setb is not None:
                self.blobs[get(setb, 1) or b""] = get(setb, 2) or b""
                self._send(msg(f3=msg(f1=kid, f3=msg())))
            elif getb is not None:
                bid = get(getb, 1) or b""
                data = self.blobs.get(bid)
                res = msg(f1=data) if data is not None else msg(f2=msg(f1="not found"))
                self._send(msg(f3=msg(f1=kid, f2=res)))
            return out
        iq = get(sm, 7)
        if iq is not None:                        # InteractionQuery: web search / fetch approval
            qid = getvar(iq, 1) or 0
            fields = {fn for fn, wt, v in parse(iq)}
            approve = {2: 2, 9: 9}                # query field -> response field
            for qf, rf in approve.items():
                if qf in fields:
                    resp = {"f1": qid, ("f%d" % rf): msg(f1=b"")}
                    self._send(msg(f6=msg(**resp)))
                    out.append(("dbg", "auto-approved interaction query"))
            if self.debug and not (fields & set(approve)):
                out.append(("dbg", f"interaction_query {sorted(fields - {1})}"))
        return out


def _trailer_error(text):
    """Connect end-of-stream trailer -> error text, or None for a clean end.

    The trailer is JSON: {} on success, {"error":{"code":..,"message":..}}
    otherwise. Matching on the substring '"error"' also fired on payloads that
    merely mentioned the word, and it threw away the code the caller needs.
    """
    try:
        obj = json.loads(text or "{}")
    except ValueError:
        return text[:600] if '"error"' in (text or "") else None
    err = obj.get("error") if isinstance(obj, dict) else None
    if not err:
        return None
    if isinstance(err, dict):
        detail = " ".join(str(err.get(k)) for k in ("code", "message") if err.get(k))
        return (detail or json.dumps(err, ensure_ascii=False))[:600]
    return str(err)[:600]


def _web_search(tool_call):
    """ToolCall.web_search (f18) -> {'id', 'query', 'results': [{title,url,text}]}.

    Cursor runs web search server-side: f18{1:args{1:query,2:id},
    2:result{1{1: repeated Ref{1:title,2:url,3:text}}}}.
    """
    ws = get(tool_call, 18)
    if ws is None:
        return None
    args = get(ws, 1) or b""
    query = (get(args, 1) or b"").decode("utf-8", "replace")
    tcid = (get(args, 2) or get(tool_call, 57) or b"").decode("utf-8", "replace")
    res = get(get(ws, 2) or b"", 1)
    if res is None:
        return None
    hits, seen, summary = [], set(), ""
    for ref in getall(res, 1):
        url = (get(ref, 2) or b"").decode("utf-8", "replace")
        text = (get(ref, 3) or b"").decode("utf-8", "replace")
        if not url:
            # The url-less first entry is the aggregated summary and often the
            # only entry Cursor sends. Dropping it turned a search that did find
            # pages into an empty result list.
            summary = summary or text
            continue
        seen.add(url)
        hits.append({"title": (get(ref, 1) or b"").decode("utf-8", "replace") or url,
                     "url": url, "text": _readable(text)})
    for title, url in _summary_links(summary):
        if url not in seen:
            seen.add(url)
            hits.append({"title": title, "url": url, "text": ""})
    return {"id": tcid, "query": query, "results": hits, "summary": summary}


_SANDBOX_TEXT = re.compile(r"^Full page text written to file: .*", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _readable(text):
    """Drop per-result bodies that only point at Cursor's own sandbox files.

    Cursor spools fetched pages into /tmp paths inside its agent sandbox, which
    the caller cannot open; passing that string through only invites the model to
    try reading a file that does not exist on this machine.
    """
    return "" if _SANDBOX_TEXT.match(text or "") else text


def _summary_links(summary):
    """Markdown links in the aggregated summary -> [(title, url)]."""
    return [(m.group(1), m.group(2)) for m in _LINK.finditer(summary or "")]


def _mcp_value(v):
    """McpArgs values are google.protobuf.Value; recover a python value.

    Nested kinds are decoded recursively: struct_value (field 5) becomes a
    dict and list_value (field 6) becomes a list, so object/array tool
    arguments survive instead of leaking raw protobuf bytes to the caller.
    """
    try:
        toks = parse(v)
    except Exception:
        return v.decode("utf-8", "replace")
    for fn, wt, val in toks:
        if fn == 1 and wt == 0:
            return None                              # null_value
        if fn == 2 and wt == 1:
            import struct
            f = struct.unpack("<d", val)[0]          # number_value
            # protobuf JSON-mapping demands ints for integer-typed fields and
            # rejecting "10000.0" where u64 is expected; keep true fractions.
            return int(f) if f.is_integer() else f
        if fn == 3 and wt == 2:
            return val.decode("utf-8", "replace")    # string_value
        if fn == 4 and wt == 0:
            return bool(val)                         # bool_value
        if fn == 5 and wt == 2:                      # struct_value
            obj = {}
            for kv in getall(val, 1):
                k = (get(kv, 1) or b"").decode("utf-8", "replace")
                ev = get(kv, 2)
                obj[k] = _mcp_value(ev) if ev is not None else None
            return obj
        if fn == 6 and wt == 2:                      # list_value
            return [_mcp_value(item) for item in getall(val, 1)]
    return v.decode("utf-8", "replace")
