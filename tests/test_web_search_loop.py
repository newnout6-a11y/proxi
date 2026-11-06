"""The WebSearch loop: the model calls the search tool, the relay runs it, and the reply goes on.

A fake upstream (the NDJSON of Retool) and a fake search object drive the real orchestrator. What is checked is what the
client sees (Anthropic's server-tool blocks), what upstream is sent (a native continuation: the signed thinking, the call and
its result, the cached prefix as it was), and that the history stays whole: the conversation the search request carries the id of
is left alone, and a later request that echoes the reply continues it."""
import asyncio
import copy
import json

import pytest

import adapters
import direct
import journal
import validate
import web_search
from test_adapter_protocols import Writer
from test_forced_choice import call
from test_native_context_reuse import FIRST, PARSE_CFG, TOOLS, request, run, setup
from test_native_history import close, strip_cache, tool_reply
from test_review_fixes_pr19 import END, THINKING, response, signature, text
from test_web_search import OTHER, PAGE, FakeSearch
from web_search import SearchError


def result_text(value):
    """The text of a tool result as upstream got it: a string, or blocks of text (the newest result carries the cache hint
    on a block of its own, see native_history.cache_messages)."""
    if isinstance(value, str):
        return value
    return "".join(block.get("text", "") for block in value if isinstance(block, dict))

QUESTION = [{"role": "user", "content": "Perform a web search for the query: printing press"}]
SERVER = {"type": "web_search_20250305", "name": "web_search"}
SEARCH_SYSTEM = "You are an assistant for performing a web search tool use"
PRESS = web_search.render_block_content(web_search.result_blocks([PAGE]))


def asks(query, sig="sig-one", words="I will search.", call_id="s1"):
    """An upstream reply: thinking, a few words, a call of the search tool."""
    return [*THINKING, signature(sig), {"event": "content_block_end", "type": "thinking"},
            *([text(words)] if words else []), *call("web_search", call_id, query=query), *END]


def answers(words="The printing press was invented in the 1440s.", sig="sig-two"):
    return [*THINKING, signature(sig), {"event": "content_block_end", "type": "thinking"}, text(words), *END]


def search_request(search, messages=QUESTION, *, key="header:search", system=SEARCH_SYSTEM, extra_tools=(), tool=None,
                   **changes):
    body = {"model": "claude-opus-5-5", "max_tokens": 8192, "system": system, "messages": copy.deepcopy(messages),
            "tools": [dict(SERVER, **(tool or {})), *copy.deepcopy(list(extra_tools))], **changes}
    parsed = validate.parse_anthropic(body, dict(PARSE_CFG, _web_search=search))
    parsed.conversation_key = key
    return parsed


def drive(tmp_path, monkeypatch, scripts, search, make, **cfg):
    """Run `make(results)` requests one after another on one pool; their events, the bodies upstream got, the pool."""
    backend, pool, runner = setup(tmp_path, transport="direct", cache_keepwarm_seconds=0, base="https://fixture.invalid",
                                  cookie_values=[], project_context=False, reasoning_effort="high", _web_search=search,
                                  web_search_provider="exa", **cfg)
    backend.headers = lambda: {}
    sent, results = [], []

    def opener(req, **kwargs):
        sent.append(json.loads(req.data))
        return response(*scripts[len(sent) - 1])

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)

    async def scenario():
        try:
            for factory in make:
                results.append(await run(runner, factory(results)))
        finally:
            await close(pool)

    asyncio.run(scenario())
    return results, sent, pool


def reply_of(parsed, events, stream=False):
    """What the client gets for these events: (the Writer's events or None, the final message)."""
    writer = Writer() if stream else None
    adapter = adapters.for_dialect("anthropic", parsed, writer)
    adapter.begin()
    reason = "stop"
    for event in events:
        if event["type"] == "end":
            reason = event["reason"]
        elif event["type"] != "error":
            adapter.feed(event)
    return writer, adapter.end(reason)


def kinds(final):
    return [block["type"] for block in final["content"]]


# -- what the client sees ------------------------------------------------------------------------------------------------

def test_a_search_is_answered_and_the_client_gets_anthropic_s_server_tool_blocks(tmp_path, monkeypatch):
    search = FakeSearch()
    made = []
    results, sent, _ = drive(tmp_path, monkeypatch, [asks("printing press history"), answers()], search,
                             [lambda _: made.append(search_request(search)) or made[-1]])
    events = results[0]
    assert len(sent) == 2 and search.calls == [("printing press history", {
        "name": "web_search", "max_uses": 10, "allowed_domains": [], "blocked_domains": [], "country": None})]
    assert events[-1] == {"type": "end", "reason": "stop"} and not any(e["type"] == "error" for e in events)
    _, final = reply_of(made[0], events)
    assert kinds(final) == ["thinking", "text", "server_tool_use", "web_search_tool_result", "thinking", "text"]
    use, found = final["content"][2], final["content"][3]
    assert (use["name"], use["id"], use["input"]) == ("web_search", "s1", {"query": "printing press history"})
    assert found["tool_use_id"] == "s1" and found["content"] == web_search.result_blocks([PAGE])
    assert final["content"][-1]["text"] == "The printing press was invented in the 1440s."
    assert final["stop_reason"] == "end_turn"
    assert final["usage"]["server_tool_use"] == {"web_search_requests": 1}


def test_the_stream_shows_the_search_as_it_happens_and_closes_every_block(tmp_path, monkeypatch):
    search, made = FakeSearch(), []
    results, _, _ = drive(tmp_path, monkeypatch, [asks("q"), answers()], search,
                          [lambda _: made.append(search_request(search, stream=True)) or made[-1]])
    writer, final = reply_of(made[0], results[0], stream=True)
    wire = [(name, payload) for name, payload in writer.events if name != "ping"]
    starts = [(p["index"], p["content_block"]["type"]) for n, p in wire if n == "content_block_start"]
    stops = [p["index"] for n, p in wire if n == "content_block_stop"]
    assert [kind for _, kind in starts] == ["thinking", "text", "server_tool_use", "web_search_tool_result", "thinking", "text"]
    assert sorted(stops) == [index for index, _ in starts] and len(stops) == len(set(stops))
    # the result arrives whole in its start event, as Anthropic streams it, after the call's block has stopped
    result_start = next(p for n, p in wire if n == "content_block_start" and p["content_block"]["type"] == "web_search_tool_result")
    assert result_start["content_block"]["content"][0]["url"] == PAGE.url
    stopped_before = [p["index"] for n, p in wire[:wire.index(("content_block_start", result_start))] if n == "content_block_stop"]
    assert result_start["index"] - 1 in stopped_before
    deltas = [p["delta"]["partial_json"] for n, p in wire if n == "content_block_delta" and p["delta"]["type"] == "input_json_delta"]
    assert json.loads("".join(deltas)) == {"query": "q"}
    last = [p for n, p in wire if n == "message_delta"][-1]
    assert last["delta"]["stop_reason"] == "end_turn" and last["usage"]["server_tool_use"] == {"web_search_requests": 1}
    assert wire[-1][0] == "done" or wire[-2][0] == "message_stop"


def test_the_continuation_is_native_the_signature_the_call_and_the_result_and_a_prefix_that_is_the_same(tmp_path, monkeypatch):
    search, made = FakeSearch(), []
    _, sent, _ = drive(tmp_path, monkeypatch, [asks("q"), answers()], search,
                       [lambda _: made.append(search_request(search)) or made[-1]])
    first, second = sent
    assert [tool["name"] for tool in first["tools"]] == ["web_search"]
    assert second["tools"] == first["tools"] and second["openAICacheKey"] == first["openAICacheKey"]
    # what the first request sent is the start of the second, and nothing of it moved
    assert strip_cache(second["messages"][:len(first["messages"])]) == strip_cache(first["messages"])
    assistant, result = second["messages"][len(first["messages"]):]
    assert assistant["role"] == "assistant" and assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-one"}
    assert [b["type"] for b in assistant["content"]] == ["thinking", "text", "tool_call"]
    assert assistant["content"][2]["toolName"] == "web_search" and assistant["content"][2]["toolInput"] == {"query": "q"}
    assert result["role"] == "tool" and result["content"]["toolCallId"] == "s1"
    assert result_text(result["content"]["toolResult"]) == PRESS
    assert second["rit"] == direct.make_rit(second["messages"], second["tools"])
    assert second["reasoningEffort"] == first["reasoningEffort"]


def test_a_forced_search_applies_to_the_first_step_only(tmp_path, monkeypatch):
    search, made = FakeSearch(), []
    results, sent, _ = drive(tmp_path, monkeypatch, [asks("q"), answers()], search, [
        lambda _: made.append(search_request(search, tool_choice={"type": "tool", "name": "web_search"})) or made[-1]])
    assert len(sent) == 2 and "toolChoice" not in sent[1]
    assert not any(e["type"] == "error" for e in results[0]) and len(search.calls) == 1
    # the first step was asked in words (Claude refuses a forced choice), the answer after the search was not
    assert "Call the tool web_search now" in json.dumps(sent[0]["messages"][-1])
    assert "Call the tool" not in json.dumps(sent[1])


# -- the limits and the failures -----------------------------------------------------------------------------------------

def test_calls_past_max_uses_get_the_error_result_and_the_model_goes_on(tmp_path, monkeypatch):
    search, made = FakeSearch(), []
    results, sent, _ = drive(tmp_path, monkeypatch, [asks("one", call_id="a"), asks("two", call_id="b"), answers()], search,
                             [lambda _: made.append(search_request(search, tool={"max_uses": 1})) or made[-1]])
    assert [query for query, _ in search.calls] == ["one"]  # the second call never reached the provider
    _, final = reply_of(made[0], results[0])
    found = [block for block in final["content"] if block["type"] == "web_search_tool_result"]
    assert found[0]["content"][0]["type"] == "web_search_result"
    assert found[1]["content"] == {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}
    assert final["usage"]["server_tool_use"] == {"web_search_requests": 1}  # a refused search is not counted
    assert result_text(sent[2]["messages"][-1]["content"]["toolResult"]) == "web_search error: max_uses_exceeded"
    assert final["content"][-1]["text"].startswith("The printing press")


def test_a_model_that_keeps_calling_after_the_limit_is_stopped(tmp_path, monkeypatch):
    search, made = FakeSearch(), []
    scripts = [asks("q%d" % n, call_id="c%d" % n, sig="s%d" % n) for n in range(8)]
    results, sent, _ = drive(tmp_path, monkeypatch, scripts, search,
                             [lambda _: made.append(search_request(search, tool={"max_uses": 1})) or made[-1]])
    # the first step, then max_uses + 2 continuations (the first of them with the one search it was allowed): then it ends
    assert len(sent) == 4 and len(search.calls) == 1
    assert results[0][-1] == {"type": "end", "reason": "stop"}
    _, final = reply_of(made[0], results[0])
    found = [b["content"] for b in final["content"] if b["type"] == "web_search_tool_result"]
    assert len(found) == 4 and all(c == {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}
                                   for c in found[1:])


def test_a_failing_provider_is_an_error_result_not_a_failed_turn(tmp_path, monkeypatch):
    search, made = FakeSearch(error=SearchError("too_many_requests", "HTTP 429")), []
    results, sent, _ = drive(tmp_path, monkeypatch, [asks("q"), answers("I could not search.")], search,
                             [lambda _: made.append(search_request(search)) or made[-1]])
    assert not any(e["type"] == "error" for e in results[0]) and len(sent) == 2
    _, final = reply_of(made[0], results[0])
    assert final["content"][3]["content"] == {"type": "web_search_tool_result_error", "error_code": "too_many_requests"}
    assert "server_tool_use" not in final["usage"] and final["content"][-1]["text"] == "I could not search."


def test_a_provider_that_raises_something_else_is_unavailable(tmp_path, monkeypatch):
    class Broken:
        def search(self, query, spec):
            raise RuntimeError("boom " * 3)

    made = []
    results, _, _ = drive(tmp_path, monkeypatch, [asks("q"), answers()], Broken(),
                          [lambda _: made.append(search_request(Broken())) or made[-1]])
    _, final = reply_of(made[0], results[0])
    assert final["content"][3]["content"]["error_code"] == "unavailable" and final["stop_reason"] == "end_turn"


@pytest.mark.parametrize("arguments, code", [('{"query": ""}', "invalid_tool_input"), ("{}", "invalid_tool_input"),
                                             ('{"query": 5}', "invalid_tool_input"), ("[1]", "invalid_tool_input"),
                                             ('{"query": "%s"}' % ("x" * 1001), "query_too_long")])
def test_a_call_with_no_usable_query_is_an_error_result_and_costs_no_search(tmp_path, monkeypatch, arguments, code):
    search, made = FakeSearch(), []
    script = [*THINKING, signature("s"), *[{"event": "content_block_start", "type": "tool_call", "toolCallId": "s1",
                                           "toolName": "web_search"},
                                          {"event": "content_block_delta", "type": "tool_call", "partialInputs": arguments},
                                          {"event": "content_block_end", "type": "tool_call", "toolCallId": "s1"}], *END]
    results, _, _ = drive(tmp_path, monkeypatch, [script, answers()], search,
                          [lambda _: made.append(search_request(search)) or made[-1]])
    _, final = reply_of(made[0], results[0])
    assert search.calls == [] and final["content"][-3]["content"] == {"type": "web_search_tool_result_error", "error_code": code}


def test_results_outside_the_allowed_domains_never_reach_the_model(tmp_path, monkeypatch):
    search, made = FakeSearch(results=[PAGE, OTHER]), []
    _, sent, _ = drive(tmp_path, monkeypatch, [asks("q"), answers()], search, [
        lambda _: made.append(search_request(search, tool={"allowed_domains": ["wikipedia.org"]})) or made[-1]])
    assert search.calls[0][1]["allowed_domains"] == ["wikipedia.org"]
    shown = result_text(sent[1]["messages"][-1]["content"]["toolResult"])
    assert PAGE.url in shown and OTHER.url not in shown


def test_a_blocked_domain_is_dropped_too(tmp_path, monkeypatch):
    search, made = FakeSearch(results=[PAGE, OTHER]), []
    _, sent, _ = drive(tmp_path, monkeypatch, [asks("q"), answers()], search, [
        lambda _: made.append(search_request(search, tool={"blocked_domains": ["wikipedia.org"]})) or made[-1]])
    shown = result_text(sent[1]["messages"][-1]["content"]["toolResult"])
    assert OTHER.url in shown and PAGE.url not in shown


def test_the_journal_says_what_was_searched_and_never_names_a_key(tmp_path, monkeypatch):
    search, made = FakeSearch(), []
    before = journal.JOURNAL.last_seq
    drive(tmp_path, monkeypatch, [asks("printing press"), answers()], search,
          [lambda _: made.append(search_request(search)) or made[-1]])
    events = journal.JOURNAL.query(since=before)
    ran = next(e for e in events if e["kind"] == "web_search.run")
    assert ran["data"]["query"] == "printing press" and ran["data"]["results"] == 1 and ran["data"]["error"] is None
    assert ran["data"]["provider"] == "exa"
    ended = next(e for e in events if e["kind"] == "turn.end")
    assert ended["data"]["web_searches"] == 1
    assert "api_key" not in json.dumps(events).lower()


# -- a request with tools of its own -------------------------------------------------------------------------------------

def test_a_step_that_also_calls_a_client_tool_ends_the_turn_with_every_block_in_it(tmp_path, monkeypatch):
    search, made = FakeSearch(), []
    both = [*THINKING, signature("sig"), {"event": "content_block_end", "type": "thinking"},
            *call("web_search", "s1", query="q"), *call("read_file", "r1", path="a"), *END]
    mixed = lambda _: made.append(search_request(search, extra_tools=[
        {"name": "read_file", "description": "r", "input_schema": TOOLS[0]["function"]["parameters"]}])) or made[-1]
    results, sent, _ = drive(tmp_path, monkeypatch, [both], search, [mixed])
    assert len(sent) == 1 and results[0][-1] == {"type": "end", "reason": "tool_calls"}
    _, final = reply_of(made[0], results[0])
    assert [b["type"] for b in final["content"] if b["type"] != "thinking"] == [
        "server_tool_use", "tool_use", "web_search_tool_result"]
    assert final["stop_reason"] == "tool_use" and final["usage"]["server_tool_use"] == {"web_search_requests": 1}


def test_a_search_only_request_is_a_job_of_its_own_and_leaves_the_conversation_it_names_alone(tmp_path, monkeypatch):
    # Claude Code runs WebSearch as a request of its own with the id of the chat it serves.
    search, made = FakeSearch(), []

    def main(messages=FIRST):
        return lambda _: request(messages, dialect="anthropic", key="header:chat")

    def follow(results):
        first = request(FIRST, dialect="anthropic", key="header:chat")
        _, final = reply_of(first, results[0])
        call_id = next(b["id"] for b in final["content"] if b["type"] == "tool_use")
        return request(FIRST + [{"role": "assistant", "content": final["content"]},
                                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": "file"}]}],
                       dialect="anthropic", key="header:chat")

    results, sent, pool = drive(tmp_path, monkeypatch,
                                [tool_reply(), asks("q"), answers(), [text("done"), *END]], search,
                                [main(), lambda _: search_request(search, key="header:chat"), follow])
    chat = pool._conversations["header:chat"]
    assert len(chat._native_history.branches) == 1 and chat._cached_head == "CLIENT_SYSTEM"
    search_body, follow_body = sent[1], sent[3]
    assert search_body["messages"][0]["content"][0]["text"] == SEARCH_SYSTEM
    assert [tool["name"] for tool in search_body["tools"]] == ["web_search"]
    # the chat's own next request continued its signed history: the head, the tools and the cache key are the chat's
    assert follow_body["tools"] == sent[0]["tools"] and follow_body["openAICacheKey"] == sent[0]["openAICacheKey"]
    assert "opaque-one" in json.dumps(follow_body) and search_body["openAICacheKey"] != sent[0]["openAICacheKey"]
    assert not any(e["type"] == "error" for run_events in results for e in run_events)


# -- the history ---------------------------------------------------------------------------------------------------------

def test_a_request_that_echoes_the_reply_continues_the_signed_history(tmp_path, monkeypatch):
    """The search reply the client holds (server_tool_use, web_search_tool_result, text) is the history the relay kept."""
    search = FakeSearch()
    extra = [{"name": "read_file", "description": "Read the client's file", "input_schema": TOOLS[0]["function"]["parameters"]}]
    first = []

    def one(_):
        first.append(search_request(search, key="header:chat", extra_tools=extra))
        return first[0]

    def two(results):
        _, final = reply_of(first[0], results[0])
        messages = QUESTION + [{"role": "assistant", "content": json.loads(json.dumps(final["content"]))},
                               {"role": "user", "content": "And who invented movable type in China?"}]
        return search_request(search, messages, key="header:chat", extra_tools=extra)

    before = journal.JOURNAL.last_seq
    results, sent, _ = drive(tmp_path, monkeypatch, [asks("q"), answers(sig="sig-two"), answers("Bi Sheng.", sig="sig-three")],
                             search, [one, two])
    assert len(sent) == 3 and not any(e["type"] == "error" for run_events in results for e in run_events)
    third = sent[2]
    # the second step's body, then the user turn: the prefix is the one upstream already has, signatures included
    assert strip_cache(third["messages"][:len(sent[1]["messages"])]) == strip_cache(sent[1]["messages"])
    signatures = [b["signature"] for m in third["messages"] for b in m.get("content", [])
                  if isinstance(b, dict) and b.get("type") == "thinking"]
    assert signatures == ["sig-one", "sig-two"]
    assert third["messages"][-1]["content"][0]["text"].endswith("And who invented movable type in China?")
    names = [e["kind"] for e in journal.JOURNAL.query(since=before)]
    assert "native_history.resume" in names and "native_history.reset" not in names


def test_the_echo_is_read_as_a_call_and_its_result_for_the_model_and_for_the_history():
    blocks = web_search.result_blocks([PAGE])
    echoed = [{"type": "text", "text": "I will search."},
              {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}},
              {"type": "web_search_tool_result", "tool_use_id": "s1", "content": blocks},
              {"type": "text", "text": "The answer."}]
    data = {"model": "claude-opus-5-5", "max_tokens": 64, "tools": [dict(SERVER)], "messages": [
        *QUESTION, {"role": "assistant", "content": echoed}, {"role": "user", "content": "Thanks."}]}
    parsed = validate.parse_anthropic(data, dict(PARSE_CFG, _web_search=FakeSearch()))
    canonical = parsed.native_messages
    assert [m["role"] for m in canonical] == ["user", "assistant", "tool", "assistant", "user"]
    assert canonical[1]["content"] == [{"type": "text", "text": "I will search."}, {
        "type": "tool_call", "toolCallId": "s1", "toolName": "web_search", "toolInput": {"query": "q"}}]
    assert canonical[2] == {"role": "tool", "content": {"toolCallId": "s1", "toolResult": PRESS}}
    assert canonical[3]["content"] == [{"type": "text", "text": "The answer."}]
    # the text bridge the model reads when the history cannot continue natively
    history = parsed.prompt
    assert "[tool_call web_search id=s1: {\"query\": \"q\"}]" in history and "[tool_result id=s1: [1] Printing press" in history
    assert "web_search_result" not in history and "encrypted_content" not in history


# -- over HTTP, and with the output cap --------------------------------------------------------------------------------

def server_events(query="q", results=(PAGE,)):
    """What the orchestrator hands the adapter for one search and the answer after it."""
    return [{"type": "text", "delta": "I will search. "},
            {"type": "tool_call", "id": "s1", "name": "web_search", "arguments": "", "tool_type": "function",
             "server": "web_search", "done": False},
            {"type": "tool_call", "id": "s1", "name": "web_search", "arguments": json.dumps({"query": query}),
             "tool_type": "function", "server": "web_search", "done": True},
            {"type": "server_tool_result", "tool_use_id": "s1", "content": web_search.result_blocks(list(results))},
            {"type": "usage_start"}, {"type": "text", "delta": "The answer."}, {"type": "end", "reason": "stop"}]


def message_body(**changes):
    return {"model": "claude-opus-5-5", "max_tokens": 4096, "messages": copy.deepcopy(QUESTION),
            "tools": [dict(SERVER, max_uses=3)], **changes}


def test_a_messages_request_with_the_server_tool_is_answered_with_its_blocks(relay):
    relay.cfg["_web_search"] = FakeSearch()
    relay.fake.script = server_events()
    status, answer = relay.json("/v1/messages", message_body())
    assert status == 200 and kinds(answer) == ["text", "server_tool_use", "web_search_tool_result", "text"]
    assert answer["stop_reason"] == "end_turn" and answer["usage"]["server_tool_use"] == {"web_search_requests": 1}
    assert relay.fake.seen[0].web_search["max_uses"] == 3


def test_the_streamed_reply_carries_the_server_tool_blocks_in_order(relay):
    relay.cfg["_web_search"] = FakeSearch()
    relay.fake.script = server_events()
    status, raw = relay.sse("/v1/messages", message_body(stream=True))
    pieces = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
    assert status == 200
    assert [p["content_block"]["type"] for p in pieces if p["type"] == "content_block_start"] == [
        "text", "server_tool_use", "web_search_tool_result", "text"]
    assert [p["type"] for p in pieces][-2:] == ["message_delta", "message_stop"]
    assert pieces[-2]["usage"]["server_tool_use"] == {"web_search_requests": 1}


def test_without_a_provider_the_request_is_still_refused_over_http(relay):
    status, answer = relay.json_any("/v1/messages", message_body())
    assert status == 422 and answer["error"]["type"] == "invalid_request_error"
    assert "web_search_provider" in answer["error"]["message"]


def test_a_search_the_cap_cannot_fit_is_dropped_with_its_result():
    class Capped:  # a request with a small output cap: the call does not fit, so its result must not reach the client either
        model, system, show_thinking, wants_usage, tools = "claude-opus-5-5", "", True, False, []
        output_cap = 3

    adapter = adapters.for_dialect("anthropic", Capped(), Writer())
    adapter.begin()
    for event in server_events("a long query that will not fit into three tokens at all")[:-1]:
        adapter.feed(event)
    final = adapter.end("stop")
    assert "server_tool_use" not in kinds(final) and "web_search_tool_result" not in kinds(final)
    assert final["stop_reason"] == "max_tokens"


def test_a_search_that_fits_the_cap_keeps_both_blocks():
    class Capped:
        model, system, show_thinking, wants_usage, tools = "claude-opus-5-5", "", True, False, []
        output_cap = 200

    adapter = adapters.for_dialect("anthropic", Capped(), Writer())
    adapter.begin()
    for event in server_events()[:-1]:
        adapter.feed(event)
    assert kinds(adapter.end("stop")) == ["text", "server_tool_use", "web_search_tool_result", "text"]


def test_a_chat_client_is_not_affected_by_the_new_events():
    class Plain:
        model, system, show_thinking, wants_usage, tools = "claude-opus-5-5", "", True, False, []

    adapter = adapters.for_dialect("chat", Plain(), None)
    adapter.begin()
    for event in server_events()[:-1]:
        adapter.feed(event)
    final = adapter.end("stop")
    assert final["choices"][0]["message"]["content"] == "I will search. The answer."
