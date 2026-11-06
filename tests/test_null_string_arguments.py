"""The string "null" in a non-string field reaches the client as null.

Retool has no way to declare "X or null", so an optional field of such a type is declared as X, and Claude writes the
string "null" when it means null: the client's tool refused middle="null", score="null", active="null" (protocol test
run of 2026-11-06). A number, boolean, array or object field never holds that text; a string field keeps it."""
import json

import direct

PARAMS = {"middle": {"type": "number"}, "active": {"type": "boolean"}, "tags": {"type": "array"},
          "meta": {"type": "object"}, "name": {"type": "string"}, "mode": {"type": "enum", "enum": ["a", "null"]}}
RAW = '{"middle": "null", "active": "null", "tags": "null", "meta": "null", "name": "null", "mode": "null"}'
EXPECTED = {"middle": None, "active": None, "tags": None, "meta": None, "name": "null", "mode": "null"}


def test_the_whole_arguments_are_repaired():
    assert json.loads(direct.coerce_arguments(RAW, PARAMS)) == EXPECTED


def test_the_streamed_arguments_are_repaired_the_same_way_whatever_the_chunks():
    for size in (1, 3, 7, len(RAW)):
        stream = direct.ArgStream(PARAMS)
        text = "".join(stream.feed(RAW[i:i + size]) for i in range(0, len(RAW), size)) + stream.finish()
        assert json.loads(text) == EXPECTED, size


def test_other_values_are_left_as_they_were():
    raw = '{"middle": "3", "active": "true", "tags": ["null"], "name": "nullable"}'
    assert json.loads(direct.coerce_arguments(raw, PARAMS)) == {"middle": 3, "active": True, "tags": ["null"],
                                                                "name": "nullable"}
