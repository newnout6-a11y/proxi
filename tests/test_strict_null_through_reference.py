"""A strict tool's field that is a reference to a nullable definition keeps its null branch, like the same field written inline.

Retool's strict mode makes every parameter required, so a field that may be null is declared "X or null" (C7); the check for
"may be null" looked at the schema as written, before its $ref or allOf was followed. A field `{"$ref": "#/$defs/OptStr"}`
whose definition is `anyOf: [string, null]` became a plain string, and the model had to invent a value where the same field
written inline could be null. zod-to-json-schema (the MCP SDK) writes a repeated nullable schema as such a reference."""
import json

import direct

NULLABLE = {"anyOf": [{"type": "string"}, {"type": "null"}]}
ROOT = {"type": "object", "required": ["inline"],
        "properties": {"inline": NULLABLE, "ref": {"$ref": "#/$defs/OptStr"}, "allof": {"allOf": [{"$ref": "#/$defs/OptStr"}]},
                       "described": {"$ref": "#/$defs/OptStr", "description": "Where to look."},
                       "map": {"$ref": "#/$defs/OptMap"}, "plain": {"$ref": "#/$defs/Str"}},
        "$defs": {"OptStr": NULLABLE, "Str": {"type": "string"},
                  "OptMap": {"anyOf": [{"type": "object", "additionalProperties": {"type": "string"}}, {"type": "null"}]}}}


def params(strict):
    tool = {"type": "function", "function": {"name": "t", "parameters": ROOT, "strict": strict}}
    return direct.tools_to_retool([tool])[0]["params"]


def test_a_reference_to_a_nullable_definition_is_declared_like_the_inline_field():
    found = params(True)
    inline = found["inline"]
    assert inline["type"] == "anyOf" and [b["type"] for b in inline["anyOf"]] == ["string", "null"]
    for name in ("ref", "allof"):
        assert found[name]["type"] == "anyOf" and [b["type"] for b in found[name]["anyOf"]] == ["string", "null"], name
        assert found[name]["required"] is False
    assert found["described"]["description"] == "Where to look."
    assert found["plain"] == {"type": "string", "required": False}  # not nullable: stays what it was


def test_a_nullable_dictionary_behind_a_reference_is_still_decoded():
    spec = params(True)["map"]
    assert spec["type"] == "anyOf" and direct._marked(spec)
    assert json.loads(direct.coerce_arguments('{"map": "{\\"k\\": \\"v\\"}"}', {"map": spec})) == {"map": {"k": "v"}}
    assert json.loads(direct.coerce_arguments('{"map": null}', {"map": spec})) == {"map": None}


def test_a_tool_that_is_not_strict_keeps_the_plain_type():
    found = params(False)
    assert found["ref"] == {"type": "string", "required": False} and found["allof"] == {"type": "string", "required": False}
