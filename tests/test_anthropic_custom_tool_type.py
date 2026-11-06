"""An Anthropic client tool keeps its input_schema whichever way its type is written.

The Messages API takes a client tool as {"name", "input_schema"}, with "type" left out, null or "custom". The relay read
"custom" as OpenAI's freeform tool and replaced the schema by one string field `input` (the model then called get_weather
with {"input": "Paris"} where the client wanted {"city": "Paris"}), and refused "type": null with a 422 (ultracode audit of
the relay, C1). A freeform tool of OpenAI has no input_schema; that is how the two are told apart."""
import copy

import pytest

import apierr
import direct
import validate
from test_client_contracts import CFG, _body

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}


def tool(**extra):
    return dict({"name": "get_weather", "description": "Weather of a city", "input_schema": SCHEMA}, **extra)


@pytest.mark.parametrize("typed", [{}, {"type": None}, {"type": "custom"}], ids=["no type", "null", "custom"])
def test_a_client_tool_keeps_its_input_schema_whatever_its_type_says(typed):
    parsed = validate.parse_anthropic(_body("anthropic", tools=[tool(**typed)]), CFG)
    function = parsed.tools[0]["function"]
    assert function["parameters"] == SCHEMA
    assert "x-client-type" not in function
    assert direct.tools_to_retool(parsed.tools)[0]["params"] == {"city": {"type": "string", "required": True}}


def test_the_tool_choice_may_name_a_tool_declared_with_type_custom():
    parsed = validate.parse_anthropic(
        _body("anthropic", tools=[tool(type="custom")], tool_choice={"type": "tool", "name": "get_weather"}), CFG)
    assert parsed.tool_choice == {"type": "function", "function": {"name": "get_weather"}}


def test_an_openai_freeform_tool_is_still_freeform():
    # No input_schema: Responses' custom tool, whose model input is the one string `input`
    body = _body("responses", tools=[{"type": "custom", "name": "apply_patch", "description": "Patch"}])
    function = validate.parse_responses(body, CFG).tools[0]["function"]
    assert function["x-client-type"] == "custom"
    assert function["parameters"]["properties"] == {"input": {"type": "string"}}


def test_an_unknown_type_is_still_refused():
    for bad in ("", "mystery", 5):
        with pytest.raises(apierr.ApiError) as error:
            validate.parse_anthropic(_body("anthropic", tools=[tool(type=bad)]), CFG)
        assert error.value.status == 422 and error.value.param == "tools[0].type"


def test_the_request_is_not_changed():
    body = _body("anthropic", tools=[tool(type="custom")])
    original = copy.deepcopy(body)
    validate.parse_anthropic(body, CFG)
    assert body == original
