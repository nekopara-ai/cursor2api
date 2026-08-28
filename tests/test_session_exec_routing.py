"""Regression tests for mutually exclusive ExecServerMessage routing."""
import unittest
from unittest.mock import patch

from cursor2api.pb import deframe, get, msg, parse
from cursor2api.session import Session


class FakeConn:
    def __init__(self):
        self.sent = []
        self.path = None
        self.headers = None

    def start(self, path, headers):
        self.path = path
        self.headers = headers

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        pass


def sent_exec(payload):
    frames, rest = deframe(payload)
    if rest or len(frames) != 1:
        raise AssertionError("expected exactly one complete Connect frame")
    return get(frames[0][1], 2)


class ExecRoutingTest(unittest.TestCase):
    def session(self):
        session = Session(tools=[{
            "name": "probe_echo",
            "description": "Return a marker.",
            "input_schema": {"type": "object"},
        }])
        session.conn = FakeConn()
        return session

    def test_mcp_args_waits_for_real_tool_result(self):
        session = self.session()
        args = msg(f1="probe_echo", f3="tool-call-1",
                   f4="anthropic-passthrough", f5="probe_echo")
        server_message = msg(f2=msg(f1=123, f11=args, f15="exec-server-1"))

        events = session._handle(server_message)

        self.assertEqual([kind for kind, _ in events], ["tool_use"])
        self.assertEqual(session.conn.sent, [])

        session.send_tool_results([(123, "exec-server-1", "PROBE_OK", False)])
        self.assertEqual(len(session.conn.sent), 1)
        exec_message = sent_exec(session.conn.sent[0])
        self.assertEqual([field for field, _, _ in parse(exec_message)], [11, 1, 15])
        mcp_result = get(exec_message, 11)
        success = get(mcp_result, 1)
        self.assertIsNotNone(success)
        self.assertIsNone(get(mcp_result, 2))

    def test_request_context_does_not_fall_through_to_builtin(self):
        session = self.session()
        server_message = msg(f2=msg(f1=7, f10=msg(), f15="exec-server-2"))

        self.assertEqual(session._handle(server_message), [])

        self.assertEqual(len(session.conn.sent), 1)
        exec_message = sent_exec(session.conn.sent[0])
        self.assertEqual([field for field, _, _ in parse(exec_message)], [10, 1, 15])

    @patch("cursor2api.session.access_token", return_value="probe-token")
    @patch("cursor2api.session.h2stream.acquire")
    def test_caller_owned_session_allows_only_mcp_tools(self, acquire, _token):
        conn = FakeConn()
        acquire.return_value = conn
        session = self.session()

        session.start("Use probe_echo.")

        self.assertEqual(conn.path, "/agent.v1.AgentService/Run")
        self.assertEqual(
            conn.headers["x-cursor-agent-allowed-tools"],
            "mcp_tool_call,get_mcp_tools_tool_call",
        )
        self.assertFalse(session.web)

    @patch("cursor2api.session.access_token", return_value="probe-token")
    @patch("cursor2api.session.h2stream.acquire")
    def test_cursor_owned_session_preserves_legacy_tools(self, acquire, _token):
        conn = FakeConn()
        acquire.return_value = conn
        session = Session(tool_owner="cursor", web=True)

        session.start("Use Cursor tools if needed.")

        self.assertNotIn("x-cursor-agent-allowed-tools", conn.headers)
        self.assertTrue(session.web)

    def test_invalid_tool_owner_fails_fast(self):
        with self.assertRaisesRegex(ValueError, "CURSOR2API_TOOL_OWNER"):
            Session(tool_owner="mixed")


if __name__ == "__main__":
    unittest.main()
