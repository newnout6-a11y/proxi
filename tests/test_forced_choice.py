"""A forced tool choice on Claude is asked for in words: Retool refuses the choice itself (400) on every Claude model.

The instruction is the last turn of the upstream request only: not in the history the next request continues, not marked for the
cache. A reply without the call is asked for again once, more firmly, and ends as an error after that; calls of other tools
under a named choice never reach the client. GPT and GLM take the choice natively and are left alone."""
import asyncio
import copy
import json

import pytest

import direct
import forced_choice
import validate
from forced_choice import Requirement
from test_native_context_reuse import FIRST, PARSE_CFG, run, setup
from test_native_history import append_result, close, strip_cache, tool_reply
from test_review_fixes_pr19 import END, THINKING, response, signature, text
from upstream_attempts import AttemptBudget

READ = {"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "number"}}}}}
WRITE = {"type": "function", "function": {"name": "write_file", "description": "Write a file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}}}}
NAMED = {"type": "function", "function": {"name": "read_file"}}
ANY = Requirement(None, frozenset({"read_file"}))
READ_ONLY = Requirement("read_file", frozenset({"read_file"}))


@pytest.fixture(autouse=True)
def fresh_run_state(monkeypatch):
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    monkeypatch.setattr(direct, "_DUMP_RECENT", {})
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)


def call(name, call_id, **arguments):
    return [{"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": name},
            {"event": "content_block_delta", "type": "tool_call", "partialInputs": json.dumps(arguments)},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": call_id}]


def harness(tmp_path, monkeypatch, scripts, **overrides):
    backend, pool, runner = setup(tmp_path, transport="direct", cache_keepwarm_seconds=0,
                                  base="https://fixture.invalid", cookie_values=[], project_context=False,
                                  reasoning_effort="high", openai_provider_resource_name="__managed__",
                                  retool_fast_provider_resource_name="__managed__", **overrides)
    backend.headers = lambda: {}
    sent = []

    def opener(req, **kwargs):
        sent.append(json.loads(req.data))
        return response(*scripts[len(sent) - 1])

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return pool, runner, sent


def forced(messages, choice, *, model="claude-opus-5-5", tools=(READ,), key="header:client-a"):
    data = {"model": model, "tools": copy.deepcopy(list(tools)), "tool_choice": choice,
            "messages": [{"role": "system", "content": "CLIENT_SYSTEM"}] + copy.deepcopy(messages)}
    parsed = validate.parse_chat(data, PARSE_CFG)
    parsed.conversation_key = key
    return parsed


def drive(tmp_path, monkeypatch, scripts, *requests, model="claude-opus-5-5", **overrides):
    """Run the requests one after another on one conversation; the events of each, and the bodies upstream got."""
    pool, runner, sent = harness(tmp_path, monkeypatch, scripts, **overrides)
    results = []

    async def scenario():
        try:
            for make in requests:
                results.append(await run(runner, make(results)))
        finally:
            await close(pool)

    asyncio.run(scenario())
    return results, sent, pool


def tool_calls(events):
    return [(e["name"], e["done"]) for e in events if e["type"] == "tool_call"]


def called(events):
    """The tools the client is asked to run: the finished calls, in order."""
    return [name for name, done in tool_calls(events) if done]


def last_text(body):
    return body["messages"][-1]["content"][0]["text"]


# --- the request ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("choice, wording", [
    ("required", forced_choice.instruction(ANY)), (NAMED, forced_choice.instruction(READ_ONLY))])
def test_a_forced_choice_goes_upstream_as_auto_with_the_call_asked_for_in_the_last_turn(tmp_path, monkeypatch, choice, wording):
    results, sent, _ = drive(tmp_path, monkeypatch, [tool_reply()], lambda _: forced(FIRST, choice))
    assert len(sent) == 1 and "toolChoice" not in sent[0]
    last = sent[0]["messages"][-1]
    assert last == {"role": "user", "content": [{"type": "text", "text": wording}]}
    assert "cache" not in json.dumps(last)
    assert sent[0]["rit"] == direct.make_rit(sent[0]["messages"], sent[0]["tools"])
    assert called(results[0]) == ["read_file"]
    assert results[0][-1] == {"type": "end", "reason": "tool_calls"}


def test_the_wording_is_the_measured_one():
    assert forced_choice.instruction(READ_ONLY) == (
        "Call the tool read_file now, and no other tool; do not answer in text. "
        "If a required argument is unknown, use a plausible value instead of asking.")
    assert forced_choice.instruction(ANY) == (
        "Call one of the available tools now; do not answer in text. "
        "If a required argument is unknown, use a plausible value instead of asking.")
    for required in (ANY, READ_ONLY):
        assert forced_choice.instruction(required, firm=True) != forced_choice.instruction(required)
        assert forced_choice.instruction(required, firm=True).startswith("IMPORTANT: your next message must be a call to ")


@pytest.mark.parametrize("model, choice, native", [
    ("gpt-6.1-sol", "required", "required"), ("gpt-6.1-sol", NAMED, "read_file"),
    ("zai-org/GLM-5.3", "required", "required"), ("zai-org/GLM-5.3", NAMED, "read_file")])
def test_gpt_and_glm_keep_the_native_choice_and_get_no_instruction(tmp_path, monkeypatch, model, choice, native):
    results, sent, _ = drive(tmp_path, monkeypatch, [tool_reply()], lambda _: forced(FIRST, choice, model=model),
                             model=model)
    assert sent[0]["toolChoice"] == native
    assert sent[0]["messages"][-1]["role"] == "user" and "plausible value" not in json.dumps(sent[0])
    assert called(results[0]) == ["read_file"]


@pytest.mark.parametrize("name", ["none", "auto", "required"])
def test_a_tool_named_like_one_of_retools_choices_is_forced_by_name_in_words(tmp_path, monkeypatch, name):
    # No choice goes upstream, so these three words are only names here: a tool called "none" is asked for like any other
    odd = {"type": "function", "function": {"name": name, "description": "d", "parameters": {
        "type": "object", "properties": {"x": {"type": "string"}}}}}
    chosen = {"type": "function", "function": {"name": name}}
    results, sent, _ = drive(tmp_path, monkeypatch, [[*call(name, "c1", x="1"), *END]],
                             lambda _: forced(FIRST, chosen, tools=(odd, READ)))
    assert len(sent) == 1 and "toolChoice" not in sent[0]
    assert last_text(sent[0]) == forced_choice.instruction(Requirement(name, frozenset({name, "read_file"})))
    assert called(results[0]) == [name]


def test_auto_and_none_are_not_a_forced_choice():
    tools = [{"name": "read_file"}]
    for choice in ("auto", "none", None):
        assert forced_choice.requirement("claude-opus-5-5", tools, choice) is None
    assert forced_choice.requirement("claude-opus-5-5", [], "required") is None
    assert forced_choice.requirement("gpt-6.1-sol", tools, "required") is None
    assert forced_choice.requirement("claude-opus-5-5", tools, "required") == Requirement(None, frozenset({"read_file"}))
    # the parsed shape the HTTP layer holds names the same tools
    assert forced_choice.requirement("claude-opus-5-5", [READ, WRITE], NAMED).names == {"read_file", "write_file"}


# --- the history ---------------------------------------------------------------------------------------------------------

def test_the_instruction_is_never_stored_so_the_next_request_continues_the_signed_history(tmp_path, monkeypatch):
    def second(results):
        first = forced(FIRST, "required")
        messages = append_result(copy.deepcopy(FIRST), first, results[0], "chat")
        return forced(messages, "auto")  # the client forces nothing now: the branch must continue all the same

    results, sent, pool = drive(tmp_path, monkeypatch, [tool_reply(), [text("done"), *END]],
                                lambda _: forced(FIRST, "required"), second)
    assert len(sent) == 2
    instruction = forced_choice.instruction(ANY)
    assert instruction in json.dumps(sent[0]) and instruction not in json.dumps(sent[1])
    # what the first request sent, bar its last turn, is the prefix of the second: the cache reads it
    assert strip_cache(sent[1]["messages"][:len(sent[0]["messages"]) - 1]) == strip_cache(sent[0]["messages"][:-1])
    assert [b["signature"] for m in sent[1]["messages"] for b in m.get("content", [])
            if isinstance(b, dict) and b.get("type") == "thinking"] == ["opaque-one"]
    assert sent[1]["messages"][-1]["role"] == "tool"
    assert not any(e["type"] == "error" for e in results[1])


def test_a_later_forced_request_gets_its_own_instruction_after_the_new_tail(tmp_path, monkeypatch):
    def second(results):
        first = forced(FIRST, NAMED)
        return forced(append_result(copy.deepcopy(FIRST), first, results[0], "chat"), NAMED)

    _, sent, _ = drive(tmp_path, monkeypatch, [tool_reply(), tool_reply("c2", "opaque-two")],
                       lambda _: forced(FIRST, NAMED), second)
    assert [last_text(body) for body in sent] == [forced_choice.instruction(READ_ONLY)] * 2
    assert sent[1]["messages"][-2]["role"] == "tool"
    assert json.dumps(sent[1]).count(forced_choice.instruction(READ_ONLY)) == 1


# --- extra calls and misses ------------------------------------------------------------------------------------------------

def test_calls_of_other_tools_under_a_named_choice_never_reach_the_client(tmp_path, monkeypatch):
    script = [*THINKING, signature("sig"), {"event": "content_block_end", "type": "thinking"},
              *call("write_file", "w1", path="out"), *call("read_file", "r1", path="a"), *END]
    results, sent, _ = drive(tmp_path, monkeypatch, [script], lambda _: forced(FIRST, NAMED, tools=(READ, WRITE)))
    assert len(sent) == 1
    assert called(results[0]) == ["read_file"]
    assert results[0][-1] == {"type": "end", "reason": "tool_calls"}


def test_a_text_reply_is_asked_for_again_once_more_firmly_and_the_client_sees_only_the_second(tmp_path, monkeypatch):
    first = [*THINKING, signature("sig-one"), {"event": "content_block_end", "type": "thinking"},
             text("I can read it, but which file?"), *END]
    results, sent, pool = drive(tmp_path, monkeypatch, [first, tool_reply(sig="opaque-two")],
                                lambda _: forced(FIRST, NAMED))
    events = results[0]
    assert len(sent) == 2
    assert last_text(sent[0]) == forced_choice.instruction(READ_ONLY)
    assert last_text(sent[1]) == forced_choice.instruction(READ_ONLY, firm=True)
    assert sent[1]["messages"][:-1] == sent[0]["messages"][:-1]
    # nothing of the missed attempt reached the client: no text, no thinking of its signature
    assert not any(e["type"] == "text" for e in events)
    assert "sig-one" not in json.dumps(events)
    assert called(events) == ["read_file"] and events[-1] == {"type": "end", "reason": "tool_calls"}
    # both attempts are paid for
    assert sum(e["type"] == "credits" for e in events) == 2


def test_only_the_accepted_attempt_is_kept_for_the_history(tmp_path, monkeypatch):
    first = [*THINKING, signature("sig-one"), {"event": "content_block_end", "type": "thinking"}, text("which file?"), *END]

    def second(results):
        request = forced(FIRST, NAMED)
        return forced(append_result(copy.deepcopy(FIRST), request, results[0], "chat"), "auto")

    _, sent, pool = drive(tmp_path, monkeypatch, [first, tool_reply(sig="opaque-two"), [text("done"), *END]],
                          lambda _: forced(FIRST, NAMED), second)
    assert len(sent) == 3
    assert "sig-one" not in json.dumps(sent[2]) and "opaque-two" in json.dumps(sent[2])


def test_the_call_of_another_tool_is_not_the_required_one_so_it_is_asked_for_again(tmp_path, monkeypatch):
    wrong = [*call("write_file", "w1", path="out"), *END]
    results, sent, _ = drive(tmp_path, monkeypatch, [wrong, tool_reply()],
                             lambda _: forced(FIRST, NAMED, tools=(READ, WRITE)))
    assert len(sent) == 2
    assert called(results[0]) == ["read_file"]


def test_a_second_miss_ends_as_the_error_a_route_that_ignored_a_native_choice_earns(tmp_path, monkeypatch):
    miss = [text("no thanks"), *END]
    results, sent, pool = drive(tmp_path, monkeypatch, [miss, miss], lambda _: forced(FIRST, "required"))
    events = results[0]
    assert len(sent) == 2
    errors = [e for e in events if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["error"].status == 502 and errors[0]["error"].code == "upstream_tool_choice_ignored"
    assert not any(e["type"] in ("text", "tool_call") for e in events)
    assert pool._conversations["header:client-a"]._native_history.branches == []


def test_an_empty_second_miss_says_so(tmp_path, monkeypatch):
    results, sent, _ = drive(tmp_path, monkeypatch, [END, END], lambda _: forced(FIRST, "required"),
                             upstream_empty_retries=0)  # the transport's own same-body retry of an empty turn is off
    assert len(sent) == 2
    error = next(e["error"] for e in results[0] if e["type"] == "error")
    assert error.code == "upstream_empty_turn" and error.status == 502


def test_a_spent_attempt_budget_ends_as_the_tool_choice_error_without_another_post(tmp_path, monkeypatch):
    # five identical empty POSTs use the whole budget of the step: there is nothing left to ask again with
    results, sent, _ = drive(tmp_path, monkeypatch, [END] * 5, lambda _: forced(FIRST, "required"),
                             upstream_empty_retries=4)
    assert len(sent) == 5 and all(body == sent[0] for body in sent)
    error = next(e["error"] for e in results[0] if e["type"] == "error")
    assert error.code == "upstream_empty_turn" and "required client tool choice" in error.message


# --- the gate ------------------------------------------------------------------------------------------------------------

def test_the_gate_holds_content_until_the_required_call_starts_and_releases_it_in_order():
    gate = forced_choice.Gate(READ_ONLY)
    usage = {"type": "usage_start"}
    assert gate.feed(usage) == [usage]
    assert gate.feed({"type": "thinking", "delta": "plan"}) == []
    assert gate.feed({"type": "text", "delta": "  "}) == [] and not gate.text
    assert gate.feed({"type": "tool_call", "id": "w", "name": "write_file", "arguments": ""}) == []
    assert not gate.complied
    start = {"type": "tool_call", "id": "r", "name": "read_file", "arguments": "", "partial": True}
    released = gate.feed(start)
    assert [e["type"] for e in released] == ["thinking", "text", "tool_call", "tool_call"] and released[-1] is start
    assert gate.complied and gate.called == ["write_file", "read_file"]
    later = {"type": "text", "delta": "after"}
    assert gate.feed(later) == [later]
    credits = {"type": "credits", "value": 0.1}
    assert gate.feed(credits) == [credits]


def test_the_gate_lets_billing_and_status_through_while_it_holds():
    gate = forced_choice.Gate(ANY)
    for kind in ("usage_start", "usage", "credits", "thinking_status", "thinking_heartbeat", "error"):
        assert gate.feed({"type": kind}) == [{"type": kind}]
    assert gate.held == []


# --- the HTTP layer ------------------------------------------------------------------------------------------------------

def message_body(model, **extra):
    return {"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "read_file", "description": "r", "input_schema": {"type": "object", "properties": {}}}],
            **extra}


@pytest.mark.parametrize("model, choice, header", [
    ("claude-opus-5-5", {"type": "any"}, "emulated"), ("claude-sonnet-5-5", {"type": "tool", "name": "read_file"}, "emulated"),
    ("claude-opus-5-5", {"type": "auto"}, None), ("gpt-6-sol", {"type": "any"}, None)])
def test_the_response_says_when_the_choice_is_emulated(relay, model, choice, header):
    if model.startswith("gpt"):
        relay.cfg["openai_provider_resource_name"] = "__managed__"  # a pool with one resource judges every route by it
    with relay.request("/v1/messages", message_body(model, tool_choice=choice)) as answer:
        assert answer.headers.get("x-proxi-tool-choice") == header
        assert "x-proxi-tool-choice" in answer.headers["access-control-expose-headers"]
        answer.read()
