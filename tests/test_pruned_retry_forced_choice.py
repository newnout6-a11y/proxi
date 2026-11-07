"""The request asked again after upstream refused a branch that lost a call is the same request, forced choice included.

A Claude route refuses a forced tool choice, so iter_events sends `auto` and asks for the call in words (forced_choice). The
retry that follows a refusal of a pruned branch (native_history.deliverable) called iter_events again with the choice it had
already turned into `auto`: the second request went upstream without the words that ask for the call, and the model was no
longer held to the tool the client forced."""
import asyncio
import copy

import forced_choice
from test_dropped_call_history import LIST_DIR, NAMED, SIGNATURE_REFUSAL, asked, refusing_second, reply
from test_dropped_tool_calls import call
from test_native_context_reuse import FIRST, run
from test_native_history import TOOLS, append_result, close

TOOLS_BOTH = TOOLS + [LIST_DIR]


def scenario(pool, runner, choice):
    async def story():
        messages = copy.deepcopy(FIRST)
        try:
            first = asked(messages, "chat", TOOLS_BOTH, choice)
            events = await run(runner, first)
            return await run(runner, asked(append_result(messages, first, events, "chat"), "chat", TOOLS_BOTH, choice))
        finally:
            await close(pool)

    return asyncio.run(story())


def test_the_retry_after_a_refused_pruned_branch_still_asks_for_the_forced_call_in_words(tmp_path, monkeypatch):
    pool, runner, bodies, refused = refusing_second(
        tmp_path, monkeypatch,
        [reply(call("c1", "list_dir", {"path": "."}), call("c2", "read_file", {"path": "a"})),
         reply(call("c3", "read_file", {"path": "b"}), sig="opaque-two")], {2: SIGNATURE_REFUSAL})
    events = scenario(pool, runner, NAMED)
    assert refused == [2] and len(bodies) == 3
    asked_for = forced_choice.instruction(forced_choice.Requirement("read_file", frozenset({"read_file", "list_dir"})))
    for number in (0, 1, 2):  # the first request, its refused second one and the retry end with the same request in words
        last = bodies[number]["messages"][-1]
        assert last["role"] == "user" and last["content"][0]["text"] == asked_for, number
        assert "toolChoice" not in bodies[number]
    assert not any(event["type"] == "error" for event in events)
