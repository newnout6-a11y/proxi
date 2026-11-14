"""Pass 2 and generation: what proxy._completion does with the body edits and the generation of a profile."""
import copy
import json

import pytest

import direct
import orchestrator
import prompt_body
import validate
from prompt_support import client_request, fake_relay, head_of, profile, prompt_relay  # noqa: F401

DIALECTS = ["chat", "anthropic", "responses"]
MARK = " DROP_ME"
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK" + MARK}]
TEMPLATE = "{{text:rules}}\n\n{{client_system}}"
RULES = "MY_RULES"


def edited(store, **options):
    store.publish(1, {"mine": profile(TEMPLATE, texts={"rules": RULES}, **options)}, default_profile="mine")
    return store


def drop_marker(data, dialect, profile_, snapshot):
    """A stand-in for prompt_body.rewrite: removes MARK from message texts and reports each change as P3's rewrite does."""
    body = copy.deepcopy(data)
    changes = []
    for index, message in enumerate(body["input" if dialect == "responses" else "messages"]):
        if isinstance(message.get("content"), str) and MARK in message["content"]:
            before = len(message["content"])
            message["content"] = message["content"].replace(MARK, "")
            changes.append({"scope": "history", "op": "drop_tag", "edit": 0, "index": index, "target": "user",
                            "removed_chars": before - len(message["content"])})
    return body, changes


def hide_every_tool(data, dialect, profile_, snapshot):
    """A stand-in for a rewrite whose tools.edits hide the only tool of the request."""
    body = copy.deepcopy(data)
    del body["tools"]
    return body, [{"scope": "tools", "op": "hide", "edit": 0, "index": 0, "tool": "read_file"}]


class Rewrites:
    """Replaces prompt_body.rewrite and keeps its calls; `result` decides what it returns."""

    def __init__(self, monkeypatch, result=drop_marker):
        self.calls, self.result = [], result
        monkeypatch.setattr(prompt_body, "rewrite", self)

    def __call__(self, data, dialect, profile_, snapshot):
        self.calls.append((copy.deepcopy(data), dialect, profile_, snapshot))
        return self.result(data, dialect, profile_, snapshot)


def user_text(body):
    """The first user turn of a body posted to Retool, as text."""
    message = next(m for m in body["messages"] if m["role"] == "user")
    return message["content"] if isinstance(message["content"], str) else "".join(
        block.get("text", "") for block in message["content"])


class Seen:
    """What reaches the runner: the request after pass 2."""

    def __init__(self, relay, monkeypatch):
        self.parsed = []
        runner, original = relay.runtime.runner, relay.runtime.runner.run

        async def run(parsed, out, holder):
            self.parsed.append(parsed)
            return await original(parsed, out, holder)

        monkeypatch.setattr(runner, "run", run)


def count_parses(monkeypatch):
    """How many times each dialect's parse runs."""
    counts = {"chat": 0, "anthropic": 0, "responses": 0}
    for dialect, name in (("chat", "parse_chat"), ("anthropic", "parse_anthropic"), ("responses", "parse_responses")):
        original = getattr(validate, name)

        def counting(data, cfg, _original=original, _dialect=dialect):
            counts[_dialect] += 1
            return _original(data, cfg)

        monkeypatch.setattr(validate, name, counting)
    return counts


# -- pass 2 -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_profile_with_body_edits_sends_the_rewritten_body_and_its_own_rit(prompt_relay, monkeypatch, dialect):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    rewrites = Rewrites(monkeypatch)
    request = client_request(dialect, USER, system="CLIENT_SYSTEM")
    status, _, _ = relay.post(dialect, request, {"x-session-affinity": "dialog-1"})
    assert status == 200
    body = relay.posts[0]
    assert user_text(body).endswith("inspect ORIGINAL_TASK") and MARK not in json.dumps(body)
    assert head_of(body) == RULES + "\n\nCLIENT_SYSTEM"
    # rit, the cache key and every cache mark follow the rewritten messages: pass 2 is a full parse
    assert body["rit"] == direct.make_rit(body["messages"], body["tools"])
    ((seen_data, seen_dialect, seen_profile, seen_snapshot),) = rewrites.calls
    assert seen_data == request and seen_dialect == dialect
    assert seen_snapshot is relay.prompts.snapshot() and seen_profile is seen_snapshot.profile("mine")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_profile_without_body_edits_parses_the_body_once_and_never_rewrites_it(prompt_relay, monkeypatch, dialect):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts)
    rewrites, counts = Rewrites(monkeypatch), None
    counts = count_parses(monkeypatch)
    assert relay.post(dialect, client_request(dialect, USER, system="CLIENT_SYSTEM"))[0] == 200
    assert rewrites.calls == [] and counts[dialect] == 1
    assert MARK in user_text(relay.posts[0])


@pytest.mark.parametrize("dialect", DIALECTS)
def test_pass_2_parses_again_and_the_edited_request_keeps_the_identity_of_the_original(
        prompt_relay, monkeypatch, dialect):
    relay = fake_relay(prompt_relay, thinking_placeholder="THINKING_LABEL")
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch)
    counts, seen = count_parses(monkeypatch), Seen(relay, monkeypatch)
    status, _, _ = relay.post(dialect, client_request(dialect, USER, system="CLIENT_SYSTEM"),
                              {"x-session-affinity": "dialog-1", "user-agent": "opencode/2.2.5"})
    assert status == 200 and counts[dialect] == 2
    (parsed,) = seen.parsed
    assert parsed.conversation_key == "header:dialog-1" and parsed.parent_conversation_key is None
    assert parsed.request_id and parsed.dialect == dialect and parsed.thinking_placeholder == "THINKING_LABEL"
    assert parsed.request_purpose == "client-tools" and parsed.prompt_purpose == "client-tools"
    assert "header:dialog-1" in relay.runtime.pool._conversations
    # the parse of the rewritten body, not of the original one
    assert MARK not in parsed.prompt and parsed.prompt_plan.profile == "mine"
    assert parsed.prompt_input.conversation_key == "header:dialog-1"


def test_what_the_virtual_model_parse_found_in_pass_1_is_carried_into_pass_2(prompt_relay, monkeypatch):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch)
    original, calls = validate.parse_chat, []

    def parse(data, cfg):
        parsed = original(data, cfg)
        calls.append(parsed)
        if len(calls) == 1:  # only the first parse reads the model name the client wrote
            parsed.virtual_profile, parsed.requested_model = "mine", "claude-opus-5-5@mine"
        return parsed

    monkeypatch.setattr(validate, "parse_chat", parse)
    seen = Seen(relay, monkeypatch)
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"))[0] == 200
    (parsed,) = seen.parsed
    assert parsed is calls[1] and parsed.virtual_profile == "mine"
    assert parsed.requested_model == "claude-opus-5-5@mine"
    assert relay.prompts.inputs[0].virtual_profile == "mine"


def test_the_ring_keeps_the_original_body_and_the_changes_and_the_journal_only_a_summary_of_them(
        prompt_relay, monkeypatch):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch)
    request = client_request("chat", USER, system="CLIENT_SYSTEM")
    mark = relay.mark()
    assert relay.post("chat", request, {"x-session-affinity": "dialog-1"})[0] == 200
    (entry,) = relay.prompts.records
    assert entry["request"]["body"] == request and MARK in json.dumps(entry["request"]["body"])
    assert entry["history_changes"] == [{"scope": "history", "op": "drop_tag", "edit": 0, "index": 1,
                                         "target": "user", "removed_chars": len(MARK)}]
    assert entry["tools_changes"] == []
    summary = [{"scope": "history", "op": "drop_tag", "edit": 0, "count": 1, "removed_chars": len(MARK),
                "added_chars": 0, "indexes": [1]}]
    assert entry["changes_summary"] == summary and "changes_total" not in entry
    (event,) = relay.events(mark, "prompt.plan")
    assert event["data"]["history_changes"] == summary and event["data"]["tools_changes"] == []
    assert "DROP_ME" not in json.dumps(event) and "ORIGINAL_TASK" not in json.dumps(event)


def test_the_changes_of_one_rewrite_are_split_by_their_scope(prompt_relay, monkeypatch):
    def result(data, dialect, profile_, snapshot):
        return copy.deepcopy(data), [
            {"scope": "history", "op": "drop_tag", "edit": 0, "index": 1, "target": "user", "removed_chars": 3},
            {"scope": "tools", "op": "append", "edit": 0, "index": 0, "tool": "read_file", "added_chars": 40},
            {"scope": "history", "op": "mid_system", "mode": "wrap", "index": 2, "target": "system",
             "removed_chars": 5, "added_chars": 32},
            {"scope": "tools", "op": "hide", "edit": 1, "index": 1, "tool": "write"}]

    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, result)
    mark = relay.mark()
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"))[0] == 200
    (entry,) = relay.prompts.records
    assert [c["op"] for c in entry["history_changes"]] == ["drop_tag", "mid_system"]
    assert [c["op"] for c in entry["tools_changes"]] == ["append", "hide"]
    (event,) = relay.events(mark, "prompt.plan")
    history, tools = event["data"]["history_changes"], event["data"]["tools_changes"]
    assert [(g["op"], g.get("mode"), g["count"]) for g in history] == [("drop_tag", None, 1), ("mid_system", "wrap", 1)]
    assert [(g["op"], g["tool"], g["count"]) for g in tools] == [("append", "read_file", 1), ("hide", "write", 1)]


def test_a_body_with_thousands_of_changes_gives_the_journal_a_line_per_kind_and_the_ring_a_bounded_list(
        prompt_relay, monkeypatch):
    many = orchestrator.RING_CHANGES * 10 + 7

    def result(data, dialect, profile_, snapshot):
        changes = [{"scope": "history", "op": "drop_tag", "edit": 0, "index": number, "target": "tool_result",
                    "removed_chars": 2} for number in range(many)]
        return copy.deepcopy(data), changes + [
            {"scope": "tools", "op": "append", "edit": 0, "index": 0, "tool": "read_file", "added_chars": 9}]

    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, result)
    mark = relay.mark()
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"))[0] == 200
    (entry,) = relay.prompts.records
    assert len(entry["history_changes"]) == orchestrator.RING_CHANGES and len(entry["tools_changes"]) == 1
    assert entry["history_changes"][0]["index"] == 0 and entry["changes_total"] == {"history": many, "tools": 1}
    assert [(g["op"], g["count"], g["removed_chars"]) for g in entry["changes_summary"]] == [
        ("drop_tag", many, 2 * many), ("append", 1, 0)]
    (event,) = relay.events(mark, "prompt.plan")
    (group,) = event["data"]["history_changes"]
    assert (group["op"], group["count"], group["removed_chars"]) == ("drop_tag", many, 2 * many)
    assert group["indexes"] == list(range(8)) and len(event["data"]["tools_changes"]) == 1
    assert len(json.dumps(event)) < 2000


def test_an_edit_the_rewrite_left_out_is_one_warning_per_kind_and_a_flagged_line_of_the_summary(
        prompt_relay, monkeypatch):
    reason = "would break a tool_use pair"

    def result(data, dialect, profile_, snapshot):
        return copy.deepcopy(data), [
            {"scope": "history", "op": "drop_tag", "edit": 0, "index": 3, "target": "user",
             "skipped": True, "reason": reason},
            {"scope": "history", "op": "drop_tag", "edit": 0, "index": 5, "target": "user",
             "skipped": True, "reason": reason},
            {"scope": "tools", "op": "append", "edit": 1, "index": 2, "tool": "bash",
             "skipped": True, "reason": "no such tool"},
            {"scope": "history", "op": "drop_tag", "edit": 0, "index": 1, "target": "user", "removed_chars": 9}]

    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, result)
    mark = relay.mark()
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"))[0] == 200
    skipped = relay.events(mark, "prompt.edit_skipped")
    assert [(e["level"], e["data"]["op"], e["data"]["reason"], e["data"]["count"]) for e in skipped] == [
        ("warn", "drop_tag", reason, 2), ("warn", "append", "no such tool", 1)]
    # where it happened: the rule, the tool and the first message or tool it concerned
    assert [(e["data"]["scope"], e["data"]["edit"], e["data"].get("tool"), e["data"]["index"]) for e in skipped] == [
        ("history", 0, None, 3), ("tools", 1, "bash", 2)]
    assert all(e["request_id"] for e in skipped)
    (event,) = relay.events(mark, "prompt.plan")
    history, tools = event["data"]["history_changes"], event["data"]["tools_changes"]
    assert [(g["op"], g.get("skipped", False), g["count"]) for g in history] == [
        ("drop_tag", True, 2), ("drop_tag", False, 1)]
    assert [(g["op"], g["skipped"], g["reason"]) for g in tools] == [("append", True, "no such tool")]


@pytest.mark.parametrize("failure", ["raises", "unparsable", "unreadable changes"])
def test_a_failing_rewrite_serves_the_request_on_the_legacy_path(prompt_relay, monkeypatch, failure):
    def result(data, dialect, profile_, snapshot):
        if failure == "raises":
            raise RuntimeError("synthetic failure of the rewrite")
        if failure == "unreadable changes":
            # `any` in the compatibility check stops at the first wrap: only a check of the whole list sees the second entry
            return copy.deepcopy(data), [{"scope": "history", "op": "mid_system", "mode": "wrap"}, "not a change"]
        return {"model": data["model"]}, []  # no messages: the second parse refuses it

    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, result)
    seen = Seen(relay, monkeypatch)
    mark = relay.mark()
    status, _, _ = relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"))
    assert status == 200
    assert head_of(relay.posts[0]) == "CLIENT_SYSTEM" and MARK in user_text(relay.posts[0])
    (event,) = relay.events(mark, "prompt.runtime_error")
    assert event["data"]["stage"] == "rewrite" and event["level"] == "error"
    assert seen.parsed[0].prompt_plan.legacy
    assert not relay.events(mark, "prompt.plan") and not relay.prompts.records


def test_hiding_every_tool_leaves_the_request_in_the_session_the_clients_tools_made(prompt_relay, monkeypatch):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, hide_every_tool)
    status, _, _ = relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"),
                              {"x-session-affinity": "dialog-1"})
    assert status == 200
    assert "tools" not in relay.posts[0]
    # the plan follows the request as the client sent it (plan I9): its own session, not the text session
    conversations = relay.runtime.pool._conversations
    assert "header:dialog-1" in conversations and ("header:dialog-1", "text") not in conversations


# -- generation ---------------------------------------------------------------------------------------------------
def effort_requests(dialect):
    """The same request with the client's own effort (low) in the form of each dialect."""
    extra = {"chat": {"reasoning_effort": "low"}, "anthropic": {"output_config": {"effort": "low"}},
             "responses": {"reasoning": {"effort": "low"}}}[dialect]
    return client_request(dialect, USER, system="CLIENT_SYSTEM", **extra)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_effort_of_the_profile_replaces_the_clients(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, generation={"effort": "high"})
    mark = relay.mark()
    assert relay.post(dialect, effort_requests(dialect))[0] == 200
    assert relay.posts[0]["reasoningEffort"] == "high"
    (begin,) = relay.events(mark, "turn.begin")
    # the client asked for low: the journal says so, and says the effort that ran is the profile's
    assert begin["data"]["effective_effort"] == "high" and begin["data"]["requested_effort"] == "low"
    assert begin["data"]["effort_source"] == "profile"
    (event,) = relay.events(mark, "prompt.plan")
    assert event["data"]["generation"] == {"effort": "high"}


def test_a_profile_without_generation_leaves_the_clients_effort(prompt_relay):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, generation={})
    assert relay.post("chat", effort_requests("chat"))[0] == 200
    assert relay.posts[0]["reasoningEffort"] == "low"


def test_the_profile_sets_an_effort_the_client_did_not_send(prompt_relay):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, generation={"effort": "max"})
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"))[0] == 200
    assert relay.posts[0]["reasoningEffort"] == "max"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("value,model", [("extra", "claude-opus-5-5"), ("max", "gpt-5.5")])
def test_an_effort_the_model_does_not_take_is_a_400_that_names_the_field_of_the_profile(
        prompt_relay, value, model, stream):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, generation={"effort": value})
    status, body, headers = relay.post("chat", client_request("chat", USER, model=model, stream=stream))
    assert status == 400 and "json" in headers["content-type"]
    assert body["error"]["param"] == "profiles.mine.generation.effort"
    assert "profile 'mine'" in body["error"]["message"] and "'%s'" % value in body["error"]["message"]
    assert relay.posts == []


def test_the_compatibility_setting_lets_a_profile_effort_the_model_lacks_be_the_nearest_one(prompt_relay):
    relay = fake_relay(prompt_relay, allow_effort_downgrade=True)
    edited(relay.prompts, generation={"effort": "max"})
    assert relay.post("chat", client_request("chat", USER, model="gpt-5.5"))[0] == 200
    assert relay.posts[0]["reasoningEffort"] == "high"


def test_the_verbosity_of_the_profile_replaces_the_clients(prompt_relay):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, generation={"verbosity": "low"})
    request = client_request("chat", USER, model="gpt-6-sol", verbosity="high")
    assert relay.post("chat", request)[0] == 200
    assert relay.posts[0]["verbosity"] == "low"


def test_a_verbosity_the_model_has_no_level_for_is_left_out_and_said_so(prompt_relay):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, generation={"verbosity": "low"})
    mark = relay.mark()
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"))[0] == 200
    assert "verbosity" not in relay.posts[0]
    (event,) = relay.events(mark, "prompt.plan")
    assert event["data"]["warnings"] == ["generation.verbosity low not applied: claude-opus-5-5 has no such level"]


@pytest.mark.parametrize("stream", [False, True])
def test_a_verbosity_that_is_no_level_at_all_is_a_400(prompt_relay, stream):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, generation={"verbosity": "loud"})
    status, body, _ = relay.post("chat", client_request("chat", USER, stream=stream))
    assert status == 400 and body["error"]["param"] == "profiles.mine.generation.verbosity"
    assert relay.posts == []


def test_a_stream_through_a_composed_head_is_an_ordinary_stream(prompt_relay):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts)
    status, raw, headers = relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM", stream=True))
    assert status == 200 and headers["content-type"].startswith("text/event-stream")
    assert "OK" in raw and raw.rstrip().endswith("data: [DONE]")
    assert head_of(relay.posts[0]) == RULES + "\n\nCLIENT_SYSTEM"


def spy_on_the_pool(relay, monkeypatch):
    """The keywords the HTTP layer puts to the pool before a response starts: (model_refusal calls, unavailable_error calls)."""
    refusals, unavailable = [], []
    pool, refusal, availability = relay.runtime.pool, relay.runtime.pool.model_refusal, relay.runtime.pool.unavailable_error
    monkeypatch.setattr(pool, "model_refusal", lambda entry, **scope: refusals.append(scope) or refusal(entry, **scope))
    monkeypatch.setattr(pool, "unavailable_error",
                        lambda entry, **scope: unavailable.append(scope) or availability(entry, **scope))
    return refusals, unavailable


@pytest.mark.parametrize("stream", [False, True])
def test_the_pool_is_asked_once_with_the_session_of_the_clients_own_request_whatever_pass_2_hides(
        prompt_relay, monkeypatch, stream):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch, hide_every_tool)
    refusals, unavailable = spy_on_the_pool(relay, monkeypatch)
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM", stream=stream),
                      {"x-session-affinity": "dialog-1"})[0] == 200
    # the conversation's own session, with its tools or without them: the plan is the one the client's request makes
    assert refusals == [{}]
    assert unavailable == ([{}] if stream else [])


def test_the_pool_is_not_asked_twice_when_pass_2_leaves_the_session_alone(prompt_relay, monkeypatch):
    relay = fake_relay(prompt_relay)
    edited(relay.prompts, edits=True)
    Rewrites(monkeypatch)
    refusals, _ = spy_on_the_pool(relay, monkeypatch)
    assert relay.post("chat", client_request("chat", USER, system="CLIENT_SYSTEM"),
                      {"x-session-affinity": "dialog-1"})[0] == 200
    assert refusals == [{}]
