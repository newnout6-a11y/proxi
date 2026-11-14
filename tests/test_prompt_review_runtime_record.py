"""What a recorded request keeps for the preview of "the last request" (R2 S3, S4): the headers that name a Codex request,
and no body for a protected service request."""
import json

import client_context
from prompt_bodies import OPENCODE_HEADERS, OPENCODE_TITLE_V2, service_body
from prompt_support import client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture

SYSTEM = "CLIENT_SYSTEM"
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
DOCUMENT = {"version": 1, "default_profile": "mine", "profiles": {"mine": {"head": "RULES\n\n{{client_system}}"}}}


class Headers:
    """What http.server hands over: a case-preserving list of pairs with items()."""

    def __init__(self, *pairs):
        self.pairs = pairs

    def items(self):
        return list(self.pairs)


def start(prompt_relay, tmp_path):
    (tmp_path / "prompts.json").write_text(json.dumps(DOCUMENT), encoding="utf-8")
    return prompt_relay()


# -- S3: a Codex request is still a Codex request when it is replayed ---------------------------------------------------------
def test_the_headers_codex_names_a_request_by_are_kept_and_no_credential_is():
    kept = client_context.recorded_headers(Headers(
        ("Authorization", "Bearer secret"), ("X-Api-Key", "secret"), ("Cookie", "accessToken=secret"),
        ("Originator", "codex_cli_rs"), ("X-Codex-Window-Id", "thread:1"), ("X-Codex-Turn-State", "opaque"),
        ("Session-Id", "s-1"), ("Thread-Id", "t-1"), ("Conversation-Id", "c-1"), ("X-Conversation-Id", "c-2"),
        ("Content-Type", "application/json"), ("Accept", "text/event-stream"), ("Chatgpt-Account-Id", "acct")))
    assert kept == {"originator": "codex_cli_rs", "x-codex-window-id": "thread:1", "x-codex-turn-state": "opaque",
                    "session-id": "s-1", "thread-id": "t-1", "conversation-id": "c-1", "x-conversation-id": "c-2"}


def test_a_padded_header_is_cut_where_the_ring_keeps_it():
    kept = client_context.recorded_headers({"X-Codex-Turn-Metadata": "x" * 100_000, "User-Agent": "u"})
    assert len(kept["x-codex-turn-metadata"]) == client_context._RECORDED_VALUE_LIMIT and kept["user-agent"] == "u"


def test_a_codex_request_the_wrapper_signs_with_its_own_agent_is_replayed_as_codex(prompt_relay, tmp_path):
    relay = start(prompt_relay, tmp_path)
    store = relay.runtime.prompts
    headers = {"user-agent": "wrapper/1.0", "x-codex-window-id": "thread-1:0", "session-id": "codex-s1",
               "thread-id": "codex-t1"}
    request = client_request("responses", USER, system=SYSTEM, tools=False)
    assert relay.post("responses", request, headers)[0] == 200
    kept = store.request()
    assert kept["headers"] == headers and "authorization" not in kept["headers"]
    assert store.preview(kept["body"], kept["dialect"], kept["headers"])["client"] == "codex"
    # without the headers the replay could not tell which client it was
    assert store.preview(kept["body"], kept["dialect"], {"user-agent": "wrapper/1.0"})["client"] == "unknown"


# -- S4: service requests do not take the places of the bodies the preview replays -------------------------------------------
def test_a_protected_request_is_recorded_without_its_body_and_the_preview_still_has_the_main_one(prompt_relay, tmp_path):
    relay = start(prompt_relay, tmp_path)
    store = relay.runtime.prompts
    main = client_request("chat", USER, system=SYSTEM, tools=False)
    assert relay.post("chat", main, {"user-agent": "some-client/1"})[0] == 200
    assert relay.post("chat", service_body(OPENCODE_TITLE_V2), OPENCODE_HEADERS)[0] == 200  # a protected title job
    newest, older = store.recent()
    assert (newest["protected"], newest["has_request"]) == (True, False) and newest["head"] == OPENCODE_TITLE_V2
    assert newest["profile"] == "passthrough" and newest["parts"]  # the head and its parts are recorded
    assert (older["protected"], older["has_request"]) == (False, True)
    assert store.request()["body"] == main  # the newest body is the main request's, not the title job's
    assert store.preview(store.request()["body"], "chat", {"user-agent": "some-client/1"})["head"].startswith("RULES")


def test_every_protected_kind_leaves_no_body_and_an_ordinary_request_keeps_its_own(prompt_relay, tmp_path):
    relay = start(prompt_relay, tmp_path)
    store = relay.runtime.prompts
    plain = [{"role": "user", "content": "hello"}]
    classifier = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": plain,
                  "system": "You are a security monitor for autonomous AI coding agents.\nstable policy"}
    compaction = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": plain, "system": "Summarize."}
    claude = {"user-agent": "claude-cli/2.1.309 (external, cli)"}
    assert relay.post("anthropic", classifier, claude)[0] == 200
    assert relay.post("anthropic", compaction, {**claude, "x-claude-code-request-class": "compaction"})[0] == 200
    assert relay.post("anthropic", {**compaction, "system": "Real system."}, claude)[0] == 200
    assert [(item["protected"], item["has_request"]) for item in reversed(store.recent())] == [
        (True, False), (True, False), (False, True)]
