"""Offline regressions for the stream paths that used to hang or drop frames.

Everything here runs against a fake H2 connection: no credentials, no network
and no Cursor quota are involved.
"""
import io
import os
import queue
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cursor2api import models, openai_api, server
from cursor2api.pb import emit, frame, msg
from cursor2api.session import Session, _trailer_error, _web_search

TOOLS = [{"name": "probe_echo", "description": "", "input_schema": {"type": "object"}}]


def exec_mcp(eid, tcid, name="probe_echo", arg="hi"):
    """One ExecServerMessage carrying an mcp_args tool call."""
    kv = msg(f1="text", f2=msg(f3=arg))          # map entry: key + protobuf Value
    a = msg(f1=name, f2=kv, f3=tcid, f5=name)    # McpArgs
    return frame(msg(f2=msg(f1=eid, f11=a)))


class FakeConn:
    def __init__(self, data_events):
        self.events = queue.Queue()
        for d in data_events:
            self.events.put(d if isinstance(d, tuple) else ("data", d))
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        pass


def session_with(data_events, **kw):
    s = Session(model="grok-4.6", tools=kw.pop("tools", TOOLS), **kw)
    s.conn = FakeConn(data_events)
    return s


def make_turn(body=None):
    body = body or {"model": "grok-4.6",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"name": "probe_echo",
                               "input_schema": {"type": "object"}}]}
    return server.Turn(body)


class OfflineCatalog(unittest.TestCase):
    """Keep Turn construction away from the AvailableModels RPC."""

    def setUp(self):
        self._resolve, self._options = models.resolve, models.options
        models.resolve = lambda name, default=None: (
            "grok-4.6", {"effort": "high", "fast": "true"})
        models.options = lambda mid: {"effort": ["low", "medium", "high", "xhigh"],
                                      "fast": ["false", "true"]}

    def tearDown(self):
        models.resolve, models.options = self._resolve, self._options


class TestFrameBatching(OfflineCatalog):
    def test_parallel_calls_in_one_frame_batch_all_reach_the_caller(self):
        """Both calls of a parallel batch must be delivered, not just the first.

        They share a single H2 DATA event; the second used to die with the
        generator when Turn handed control back, so the agent waited forever
        for a result nobody could produce.
        """
        turn = make_turn()
        turn.session = session_with([exec_mcp(1, "call_A") + exec_mcp(2, "call_B")])
        seen = [v["id"] for k, v in turn.stream() if k == "tool_use"]
        self.assertEqual(["call_A", "call_B"], seen)
        self.assertEqual("tool_use", turn.stop_reason)
        self.assertEqual(2, len(turn.pending))

    def test_a_lone_call_still_returns_immediately(self):
        turn = make_turn()
        turn.session = session_with([exec_mcp(1, "call_A")])
        seen = [v["id"] for k, v in turn.stream() if k == "tool_use"]
        self.assertEqual(["call_A"], seen)

    def test_abandoning_the_generator_keeps_the_rest_of_the_batch(self):
        s = session_with([exec_mcp(1, "call_A") + exec_mcp(2, "call_B")])
        gen = s.events(idle_stop=2.0, hard_timeout=6.0, first_timeout=2.0)
        first = next(v["id"] for k, v in gen if k == "tool_use")
        gen.close()
        self.assertEqual("call_A", first)
        self.assertTrue(s.buffered(), "leftover frames must survive the generator")
        rest = [v["id"] for k, v in s.events(idle_stop=2.0, hard_timeout=6.0,
                                             first_timeout=2.0) if k == "tool_use"]
        self.assertEqual(["call_B"], rest)


class TestErrorClassification(OfflineCatalog):
    def test_transport_error_is_not_a_clean_end(self):
        s = session_with([("data", frame(msg(f1=msg(f1=msg(f1="partial"))))),
                          ("error", "TimeoutError('timed out')")], tools=[])
        kinds = [k for k, _ in s.events(idle_stop=2, hard_timeout=5, first_timeout=2)]
        self.assertIn("error", kinds)
        self.assertNotIn("end", kinds)

    def test_peer_eof_is_not_a_clean_end(self):
        s = session_with([("closed", None)], tools=[])
        kinds = [k for k, _ in s.events(idle_stop=2, hard_timeout=5, first_timeout=2)]
        self.assertEqual(["error"], kinds)

    def test_malformed_frame_does_not_escape_the_generator(self):
        s = session_with([frame(emit([(2, 2, emit([(9, 3, b"")]))]))], tools=[])
        kinds = [k for k, _ in s.events(idle_stop=1.5, hard_timeout=4,
                                        first_timeout=1.5)]
        self.assertTrue(kinds, "the turn must still terminate on its own")

    def test_trailer_error_is_parsed_not_substring_matched(self):
        self.assertIsNone(_trailer_error("{}"))
        self.assertIsNone(_trailer_error('{"metadata":{"note":"no \\"error\\" here"}}'))
        self.assertIn("resource_exhausted",
                      _trailer_error('{"error":{"code":"resource_exhausted",'
                                     '"message":"quota"}}'))


class TestExecAlwaysAnswered(OfflineCatalog):
    def test_unknown_exec_message_still_gets_a_reply(self):
        """An ExecServerMessage nobody answers stalls the stream permanently."""
        s = session_with([frame(msg(f2=msg(f1=7, f77=msg(f1="?"))))], tools=[])
        list(s.events(idle_stop=1.5, hard_timeout=4, first_timeout=1.5))
        self.assertTrue(s.conn.sent, "an unrecognised exec must be refused, not ignored")


class TestChunkedTermination(unittest.TestCase):
    """Every exit from a chunked response has to write the terminating chunk."""

    class Wire(io.BytesIO):
        def flush(self):
            pass

    def handler(self):
        h = server.Handler.__new__(server.Handler)
        h.wfile = self.Wire()
        h._headers_sent = False
        h._stream_flavor = None
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda: None
        return h

    class FakeTurn:
        def __init__(self, events):
            self._events = events
            self.session = type("FakeSession", (), {"_usage_est": {}})()
            self.model_in = "claude-fable-5"
            self.stop_reason = None
            self.stop_sequence = None
            self.pending = []
            self.usage = {}
            self._usage_final = None
            self.body = {}
            self.resumed = False
            self.text_so_far = ""
            self.think_chars = 0
            self.sent_chars = 40

        def stream(self):
            return iter(self._events)

    def test_anthropic_error_terminates_the_body(self):
        h = self.handler()
        h._stream_turn(self.FakeTurn([("text", "half"), ("error", "boom")]))
        body = h.wfile.getvalue()
        self.assertIn(b"event: error", body)
        self.assertTrue(body.endswith(b"0\r\n\r\n"), body[-40:])

    def test_openai_error_terminates_the_body(self):
        h = self.handler()
        h._stream_turn_openai(self.FakeTurn([("text", "half"), ("error", "boom")]))
        self.assertTrue(h.wfile.getvalue().endswith(b"0\r\n\r\n"))

    def test_success_terminates_the_body(self):
        h = self.handler()
        h._stream_turn(self.FakeTurn([("text", "all good"), ("done", None)]))
        body = h.wfile.getvalue()
        self.assertIn(b"message_stop", body)
        self.assertTrue(body.endswith(b"0\r\n\r\n"))

    def test_fail_after_headers_does_not_write_a_second_status_line(self):
        h = self.handler()
        h._headers_sent = True
        h._stream_flavor = "anthropic"
        h._fail(500, "api_error", "late boom")
        body = h.wfile.getvalue()
        self.assertNotIn(b"HTTP/1.1", body)
        self.assertIn(b"late boom", body)
        self.assertTrue(body.endswith(b"0\r\n\r\n"))


class TestRequestTranslation(OfflineCatalog):
    def test_single_assistant_message_keeps_its_text(self):
        """A lone assistant turn used to be beheaded into 'nt: hello there'."""
        prompt, _, _ = server.render_history([{"role": "assistant",
                                               "content": "hello there"}])
        self.assertEqual("Assistant: hello there", prompt)

    def test_single_user_message_loses_only_the_prefix(self):
        prompt, _, _ = server.render_history([{"role": "user", "content": "hi"}])
        self.assertEqual("hi", prompt)

    def test_max_completion_tokens_wins_over_legacy_max_tokens(self):
        out = openai_api.to_anthropic({"model": "x", "messages": [],
                                       "max_completion_tokens": 32000,
                                       "max_tokens": 1024})
        self.assertEqual(32000, out["max_tokens"])

    def test_reasoning_effort_survives_the_openai_translation(self):
        out = openai_api.to_anthropic({"model": "x", "messages": [],
                                       "reasoning_effort": "low"})
        self.assertEqual("low", out["thinking"]["effort"])


class TestEffortMapping(OfflineCatalog):
    def _params(self, body):
        turn = make_turn(dict(body, messages=[{"role": "user", "content": "hi"}]))
        turn._tune([], [])
        return turn.model_params

    def test_requested_level_reaches_the_model_parameter(self):
        self.assertEqual("low", self._params({"model": "grok-4.6",
                                              "reasoning_effort": "low"})["effort"])
        self.assertEqual("high", self._params({"model": "grok-4.6",
                                               "reasoning_effort": "high"})["effort"])

    def test_unsupported_level_is_clamped_to_a_published_one(self):
        # grok publishes low..xhigh; "max" must degrade instead of being sent.
        self.assertEqual("xhigh", self._params({"model": "grok-4.6",
                                                "reasoning_effort": "max"})["effort"])

    def test_thinking_budget_buckets_onto_the_ladder(self):
        # LiteLLM spells the caller's low/medium/high as 1024/2048/4096 budgets.
        for budget, level in ((1024, "low"), (2048, "medium")):
            params = self._params({"model": "grok-4.6",
                                   "thinking": {"type": "enabled",
                                                "budget_tokens": budget}})
            self.assertEqual(level, params["effort"], budget)

    def test_a_budget_above_the_middle_rung_asks_for_the_strongest_level(self):
        # grok stops at xhigh, so "max" clamps; claude publishes max verbatim.
        params = self._params({"model": "grok-4.6",
                               "thinking": {"type": "enabled",
                                            "budget_tokens": 4096}})
        self.assertEqual("xhigh", params["effort"])

    def test_no_thinking_parameter_is_invented_for_models_without_one(self):
        # grok publishes effort/fast only; a `thinking` id would be rejected.
        self.assertNotIn("thinking", self._params({"model": "grok-4.6"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWebSearchNaming(OfflineCatalog):
    """The caller's own search tool must survive Cursor's builtin collision."""

    WEB_TOOLS = [{"name": "WebSearch", "description": "",
                  "input_schema": {"type": "object"}}]

    def test_builtin_spelling_maps_back_to_the_callers_tool(self):
        s = Session(tools=self.WEB_TOOLS)
        s.conn = FakeConn([])
        self.assertEqual("WebSearch_", s.wire_name["WebSearch"])
        for spoken in ("WebSearch_", "web_search", "WebSearch", "websearch"):
            self.assertEqual("WebSearch", s._caller_name(spoken), spoken)

    def test_unknown_tool_names_are_left_alone(self):
        s = Session(tools=self.WEB_TOOLS)
        self.assertEqual("shell", s._caller_name("shell"))

    def test_caller_owned_session_disables_cursor_web_tools(self):
        s = Session(tools=self.WEB_TOOLS)
        s.conn = FakeConn([])
        self.assertFalse(s.builtin_web["web_search"])
        self.assertFalse(s.builtin_web["web_fetch"])

    def test_cursor_owned_search_is_disabled_on_name_collision(self):
        s = Session(tools=self.WEB_TOOLS, tool_owner="cursor")
        self.assertFalse(s.builtin_web["web_search"])
        self.assertTrue(s.builtin_web["web_fetch"])

    def test_cursor_owned_search_stays_on_without_a_colliding_tool(self):
        s = Session(tools=TOOLS, tool_owner="cursor")
        self.assertTrue(s.builtin_web["web_search"])


class TestWebSearchResults(unittest.TestCase):
    """Cursor sends the hit list inside a url-less summary entry."""

    @staticmethod
    def _call(refs):
        return msg(f18=msg(f1=msg(f1="q", f2="ws1"),
                           f2=msg(f1=emit([(1, 2, msg(f1=t, f2=u, f3=x))
                                           for t, u, x in refs]))))

    def test_summary_only_search_is_not_reported_as_empty(self):
        ws = _web_search(self._call([
            ("Web search results", "", "Links:\n1. [Kimi](https://kimi.ai/a)\n")]))
        self.assertEqual([("Kimi", "https://kimi.ai/a")],
                         [(r["title"], r["url"]) for r in ws["results"]])

    def test_real_hits_win_and_are_not_duplicated_by_the_summary(self):
        ws = _web_search(self._call([
            ("Web search results", "", "Links:\n1. [Kimi](https://kimi.ai/a)\n"),
            ("Kimi", "https://kimi.ai/a", "body")]))
        self.assertEqual(1, len(ws["results"]))
        self.assertEqual("body", ws["results"][0]["text"])

    def test_sandbox_file_pointers_are_not_passed_to_the_caller(self):
        ws = _web_search(self._call([
            ("Kimi", "https://kimi.ai/a",
             "Full page text written to file: /tmp/x.txt\nSize: 21.9 KB")]))
        self.assertEqual("", ws["results"][0]["text"])
