"""prompt_client and PlanInput.from_request: which client family a request is, and what rule matching may see.

Sources checked for the Codex and DeepSeek Harness signals (2026-11-05):
- openai/codex rust-v0.164.0 codex-rs/login/src/auth/default_client.rs: the originator (default `codex_cli_rs`, first-party
  values `codex-tui`, `codex_vscode`, `Codex ...`), the User-Agent `<originator>/<version> (<os>; <arch>) <terminal>`;
  codex-rs/otel/src/metrics/tags.rs: the known originators; codex-rs/core/src/responses_metadata.rs: x-codex-window-id on
  every Responses request.
- deepseek-ai/deepseek-harness packages/llm/llm/src/attribution.ts: every provider request sends
  `User-Agent: deepseek-harness/<version> (+https://github.com/deepseek-ai/deepseek-harness)`.
"""
import email.message
import json

import pytest

import client_context
import prompt_policy as pp
import validate

CODEX_UA = "codex_cli_rs/0.164.0 (Windows 10.0.26100; x86_64) WindowsTerminal"
HARNESS_UA = "deepseek-harness/0.2.2-alpha.3 (+https://github.com/deepseek-ai/deepseek-harness)"
CFG = {"strict_models": True, "model": "claude-opus-5-5"}


# -- explicit and recognised clients ----------------------------------------------------------------------------------


@pytest.mark.parametrize("value, family", [
    ("opencode", "opencode"), ("OpenCode", "opencode"), ("claude-code", "claude-code"), ("Claude Code", "claude-code"),
    ("claude_code", "claude-code"), ("  claude-desktop ", "claude-desktop"), ("Claude Desktop", "claude-desktop"),
    ("codex", "codex"), ("harness", "harness"), ("unknown", "unknown"),
    ("claude-cli", "claude-code"), ("codex_cli_rs", "codex"), ("codex-tui", "codex"), ("codex_vscode", "codex"),
    ("Codex Desktop", "codex"), ("deepseek-harness", "harness"), ("DSH", "harness"),
])
def test_an_explicit_x_proxi_client_names_the_family(value, family):
    assert pp.prompt_client({}, {"x-proxi-client": value}) == family
    assert pp.prompt_client({}, {"X-Proxi-Client": value}) == family


def test_an_explicit_client_beats_everything_else():
    headers = {"x-proxi-client": "opencode", "originator": "codex_cli_rs", "user-agent": CODEX_UA}
    assert pp.prompt_client({"client": "claude-code"}, headers) == "opencode"


@pytest.mark.parametrize("value", ["", "  ", None, "my-tool", "aider", "gpt", "claude", "open code 2"])
def test_a_value_that_names_no_family_is_ignored(value):
    assert pp.prompt_client({}, {"x-proxi-client": value}) == "unknown"
    assert pp.prompt_client({}, {"x-proxi-client": value, "user-agent": CODEX_UA}) == "codex"


@pytest.mark.parametrize("client", ["opencode", "claude-code", "claude-desktop", "Claude Code"])
def test_the_client_recognised_by_client_context_is_the_family(client):
    family = client.lower().replace(" ", "-")
    assert pp.prompt_client({"client": client}, {}) == family
    assert pp.prompt_client({"client": client}, {"user-agent": "something"}) == family


@pytest.mark.parametrize("identity", [None, {}, {"client": ""}, {"client": "aider"}, {"agent": "build"}])
def test_an_identity_without_a_family_is_unknown(identity):
    assert pp.prompt_client(identity, None) == "unknown"
    assert pp.prompt_client(identity, {}) == "unknown"


def test_the_explicit_header_beats_the_recognised_client():
    assert pp.prompt_client({"client": "opencode"}, {"x-proxi-client": "claude-code"}) == "claude-code"


def test_real_describe_output_gives_the_families():
    cases = [({"x-proxi-client": "opencode"}, "opencode"), ({"user-agent": "claude-cli/2.1.310 (external, cli)"}, "claude-code"),
             ({"user-agent": "Claude/1.8089.0 Chrome/140"}, "claude-desktop"), ({"user-agent": "opencode/1.2.3"}, "opencode")]
    for headers, family in cases:
        identity = client_context.describe({"model": "m"}, headers, {"client_metadata_home": "Z:\\nowhere"})
        assert pp.prompt_client(identity, headers) == family, headers


# -- Codex ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("originator", [
    "codex_cli_rs", "codex-tui", "codex_exec", "codex_vscode", "codex_desktop", "Codex Desktop", "codex-app-server",
    "codex_mcp_server", "codex-cli", "codex_sdk_ts", "codex-app-server-sdk", "codex_atlas", "codex_chatgpt_desktop"])
def test_every_first_party_codex_originator_is_codex(originator):
    assert pp.prompt_client({}, {"originator": originator}) == "codex"
    assert pp.prompt_client({}, {"Originator": originator.upper()}) == "codex"


@pytest.mark.parametrize("agent", [
    CODEX_UA, "codex-tui/0.164.0 (Mac OS 15.1; arm64) iTerm.app/3.5", "Codex Desktop/0.164.0 (Mac OS 15.1; arm64)",
    "codex_vscode/0.164.0 (Ubuntu 24.04; x86_64) vscode/1.99 (codex-app-server)", "codex_exec/0.164.0 (Debian 12; x86_64) tmux"])
def test_a_codex_user_agent_is_codex(agent):
    assert pp.prompt_client({}, {"user-agent": agent}) == "codex"
    assert pp.prompt_client({}, {"User-Agent": agent.lower()}) == "codex"


@pytest.mark.parametrize("header", ["x-codex-window-id", "X-Codex-Turn-Metadata", "x-codex-installation-id",
                                    "x-codex-turn-state", "x-codex-beta-features", "x-codex-parent-thread-id"])
def test_an_x_codex_header_gives_codex_even_when_a_wrapper_changes_the_originator(header):
    headers = {"originator": "my_wrapper", "user-agent": "my_wrapper/1.0 (Linux; x86_64)", header: "abc"}
    assert pp.prompt_client({}, headers) == "codex"


@pytest.mark.parametrize("headers", [
    {"user-agent": "Mozilla/5.0 codex"}, {"x-codex": "1"}, {"x-codexx-id": "1"}, {"originator": "not-codex"},
    {"originator": "my_codex"}, {"user-agent": "xcodex/1"}, {"x-claude-code-session-id": "s"}])
def test_things_that_only_look_like_codex_are_not(headers):
    assert pp.prompt_client({}, headers) == "unknown"


def test_a_codex_request_keeps_its_old_identity_and_request_kind():
    """Rules see the new family; identity["client"] and request_kind are what they were (plan I1)."""
    headers = {"originator": "codex_cli_rs", "user-agent": CODEX_UA}
    identity = client_context.describe({"model": "gpt-5.5"}, headers, {"client_metadata_home": "Z:\\nowhere"})
    assert identity.get("client", "") == ""
    parsed = validate.parse_responses({"model": "gpt-5.5", "input": "hi", "instructions": "S"}, CFG)
    assert client_context.request_kind(parsed, identity) == "generation"
    assert pp.prompt_client(identity, headers) == "codex"


# -- DeepSeek Harness -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", [HARNESS_UA, "deepseek-harness/1.0.0", "DeepSeek-Harness/0.1.5-rc.2 (+https://x)"])
def test_the_user_agent_of_the_harness_is_harness(agent):
    assert pp.prompt_client({}, {"user-agent": agent}) == "harness"


@pytest.mark.parametrize("agent", [
    "OpenAI/JS 6.1.0", "ai-sdk/openai-compatible/1.0.5", "pi-ai/1.0", "node", "deepseek-harness", "my-deepseek-harness/1",
    "deepseek/1.0", " (+https://github.com/deepseek-ai/deepseek-harness)"])
def test_other_agents_are_not_the_harness(agent):
    assert pp.prompt_client({}, {"user-agent": agent}) == "unknown"


def test_the_harness_can_always_say_so_in_a_header():
    assert pp.prompt_client({}, {"x-proxi-client": "harness", "user-agent": "OpenAI/JS 6.1.0"}) == "harness"


def test_a_harness_that_masquerades_is_what_it_claims():
    # dsh-client-masquerade makes a provider look like Codex or Claude Code: the headers it sends win over its own agent
    headers = {"originator": "codex_cli_rs", "user-agent": CODEX_UA}
    assert pp.prompt_client({}, headers) == "codex"


# -- PlanInput.from_request -------------------------------------------------------------------------------------------


def parsed_chat(system="SYS", tools=True, **extra):
    body = {"model": "claude-opus-5-5", "messages": [{"role": "system", "content": system},
                                                     {"role": "user", "content": "hi"}], **extra}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    parsed = validate.parse_chat(body, CFG)
    parsed.dialect = "chat"
    parsed.request_purpose = "client-tools" if tools else "client-text"
    parsed.conversation_key = "header:abc"
    return parsed


def test_the_input_of_a_request():
    identity = {"client": "opencode", "agent": "build", "request_class": "compaction"}
    request = pp.PlanInput.from_request(parsed_chat(), identity, {"X-Proxi-Profile": " mine "}, "chat")
    assert request == pp.PlanInput(
        dialect="chat", client="opencode", agent="build", purpose="client-tools", request_class="compaction",
        model="claude-opus-5-5", requested_model="claude-opus-5-5", virtual_profile=None, header_profile="mine",
        tools=True, structured=False, has_client_system=True, conversation_key="header:abc")


def test_the_prompt_purpose_goes_before_the_request_purpose():
    parsed = parsed_chat()
    assert pp.PlanInput.from_request(parsed, {}, {}).purpose == "client-tools"
    parsed.prompt_purpose = "client-service"
    assert pp.PlanInput.from_request(parsed, {}, {}).purpose == "client-service"
    parsed.prompt_purpose = ""
    assert pp.PlanInput.from_request(parsed, {}, {}).purpose == "client-tools"
    del parsed.request_purpose
    parsed.prompt_purpose = None
    assert pp.PlanInput.from_request(parsed, {}, {}).purpose == ""


def test_the_dialect_argument_goes_before_the_parsed_one():
    parsed = parsed_chat()
    assert pp.PlanInput.from_request(parsed, {}, {}).dialect == "chat"
    assert pp.PlanInput.from_request(parsed, {}, {}, "responses").dialect == "responses"
    del parsed.dialect
    assert pp.PlanInput.from_request(parsed, {}, {}).dialect == ""


def test_virtual_and_requested_model_fall_back():
    parsed = parsed_chat()
    request = pp.PlanInput.from_request(parsed, {}, {})
    assert request.virtual_profile is None and request.requested_model == "claude-opus-5-5"
    parsed.virtual_profile, parsed.requested_model = "mine", "claude-opus"
    request = pp.PlanInput.from_request(parsed, {}, {})
    assert (request.virtual_profile, request.requested_model, request.model) == ("mine", "claude-opus", "claude-opus-5-5")
    parsed.virtual_profile = ""
    assert pp.PlanInput.from_request(parsed, {}, {}).virtual_profile is None


@pytest.mark.parametrize("headers, expected", [({}, None), ({"x-proxi-profile": ""}, None), ({"x-proxi-profile": "  "}, None),
                                               ({"X-PROXI-PROFILE": "mine"}, "mine"), (None, None)])
def test_the_header_profile(headers, expected):
    assert pp.PlanInput.from_request(parsed_chat(), {}, headers).header_profile == expected


def test_flags_are_booleans_of_the_parsed_request():
    request = pp.PlanInput.from_request(parsed_chat(system="  \n ", tools=False), {}, {})
    assert (request.tools, request.structured, request.has_client_system) == (False, False, False)
    parsed = parsed_chat(system="S", tools=False, response_format={"type": "json_object"})
    request = pp.PlanInput.from_request(parsed, {}, {})
    assert (request.tools, request.structured, request.has_client_system) == (False, True, True)


def test_a_missing_conversation_key_is_none():
    parsed = parsed_chat()
    parsed.conversation_key = ""
    assert pp.PlanInput.from_request(parsed, {}, {}).conversation_key is None
    parsed.conversation_key = None
    assert pp.PlanInput.from_request(parsed, None, None).conversation_key is None


def test_http_header_objects_work_as_headers():
    message = email.message.Message()
    message["X-Proxi-Client"] = "opencode"
    message["x-proxi-profile"] = "mine"
    request = pp.PlanInput.from_request(parsed_chat(), {}, message)
    assert (request.client, request.header_profile) == ("opencode", "mine")


def test_the_input_is_immutable():
    request = pp.PlanInput.from_request(parsed_chat(), {}, {})
    with pytest.raises(Exception):
        request.client = "x"
    assert json.dumps(request.__dict__)


def test_a_codex_responses_request_without_instructions_is_still_a_request_with_a_client_system():
    developer = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}
                 for text in ("You are Codex.", "<permissions instructions>\nread-only\n</permissions instructions>",
                              "<skills_instructions>\n## Skills\n</skills_instructions>")]
    body = {"model": "gpt-5.5", "stream": True, "tools": [{"type": "function", "name": "shell",
                                                           "parameters": {"type": "object"}}],
            "input": developer + [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "fix the bug"}]}]}
    headers = {"originator": "codex_cli_rs", "user-agent": CODEX_UA, "x-codex-window-id": "w1",
               "session-id": "9f1c"}
    parsed = validate.parse_responses(body, CFG)
    assert parsed.system == "\n\n".join(item["content"][0]["text"] for item in developer)
    identity = client_context.describe(body, headers, {"client_metadata_home": "Z:\\nowhere"})
    parsed.dialect = "responses"
    parsed.request_purpose = client_context.request_kind(parsed, identity)
    request = pp.PlanInput.from_request(parsed, identity, headers)
    assert (request.client, request.purpose, request.dialect) == ("codex", "client-tools", "responses")
    assert request.has_client_system and request.tools and not request.structured
    assert identity.get("client", "") == ""                      # client_context still does not name Codex
    body["input"] = body["input"][3:]
    bare = validate.parse_responses(body, CFG)
    assert pp.PlanInput.from_request(bare, identity, headers, "responses").has_client_system is False
