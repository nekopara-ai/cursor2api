"""Offline contract tests for the Sand v2 GrokBot compatibility backend."""

import base64
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cursor2api import grokbot, models, server, session as session_mod
from cursor2api.grokbot import GrokBotError, GrokBotSession


def transcript(text=None, terminal=False):
    entries = []
    if text is not None:
        body = {"kind": "send-message",
                "message": {"type": "text", "content": text}}
        entries.append({
            "seq": "1",
            "entryKind": "send-message",
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
        })
    if terminal:
        body = {"kind": "turn-completed", "status": "completed"}
        entries.append({
            "seq": "2",
            "entryKind": "turn-completed",
            "body": base64.b64encode(json.dumps(body).encode()).decode(),
        })
    return {"entries": entries}


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeClient:
    def __init__(self, snapshots=None):
        self.snapshots = list(snapshots or [transcript("OK", terminal=True)])
        self.created = []
        self.sent_messages = []
        self.status_queries = []
        self.interrupted = []
        self.deleted = []
        self.send_errors = []
        self.health_states = []
        self.state = "SAND_BOX_RUN_STATE_RUNNING"
        self.lock = threading.Lock()

    def sandbox_state(self):
        return self.state

    def ensure_sandbox(self):
        self.state = "SAND_BOX_RUN_STATE_RUNNING"
        return {}

    def gateway_health(self):
        busy = self.health_states.pop(0) if self.health_states else False
        return {"ok": True, "isBusy": busy}

    def create_agent(self, name, agent_id):
        with self.lock:
            row_id = "row-%d" % (len(self.created) + 1)
            self.created.append((row_id, agent_id, name))
        return {"id": row_id, "agentId": agent_id}

    def send(self, agent_id, message_id, text):
        self.sent_messages.append((agent_id, message_id, text))
        if self.send_errors:
            raise self.send_errors.pop(0)
        return {"accepted": True}

    def send_status(self, agent_id, message_id):
        self.status_queries.append((agent_id, message_id))
        return {"outcome": "found", "record": {"status": "accepted"}}

    def transcript(self, agent_id):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def interrupt(self, agent_id, reason):
        self.interrupted.append((agent_id, reason))
        return {}

    def delete_agent(self, row_id):
        self.deleted.append(row_id)
        return {}


class GrokBotSessionTest(unittest.TestCase):
    def session(self, client=None, **kwargs):
        clock = kwargs.pop("clock", Clock())
        return GrokBotSession(
            model="claude-fable-5",
            _client=client or FakeClient(),
            _clock=clock.now,
            _sleep=clock.sleep,
            poll_interval=1,
            settle_seconds=2,
            **kwargs,
        )

    @patch("cursor2api.grokbot.AGENT_READY_DELAY", 0)
    def test_snapshot_growth_becomes_incremental_text_and_cleans_up(self):
        client = FakeClient([
            transcript("Hel"),
            transcript("Hello", terminal=True),
        ])
        client.health_states = [False, True, False]
        session = self.session(client)
        session.start("say hello")

        self.assertEqual(
            [("text", "Hel"), ("tick", None), ("text", "lo"),
             ("end", "turn_finished")],
            list(session.events(first_timeout=5, hard_timeout=20)),
        )

        session.close()
        self.assertFalse(client.interrupted)
        self.assertEqual([session.row_id], client.deleted)
        session.close()
        self.assertEqual(1, len(client.deleted), "cleanup must be idempotent")

    @patch("cursor2api.grokbot.AGENT_READY_DELAY", 0)
    def test_retry_checks_idempotency_status_before_resending(self):
        client = FakeClient()
        client.send_errors = [GrokBotError(
            "SendGrokBotUserMessage", 503, "unavailable", "connection reset", True)]
        session = self.session(client)

        session.start("one charge only")

        self.assertTrue(session.sent)
        self.assertEqual(1, len(client.sent_messages))
        self.assertEqual(1, len(client.status_queries))
        self.assertEqual(client.sent_messages[0][1], client.status_queries[0][1])

    @patch("cursor2api.grokbot.AGENT_READY_DELAY", 0)
    def test_rejected_send_is_an_error_and_agent_is_deleted(self):
        class RefusingClient(FakeClient):
            def send(self, agent_id, message_id, text):
                self.sent_messages.append((agent_id, message_id, text))
                raise GrokBotError("sendPrompt", 403, "permission_denied",
                                   "quota", False)

        client = RefusingClient()
        session = self.session(client)
        session.start("hello")
        events = list(session.events())
        session.close()

        self.assertEqual("error", events[0][0])
        self.assertIn("permission_denied", events[0][1])
        self.assertEqual([session.row_id], client.deleted)
        self.assertFalse(client.interrupted, "an unsent request must not be interrupted")

    @patch("cursor2api.grokbot.AGENT_READY_DELAY", 0)
    def test_no_output_times_out_instead_of_returning_empty_success(self):
        client = FakeClient([transcript()])
        session = self.session(client)
        session.start("hello")

        events = list(session.events(first_timeout=3, hard_timeout=10))
        session.close()

        self.assertEqual("error", events[-1][0])
        self.assertIn("did not respond", events[-1][1])
        self.assertEqual([session.row_id], client.deleted)

    @patch("cursor2api.grokbot.AGENT_READY_DELAY", 0)
    def test_transcript_rewrite_is_not_duplicated(self):
        client = FakeClient([transcript("abcdef"), transcript("abcXYZ")])
        client.health_states = [False, True, True]
        session = self.session(client)
        session.start("hello")

        events = list(session.events(first_timeout=5, hard_timeout=10))

        self.assertEqual(("text", "abcdef"), events[0])
        self.assertEqual("error", events[-1][0])
        self.assertIn("rewrote", events[-1][1])

    def test_tools_and_attachments_fail_explicitly_without_creating_agent(self):
        client = FakeClient()
        with_tools = self.session(client, tools=[{
            "name": "echo", "description": "", "input_schema": {"type": "object"}
        }])
        with_tools.start("hello")
        self.assertIn("unsupported_feature", list(with_tools.events())[0][1])

        with_image = self.session(client)
        with_image.start("hello", images=[(b"x", "image/png", 1, 1)])
        self.assertIn("unsupported_feature", list(with_image.events())[0][1])
        self.assertFalse(client.created)

    @patch("cursor2api.grokbot.AGENT_READY_DELAY", 0)
    def test_system_prompt_is_preserved_in_sent_text(self):
        client = FakeClient()
        session = self.session(client, system="Follow the system marker.")
        session.start("Human: history\nAssistant: prior\nHuman: current")

        payload = client.sent_messages[0][2]
        self.assertIn("<system>\nFollow the system marker.\n</system>", payload)
        self.assertTrue(payload.endswith("Human: current"))

    @patch("cursor2api.grokbot.AGENT_READY_DELAY", 0)
    def test_concurrent_sessions_use_distinct_agents_and_all_clean_up(self):
        client = FakeClient()

        def run(marker):
            session = self.session(client)
            session.start(marker)
            events = list(session.events(first_timeout=5, hard_timeout=10))
            session.close()
            return session.agent_id, events

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(run, ["case-%d" % i for i in range(5)]))

        agent_ids = [agent_id for agent_id, _ in results]
        self.assertEqual(5, len(set(agent_ids)))
        self.assertEqual(5, len(client.created))
        self.assertEqual(5, len(client.deleted))
        self.assertEqual({row for row, _, _ in client.created}, set(client.deleted))


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

    def test_global_sand_default_uses_the_same_capability_gate(self):
        with patch.object(session_mod, "CLIENT_TYPE", "sand"):
            self.assertIn("caller-owned tools", server.sand_request_error({
                "model": "claude-fable-5",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"name": "echo"}],
            }))

    def test_sand_rejects_tools_attachments_and_structured_tool_history(self):
        cases = [
            ({"tools": [{"name": "echo"}]}, False, "caller-owned tools"),
            ({"messages": [{"role": "user", "content": [{"type": "image"}]}]},
             False, "image input"),
            ({"messages": [{"role": "user", "content": [{"type": "document"}]}]},
             False, "PDF input"),
            ({"messages": [{"role": "assistant", "tool_calls": [{"id": "call_1"}]}]},
             True, "tool-call history"),
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
