"""A "number or string" field gets the number when the model writes the canonical text of an integer.

The model writes the value of a union with a string branch as text: an MCP tool of the Claude Code test run of 2026-11-07
with `code: integer | string` got "42" for 42 (72e6f9f0a91a, 993110d098d2, 13dcf0681ac0), while an integer field, an
integer-or-null and an integer-or-boolean got 42."""
import json

import pytest

import direct

TOOL = [{"type": "function", "function": {"name": "ticket_lookup", "parameters": {"type": "object", "properties": {
    "code": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
    "listed": {"type": ["integer", "string"]},
    "maybe": {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]},
    "label": {"type": "string"},
    "count": {"type": "integer"}}}}}]
PARAMS = direct.tools_to_retool(TOOL)[0]["params"]


@pytest.mark.parametrize("text,expected", [
    ("42", 42), ("-7", -7), ("0", 0),
    ("007", "007"), ("4.0", "4.0"), ("+5", "+5"), ("1e3", "1e3"), ("x42", "x42"), ("", ""),
    ("12345678901234567890", "12345678901234567890"),  # past the safe integers: stays text
])
def test_the_canonical_text_of_an_integer_becomes_the_integer_and_any_other_text_stays(text, expected):
    for field in ("code", "listed", "maybe"):
        assert json.loads(direct.coerce_arguments(json.dumps({field: text}), PARAMS))[field] == expected


def test_a_string_field_and_a_number_field_keep_their_rules():
    assert json.loads(direct.coerce_arguments(json.dumps({"label": "42"}), PARAMS))["label"] == "42"
    assert json.loads(direct.coerce_arguments(json.dumps({"count": "42"}), PARAMS))["count"] == 42
    assert json.loads(direct.coerce_arguments(json.dumps({"code": 42}), PARAMS))["code"] == 42  # a number stays a number


def test_the_stream_gives_the_client_the_number_too():
    stream = direct.ArgStream(PARAMS)
    out = "".join(stream.feed(chunk) for chunk in ['{"code": "4', '2", "label": "4', '2"}']) + stream.finish()
    assert json.loads(out) == {"code": 42, "label": "42"}
    assert out == direct.coerce_arguments('{"code": "42", "label": "42"}', PARAMS)
