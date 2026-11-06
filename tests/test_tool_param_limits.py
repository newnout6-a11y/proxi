"""Tool field limits reach the model in the field's description, and odd schemas no longer fail the request.

Retool's param shape has no place for maxLength, pattern, minimum, maximum and the like, so the model never saw them and
broke them; the client's tool or its MCP server then refused the call. Claude Code writes its patterns inside allOf
(SendMessage.to). A draft-3 boolean `required` or a list of properties raised TypeError while the tools were converted,
and the client got a 500 (ultracode audit of the relay, C15)."""
import direct
import validate
from test_client_contracts import CFG

SEND_MESSAGE = {"type": "object", "required": ["to"], "properties": {
    "to": {"type": "string", "description": "Recipient.",
           "allOf": [{"pattern": "^[^\\n\\r]*$"}, {"pattern": "^[\\s\\S]{0,300}$"}]},
    "summary": {"type": "string", "description": "A short label.", "maxLength": 200},
    "wait": {"type": "number", "minimum": 0, "maximum": 60.0},
    "tags": {"type": "array", "items": {"type": "string", "format": "uri"}, "maxItems": 5, "uniqueItems": True}}}


def convert(parameters):
    return direct.tools_to_retool([{"type": "function", "function": {"name": "t", "parameters": parameters}}])[0]["params"]


def test_limits_reach_the_description():
    params = convert(SEND_MESSAGE)
    assert params["to"]["description"] == ("Recipient. Limits: matching the regular expression ^[^\\n\\r]*$; matching the "
                                           "regular expression ^[\\s\\S]{0,300}$.")
    assert params["summary"]["description"] == "A short label. Limits: at most 200 characters."
    assert params["wait"]["description"] == "Limits: >= 0; <= 60."
    assert params["tags"]["description"] == "Limits: at most 5 items; no duplicate items."
    assert params["tags"]["items"]["description"] == "Limits: in uri format."
    assert params["to"]["required"] is True and params["wait"]["type"] == "number"


def test_safe_integer_bounds_say_nothing_and_one_is_singular():
    params = convert({"type": "object", "properties": {
        "count": {"type": "integer", "minimum": -9007199254740991, "maximum": 9007199254740991},
        "pick": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": "string", "minLength": 1}}}})
    assert "description" not in params["count"]
    assert params["pick"]["description"] == "Limits: at least 1 item; at most 1 item."
    assert params["pick"]["items"]["description"] == "Limits: at least 1 character."


def test_a_field_without_limits_is_declared_as_before():
    params = convert({"type": "object", "properties": {"path": {"type": "string", "description": "A path."}}})
    assert params == {"path": {"type": "string", "description": "A path.", "required": False}}


def test_odd_schemas_convert_instead_of_failing():
    assert convert({"type": "object", "required": True, "properties": {"a": {"type": "string"}}}) == {
        "a": {"type": "string", "required": False}}
    assert convert({"type": "object", "properties": [{"name": "a"}]}) == {}
    nested = convert({"type": "object", "properties": {"o": {"type": "object", "required": "a",
                                                             "properties": {"a": {"type": "string"}}}}})
    assert nested["o"]["properties"] == {"a": {"type": "string", "required": False}}
    assert direct.tools_to_retool([{"type": "function", "function": {"name": "t", "parameters": None}}])[0]["params"] == {}


def test_the_relay_parses_and_converts_a_draft_3_tool():
    body = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "legacy", "input_schema": {"type": "object", "required": True,
                                                          "properties": {"a": {"type": "string", "required": True}}}}]}
    parsed = validate.parse_anthropic(body, CFG)
    assert direct.tools_to_retool(parsed.tools)[0]["params"]["a"]["type"] == "string"
