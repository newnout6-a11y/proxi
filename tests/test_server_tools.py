"""A server tool of the provider (Claude Code's WebSearch) is refused with a reason the model can act on.

The provider runs web_search_20250305 and its kind itself; the Retool route runs client tools only. The refusal used to
say only that the type must be 'function' or 'custom', and the model kept trying; it now names the tool as a server
tool and points to a client tool."""
import pytest

import apierr
import validate
from test_client_contracts import CFG, _body

WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}


@pytest.mark.parametrize("kind", ["web_search_20250305", "web_fetch_20250910", "code_execution_20250825"])
def test_a_server_tool_is_named_as_one_with_a_way_out(kind):
    with pytest.raises(apierr.ApiError) as error:
        validate.parse_anthropic(_body("anthropic", tools=[dict(WEB_SEARCH, type=kind)]), CFG)
    assert error.value.status == 422 and error.value.param == "tools[0].type"
    assert "server tool of the provider ('%s')" % kind in error.value.message
    assert "client tool" in error.value.message and "WebFetch" in error.value.message


def test_another_unknown_type_keeps_the_plain_refusal():
    with pytest.raises(apierr.ApiError) as error:
        validate.parse_anthropic(_body("anthropic", tools=[{"type": "mystery", "name": "x"}]), CFG)
    assert error.value.message == "tools[0].type must be 'function' or 'custom'"


def test_the_messages_route_answers_with_an_invalid_request_naming_the_server_tool(relay):
    import json
    import urllib.error
    try:
        with relay.request("/v1/messages", _body("anthropic", tools=[WEB_SEARCH])) as response:
            status, body = response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        status, body = exc.code, json.loads(exc.read())
    assert status == 422 and body["error"]["type"] == "invalid_request_error"
    assert "server tool" in body["error"]["message"]
