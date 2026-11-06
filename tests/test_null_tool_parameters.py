"""A tool whose parameters are null is a tool without parameters, as it is when they are left out.

The Responses API writes a function tool without parameters as "parameters": null (FunctionTool.parameters is nullable),
and the relay refused the whole request with a 422 "must be an object" (ultracode audit of the relay, C14; offline
reproduction: 422 for a Responses function tool and for the same tool in Chat). Anything that is neither null nor an
object is still refused."""
import pytest

import apierr
import direct
import validate
from test_client_contracts import CFG, _body

NULL_TOOLS = [
    pytest.param("responses", {"type": "function", "name": "now", "parameters": None}, id="responses"),
    pytest.param("responses", {"type": "function", "name": "now", "description": None, "parameters": None,
                               "strict": None}, id="responses, every optional field null"),
    pytest.param("chat", {"type": "function", "function": {"name": "now", "parameters": None}}, id="chat"),
    pytest.param("anthropic", {"name": "now", "input_schema": None}, id="anthropic")]


@pytest.mark.parametrize("dialect,tool", NULL_TOOLS)
def test_a_tool_whose_parameters_are_null_has_none(dialect, tool):
    function = getattr(validate, "parse_" + dialect)(_body(dialect, tools=[tool]), CFG).tools[0]["function"]
    assert function["name"] == "now" and function["description"] == "" and function["parameters"] == {}
    assert "strict" not in function
    assert direct.tools_to_retool([{"type": "function", "function": function}])[0]["params"] == {}


def test_a_tool_that_leaves_them_out_is_as_before():
    assert validate.parse_responses(_body("responses", tools=[{"type": "function", "name": "now"}]),
                                    CFG).tools[0]["function"]["parameters"] == {}


def test_null_parameters_do_not_hide_a_schema_given_under_the_other_name():
    tool = {"name": "now", "parameters": None, "input_schema": {"type": "object", "properties": {"tz": {"type": "string"}}}}
    function = validate.parse_anthropic(_body("anthropic", tools=[tool]), CFG).tools[0]["function"]
    assert function["parameters"]["properties"] == {"tz": {"type": "string"}}


@pytest.mark.parametrize("bad", ["object", [], 5, True])
def test_parameters_that_are_not_an_object_are_still_refused(bad):
    with pytest.raises(apierr.ApiError) as error:
        validate.parse_responses(_body("responses", tools=[{"type": "function", "name": "now", "parameters": bad}]), CFG)
    assert error.value.status == 422 and error.value.param == "tools[0].function.parameters"
