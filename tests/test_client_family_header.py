"""x-proxi-client names a client family however it is spelled, for the client's identity as for the prompts layer."""
import pytest

import client_context
import journal
import prompt_policy

CLAUDE_UA = {"user-agent": "claude-cli/2.1.310 (external, cli)"}
SPELLINGS = ["claude-code", "Claude Code", "claude_code", "CLAUDE-CODE", "claude-cli", "Claude Desktop", "claude_desktop",
             "codex", "Codex Desktop", "codex_exec", "OpenCode", "opencode", "harness", "DeepSeek Harness", "dsh",
             "unknown", "", "my own client", "retool"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_the_identity_and_the_prompts_layer_agree_on_the_family(spelling):
    assert client_context.family(spelling) == prompt_policy._family(spelling)


@pytest.mark.parametrize("spelling", ["Claude Code", "claude_code", "claude-cli", "claude code"])
def test_a_documented_spelling_of_claude_code_is_claude_code(spelling):
    identity = client_context.describe({}, {**CLAUDE_UA, "x-proxi-client": spelling})
    assert identity["client"] == "claude-code"


def test_a_name_that_is_no_family_is_kept_as_written():
    identity = client_context.describe({}, {"x-proxi-client": "My Own Client"})
    assert identity["client"] == "my own client"


@pytest.mark.parametrize("headers, name, label", [
    ({"originator": "codex_exec", "user-agent": "codex_exec/0.167.0 (Windows)"}, "codex", "Codex"),
    ({"x-codex-window-id": "probe", "user-agent": "local-wrapper/1"}, "codex", "Codex"),
    ({"user-agent": "deepseek-harness/0.2.2-alpha.3"}, "harness", "DeepSeek Harness"),
])
def test_a_native_client_is_labeled_without_an_integration_plugin(headers, name, label):
    identity = client_context.describe({}, headers)
    assert not identity.get("client")
    assert client_context.log_client(identity) == prompt_policy.prompt_client({}, headers) == name
    assert client_context.label(identity) == label
    token = client_context.bind(identity)
    try:
        event = journal.Journal().emit("http.request", "local check")
        assert event["data"]["client"] == name
    finally:
        client_context.reset(token)
    explicit = client_context.describe({}, {**headers, "x-proxi-client": "OpenCode"})
    assert explicit["client"] == "opencode"


def test_the_tool_search_of_claude_code_is_refused_whatever_the_spelling(relay):
    from test_tool_search_refusal import CLAUDE, request
    status, _ = relay.json_any("/v1/messages", request(), headers={**CLAUDE, "x-proxi-client": "Claude Code"})
    assert status == 400


def test_an_agent_name_the_opencode_plugin_percent_encoded_is_read_back():
    from urllib.parse import quote
    identity = client_context.describe({}, {"x-proxi-client": "opencode", "x-proxi-agent": quote("ревьюер")})
    assert identity["agent"] == "ревьюер"
    assert client_context.describe({}, {"x-proxi-agent": "build"})["agent"] == "build"
