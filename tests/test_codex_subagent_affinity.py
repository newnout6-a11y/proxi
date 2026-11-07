"""A Codex sub-agent is a conversation of its own, with its parent behind it, like a Claude Code agent.

A sub-agent of Codex (spawned, review, guardian) sends the session-id of its root, a thread-id of its own and the id of its
parent thread (a2's Codex run on 2026-11-06). The relay keyed every one of them by the session-id, so the root, its children
and its background jobs were ONE conversation: one pool session, whose single keepwarm body is the newest turn's, whichever
thread it came from. The first request of a child reset the signed history of the session and dropped the parent's keepwarm
body; the parent's prefix, which the child left alone for as long as it worked, was then kept warm by nobody and expired
after five minutes, and the parent's next request wrote it all again. Claude Code's agents were split off long ago
(x-claude-code-agent-id) and the pool pins the parent's cache while such a child works."""
import validate

ROOT = {"session-id": "root-session", "thread-id": "root-session"}
CHILD = {"session-id": "root-session", "thread-id": "child-thread", "x-codex-parent-thread-id": "root-session",
         "x-openai-subagent": "collab_spawn"}


def test_the_root_keeps_the_key_of_its_session_and_a_child_gets_its_own_with_the_root_as_parent():
    assert validate.conversation_affinity({}, ROOT) == ("header:root-session", None)
    key, parent = validate.conversation_affinity({}, CHILD)
    assert key.startswith("agent:") and parent == "header:root-session"
    assert (key, parent) == validate.conversation_affinity({}, dict(CHILD))
    other = validate.conversation_affinity({}, {**CHILD, "thread-id": "second-child"})
    assert other[0] != key and other[1] == parent


def test_a_child_is_told_apart_by_its_thread_and_by_nothing_else():
    key = validate.conversation_affinity({}, CHILD)[0]
    assert validate.conversation_affinity({}, {**CHILD, "x-openai-subagent": "guardian"})[0] == key
    assert validate.conversation_affinity({}, {**CHILD, "x-codex-parent-thread-id": "a-child"})[0] == key
    # the same thread under another session is another conversation
    assert validate.conversation_affinity({}, {**CHILD, "session-id": "another-root"})[0] != key


def test_what_names_no_thread_or_no_parent_stays_what_it_was():
    no_thread = {k: v for k, v in CHILD.items() if k != "thread-id"}
    no_parent = {k: v for k, v in CHILD.items() if k != "x-codex-parent-thread-id"}
    assert validate.conversation_affinity({}, no_thread) == ("header:root-session", None)
    assert validate.conversation_affinity({}, no_parent) == ("header:root-session", None)
    assert validate.conversation_affinity({}, {"x-codex-parent-thread-id": "x", "thread-id": "y"}) == (None, None)


def test_a_claude_agent_header_still_decides_first():
    claude = {**CHILD, "x-claude-code-agent-id": "agent-a"}
    assert validate.conversation_affinity({}, claude) == validate.conversation_affinity(
        {}, {"session-id": "root-session", "x-claude-code-agent-id": "agent-a"})


def test_over_http_the_sub_agent_request_carries_its_own_key_and_its_parent(relay):
    payload = {"model": "claude-opus-5-5", "input": "hello"}
    for headers in (ROOT, CHILD, ROOT):
        assert relay.json("/v1/responses", payload, headers=headers)[0] == 200
    main, child, again = relay.fake.seen
    assert main.conversation_key == again.conversation_key == "header:root-session"
    assert child.conversation_key.startswith("agent:") and child.parent_conversation_key == main.conversation_key
    assert main.parent_conversation_key is None


def test_the_parent_keeps_its_own_keepwarm_body_and_history_while_a_child_runs(tmp_path, monkeypatch):
    import asyncio
    import copy

    import journal
    from test_conversation_state import stored
    from test_native_context_reuse import FIRST, run
    from test_native_history import append_result, close, harness, parsed, tool_reply
    from test_review_fixes_pr19 import END, text

    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply("c1", "opaque-one", "a"), tool_reply("k1", "opaque-kid", "b"),
                                                         [text("done"), *END]])
    seq = journal.JOURNAL.last_seq
    root_key = validate.conversation_affinity({}, ROOT)[0]
    child_key, parent_key = validate.conversation_affinity({}, CHILD)

    async def scenario():
        try:
            history = copy.deepcopy(FIRST)
            first = parsed(history, key=root_key, system="PARENT_SYSTEM")
            history = append_result(history, first, await run(runner, first), "chat")
            child = parsed(copy.deepcopy(FIRST), key=child_key, system="CHILD_SYSTEM")
            child.parent_conversation_key = parent_key
            await run(runner, child)
            warm = pool._conversations[root_key]._warm_body
            await run(runner, parsed(history, key=root_key, system="PARENT_SYSTEM"))
            return warm, set(pool._conversations)
        finally:
            await close(pool)

    warm, conversations = asyncio.run(scenario())
    assert conversations == {root_key, child_key}
    assert warm is not None and warm["openAICacheKey"] == sent[0]["openAICacheKey"] != sent[1]["openAICacheKey"]
    assert "opaque-one" in str(sent[2]["messages"]) and "opaque-kid" not in str(sent[2]["messages"])
    assert not stored(seq, "native_history.reset", "native_history.cold")
