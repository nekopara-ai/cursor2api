"""Offline contract tests for the Sand InferenceService/Stream backend."""

import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cursor2api import grokbot, models, server
from cursor2api.grokbot import GrokBotError, GrokBotSession


def stream_bytes(*frames):
    return b"".join(grokbot.envelope(frame) for frame in frames)


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeResp:
    def __init__(self, data):
        self._fp = io.BytesIO(data)
        self.closed = False

    def read(self, n=-1):
        return self._fp.read(n)

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, streams=None):
        default = [[{"textPart": {"text": "OK"}}]]
        self.streams = list(streams if streams is not None else default)
        self.opened = []
        self.deleted = []
        self.interrupted = False
        self.open_errors = []
        self.lock = threading.Lock()

    def open_stream(self, req_body, timeout=600):
        with self.lock:
            self.opened.append(req_body)
            if self.open_errors:
                raise self.open_errors.pop(0)
            payload = self.streams.pop(0) if len(self.streams) > 1 else self.streams[0]
        if isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        else:
            data = stream_bytes(*payload)
        return FakeResp(data)

    def delete_agent(self, row_id):
        self.deleted.append(row_id)
        return {}


class GrokBotSessionTest(unittest.TestCase):
    def session(self, client=None, **kwargs):
        clock = kwargs.pop("clock", Clock())
        model = kwargs.pop("model", "claude-fable-5")
        return GrokBotSession(
            model=model,
            _client=client or FakeClient(),
            _clock=clock.now,
            _sleep=clock.sleep,
            **kwargs,
        )

    def test_text_parts_are_streamed_and_closed(self):
        client = FakeClient([[
            {"textPart": {"text": "Hel"}},
            {"textPart": {"text": "lo"}},
        ]])
        session = self.session(client)
        session.start("say hello")
        self.assertEqual(
            [("text", "Hel"), ("text", "lo"), ("end", "turn_finished")],
            list(session.events(first_timeout=5, hard_timeout=20)),
        )
        session.close()
        self.assertTrue(client.opened)
        self.assertTrue(session.closed)
        session.close()
        self.assertEqual(1, len(client.opened), "cleanup must be idempotent")

    def test_rejected_stream_is_an_error(self):
        client = FakeClient()
        client.open_errors = [GrokBotError("Stream", 403, "permission_denied",
                                           "quota", False)]
        session = self.session(client)
        session.start("hello")
        events = list(session.events())
        session.close()
        self.assertEqual("error", events[0][0])
        self.assertIn("permission_denied", events[0][1])
        self.assertFalse(session.sent)

    def test_no_output_times_out_instead_of_returning_empty_success(self):
        client = FakeClient([[]])
        clock = Clock()
        session = self.session(client, clock=clock)
        session.start("hello")
        clock.value = 5
        events = list(session.events(first_timeout=3, hard_timeout=10))
        session.close()
        self.assertEqual("error", events[-1][0])
        self.assertIn("did not respond", events[-1][1])

    def test_attachments_fail_without_opening_stream(self):
        client = FakeClient()
        with_image = self.session(client)
        with_image.start("hello", images=[(b"x", "image/png", 1, 1)])
        self.assertIn("unsupported_feature", list(with_image.events())[0][1])
        self.assertFalse(client.opened)

    def test_grok_tools_are_sent_natively(self):
        client = FakeClient([[
            {"toolCallPart": {"toolName": "echo", "args": '{"q":1}',
                              "toolCallId": "call_1", "isComplete": True}},
        ]])
        session = self.session(client, model="grok-4.6", tools=[{
            "name": "echo", "description": "", "input_schema": {"type": "object"}
        }])
        session.start("hello")
        events = list(session.events())
        self.assertEqual("tool_use", events[0][0])
        self.assertEqual("echo", events[0][1]["name"])
        req = client.opened[0]
        self.assertEqual("echo", req["tools"][0]["name"])
        session.send_tool_results([(None, "call_1", "ok", False)])
        self.assertEqual(2, len(client.opened))
        follow = client.opened[1]
        self.assertTrue(any(m.get("toolContent") for m in follow["messages"]))

    def test_claude_tools_are_prompted_as_xml(self):
        xml = "<tool_call><name>echo</name><parameter name=\"q\">1</parameter></tool_call>"
        client = FakeClient([[{"textPart": {"text": xml}}]])
        session = self.session(client, tools=[{
            "name": "echo", "description": "d",
            "input_schema": {"type": "object", "properties": {"q": {"type": "integer"}}}
        }])
        session.start("hello")
        events = list(session.events())
        self.assertEqual("tool_use", events[0][0])
        self.assertEqual("echo", events[0][1]["name"])
        self.assertEqual(1, events[0][1]["input"]["q"])
        req = client.opened[0]
        self.assertNotIn("tools", req)
        sys_text = req["messages"][0]["text"]
        self.assertIn("<tool_call>", sys_text)

    def test_system_prompt_is_preserved_in_sent_text(self):
        client = FakeClient()
        session = self.session(client, system="Follow the system marker.")
        session.start("Human: history\nAssistant: prior\nHuman: current")
        payload = client.opened[0]
        texts = [m["text"] for m in payload["messages"]]
        self.assertIn("Follow the system marker.", texts[0])
        self.assertTrue(texts[-1].endswith("Human: current"))

    def test_concurrent_sessions_use_distinct_conversations(self):
        client = FakeClient()

        def run(marker):
            session = self.session(client)
            session.start(marker)
            events = list(session.events(first_timeout=5, hard_timeout=10))
            session.close()
            return session.conv, events

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(run, ["case-%d" % i for i in range(5)]))

        convs = [conv for conv, _ in results]
        self.assertEqual(5, len(set(convs)))
        self.assertEqual(5, len(client.opened))


class ServerRoutingTest(unittest.TestCase):
    def setUp(self):
        self.resolve = models.resolve
        self.options = models.options
        models.resolve = lambda name, default=None: (name, {})
        models.options = lambda name: {}

    def tearDown(self):
        models.resolve = self.resolve
        models.options = self.options

    def _fake_session_type(self, label, seen):
        class FakeSession:
            def __init__(self, **kwargs):
                seen.append((label, kwargs))

            def start(self, text, images=(), documents=()):
                seen.append(("start", text, images, documents))

        return FakeSession

    def test_only_sand_prefix_selects_grokbot_backend(self):
        seen = []
        with patch.object(server, "GrokBotSession",
                          self._fake_session_type("sand", seen)), \
             patch.object(server, "Session", self._fake_session_type("cli", seen)):
            sand = server.Turn({"model": "sand/claude-fable-5",
                                "messages": [{"role": "user", "content": "sand"}]})
            sand.start()
            cli = server.Turn({"model": "cli/claude-fable-5",
                               "messages": [{"role": "user", "content": "cli"}]})
            cli.start()

        self.assertEqual(["sand", "cli"], [item[0] for item in seen if item[0] != "start"])
        self.assertEqual("sand", seen[0][1]["client_type"])
        self.assertEqual("cli", seen[2][1]["client_type"])

    def test_unsupported_feature_maps_to_stable_client_error(self):
        self.assertEqual(
            (400, "invalid_request_error", "attachments are unavailable"),
            server.upstream_error(
                "unsupported_feature: attachments are unavailable"),
        )

    def test_plain_sand_text_stays_inside_supported_surface(self):
        self.assertIsNone(server.sand_request_error({
            "model": "sand/claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "max_tokens": 128,
            "stop_sequences": ["END"],
        }))
        self.assertIsNone(server.sand_request_error({
            "model": "sand/claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "n": 1,
            "logprobs": False,
            "response_format": {"type": "text"},
            "modalities": ["text"],
        }, openai=True))

    def test_non_sand_requests_keep_existing_capabilities(self):
        self.assertIsNone(server.sand_request_error({
            "model": "cli/claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "echo"}],
            "thinking": {"type": "enabled"},
        }))

    def test_sand_allows_tools_but_rejects_attachments(self):
        self.assertIsNone(server.sand_request_error({
            "model": "sand/claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "echo"}],
        }))
        self.assertIsNone(server.sand_request_error({
            "model": "sand/claude-fable-5",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_1", "name": "echo", "input": {}}
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}
                ]},
            ],
            "tools": [{"name": "echo"}],
        }))
        cases = [
            ({"messages": [{"role": "user", "content": [{"type": "image"}]}]},
             False, "image input"),
            ({"messages": [{"role": "user", "content": [{"type": "document"}]}]},
             False, "PDF input"),
            ({"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}
            ]}]}, True, "image input"),
        ]
        for extra, openai, expected in cases:
            body = {"model": "sand/claude-fable-5",
                    "messages": [{"role": "user", "content": "hello"}]}
            body.update(extra)
            with self.subTest(expected=expected, openai=openai):
                self.assertIn(expected, server.sand_request_error(body, openai=openai))

    def test_sand_rejects_silently_ignored_output_capabilities(self):
        cases = [
            ({"response_format": {"type": "json_schema"}}, "response_format"),
            ({"n": 2}, "n=1"),
            ({"logprobs": True}, "logprobs"),
            ({"top_logprobs": 3}, "logprobs"),
            ({"reasoning_effort": "high"}, "reasoning blocks"),
            ({"modalities": ["text", "audio"]}, "text output only"),
            ({"audio": {"voice": "alloy"}}, "audio output"),
        ]
        for extra, expected in cases:
            body = {"model": "sand/claude-fable-5",
                    "messages": [{"role": "user", "content": "hello"}]}
            body.update(extra)
            with self.subTest(expected=expected):
                self.assertIn(expected, server.sand_request_error(body, openai=True))

        self.assertIn("thinking blocks", server.sand_request_error({
            "model": "sand/claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "enabled", "budget_tokens": 128},
        }))
        self.assertIn("structured output", server.sand_request_error({
            "model": "sand/claude-fable-5",
            "messages": [{"role": "user", "content": "hello"}],
            "output_config": {"format": {"type": "json_schema"}},
        }))


if __name__ == "__main__":
    unittest.main()
