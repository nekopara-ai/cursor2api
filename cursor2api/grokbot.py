"""Grok Bot (Sand v2) backend adapted to the legacy Session event contract.

The public HTTP server remains unchanged.  A request whose model has the
``sand/`` prefix gets one temporary Grok Bot agent, sends one message, converts
transcript snapshots into incremental text events, then interrupts/deletes the
agent during cleanup.
"""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .auth import access_token


BASE_URL = os.environ.get("CURSOR_GROKBOT_URL", "https://api2.cursor.sh").rstrip("/")
SERVICE = "aiserver.v1.GrokBotService"
CLIENT_VERSION = os.environ.get("CURSOR_DESKTOP_VERSION", "3.18.9")
POLL_INTERVAL = float(os.environ.get("CURSOR2API_SAND_POLL", "0.5"))
SETTLE_SECONDS = float(os.environ.get("CURSOR2API_SAND_SETTLE", "6"))
AGENT_READY_DELAY = float(os.environ.get("CURSOR2API_SAND_AGENT_READY", "0"))


def _field(obj, snake, default=None):
    if not isinstance(obj, dict):
        return default
    parts = snake.split("_")
    camel = parts[0] + "".join(part.title() for part in parts[1:])
    return obj.get(snake, obj.get(camel, default))


def _decode_body(value):
    if not value:
        return b""
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        return str(value).encode()


def _entry_body(entry):
    raw = _decode_body(_field(entry, "body"))
    try:
        body = json.loads(raw)
    except Exception:
        body = None
    return body if isinstance(body, dict) else {}


def _seq(entry):
    try:
        return int(_field(entry, "seq", 0))
    except (TypeError, ValueError):
        return 0


class GrokBotError(RuntimeError):
    def __init__(self, method, status, code, message, retryable=False):
        self.method = method
        self.status = int(status)
        self.code = str(code or "")
        self.message = str(message or code or "upstream error")
        self.retryable = bool(retryable)
        super().__init__(f"{self.code or 'grokbot_error'}: {self.message} (HTTP {self.status})")


class GrokBotClient:
    """Cursor control-plane client plus the authenticated in-box Sand gateway."""

    def __init__(self, token=None, opener=None):
        self.token = token or access_token()
        self.opener = opener or urllib.request.urlopen
        self.gateway_url = ""
        self.gateway_headers = {}

    def _headers(self):
        headers = {
            "authorization": "Bearer " + self.token,
            "content-type": "application/json",
            "connect-protocol-version": "1",
            "user-agent": "Cursor/%s" % CLIENT_VERSION,
            "x-cursor-client-version": CLIENT_VERSION,
            "x-request-id": str(uuid.uuid4()),
        }
        return headers

    def _request(self, method, request, timeout):
        try:
            with self.opener(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except Exception as exc:
            raise GrokBotError(method, 503, "unavailable", str(exc), True) from exc

        text = raw.decode(errors="replace")
        try:
            body = json.loads(text) if text else {}
        except json.JSONDecodeError:
            body = {"raw": text[:1000]}
        if status == 200:
            return body

        code = _field(body, "code", "") or "gateway_error"
        message = _field(body, "message", "") or _field(body, "error", "")
        retryable = status in (408, 429, 500, 502, 503, 504)
        for detail in _field(body, "details", []) or []:
            debug = _field(detail, "debug", {})
            info = _field(debug, "details", {})
            message = _field(info, "detail", message) or message
            if _field(info, "is_retryable") is not None:
                retryable = bool(_field(info, "is_retryable"))
        raise GrokBotError(method, status, code, message, retryable)

    def rpc(self, method, payload, timeout=45):
        request = urllib.request.Request(
            "%s/%s/%s" % (BASE_URL, SERVICE, method),
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers=self._headers(),
            method="POST",
        )
        return self._request(method, request, timeout)

    def _set_gateway(self, descriptor):
        raw_url = str(_field(descriptor, "gateway_url", "")).rstrip("/")
        token = str(_field(descriptor, "gateway_token", ""))
        network_token = str(_field(descriptor, "network_token", ""))
        parsed = urllib.parse.urlsplit(raw_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host.endswith(".cursorvm.com"):
            raise GrokBotError("EnsureSandBox", 502, "invalid_response",
                               "Cursor returned an invalid Sand gateway URL")
        if not token or not network_token:
            raise GrokBotError("EnsureSandBox", 502, "invalid_response",
                               "Cursor returned incomplete Sand gateway credentials")
        self.gateway_url = raw_url
        self.gateway_headers = {
            "authorization": "Bearer " + token,
            "content-type": "application/json",
            "x-anyrun-network-token": network_token,
            "x-sand-slim-avatars": "1",
        }

    def gateway_rpc(self, method, payload=None, timeout=90):
        if not self.gateway_url:
            raise GrokBotError(method, 503, "unavailable",
                               "Sand gateway is not connected", True)
        headers = dict(self.gateway_headers)
        headers["x-sand-request-id"] = str(uuid.uuid4())
        request = urllib.request.Request(
            "%s/api/%s" % (self.gateway_url, method),
            data=json.dumps(payload or {}, separators=(",", ":")).encode(),
            headers=headers,
            method="POST",
        )
        return self._request(method, request, timeout)

    def gateway_health(self):
        if not self.gateway_url:
            raise GrokBotError("health", 503, "unavailable",
                               "Sand gateway is not connected", True)
        request = urllib.request.Request(
            self.gateway_url + "/health", headers=self.gateway_headers)
        return self._request("health", request, 20)

    def sandbox_state(self):
        return _field(self.rpc("GetSandBoxRunState", {}), "state", "")

    def ensure_sandbox(self):
        descriptor = self.rpc("EnsureSandBox", {}, timeout=180)
        self._set_gateway(descriptor)
        return descriptor

    def create_agent(self, name, agent_id):
        return _field(self.gateway_rpc("createAgent", {
            "name": name,
            "description": "Temporary cursor2api Sand request.",
            "title": "cursor2api request",
            "avatarShape": "circle",
            "avatarColor": "#2563EB",
            "origin": "user",
            "isIntroductionSuppressed": True,
            "isKickstartRequested": False,
            "clientNonce": agent_id,
        }), "agent", {})

    def send(self, agent_id, message_id, text):
        return self.gateway_rpc("sendPrompt", {
            "agentId": agent_id,
            "prompt": text,
            "clientNonce": message_id,
            "isFork": False,
            "attachmentPaths": [],
            "attachmentNames": [],
        })

    def send_status(self, agent_id, message_id):
        return self.gateway_rpc("promptAcceptanceStatus", {
            "accountSlot": "host",
            "clientNonce": message_id,
        })

    def transcript(self, agent_id):
        return self.gateway_rpc("getAgentTranscript", {"id": agent_id})

    def interrupt(self, agent_id, reason):
        # deleteAgent below interrupts an active run before removing its session.
        return {"accepted": True}

    def delete_agent(self, agent_id):
        return self.gateway_rpc("deleteAgent", {"id": agent_id})


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
        self.poll_interval = POLL_INTERVAL if poll_interval is None else poll_interval
        self.settle_seconds = SETTLE_SECONDS if settle_seconds is None else settle_seconds
        self.row_id = None
        self.agent_id = None
        self.message_id = None
        self.started_at = None
        self.last_change = None
        self.text_snapshot = ""
        self.sent = False
        self.finished = False
        self.closed = False
        self.start_error = None
        self._usage_est = {}

    def _ensure_sandbox(self):
        with self._sandbox_lock:
            self.client.ensure_sandbox()
            deadline = self.clock() + 90
            while True:
                try:
                    health = self.client.gateway_health()
                    if _field(health, "ok", False):
                        return
                except Exception:
                    pass
                if self.clock() >= deadline:
                    raise GrokBotError("EnsureSandBox", 503, "unavailable",
                                       "Sand gateway did not become healthy", True)
                self.sleep(1)

    def _prompt(self, text):
        if not self.system:
            return text
        return "<system>\n%s\n</system>\n\n%s" % (self.system, text)

    def start(self, text, images=(), documents=()):
        self.started_at = self.clock()
        if self.tools:
            self.start_error = (
                "unsupported_feature: Sand v2 does not expose caller-owned tool results")
            return
        if images or documents:
            self.start_error = (
                "unsupported_feature: Sand v2 attachment upload is not implemented")
            return
        try:
            self._ensure_sandbox()
            client_id = str(uuid.uuid4())
            agent = self.client.create_agent(
                "cursor2api-%s" % uuid.uuid4().hex[:12], client_id)
            self.agent_id = str(_field(agent, "id", "")) or None
            self.row_id = self.agent_id
            if not self.agent_id:
                raise GrokBotError("createAgent", 502, "invalid_response",
                                   "Sand gateway createAgent returned no id")
            if AGENT_READY_DELAY:
                self.sleep(AGENT_READY_DELAY)
            self.message_id = str(uuid.uuid4())
            payload = self._prompt(text)
            last_error = None
            for attempt in range(4):
                try:
                    sent = self.client.send(self.agent_id, self.message_id, payload)
                    if not _field(sent, "accepted", False):
                        raise GrokBotError("sendPrompt", 502, "invalid_response",
                                           "Sand gateway did not accept the prompt")
                    self.sent = True
                    self.last_change = self.clock()
                    return
                except GrokBotError as exc:
                    last_error = exc
                    if not exc.retryable or attempt == 3:
                        raise
                    # The send may have reached the service before a transport
                    # failure. Query the idempotency key before retrying it.
                    try:
                        status = self.client.send_status(self.agent_id, self.message_id)
                    except Exception:
                        status = {}
                    outcome = _field(status, "outcome", "")
                    record = _field(status, "record", {}) or {}
                    if ((outcome == "found" and _field(record, "status", "") in (
                            "accepted", "pending")) or
                            outcome == "unknown-durability"):
                        self.sent = True
                        self.last_change = self.clock()
                        return
                    self.sleep(min(2 ** attempt, 5))
            raise last_error
        except Exception as exc:
            self.start_error = str(exc)

    def _combined_text(self, entries):
        direct = (isinstance(entries, list) and not any(
            _field(entry, "entry_kind") or _field(entry, "body")
            for entry in entries if isinstance(entry, dict)))
        if direct:
            request_id = ""
            for entry in entries:
                if (_field(entry, "kind", "") == "message" and
                        _field(entry, "role", "") == "user" and
                        _field(entry, "client_nonce", "") == self.message_id):
                    request_id = str(_field(entry, "request_id", ""))
            parts = []
            for entry in entries:
                if _field(entry, "kind", "") != "send-message":
                    continue
                if request_id and str(_field(entry, "request_id", "")) != request_id:
                    continue
                message = _field(entry, "message", {}) or {}
                if (_field(message, "type", "") == "text" and
                        isinstance(_field(message, "content"), str)):
                    parts.append(_field(message, "content"))
            return "".join(parts), False

        parts = []
        terminal = False
        for entry in sorted(entries, key=_seq):
            body = _entry_body(entry)
            entry_kind = str(_field(entry, "entry_kind", "")).lower()
            body_kind = str(body.get("kind") or "").lower()
            kind = body_kind or entry_kind
            message = body.get("message")
            if kind == "send-message" and isinstance(message, dict):
                if message.get("type") == "text" and isinstance(message.get("content"), str):
                    parts.append(message["content"])
            marker = " ".join(str(body.get(key, "")) for key in
                              ("kind", "status", "state", "phase", "reason")).lower()
            marker = "%s %s" % (entry_kind, marker)
            if any(token in marker for token in (
                    "turn-finished", "turn-completed", "run-finished", "run-completed",
                    "spend-complete", "spend-completion", "agent-finished")):
                terminal = True
        return "".join(parts), terminal

    def events(self, idle_stop=180.0, hard_timeout=600.0, first_timeout=90.0,
               first_output_timeout=None):
        if self.start_error:
            yield "error", self.start_error
            return
        if not self.sent:
            yield "error", "Grok Bot message was not dispatched"
            return

        began = self.started_at or self.clock()
        first_output_at = None
        while self.clock() - began < hard_timeout:
            try:
                response = self.client.transcript(self.agent_id)
            except Exception as exc:
                yield "error", str(exc)
                return
            entries = response if isinstance(response, list) else (
                _field(response, "entries", []) or [])
            text, terminal = self._combined_text(entries)
            if text != self.text_snapshot:
                if not text.startswith(self.text_snapshot):
                    yield "error", "Grok Bot rewrote already-emitted transcript text"
                    return
                delta = text[len(self.text_snapshot):]
                self.text_snapshot = text
                self.last_change = self.clock()
                if delta:
                    first_output_at = first_output_at or self.clock()
                    yield "text", delta
            now = self.clock()
            if self.text_snapshot:
                try:
                    health = self.client.gateway_health()
                    if (not _field(health, "is_busy", True) and
                            _field(health, "active_agent_id", self.agent_id) == self.agent_id):
                        terminal = True
                except Exception:
                    pass
            if terminal and self.text_snapshot:
                self.finished = True
                yield "end", "turn_finished"
                return
            if self.text_snapshot and now - (self.last_change or now) >= self.settle_seconds:
                self.finished = True
                yield "end", "settled"
                return
            if not self.text_snapshot and now - began >= first_timeout:
                yield "error", "upstream did not respond"
                return
            if (first_output_timeout and first_output_at is None and
                    self.last_change is not None and now - self.last_change >= first_output_timeout):
                yield "error", "upstream produced no output for %ds" % int(first_output_timeout)
                return
            yield "tick", None
            self.sleep(self.poll_interval)
        yield "error", "turn exceeded hard timeout of %ds" % int(hard_timeout)

    def send_tool_results(self, results):
        self.start_error = (
            "unsupported_feature: Sand v2 does not expose caller-owned tool results")

    def buffered(self):
        return False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.row_id:
            try:
                self.client.delete_agent(self.row_id)
            except Exception:
                pass
