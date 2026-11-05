"""Prompt-cache breakpoint invariants for the chunk path and the native-continuation path.

A history whose newest chunk is an image has no text block to carry a cache flag. Before
87aaa6e the relay still spent its tail breakpoint on that chunk, so such a turn kept flags
only on the head and chunk 0: every later token was billed as uncached input (cache_read
stuck at the head size, cache_write 0). These tests pin the placement rules for both
request shapes without any network access.
"""
import asyncio
import copy
import random
from types import SimpleNamespace

import pytest

import direct
import native_history
from test_native_context_reuse import FIRST, run
from test_native_history import append_result, close, harness, parsed, tool_reply
from test_review_fixes_pr19 import END, text

MODEL = {"model": "claude-opus-5-5"}
TOOLS = [{"name": "inspect", "params": {}, "strict": True}]
IMAGE = {"type": "image", "imageData": "fixture", "mimeType": "image/png"}
IMAGE_URL = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}


def build(chunks, tools=TOOLS, session=None, head="stable head"):
    return direct.build_body({}, MODEL, head, "", tools, "auto", 32,
                             chunks=copy.deepcopy(chunks), session=session)


def flags(body):
    """(message index, block type) of every cache flag in a request body."""
    return [(i, block.get("type")) for i, message in enumerate(body["messages"])
            if isinstance(message["content"], list)
            for block in message["content"] if isinstance(block, dict) and block.get("cache")]


def tool_loop(count):
    return ["User: start"] + ["[tool result id=%d]: ok" % i for i in range(count)]


# --- chunk path -------------------------------------------------------------------------

@pytest.mark.parametrize("tools", [TOOLS, None])
@pytest.mark.parametrize("tail", [[IMAGE], [IMAGE, IMAGE]])
def test_image_only_tail_never_leaves_a_long_history_marked_only_at_its_start(tools, tail):
    history = tool_loop(120)
    body = build(history + [copy.deepcopy(tail)], tools)
    marked = flags(body)
    assert {kind for _, kind in marked} == {"text"}
    assert len(marked) <= (3 if tools else 4)
    # message index = chunk index + 1 (system head); the image chunk is the final message
    assert max(index for index, _ in marked) == len(history)
    assert body["messages"][-1]["content"] == tail


def test_cache_endpoint_survives_an_image_only_message_inside_the_history():
    session = SimpleNamespace(_cached_turn_ends=[])
    first = tool_loop(30)
    second = first + ["Assistant: done with this part", [copy.deepcopy(IMAGE)]]
    third = second + ["Assistant: [tool_call inspect id=u1: {}]", "[tool result id=u1]: ok"]
    marked = [{index for index, _ in flags(build(chunks, session=session)) if index} for chunks in (first, second, third)]
    tail, before_image = len(first), len(first) + 1
    assert tail in marked[0]
    # the image turn reads the previous entry explicitly instead of relying on the provider's lookback
    # window, and writes up to the text block in front of the image
    assert {tail, before_image} <= marked[1]
    assert {before_image, len(third)} <= marked[2]


def random_chunk(rng, index):
    roll = rng.random()
    if roll < 0.55:
        return "[tool result t id=%d]: ok" % index
    if roll < 0.65:
        return "User: question %d" % index
    if roll < 0.77:
        return [copy.deepcopy(IMAGE)]
    if roll < 0.83:
        return [copy.deepcopy(IMAGE), copy.deepcopy(IMAGE)]
    if roll < 0.93:
        return [{"type": "text", "text": "[tool result s id=%d]: shot" % index}, copy.deepcopy(IMAGE)]
    return {"type": "text", "text": "dict chunk %d" % index}


def carries_text(chunk):
    blocks = chunk if isinstance(chunk, list) else [chunk]
    return any(isinstance(block, str) or block.get("type") == "text" for block in blocks)


@pytest.mark.parametrize("seed", range(4))
def test_chunk_breakpoints_hold_for_random_sessions(seed):
    rng = random.Random(seed)
    for _ in range(120):
        tools = TOOLS if rng.random() < 0.7 else None
        session = SimpleNamespace(_cached_turn_ends=[])
        chunks = [random_chunk(rng, 0)]
        endpoint = None
        for turn in range(rng.randint(1, 6)):
            chunks += [random_chunk(rng, len(chunks)) for _ in range(rng.randint(1, 3 if turn else 40))]
            body = build(chunks, tools, session)
            marked = flags(body)
            assert {kind for _, kind in marked} == {"text"}
            assert len(marked) <= (3 if tools else 4)
            assert body["messages"][0]["content"][-1].get("cache") is True
            history = {index - 1 for index, _ in marked if index}
            carriers = [i for i, chunk in enumerate(chunks) if carries_text(chunk)]
            if carriers:
                assert carriers[-1] in history
            if endpoint is not None and endpoint in carriers and endpoint < len(chunks) - 1:
                assert endpoint in history
            endpoint = max(history) if history else endpoint
            stripped = [[{k: v for k, v in block.items() if k != "cache"} for block in message["content"]]
                        for message in body["messages"][1:]]
            expected = [[dict(b) for b in c] if isinstance(c, list) else [dict(c)] if isinstance(c, dict)
                        else [{"type": "text", "text": c}] for c in chunks]
            assert stripped == expected


# --- native continuation path -----------------------------------------------------------

def native_conversation():
    return [
        {"role": "system", "content": [{"type": "text", "text": "system"}]},
        {"role": "user", "content": [{"type": "text", "text": "task"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": "opaque"},
                                          {"type": "tool_call", "toolCallId": "a", "toolName": "inspect", "toolInput": {}}]},
        {"role": "tool", "content": {"toolCallId": "a", "toolResult": [{"type": "text", "text": "r"}, dict(IMAGE)]}},
        {"role": "user", "content": [dict(IMAGE)]},
    ]


def native_flags(messages):
    return [(i, block["type"]) for i, message in enumerate(messages) if isinstance(message["content"], list)
            for block in message["content"] if block.get("cache")]


@pytest.mark.parametrize("tools", [TOOLS, None])
def test_native_hints_skip_images_and_tool_results_and_stay_on_the_last_call_block(tools):
    messages = native_conversation()
    before = copy.deepcopy(messages)
    cached = native_history.cache_messages(messages, True, tools)
    assert messages == before
    assert native_flags(cached) == [(0, "text"), (2, "tool_call")]
    assert cached[2]["content"][0] == messages[2]["content"][0]
    assert cached[3] == messages[3] and cached[4] == messages[4]

    messages[1]["content"][0]["cache"] = True
    assert native_flags(native_history.cache_messages(messages, True, tools)) == [
        (0, "text"), (1, "text"), (2, "tool_call")]
    assert native_flags(native_history.cache_messages(messages, False, tools)) == []


def random_native(rng):
    messages = [{"role": "system", "content": [{"type": "text", "text": "x-anthropic-billing-header: v=1"},
                                               {"type": "text", "text": "system", "cache": True}]
                 if rng.random() < 0.3 else [{"type": "text", "text": "system", "cache": True}]},
                {"role": "user", "content": [{"type": "text", "text": "task", "cache": rng.random() < 0.5}]}]
    for number in range(rng.randint(0, 30)):
        roll = rng.random()
        if roll < 0.55:
            calls = [{"type": "tool_call", "toolCallId": "c%d_%d" % (number, k), "toolName": "inspect", "toolInput": {}}
                     for k in range(rng.randint(1, 3))]
            signed = [{"type": "thinking", "thinking": "", "signature": "s%d" % number}] if rng.random() < 0.8 else []
            messages.append({"role": "assistant", "content": signed + calls})
            for call in calls:
                result = rng.choice(["result", [{"type": "text", "text": "r"}, dict(IMAGE)], [dict(IMAGE)]])
                messages.append({"role": "tool", "content": {"toolCallId": call["toolCallId"], "toolResult": result}})
        elif roll < 0.75:
            messages.append({"role": "assistant", "content": [{"type": "text", "text": "reply"}]})
        elif roll < 0.9:
            messages.append({"role": "user", "content": [dict(IMAGE)]})
        else:
            messages.append({"role": "user", "content": [{"type": "text", "text": "more"}, dict(IMAGE)]})
    return messages


def strip_flags(value):
    if isinstance(value, dict):
        return {k: strip_flags(v) for k, v in value.items() if k != "cache"}
    if isinstance(value, list):
        return [strip_flags(v) for v in value]
    return value


@pytest.mark.parametrize("seed", range(4))
def test_native_breakpoints_hold_for_random_conversations(seed):
    rng = random.Random(seed)
    for _ in range(400):
        messages, tools = random_native(rng), TOOLS if rng.random() < 0.7 else None
        before = copy.deepcopy(messages)
        cached = native_history.cache_messages(messages, True, tools)
        assert messages == before and strip_flags(cached) == strip_flags(before)
        marked = native_flags(cached)
        assert {kind for _, kind in marked} <= {"text", "tool_call"}
        assert len(marked) <= (3 if tools else 4)
        eligible = [(i, b) for i, m in enumerate(cached) if isinstance(m["content"], list)
                    for b, block in enumerate(m["content"]) if block["type"] in ("text", "tool_call")
                    and not str(block.get("text", "")).startswith("x-anthropic-billing-header:")]
        flagged = [(i, b) for i, m in enumerate(cached) if isinstance(m["content"], list)
                   for b, block in enumerate(m["content"]) if block.get("cache")]
        assert eligible[0] in flagged and eligible[-1] in flagged


@pytest.mark.parametrize("attachment", ["user", "tool"])
def test_native_chain_keeps_its_breakpoints_when_screenshots_end_the_history(tmp_path, monkeypatch, attachment):
    pool, runner, sent = harness(tmp_path, monkeypatch, [
        tool_reply("c1", "sig-a"), tool_reply("c2", "sig-b"), tool_reply("c3", "sig-c"), [text("done"), *END]])

    async def scenario():
        messages = copy.deepcopy(FIRST)
        try:
            for _ in range(3):
                request = parsed(messages)
                messages = append_result(messages, request, await run(runner, request), "chat")
                if attachment == "user":
                    messages.append({"role": "user", "content": [copy.deepcopy(IMAGE_URL)]})
                else:
                    messages[-1] = dict(messages[-1], content=[copy.deepcopy(IMAGE_URL)])
            await run(runner, parsed(messages))
        finally:
            await close(pool)

    asyncio.run(scenario())
    assert len(sent) == 4
    previous_endpoint = None
    for body in sent:
        marked = flags(body)
        assert {kind for _, kind in marked} <= {"text", "tool_call"}
        assert len(marked) <= 3
        calls = [i for i, m in enumerate(body["messages"]) if m["role"] == "assistant"]
        if calls:
            assert (calls[-1], "tool_call") in marked
        if previous_endpoint is not None:
            assert previous_endpoint in {index for index, _ in marked}
        previous_endpoint = max(index for index, _ in marked)
