"""Signed thinking survives actual direct tool loops without rebuilding its prefix."""
import asyncio
import copy
import json

import pytest

import adapters
import direct
import native_history
from test_native_context_reuse import FIRST, Backend, request, run, setup
from test_review_fixes_pr19 import END, THINKING, response, signature, text


TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "number"}}}}}]


def tool_reply(call_id="c1", sig="opaque-one", path="a"):
    return [*THINKING, signature(sig[:3]), signature(sig[3:]),
            {"event": "content_block_end", "type": "thinking"},
            {"event": "content_block_start", "type": "tool_call", "toolCallId": call_id, "toolName": "read_file"},
            {"event": "content_block_delta", "type": "tool_call",
             "partialInputs": json.dumps({"path": path, "limit": "3"})},
            {"event": "content_block_end", "type": "tool_call", "toolCallId": call_id}, *END]


def harness(tmp_path, monkeypatch, scripts):
    backend, pool, runner = setup(tmp_path, transport="direct", cache_keepwarm_seconds=0,
                                  base="https://fixture.invalid", cookie_values=[], project_context=False,
                                  reasoning_effort="high")
    backend.headers = lambda: {}
    sent = []

    def opener(req, **kwargs):
        sent.append(json.loads(req.data))
        return response(*scripts[len(sent) - 1])

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return pool, runner, sent


async def close(pool):
    for session in pool._conversations.values():
        await session.aclose()


def parsed(messages, dialect="chat", key="header:client-a", system="CLIENT_SYSTEM"):
    return request(messages, dialect=dialect, tools=TOOLS, key=key, system=system)


def append_result(messages, p, events, dialect, value="file contents"):
    cls = {"chat": adapters.ChatAdapter, "anthropic": adapters.AnthropicAdapter,
           "responses": adapters.ResponsesAdapter}[dialect]
    adapter = cls(p, None)
    for event in events:
        adapter.feed(event)
    payload = adapter.payload("tool_calls")
    if dialect == "chat":
        assistant = payload["choices"][0]["message"]
        call_id = assistant["tool_calls"][0]["id"]
        return messages + [assistant, {"role": "tool", "tool_call_id": call_id, "content": value}]
    if dialect == "anthropic":
        call_id = next(b["id"] for b in payload["content"] if b["type"] == "tool_use")
        return messages + [{"role": "assistant", "content": payload["content"]},
                           {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                          "content": value}]}]
    call_id = next(b["call_id"] for b in payload["output"] if b["type"] == "function_call")
    return messages + payload["output"] + [{"type": "function_call_output", "call_id": call_id, "output": value}]


def strip_cache(value):
    if isinstance(value, dict):
        return {k: strip_cache(v) for k, v in value.items() if k != "cache"}
    if isinstance(value, list):
        return [strip_cache(v) for v in value]
    return value


@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_two_tool_round_trips_preserve_signatures_raw_arguments_and_prefix(tmp_path, monkeypatch, dialect):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), tool_reply("c2", "opaque-two"), [text("done"), *END]])

    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            for _ in range(2):
                p = parsed(messages, dialect)
                events = await run(runner, p)
                assert not any(e["type"] == "error" for e in events)
                messages = append_result(messages, p, events, dialect)
            final = await run(runner, parsed(messages, dialect))
            assert any(e.get("delta") == "done" for e in final)
            assert len(pool._conversations["header:client-a"]._native_history.branches) == 1
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 3
    for previous, following, sig in zip(sent, sent[1:], ("opaque-one", "opaque-two")):
        prefix = following["messages"][:len(previous["messages"])]
        assert strip_cache(prefix) == strip_cache(previous["messages"])
        assistant = following["messages"][len(previous["messages"])]
        assert assistant["role"] == "assistant"
        assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": sig}
        # Client tools receive a coerced number; upstream gets its original block.
        assert strip_cache(assistant["content"][1])["toolInput"] == {"path": "a", "limit": "3"}
        assert following["messages"][-1]["role"] == "tool"
        # a result carries the newest hint where it arrives (item 16): one text block, hinted
        assert following["messages"][-1]["content"]["toolResult"] == [{"type": "text", "text": "file contents", "cache": True}]
        assert following["rit"] == direct.make_rit(following["messages"], following["tools"])
        assert following["reasoningEffort"] == previous["reasoningEffort"]
        assert following["openAICacheKey"] == previous["openAICacheKey"]
    assert [b["signature"] for m in sent[-1]["messages"] for b in m.get("content", [])
            if isinstance(b, dict) and b.get("type") == "thinking"] == ["opaque-one", "opaque-two"]


@pytest.mark.parametrize("change", ["prefix", "arguments", "call_id", "result_id", "missing_result", "head", "key", "effort", "tools"])
def test_changed_client_or_settings_never_receive_a_signature_from_another_prefix(tmp_path, monkeypatch, change):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text("new answer"), *END]])

    async def scenario():
        try:
            p = parsed(FIRST)
            events = await run(runner, p)
            messages = append_result(copy.deepcopy(FIRST), p, events, "chat")
            if change == "prefix":
                messages[0]["content"] = "edited task"
            elif change == "arguments":
                messages[-2]["tool_calls"][0]["function"]["arguments"] = '{"path":"different","limit":3}'
            elif change == "call_id":
                messages[-2]["tool_calls"][0]["id"] = "other"
            elif change == "result_id":
                messages[-1]["tool_call_id"] = "other"
            elif change == "missing_result":
                messages[-1] = {"role": "user", "content": "continue"}
            p = parsed(messages, key="header:other" if change == "key" else "header:client-a",
                       system="NEW_SYSTEM" if change == "head" else "CLIENT_SYSTEM")
            if change == "effort":
                p.effort = "low"
            if change == "tools":
                p.tools[0]["function"]["description"] = "changed tool"
            await run(runner, p)
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert not any(b.get('type') in ('thinking', 'redacted_thinking')
                   for m in sent[1]['messages'] if isinstance(m.get('content'), list)
                   for b in m['content'] if isinstance(b, dict))
    assert "opaque-one" not in json.dumps(sent[1])


def test_discarded_thinking_attempt_never_becomes_a_replay_snapshot(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [[*THINKING, signature("discarded"), *END],
                                                        tool_reply(sig="accepted"), [text("done"), *END]])

    async def scenario():
        try:
            p = parsed(FIRST)
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), "chat")
            await run(runner, parsed(messages))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 3
    assert "discarded" not in json.dumps(sent[-1])
    assert "accepted" in json.dumps(sent[-1])


@pytest.mark.parametrize("limits", [(128000, None), (None, 128000), (4096, 8192), (8192, 4096)])
def test_output_limit_changes_keep_signed_history_and_forward_the_new_limit(tmp_path, monkeypatch, limits):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text("done"), *END]])
    previous_limit, next_limit = limits

    async def scenario():
        try:
            p = parsed(FIRST)
            p.max_tokens = previous_limit
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), "chat")
            following = parsed(messages)
            following.max_tokens = next_limit
            await run(runner, following)
            assert len(pool._conversations["header:client-a"]._native_history.branches) == 1
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2
    assert sent[0].get("maxCompletionTokens") == previous_limit
    assert sent[1].get("maxCompletionTokens") == next_limit
    assert "opaque-one" in json.dumps(sent[1])
    assert sent[1]["messages"][-1]["role"] == "tool"
    assert strip_cache(sent[1]["messages"][:len(sent[0]["messages"])]) == strip_cache(sent[0]["messages"])
    assert sent[0]["openAICacheKey"] == sent[1]["openAICacheKey"]


def test_hidden_thinking_is_still_saved_and_restored_server_side(tmp_path, monkeypatch, capsys):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text("done"), *END]])

    async def scenario():
        try:
            p = parsed(FIRST)
            p.show_thinking = False
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), "chat")
            following = parsed(messages)
            following.show_thinking = False
            await run(runner, following)
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert "opaque-one" in json.dumps(sent[-1])
    assert "[history] native continuation restored thinking=1 tool_calls=1" in capsys.readouterr().out


def test_parallel_branches_keep_their_own_signed_continuations(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply("child", "child-sig"), tool_reply("main", "main-sig"),
                                                        [text("done child"), *END], [text("done main"), *END]])
    real_stream = direct._stream_body

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        first = True

        async def gated(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                started.set()
                await release.wait()
            async for event in real_stream(*args, **kwargs):
                yield event

        monkeypatch.setattr(direct, "_stream_body", gated)
        p1, p2 = parsed(FIRST), parsed(FIRST)
        task = asyncio.create_task(run(runner, p1))
        try:
            await asyncio.wait_for(started.wait(), 2)
            child = await run(runner, p2)
            release.set()
            main = await task
            history = pool._conversations["header:client-a"]._native_history
            assert len(history.branches) == 2
            await run(runner, parsed(append_result(copy.deepcopy(FIRST), p2, child, "chat")))
            await run(runner, parsed(append_result(copy.deepcopy(FIRST), p1, main, "chat")))
        finally:
            release.set()
            await task
            await close(pool)

    asyncio.run(scenario())
    assert "child-sig" in json.dumps(sent[2]) and "main-sig" not in json.dumps(sent[2])
    assert "main-sig" in json.dumps(sent[3]) and "child-sig" not in json.dumps(sent[3])


def test_fresh_account_does_not_borrow_signed_state(tmp_path, monkeypatch):
    pool1, runner1, sent1 = harness(tmp_path / "one", monkeypatch, [tool_reply()])

    async def scenario():
        try:
            p = parsed(FIRST)
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner1, p), "chat")
            pool2, runner2, sent2 = harness(tmp_path / "two", monkeypatch, [[text("fresh account"), *END]])
            try:
                await run(runner2, parsed(messages))
                assert "opaque-one" not in json.dumps(sent2[0])
            finally:
                await close(pool2)
        finally:
            await close(pool1)

    asyncio.run(scenario())


def test_pool_account_migration_does_not_transfer_signed_snapshots(tmp_path, monkeypatch):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply(), [text("migrated"), *END]])
    first_backend = pool.backend
    second_backend = Backend(tmp_path, **first_backend.cfg)
    second_backend.headers = lambda: {}
    pool.backends.append(second_backend)

    async def scenario():
        try:
            p = parsed(FIRST)
            messages = append_result(copy.deepcopy(FIRST), p, await run(runner, p), "chat")
            first_backend.unavailable = {"reason": "exhausted", "available": 0, "threshold": 20}
            await run(runner, parsed(messages))
            assert pool._conversations["header:client-a"].backend is second_backend
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 2 and "opaque-one" not in json.dumps(sent[-1])


@pytest.mark.parametrize("outcome", ["truncated", "cancelled", "error"])
def test_incomplete_responses_are_never_committed_to_signed_history(tmp_path, monkeypatch, outcome):
    script = tool_reply()
    if outcome == "truncated":
        script = script[:-2]
    elif outcome == "error":
        script = [*THINKING, signature("bad"), {"event": "error", "error": "fixture failure"}]
    pool, runner, sent = harness(tmp_path, monkeypatch, [script])
    real_stream = direct._stream_body

    async def cancel_at_end(cfg, session, *args, **kwargs):
        async for event in real_stream(cfg, session, *args, **kwargs):
            if event["type"] == "end":
                session.cancel()
            yield event

    if outcome == "cancelled":
        monkeypatch.setattr(direct, "_stream_body", cancel_at_end)

    async def scenario():
        try:
            await run(runner, parsed(FIRST))
            assert pool._conversations["header:client-a"]._native_history.branches == []
        finally:
            await close(pool)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["reverse", "duplicate", "partial"])
def test_multiple_tool_results_must_complete_the_exact_call_set(tmp_path, monkeypatch, mode):
    pool, runner, sent = harness(tmp_path, monkeypatch, [tool_reply("a", "sig-a")[:-2] + tool_reply("b", "sig-b"),
                                                        [text("done"), *END]])

    async def scenario():
        try:
            p = parsed(FIRST)
            adapter = adapters.ChatAdapter(p, None)
            for event in await run(runner, p):
                adapter.feed(event)
            assistant = adapter.payload("tool_calls")["choices"][0]["message"]
            ids = ["b", "a"] if mode == "reverse" else ["b", "b"] if mode == "duplicate" else ["b"]
            messages = copy.deepcopy(FIRST) + [assistant] + [
                {"role": "tool", "tool_call_id": call_id, "content": call_id} for call_id in ids]
            await run(runner, parsed(messages))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert ("sig-a" in json.dumps(sent[-1])) == (mode == "reverse")
    assert ("sig-b" in json.dumps(sent[-1])) == (mode == "reverse")


def test_multimodal_tool_results_keep_content_order_and_error_flag():
    image = {"type": "image", "imageData": "fixture", "mimeType": "image/png"}
    messages = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "is_error": True,
                 "content": [{"type": "text", "text": "before"}, image, {"type": "text", "text": "after"}]}]}]
    native = native_history.client_messages(messages, lambda value, where: dict(value))
    assert native == [{"role": "tool", "isError": True, "content": {"toolCallId": "c",
                      "toolResult": [{"type": "text", "text": "before"}, image, {"type": "text", "text": "after"}]}}]


def test_complete_redacted_thinking_is_preserved_as_opaque_data():
    capture = native_history.ReplyCapture()
    capture.feed({"event": "content_block_start", "type": "redacted_thinking", "data": "opaque",
                  "responseItemId": "fixture-item"})
    assert capture.finish() == [{"type": "redacted_thinking", "data": "opaque", "responseItemId": "fixture-item"}]


def test_raw_capture_keeps_multiple_thinking_blocks_and_interleaved_calls():
    capture = native_history.ReplyCapture()
    events = [*THINKING, signature("first"), text("visible"), *THINKING, signature("second"),
              {"event": "content_block_start", "type": "tool_call", "toolCallId": "a", "toolName": "read_file"},
              {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path":"a"'},
              {"event": "content_block_start", "type": "tool_call", "toolCallId": "b", "toolName": "read_file"},
              {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{"path":"b"}'},
              {"event": "content_block_end", "toolCallId": "b"},
              {"event": "content_block_delta", "type": "tool_call", "partialInputs": '}'},
              {"event": "content_block_end", "toolCallId": "a"}]
    for event in events:
        capture.feed(event)
    blocks = capture.finish()
    assert [b["type"] for b in blocks] == ["thinking", "text", "thinking", "tool_call", "tool_call"]
    assert [b["signature"] for b in blocks if b["type"] == "thinking"] == ["first", "second"]
    assert [b["toolInput"] for b in blocks if b["type"] == "tool_call"] == [{"path": "a"}, {"path": "b"}]


@pytest.mark.parametrize("events", [THINKING, [signature("orphan")],
    [{"event": "content_block_start", "type": "unsupported"}],
    [{"event": "content_block_start", "type": "tool_call", "toolName": "read_file"}],
    [{"event": "content_block_start", "type": "tool_call", "toolCallId": "a", "toolName": "read_file"},
     {"event": "content_block_delta", "type": "tool_call", "partialInputs": '{bad'}]])
def test_incomplete_or_unknown_blocks_cannot_be_saved(events):
    capture = native_history.ReplyCapture()
    for event in events:
        capture.feed(event)
    assert capture.finish() is None


def test_cache_hints_do_not_mutate_signed_blocks_or_overrun_the_budget():
    messages = [{"role": "system", "content": [{"type": "text", "text": "system", "cache": True}]},
                {"role": "user", "content": [{"type": "text", "text": "task", "cache": True}]},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "opaque"},
                    {"type": "tool_call", "toolCallId": "c", "toolName": "read_file", "toolInput": {}}]},
                {"role": "tool", "content": {"toolCallId": "c", "toolResult": "value"}}]
    before = copy.deepcopy(messages)
    cached = native_history.cache_messages(messages, True, TOOLS)
    assert messages == before
    assert strip_cache(cached) == strip_cache(messages)
    assert sum(bool(b.get("cache")) for m in cached for b in m.get("content", []) if isinstance(b, dict)) == 3
    assert cached[2]["content"][0] == messages[2]["content"][0]


def test_snapshot_storage_replaces_sequential_parents_and_bounds_live_branches():
    history = native_history.History()
    step = [{"type": "tool_call", "toolCallId": "c", "toolName": "read_file", "toolInput": {}}]
    for _ in range(30):
        parent = history.branches[-1] if history.branches else None
        history.record("scope", [], [], step, {}, parent)
        assert len(history.branches) == 1
    for _ in range(30):
        # A final answer is kept when the next turn continues it, for a few turns (test_native_history_rewind).
        history.record("scope", [], [], [], {}, history.branches[-1])
    assert len(history.branches) == native_history.REWIND_POINTS + 1
    for _ in range(30):
        history.record("scope", [], [], [], {})
    assert len(history.branches) == 8
