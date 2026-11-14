"""The integrity hint (rit) measures the body the way the endpoint does: JSON.stringify, not Python's json.dumps.

The endpoint re-serialises the parsed body in JavaScript and rejects a hint whose length does not match. Python writes
3.0, 1e-07, 1e+16 and integers past 2**53 differently from JavaScript (3, 1e-7, 10000000000000000, a rounded double),
so a float in a tool argument of a resumed or cold history gave a hint that did not match (found by the ultracode
audit, C2). A lone surrogate in a string crashed the length (UnicodeEncodeError) instead of counting \\udXXX."""
import json

import direct

# JSON.stringify of the same values, from Node 22:
# node -e "console.log(JSON.stringify([3.0, 1e-7, 1e21, 123456789012345680000, -0, 0.1, 1.5e16, 1e-6, 5e-324,
#   1.7976931348623157e308, 100.0, 0.000123, 1234.5678, 2.5e-7, 9007199254740993, 1e400, -1e-7,
#   12345678901234567890]))"
VALUES = [3.0, 1e-7, 1e21, 123456789012345680000.0, -0.0, 0.1, 1.5e16, 1e-6, 5e-324, 1.7976931348623157e308, 100.0,
          0.000123, 1234.5678, 2.5e-7, 9007199254740993, float("inf"), -1e-7, 12345678901234567890]
NODE = ("[3,1e-7,1e+21,123456789012345680000,0,0.1,15000000000000000,0.000001,5e-324,1.7976931348623157e+308,100,"
        "0.000123,1234.5678,2.5e-7,9007199254740992,null,-1e-7,12345678901234567000]")


def test_numbers_are_written_as_javascript_writes_them():
    written = [direct._js_number(float(value)) for value in VALUES]
    assert "[" + ",".join(written) + "]" == NODE


def test_the_length_of_a_body_with_floats_matches_json_stringify():
    assert direct._js_length(VALUES) == len(NODE)
    assert direct._js_length(VALUES) != len(json.dumps(VALUES, separators=(",", ":")))


def test_a_tool_argument_with_a_float_gives_the_hint_javascript_computes():
    messages = [{"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c1", "toolName": "sleep",
                                                   "toolInput": {"seconds": 3.0, "jitter": 1e-07}}]}]
    javascript = ('[{"role":"assistant","content":[{"type":"tool_call","toolCallId":"c1","toolName":"sleep",'
                  '"toolInput":{"seconds":3,"jitter":1e-7}}]}]')
    assert direct.make_rit(messages)[:8] == direct._hash32(len(javascript))


def test_a_lone_surrogate_is_counted_as_its_escape_instead_of_crashing():
    # node -e "console.log(JSON.stringify({a:'\ud800x', b:'\u2028', c:'\u001f'}).length)" -> 36
    assert direct._js_length({"a": "\ud800x", "b": "\u2028", "c": "\x1f"}) == 36
