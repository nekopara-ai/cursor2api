"""Offline regression for MCP tool-call argument decoding (protobuf Value).

Codex rejects tool arguments like {"yield_time_ms": 10000.0} because its
schema expects an integer (u64).  Cursor transports them as
google.protobuf.Value where every number is a double, so _mcp_value must
hand back int for integral doubles.
"""
import struct
import unittest

from cursor2api.pb import emit
from cursor2api.session import _mcp_value


def number(n):
    return emit([(2, 1, struct.pack("<d", n))])


def string(s):
    return emit([(3, 2, s.encode())])


def struct_value(pairs):
    entries = [emit([(1, 2, k.encode()), (2, 2, v)]) for k, v in pairs]
    return emit([(5, 2, emit([(1, 2, e) for e in entries]))])


def list_value(items):
    return emit([(6, 2, emit([(1, 2, it) for it in items]))])


class McpValueTest(unittest.TestCase):
    def test_integral_double_becomes_int(self):
        out = _mcp_value(number(10000.0))
        self.assertEqual(out, 10000)
        self.assertIsInstance(out, int)

    def test_fractional_double_stays_float(self):
        out = _mcp_value(number(2.5))
        self.assertEqual(out, 2.5)
        self.assertIsInstance(out, float)

    def test_nested_struct_and_list(self):
        arg = struct_value([
            ("yield_time_ms", number(10000.0)),
            ("max_output_tokens", number(2048.0)),
            ("weight", number(0.75)),
            ("names", list_value([string("a"), number(1.0)])),
        ])
        self.assertEqual(_mcp_value(arg), {
            "yield_time_ms": 10000,
            "max_output_tokens": 2048,
            "weight": 0.75,
            "names": ["a", 1],
        })


if __name__ == "__main__":
    unittest.main()
