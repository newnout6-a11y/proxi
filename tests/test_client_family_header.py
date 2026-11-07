"""x-proxi-client names a client family however it is spelled, for the client's identity as for the prompts layer."""
import pytest

import client_context
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


def test_the_tool_search_of_claude_code_is_refused_whatever_the_spelling(relay):
    from test_tool_search_refusal import CLAUDE, request
    status, _ = relay.json_any("/v1/messages", request(), headers={**CLAUDE, "x-proxi-client": "Claude Code"})
    assert status == 400
