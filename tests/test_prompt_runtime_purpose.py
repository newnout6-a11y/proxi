"""client-service: the purpose only the prompt layer sees for OpenCode's own title, compaction and summary jobs."""
import pytest

import client_context
import validate
from prompt_bodies import (OPENCODE_COMPACTION_V1, OPENCODE_HEADERS, OPENCODE_SUMMARY, OPENCODE_TITLE_V1,
                           OPENCODE_TITLE_V2, filler, opencode_body, service_body)

CFG = {"strict_models": True, "model": "claude-opus-5-5", "show_thinking": False}
# The first words of every built-in prompt of OpenCode's hidden agents, as the plan's research found them:
# 1.x agent/prompt/{title,compaction,summary}.txt (github.com/anomalyco/opencode@846d5481), the 2.2 title agent
# and the 2.x summary agent in core/src/plugin/agent.ts, and compaction.txt before 2026-04-22.
BUILT_IN_OPENING = [
    "You are a title generator. You output ONLY a thread title. Nothing else.\n\n<task>\nGenerate a brief title",
    "You name conversations so the user can find them later in a list of past sessions.\n\nYou will receive",
    "You are an anchored context summarization assistant for coding sessions.\n\nSummarize only the conversation",
    "You are a helpful AI assistant tasked with summarizing conversations.\n\nWhen asked to summarize",
    "Summarize what was done in this conversation. Write like a pull request description.\n\nRules:\n- 2-3",
]


def job(system, client="opencode", agent="", tools=False, request_class="", content="hello"):
    """(parsed, identity) of a request as proxy._completion has them right after request_kind."""
    body = service_body(system, content)
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "read", "description": "d", "parameters": {
            "type": "object", "properties": {}}}}]
    identity = {key: value for key, value in {"client": client, "agent": agent,
                                              "request_class": request_class}.items() if value}
    parsed = validate.parse_chat(body, CFG)
    parsed.request_purpose = client_context.request_kind(parsed, identity)
    return parsed, identity


@pytest.mark.parametrize("system", BUILT_IN_OPENING)
@pytest.mark.parametrize("client,legacy", [("opencode", "client-text"), ("", "generation")])
def test_a_built_in_opencode_prompt_marks_a_toolless_request_client_service_for_the_prompt_layer_only(
        system, client, legacy):
    parsed, identity = job(system, client=client)
    assert parsed.request_purpose == legacy
    assert client_context.prompt_purpose(parsed, identity) == "client-service"
    # request_kind and request_purpose are what the journal, the console and the retries read: untouched.
    assert client_context.request_kind(parsed, identity) == legacy and parsed.request_purpose == legacy


@pytest.mark.parametrize("system", [OPENCODE_TITLE_V1, OPENCODE_TITLE_V2, OPENCODE_SUMMARY, OPENCODE_COMPACTION_V1])
def test_the_prompts_of_the_synthetic_hidden_agents_are_recognised(system):
    parsed, identity = job(system)
    assert client_context.prompt_purpose(parsed, identity) == "client-service"


@pytest.mark.parametrize("agent", ["title", "compaction", "summary", "Title", " SUMMARY "])
def test_the_agent_the_plugin_names_marks_a_job_whose_prompt_was_replaced_by_the_user(agent):
    # agent.<name>.prompt in the user's OpenCode config replaces the built-in text; the header still names the agent.
    parsed, identity = job("Write the title in the style of my team.", agent=agent)
    assert parsed.request_purpose == "client-text"
    assert client_context.prompt_purpose(parsed, identity) == "client-service"


def test_the_plugin_header_travels_through_describe_into_the_purpose(tmp_path):
    data = service_body("My own title prompt.")
    headers = {**OPENCODE_HEADERS, "x-proxi-client": "opencode", "x-proxi-agent": "title"}
    identity = client_context.describe(data, headers, {"client_metadata_home": str(tmp_path)})
    assert identity["client"] == "opencode" and identity["agent"] == "title"
    parsed = validate.parse_chat(data, CFG)
    parsed.request_purpose = client_context.request_kind(parsed, identity)
    assert client_context.prompt_purpose(parsed, identity) == "client-service"


@pytest.mark.parametrize("agent", ["build", "plan", "explore", "general", "title-ish", ""])
def test_an_ordinary_opencode_agent_is_not_a_service_job(agent):
    parsed, identity = job("You are an AI agent running in OpenCode.", agent=agent)
    assert client_context.prompt_purpose(parsed, identity) == "client-text" == parsed.request_purpose


@pytest.mark.parametrize("client", ["claude-code", "claude-desktop", ""])
def test_the_agent_name_alone_proves_nothing_for_another_client(client):
    # x-claude-code-agent-id and other agent identities share the field with OpenCode's agent names.
    parsed, identity = job("Some other client's own prompt.", client=client, agent="title")
    assert client_context.prompt_purpose(parsed, identity) == parsed.request_purpose


@pytest.mark.parametrize("agent", ["title", "compaction", "summary"])
def test_a_request_with_tools_is_never_a_service_job(agent):
    # OpenCode 2.x compaction resends the chat's own system and tools: it follows the chat's profile and shares its cache.
    parsed, identity = job(OPENCODE_TITLE_V2, agent=agent, tools=True)
    assert parsed.request_purpose == "client-tools"
    assert client_context.prompt_purpose(parsed, identity) == "client-tools"
    main, identity = job("You are an AI agent running in OpenCode.", tools=True)
    assert client_context.prompt_purpose(main, identity) == "client-tools"


def test_opencode_2_compaction_is_not_told_apart_from_the_chat():
    body = opencode_body()
    body["messages"].append({"role": "user", "content": "You MUST summarize the conversation above into a structured "
                                                        "summary that will be given to another agent to resume the work."})
    parsed = validate.parse_chat(body, CFG)
    identity = {"client": "opencode", "agent": "build"}
    parsed.request_purpose = client_context.request_kind(parsed, identity)
    assert client_context.prompt_purpose(parsed, identity) == "client-tools"


def test_the_opening_must_open_the_system_prompt():
    quoted = "You are an AI agent.\n\n" + BUILT_IN_OPENING[0] + "\n\n" + filler("rest", 3)
    parsed, identity = job(quoted)
    assert client_context.prompt_purpose(parsed, identity) == "client-text"
    leading, identity = job("\n  " + BUILT_IN_OPENING[0])
    assert client_context.prompt_purpose(leading, identity) == "client-service"


def test_a_near_miss_of_a_prompt_opening_is_not_matched():
    parsed, identity = job("You are a title generator. You output ONLY a headline.")
    assert client_context.prompt_purpose(parsed, identity) == "client-text"


@pytest.mark.parametrize("client,system,request_class,expected", [
    ("claude-code", "You are naming a coding session so the user can pick it out of a long list of sessions.\nTitle.",
     "", "session-title"),
    ("claude-code", "You are a security monitor for autonomous AI coding agents.\nrules", "", "auto-mode-check"),
    ("claude-code", "Own output contract.", "compaction", "client-text"),
    ("claude-code", "Own output contract.", "auxiliary", "client-text"),
])
def test_other_service_purposes_are_passed_on_unchanged(client, system, request_class, expected):
    parsed, identity = job(system, client=client, request_class=request_class)
    assert parsed.request_purpose == expected
    assert client_context.prompt_purpose(parsed, identity) == expected


def test_a_desktop_connection_probe_stays_a_connection_probe():
    body = {"model": "claude-opus-5-5", "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}
    parsed = validate.parse_anthropic(body, CFG)
    identity = {"client": "claude-desktop"}
    parsed.request_purpose = client_context.request_kind(parsed, identity)
    assert parsed.request_purpose == "connection-probe"
    assert client_context.prompt_purpose(parsed, identity) == "connection-probe"


def test_without_a_stored_request_purpose_it_is_worked_out_the_same_way():
    parsed, identity = job(BUILT_IN_OPENING[1])
    del parsed.request_purpose
    assert client_context.prompt_purpose(parsed, identity) == "client-service"
    plain, identity = job("A client's ordinary system prompt.")
    del plain.request_purpose
    assert client_context.prompt_purpose(plain, identity) == "client-text"


def test_the_bound_identity_is_used_when_none_is_passed():
    parsed, _ = job("My own title prompt.", agent="title")
    token = client_context.bind({"client": "opencode", "agent": "title"})
    try:
        assert client_context.prompt_purpose(parsed) == "client-service"
    finally:
        client_context.reset(token)
    assert client_context.prompt_purpose(parsed, {"client": "opencode"}) == "client-text"


class Headers:
    """What http.server hands over: a case-preserving list of pairs with items()."""

    def __init__(self, *pairs):
        self.pairs = pairs

    def items(self):
        return list(self.pairs)


def test_a_recorded_request_keeps_identity_headers_and_never_a_credential():
    kept = client_context.recorded_headers(Headers(
        ("Authorization", "Bearer secret"), ("X-Api-Key", "secret"), ("Cookie", "accessToken=secret"),
        ("Content-Type", "application/json"), ("Content-Length", "10"), ("Host", "localhost"),
        ("User-Agent", "opencode/2.2.5"), ("Originator", "codex_cli_rs"), ("X-Proxi-Profile", "mine"),
        ("X-Proxi-Agent", "title"), ("X-Claude-Code-Request-Class", "compaction"),
        ("X-Claude-Code-Session-Id", "abc"), ("X-Opencode-Session-Id", "ses_1"), ("X-OpenCode-Client", "cli"),
        ("X-Session-Affinity", "ses_1"), ("X-Session-Id", "ses_1"), ("Anthropic-Version", "2023-06-01"),
        ("X-Request-Id", "r1")))
    assert kept == {"user-agent": "opencode/2.2.5", "originator": "codex_cli_rs", "x-proxi-profile": "mine",
                    "x-proxi-agent": "title", "x-claude-code-request-class": "compaction",
                    "x-claude-code-session-id": "abc", "x-opencode-session-id": "ses_1", "x-opencode-client": "cli",
                    "x-session-affinity": "ses_1", "x-session-id": "ses_1"}


def test_recorded_headers_accept_a_plain_dict_and_nothing():
    assert client_context.recorded_headers({"User-Agent": "x", "Authorization": "y"}) == {"user-agent": "x"}
    assert client_context.recorded_headers(None) == {} and client_context.recorded_headers({}) == {}
