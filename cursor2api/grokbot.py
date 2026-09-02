"""Sand backend: Cursor InferenceService/Stream (connect+json).

Requests with the ``sand/`` prefix used to create a Grok Bot agent and poll
``sendPrompt``. That path cannot return caller-owned tool results. The live
protocol is now:

  POST https://api2.cursor.sh/aiserver.v1.InferenceService/Stream
  content-type: application/connect+json
  5-byte envelope (0x00 + uint32be length) around JSON
  Authorization: Bearer <session JWT>
  x-cursor-client-type: sand
  x-cursor-checksum: required

Grok accepts native ``tools`` and emits ``toolCallPart`` frames. Claude's
provider rejects native tools, so those models get an XML prompt and a
stream-side parser. Tool results are sent on the next Stream POST (not MCP).
"""

import base64
import hashlib
import json
import os
import platform
import re
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid

from .auth import access_token


BASE_URL = os.environ.get("CURSOR_GROKBOT_URL", "https://api2.cursor.sh").rstrip("/")
STREAM_PATH = "/aiserver.v1.InferenceService/Stream"
CLIENT_VERSION = os.environ.get("CURSOR_DESKTOP_VERSION", "3.18.9")
ROLE = {"system": 4, "user": 1, "assistant": 2, "tool": 3, "developer": 4}
PROMPTED_TOOL_PREFIXES = ("claude",)
_TOOL_OPEN_RE = re.compile(r"<tool_call\s*>", re.IGNORECASE)
_TOOL_CLOSE_RE = re.compile(r"</tool_call\s*>", re.IGNORECASE)
_NAME_RE = re.compile(r"<name>\s*(.*?)\s*</name>", re.DOTALL | re.IGNORECASE)
_INVOKE_RE = re.compile(r"<invoke\s+name\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>/]+)", re.IGNORECASE)
_PARAM_OPEN_RE = re.compile(
    r"<parameter\s+name\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>/]+)\s*(/?)>",
    re.IGNORECASE,
)
_PARAM_END_RE = re.compile(
    r"</parameter\s*>(?=\s*(?:<parameter\b|</invoke\s*>|</tool_call\s*>|\Z))",
    re.IGNORECASE,
)


def _needs_prompted_tools(model):
    base = (model or "").split(":", 1)[0].strip().lower()
    return base.startswith(PROMPTED_TOOL_PREFIXES)


def envelope(obj):
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def checksum_header(machine_id, mac_id=""):
    x = int(time.time() * 1000) // 1000000
    arr = bytearray([(x >> s) & 255 for s in (40, 32, 24, 16, 8, 0)])
    e = 165
    for i in range(len(arr)):
        arr[i] = ((arr[i] ^ e) + i % 256) & 0xFF
        e = arr[i]
    p = base64.b64encode(bytes(arr)).decode()
    return p + (machine_id or "") + ("/" + mac_id if mac_id else "")


def machine_ids(token=""):
    mid = os.environ.get("CURSOR_MACHINE_ID") or ""
    mac = os.environ.get("CURSOR_MAC_MACHINE_ID") or ""
    if not mid:
        seed = token or access_token()
        mid = hashlib.sha256(("cursor2api-machine:" + seed).encode()).hexdigest()
    if not mac:
        mac = hashlib.sha256(("cursor2api-mac:" + mid).encode()).hexdigest()
    return mid, mac


def jwt_type(tok):
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg)).get("type")
    except Exception:
        return None


def _os_arch():
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"linux": "linux", "darwin": "mac", "windows": "win"}.get(system, system or "linux")
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return os_name, arch


def _read_exact(fp, n):
    buf = b""
    while len(buf) < n:
        chunk = fp.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def read_frames(resp):
    while True:
        hdr = _read_exact(resp, 5)
        if len(hdr) < 5:
            break
        (ln,) = struct.unpack(">I", hdr[1:5])
        payload = _read_exact(resp, ln) if ln else b""
        if len(payload) < ln:
            break
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except Exception:
            continue


def extract_error(m):
    err = m.get("error") or {}
    if isinstance(err, str):
        return err
    code = err.get("code")
    msg = err.get("message")
    details = err.get("details")
    if isinstance(details, list) and details:
        dbg = details[0].get("debug") if isinstance(details[0], dict) else None
        if isinstance(dbg, dict) and dbg.get("error"):
            return "%s: %s" % (code or "error", dbg["error"])
    return "%s: %s" % (code or "error", msg or "unknown")


def _merge_args(acc, a, is_complete=False):
    a = a or ""
    if not a or a == acc:
        return acc, ""
    if a.startswith(acc):
        return a, a[len(acc):]
    if acc.startswith(a) or acc.endswith(a):
        return acc, ""
    return acc + a, a


def _xml_unescape(text):
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            .replace("&quot;", '"').replace("&#39;", "'"))


def _tool_prompt(tools):
    lines = [
        "You can call tools by emitting this XML (no other wrapper):",
        "<tool_call>",
        "<name>TOOL_NAME</name>",
        '<parameter name="ARG">VALUE</parameter>',
        "</tool_call>",
        "Available tools:",
    ]
    for t in tools or []:
        name = t.get("name") or ""
        desc = t.get("description") or ""
        schema = t.get("input_schema") or {"type": "object"}
        lines.append("- %s: %s" % (name, desc))
        lines.append("  schema: %s" % json.dumps(schema, ensure_ascii=False))
    return "\n".join(lines)


def _parse_tool_xml(block, tools):
    name = ""
    m = _NAME_RE.search(block)
    if m:
        name = m.group(1).strip()
    else:
        inv = _INVOKE_RE.search(block)
        if inv:
            name = inv.group(1).strip().strip("\"'")
    args = {}
    schemas = {}
    for t in tools or []:
        if t.get("name"):
            schemas[t["name"]] = (t.get("input_schema") or {}).get("properties") or {}
    props = schemas.get(name) or {}
    pos = 0
    while True:
        om = _PARAM_OPEN_RE.search(block, pos)
        if not om:
            break
        key = om.group(1).strip().strip("\"'")
        if om.group(2) == "/":
            args[key] = ""
            pos = om.end()
            continue
        em = _PARAM_END_RE.search(block, om.end())
        if not em:
            raw = block[om.end():]
            pos = len(block)
        else:
            raw = block[om.end():em.start()]
            pos = em.end()
        raw = _xml_unescape(raw.strip())
        spec = props.get(key) if isinstance(props, dict) else None
        typ = (spec or {}).get("type") if isinstance(spec, dict) else None
        if typ == "integer":
            try:
                raw = int(raw)
            except (TypeError, ValueError):
                pass
        elif typ == "number":
            try:
                raw = float(raw)
            except (TypeError, ValueError):
                pass
        elif typ == "boolean":
            raw = raw.strip().lower() in ("true", "1", "yes")
        elif typ in ("object", "array"):
            try:
                raw = json.loads(raw)
            except Exception:
                pass
        args[key] = raw
    return {"name": name, "input": args}


class PromptedStreamParser:
    def __init__(self, tools):
        self.tools = tools or []
        self.buf = ""

    def feed(self, text):
        self.buf += text or ""
        events = []
        while True:
            open_m = _TOOL_OPEN_RE.search(self.buf)
            if not open_m:
                if self.buf:
                    events.append(("text", self.buf))
                    self.buf = ""
                break
            prefix = self.buf[:open_m.start()]
            if prefix:
                events.append(("text", prefix))
            close_m = _TOOL_CLOSE_RE.search(self.buf, open_m.end())
            if not close_m:
                self.buf = self.buf[open_m.start():]
                break
            block = self.buf[open_m.end():close_m.start()]
            events.append(("tool_use", _parse_tool_xml(block, self.tools)))
            self.buf = self.buf[close_m.end():]
        return events

    def flush(self):
        events = []
        if self.buf:
            events.append(("text", self.buf))
            self.buf = ""
        return events


class GrokBotError(RuntimeError):
    def __init__(self, method, status, code, message, retryable=False):
        self.method = method
        self.status = int(status)
        self.code = str(code or "")
        self.message = str(message or code or "upstream error")
        self.retryable = bool(retryable)
        super().__init__(f"{self.code or 'grokbot_error'}: {self.message} (HTTP {self.status})")


class GrokBotClient:
    """Connect+json client for InferenceService/Stream."""

    def __init__(self, token=None, opener=None):
        self.token = token or access_token()
        self.opener = opener or urllib.request.urlopen
        self.machine_id, self.mac_id = machine_ids(self.token)
        self.os_name, self.arch = _os_arch()

    def headers(self, content_type="application/connect+json"):
        tok = self.token or access_token()
        self.token = tok
        return {
            "content-type": content_type,
            "connect-protocol-version": "1",
            "authorization": "Bearer " + tok,
            "user-agent": "Cursor/%s" % CLIENT_VERSION,
            "x-cursor-version": CLIENT_VERSION,
            "x-cursor-client-version": CLIENT_VERSION,
            "x-cursor-client-type": "sand",
            "x-cursor-client-os": self.os_name,
            "x-cursor-client-arch": self.arch,
            "x-cursor-client-device-type": "desktop",
            "x-cursor-checksum": checksum_header(self.machine_id, self.mac_id),
            "x-request-id": str(uuid.uuid4()),
        }

    def open_stream(self, req_body, timeout=600):
        kind = jwt_type(self.token)
        if kind == "web":
            raise GrokBotError("Stream", 401, "unauthenticated",
                               "Stream requires a session JWT, not a web token")
        payload = envelope(req_body)
        request = urllib.request.Request(
            BASE_URL + STREAM_PATH,
            data=payload,
            headers=self.headers(),
            method="POST",
        )
        try:
            return self.opener(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            text = raw.decode(errors="replace")
            try:
                body = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = {"raw": text[:1000]}
            raise GrokBotError(
                "Stream", exc.code,
                body.get("code") or "upstream_error",
                extract_error(body) if isinstance(body, dict) else text[:500],
                exc.code in (408, 429, 500, 502, 503, 504),
            ) from exc
        except Exception as exc:
            raise GrokBotError("Stream", 503, "unavailable", str(exc), True) from exc


class GrokBotSession:
    """Implements the subset of ``session.Session`` consumed by ``Turn``."""

    client_factory = GrokBotClient
    _sandbox_lock = threading.Lock()

    def __init__(self, model, system=None, tools=None, web=True, model_params=None,
                 debug=False, chat=False, client_type="sand", _client=None,
                 _clock=None, _sleep=None, poll_interval=None, settle_seconds=None):
        self.model = model
        self.system = system or ""
        self.tools = list(tools or [])
        self.web = web
        self.model_params = dict(model_params or {})
        self.debug = debug
        self.chat = chat
        self.client_type = client_type
        self.client = _client or self.client_factory()
        self.clock = _clock or time.monotonic
        self.sleep = _sleep or time.sleep
        self.conv = str(uuid.uuid4())
        self.row_id = self.conv
        self.agent_id = self.conv
        self.message_id = None
        self.started_at = None
        self.sent = False
        self.finished = False
        self.closed = False
        self.start_error = None
        self.resp = None
        self.prompted = bool(self.tools) and _needs_prompted_tools(self.model)
        self.history = []
        self.pending_events = []
        self._usage_est = {}
        self._open_tools = []

    def _requested_model(self):
        params = [{"id": k, "value": str(v).lower() if isinstance(v, bool) else str(v)}
                  for k, v in (self.model_params or {}).items()]
        out = {
            "modelId": self.model,
            "builtInModel": True,
            "maxMode": False,
            "isVariantStringRepresentation": False,
        }
        if params:
            out["parameters"] = params
        return out

    def _cursor_tools(self):
        out = []
        for t in self.tools:
            name = t.get("name")
            if not name:
                continue
            out.append({
                "name": name,
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            })
        return out

    def _build_request(self, messages):
        req = {
            "messages": messages,
            "modelId": self.model,
            "conversationId": self.conv,
            "invocationId": str(uuid.uuid4()),
            "requestedModel": self._requested_model(),
        }
        if self.tools and not self.prompted:
            tools = self._cursor_tools()
            if tools:
                req["tools"] = tools
        return req

    def _open(self, messages):
        if self.resp is not None:
            try:
                self.resp.close()
            except Exception:
                pass
            self.resp = None
        self.resp = self.client.open_stream(self._build_request(messages))
        self.sent = True
        self.finished = False
        self.pending_events = []

    def start(self, text, images=(), documents=()):
        self.started_at = self.clock()
        if images or documents:
            self.start_error = (
                "unsupported_feature: Sand Stream attachment upload is not implemented")
            return
        try:
            messages = []
            system = self.system or ""
            if self.prompted:
                extra = _tool_prompt(self.tools)
                system = (system + "\n\n" + extra).strip() if system else extra
            if system:
                messages.append({"role": ROLE["system"], "text": system})
            messages.append({"role": ROLE["user"], "text": text})
            self.history = list(messages)
            self.message_id = str(uuid.uuid4())
            self._open(messages)
        except Exception as exc:
            self.start_error = str(exc)

    def _emit_tool(self, name, args, call_id=None):
        if isinstance(args, str):
            raw = args
            try:
                parsed = json.loads(args) if args.strip() else {}
            except Exception:
                parsed = {"_raw": args}
        else:
            parsed = args or {}
            raw = json.dumps(parsed, ensure_ascii=False)
        cid = call_id or ("toolu_" + uuid.uuid4().hex[:16])
        rec = {"id": cid, "name": name or "", "input": parsed, "args": raw,
               "exec": (None, cid)}
        self._open_tools.append(rec)
        return ("tool_use", {"id": cid, "name": name or "", "input": parsed,
                             "exec": (None, cid)})

    def events(self, idle_stop=180.0, hard_timeout=600.0, first_timeout=90.0,
               first_output_timeout=None):
        if self.start_error:
            yield "error", self.start_error
            return
        if not self.sent or self.resp is None:
            yield "error", "Stream request was not dispatched"
            return

        parser = PromptedStreamParser(self.tools) if self.prompted else None
        tool_keys = []
        tool_acc = {}
        tool_meta = {}
        cur_key = [None]
        saw_output = False
        began = self.clock() if self.started_at is None else self.started_at

        def drain_parsed(items):
            out = []
            for kind, payload in items:
                if kind == "text" and payload:
                    out.append(("text", payload))
                elif kind == "tool_use":
                    out.append(self._emit_tool(payload.get("name"), payload.get("input") or {}))
            return out

        try:
            for m in read_frames(self.resp):
                if self.clock() - began > hard_timeout:
                    yield "error", "turn exceeded hard timeout of %ds" % int(hard_timeout)
                    return
                if "error" in m:
                    yield "error", extract_error(m)
                    return
                if "usage" in m:
                    usage = m.get("usage") or {}
                    yield "turn_ended", {
                        "input_tokens": usage.get("promptTokens") or 0,
                        "output_tokens": usage.get("completionTokens") or 0,
                    }
                    continue
                if "thinkingPart" in m:
                    t = (m.get("thinkingPart") or {}).get("text", "")
                    if t:
                        saw_output = True
                        yield "thinking", t
                    continue
                if "textPart" in m:
                    t = (m.get("textPart") or {}).get("text", "")
                    if not t:
                        continue
                    if parser:
                        for ev in drain_parsed(parser.feed(t)):
                            saw_output = True
                            yield ev
                    else:
                        saw_output = True
                        yield "text", t
                    continue
                if parser:
                    continue
                if "toolCallPart" in m:
                    tc = m.get("toolCallPart") or {}
                    key = tc.get("toolIndex")
                    if key is None:
                        key = tc.get("toolCallId") or cur_key[0]
                    if key is None:
                        key = len(tool_keys)
                    cur_key[0] = key
                    new_acc, _delta = _merge_args(tool_acc.get(key, ""), tc.get("args"),
                                                  tc.get("isComplete"))
                    tool_acc[key] = new_acc
                    meta = tool_meta.setdefault(key, {})
                    if tc.get("toolCallId"):
                        meta["id"] = tc["toolCallId"]
                    if tc.get("toolName"):
                        meta["name"] = tc["toolName"]
                    if key not in tool_keys:
                        tool_keys.append(key)
                    if tc.get("isComplete"):
                        # final frame: emit the complete args so send_tool_results
                        # can claim this call; keep id/name from an eager frame
                        meta["_emitted"] = True
                        saw_output = True
                        yield self._emit_tool(
                            meta.get("name") or tc.get("toolName") or "",
                            tool_acc[key], meta.get("id") or tc.get("toolCallId"))
                        continue
                    # mid-stream: finish and emit once there is a name + args,
                    # never flushing a partial JSON to the caller; the complete
                    # args are refreshed on the terminal isComplete frame.
                    if tc.get("args") and not meta["_emitted"]:
                        _frag = tc.get("args") or ""
                        if (meta.get("name") or tc.get("toolName")) and (meta.get("_partial") or _frag):
                            meta.setdefault("_partial", "")
                            if not meta["_partial"]:
                                meta["_partial"] = _frag
                            else:
                                meta["_partial"] += _frag
                            meta["_emitted"] = True
                            saw_output = True
                            yield self._emit_tool(
                                meta.get("name") or tc.get("toolName") or "",
                                meta["_partial"],
                                meta.get("id") or tc.get("toolCallId"))
                    continue
            if parser:
                for ev in drain_parsed(parser.flush()):
                    saw_output = True
                    yield ev
            # Flush incomplete native tool calls that never set isComplete.
            # Completeness is best-effort: JSON args are vulnerable to a non-
            # isComplete stream, and `_raw` preserves the lossless text so the
            # caller can still claim this call with correct content.
            for key in tool_keys:
                meta = tool_meta.get(key) or {}
                if meta.get("_emitted") or not meta.get("name"):
                    continue
                if not tool_acc.get(key) or tool_acc[key] == ":" or " " in (tool_acc.get(key) or ""):
                    pass
                saw_output = True
                yield self._emit_tool(meta.get("name"), tool_acc.get(key, ""), meta.get("id"))
        except Exception as exc:
            yield "error", str(exc)
            return

        if not saw_output:
            elapsed = self.clock() - began
            if elapsed >= first_timeout:
                yield "error", "upstream did not respond"
                return
        self.finished = True
        yield "end", "turn_finished"

    def send_tool_results(self, results):
        """results: [(exec_id_int, exec_id_str, text, is_error)]."""
        self.start_error = None
        assistant = {"role": ROLE["assistant"], "text": ""}
        calls = []
        by_id = {t["id"]: t for t in self._open_tools}
        if not by_id and len(self._open_tools) == 1:
            by_id = {self._open_tools[0]["id"]: self._open_tools[0]}
        for eid, esid, text, is_error in results:
            rec = by_id.get(esid)
            if rec is None:
                # a result only ever answers a call already handed to us
                rec = {"id": esid or str(uuid.uuid4()), "name": "", "input": {}}
            calls.append({
                "toolCallId": rec["id"],
                "toolName": rec.get("name") or "",
                "args": rec.get("input") or {},
            })
        if calls:
            assistant["toolCalls"] = calls
            self.history.append(assistant)
        if self.prompted:
            chunks = []
            for eid, esid, text, is_error in results:
                rec = by_id.get(esid) or {}
                chunks.append('<tool_result name="%s">\n%s\n</tool_result>' % (
                    rec.get("name") or "", text))
            self.history.append({"role": ROLE["user"], "text": "\n\n".join(chunks)})
        else:
            for eid, esid, text, is_error in results:
                rec = by_id.get(esid) or {}
                self.history.append({
                    "role": ROLE["tool"],
                    "toolContent": {"parts": [{
                        "toolCallId": rec.get("id") or esid or "",
                        "toolName": rec.get("name") or "",
                        "result": str(text),
                        "isError": bool(is_error),
                    }]},
                })
        self._open_tools = []
        try:
            self._open(self.history)
        except Exception as exc:
            self.start_error = str(exc)

    def buffered(self):
        return bool(self.pending_events)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.resp is not None:
            try:
                self.resp.close()
            except Exception:
                pass
            self.resp = None
