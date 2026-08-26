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
# name -> parameters sent as RequestedModel.parameters
MODEL_PARAMS = {
    "claude-fable-5":  {"thinking": "true", "context": "300k", "effort": "high"},
    "claude-opus-5":   {"thinking": "true", "context": "300k", "effort": "high"},
    "claude-sonnet-5": {"thinking": "true", "context": "300k", "effort": "high"},
    "composer-2.5":    {"fast": "true"},
    "grok-4.6":        {"effort": "high", "fast": "true"},
    "grok-4.5":        {"effort": "high", "fast": "true"},
    "gpt-5.6-sol":     {"context": "272k", "reasoning": "medium", "fast": "false"},
    "gpt-5.6-terra":   {"context": "272k", "reasoning": "medium", "fast": "false"},
    "kimi-k3":         {"effort": "high"},
}


def requested_model(name, params=None):
    p = params if params is not None else MODEL_PARAMS.get(name, {})
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


# ExecServerMessage field -> the caller's tool that does the same job. Cursor always
# offers the model its own file/shell tools and they cannot be turned off, so a
# caller like Claude Code otherwise loses half of its tool calls to the sandbox.
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
                 chat=False, client_type=None):
        self.model, self.system, self.tools = model, system, tools or []
        self.client_type = client_type or CLIENT_TYPE
        self.web, self.workspace, self.debug = web, workspace, debug
        # Plain chat: no caller tools and no attachments, so nothing the agent's
        # builtin file/shell tools could do is of any use to the caller.
        self.chat = chat
        self.model_params = model_params
        self.conv = str(uuid.uuid4())
        self.rid = str(uuid.uuid4())
        self.conn = None
        self.buf = b""
        self.usage = {}
        self.tool_by_name = {t["name"]: t for t in self.tools}
        self.wire_name = wire_names(self.tools)
        self.name_of_wire = {v: k for k, v in self.wire_name.items()}
        self.by_norm = {_normalise(t["name"]): t["name"] for t in self.tools}
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
            ctx["f17"] = True   # web_search_enabled
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
            if kind in ("end", "closed", "error"):
                yield "end", kind
                return
            frames, self.buf = deframe(self.buf + val)
            for flag, payload in frames:
                if flag & 0x02:
                    j = payload.decode("utf-8", "replace")
                    if '"error"' in j:
                        yield "error", j[:600]
                    yield "end", "trailer"
                    return
                for ev in self._handle(payload):
                    if not self.first_output and (
                            ev[0] in ("text", "thinking", "tool_use", "web", "dbg")
                            or self.turn_ended):
                        self.first_output = True
                        self.last_activity = time.time()
                    yield ev
                if self.turn_ended:
                    yield "end", "turn_ended"
                    return
            if self._idle_over(idle_stop):
                yield "end", "idle"
                return
        self.close()
        yield "error", f"turn exceeded hard timeout of {int(hard_timeout)}s"

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

    def _builtin_tool(self, fields, ex, eid, esid, out):
        """Answer the agent's builtin file/shell tools from the sandbox."""
        for fn in sorted(fields - {1, 15, 19, 55}):
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
            for rf, payload in done:
                inner = {"f%d" % rf: payload}
                if eid:
                    inner["f1"] = eid
                if esid:
                    inner["f15"] = esid
                self._send(msg(f2=msg(**inner)))
            if any(rf == 14 for rf, _ in done) and eid:
                # streamed execs must be closed: ExecClientControlMessage.stream_close
                self._send(msg(f5=msg(f1=msg(f1=eid))))
            if self.debug:
                out.append(("dbg", f"sandbox answered f{fn}"))
            return

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
                name = self.name_of_wire.get(name, name)
                tcid = (get(a, 3) or b"").decode()
                args = {}
                for kv in getall(a, 2):
                    k = (get(kv, 1) or b"").decode()
                    v = get(kv, 2)
                    args[k] = _mcp_value(v) if v is not None else None
                out.append(("tool_use", {"id": tcid or "toolu_" + uuid.uuid4().hex[:16],
                                         "name": name, "input": args,
                                         "exec": (eid or None, esid)}))
            key = next((k for k in (41, 42, 43) if k in fields), None)
            if key:                           # allowlist precheck -> allowlisted
                inner = {"f%d" % key: msg(f1=True)}
                if eid:
                    inner["f1"] = eid
                if esid:
                    inner["f15"] = esid
                self._send(msg(f2=msg(**inner)))
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
    hits = []
    for ref in getall(res, 1):
        url = (get(ref, 2) or b"").decode("utf-8", "replace")
        if not url:
            continue                        # first entry is the aggregated summary
        hits.append({"title": (get(ref, 1) or b"").decode("utf-8", "replace"),
                     "url": url,
                     "text": (get(ref, 3) or b"").decode("utf-8", "replace")})
    return {"id": tcid, "query": query, "results": hits}


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
            return struct.unpack("<d", val)[0]       # number_value
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
