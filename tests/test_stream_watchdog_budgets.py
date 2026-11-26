"""The stall watchdog waits for a model that is really at work (2026-11-18).

Retool sends nothing between the start of an Opus thinking block and its signature (9-107 s in probes, over 300 s on a
~1M-character prompt at max effort), and without fine-grained streaming it sends the first ~20 characters of a tool
call's arguments and holds the rest. The first-token budget (300 s) and the idle budget (120 s) cut such turns with a 504,
the client sent them again, and the same cut came back: workflow agents and Claude Code turns at a Write looped on it."""
import asyncio

import pytest

import apierr
import direct
import stream_watchdog

CFG = {"stream_first_token_timeout": 0.05, "stream_idle_timeout": 0.05, "stream_tool_timeout": 0.5,
       "stream_thinking_timeout": 0.5}


def collect(source, cfg=CFG):
    async def run():
        return [event async for event in stream_watchdog.guard(source, cfg)]
    return asyncio.run(run())


def expires(source, cfg=CFG):
    async def run():
        async for _ in stream_watchdog.guard(source, cfg):
            pass
    with pytest.raises(apierr.ApiError) as caught:
        asyncio.run(run())
    return caught.value


def test_a_buffered_tool_call_keeps_its_budget_after_the_first_piece_of_its_arguments():
    async def stream():
        yield {"type": "tool_call", "id": "call_1", "name": "Write", "partial": True, "arguments": ""}
        yield {"type": "tool_call", "id": "call_1", "name": "Write", "partial": True, "arguments": '{"path": "table.csv"'}
        await asyncio.sleep(0.15)  # past the idle budget, within the tool budget: Retool holds the rest of the arguments
        yield {"type": "tool_call", "id": "call_1", "name": "Write", "partial": True,
               "arguments": '{"path": "table.csv", "content": "n,square\\n1,1"}'}
        yield {"type": "tool_call", "id": "call_1", "name": "Write",
               "arguments": '{"path": "table.csv", "content": "n,square\\n1,1"}'}
        yield {"type": "end", "reason": "tool_calls"}

    assert collect(stream())[-1] == {"type": "end", "reason": "tool_calls"}


def test_once_the_call_is_complete_the_idle_budget_is_back():
    async def stream():
        yield {"type": "tool_call", "id": "call_1", "name": "Write", "partial": True, "arguments": ""}
        yield {"type": "tool_call", "id": "call_1", "name": "Write", "arguments": "{}"}
        await asyncio.sleep(0.15)
        yield {"type": "text", "delta": "late"}

    error = expires(stream())
    assert error.status == 504 and "idle timeout" in error.message


def test_a_real_thinking_start_extends_the_wait_for_the_first_output():
    async def stream():
        yield {"type": "thinking_status", "source": "upstream"}
        await asyncio.sleep(0.15)  # past the first-token budget, within the thinking budget
        yield {"type": "text", "delta": "24736"}
        yield {"type": "end", "reason": "stop"}

    assert collect(stream())[-2] == {"type": "text", "delta": "24736"}


@pytest.mark.parametrize("sign", [{"type": "thinking_start"}, {"type": "thinking", "delta": ""},
                                  {"type": "thinking_signature", "signature": "CAQS"}])
def test_every_real_thinking_sign_counts(sign):
    async def stream():
        yield sign
        await asyncio.sleep(0.15)
        yield {"type": "text", "delta": "ok"}

    assert collect(stream())[-1] == {"type": "text", "delta": "ok"}


@pytest.mark.parametrize("timer", [{"type": "thinking_status", "source": "relay"},
                                   {"type": "thinking_heartbeat", "elapsed": 6.5}])
def test_the_relays_own_timer_events_do_not_extend_it(timer):
    async def stream():
        yield timer
        await asyncio.sleep(0.15)
        yield {"type": "text", "delta": "late"}

    error = expires(stream())
    assert error.status == 504 and "first-token" in error.message


def test_the_thinking_budget_ends_too():
    async def stream():
        yield {"type": "thinking_start"}
        await asyncio.sleep(0.8)
        yield {"type": "text", "delta": "too late"}

    error = expires(stream())
    assert error.status == 504 and "thinking timeout" in error.message and error.phase == stream_watchdog.THINKING


def test_thinking_after_text_gets_the_thinking_budget_instead_of_the_idle_one():
    async def stream():
        yield {"type": "text", "delta": "Let me check. "}
        yield {"type": "thinking_start"}
        await asyncio.sleep(0.15)
        yield {"type": "text", "delta": "Done."}

    assert collect(stream())[-1] == {"type": "text", "delta": "Done."}


def test_a_thinking_sign_never_shortens_a_longer_budget():
    cfg = dict(CFG, stream_first_token_timeout=1.0, stream_thinking_timeout=0.05)

    async def stream():
        yield {"type": "thinking_start"}
        await asyncio.sleep(0.15)  # past the (short) thinking budget, within the first-token budget
        yield {"type": "text", "delta": "ok"}

    assert collect(stream(), cfg)[-1] == {"type": "text", "delta": "ok"}


def test_the_largest_silence_budget_counts_the_thinking_one():
    assert stream_watchdog.max_silence_budget({}) == stream_watchdog.DEFAULT_THINKING_TIMEOUT
    assert stream_watchdog.thinking_timeout({"stream_thinking_timeout": 0}) is None


def test_reasoning_text_gets_the_thinking_budget_until_the_answer_starts():
    """gpt-6.2-sol at max streamed its reasoning summary for 59-64 s and then reasoned in silence: the idle budget (120 s)
    cut it with a 504 "no output for 120s after output started" while it was still thinking (live QA, 2026-11-19)."""
    async def stream():
        yield {"type": "thinking", "delta": "Weighing the two layouts."}
        await asyncio.sleep(0.15)  # past the idle budget, within the thinking budget
        yield {"type": "thinking", "delta": " The second one is simpler."}
        await asyncio.sleep(0.15)
        yield {"type": "text", "delta": "Done."}
        yield {"type": "end", "reason": "stop"}

    assert collect(stream())[-2] == {"type": "text", "delta": "Done."}


def test_the_thinking_budget_after_reasoning_text_ends_too():
    async def stream():
        yield {"type": "thinking", "delta": "Weighing the two layouts."}
        await asyncio.sleep(0.8)
        yield {"type": "text", "delta": "too late"}

    error = expires(stream())
    assert error.status == 504 and "thinking timeout" in error.message and error.phase == stream_watchdog.THINKING


@pytest.mark.parametrize("answer", [{"type": "text", "delta": "Here it is. "},
                                    {"type": "tool_call", "id": "call_1", "name": "Write", "arguments": "{}"}])
def test_once_the_answer_starts_after_reasoning_the_idle_budget_is_back(answer):
    async def stream():
        yield {"type": "thinking", "delta": "Weighing the two layouts."}
        yield answer
        await asyncio.sleep(0.15)
        yield {"type": "text", "delta": "late"}

    error = expires(stream())
    assert error.status == 504 and "idle timeout" in error.message


def test_reasoning_text_never_shortens_the_idle_budget():
    cfg = dict(CFG, stream_idle_timeout=1.0, stream_thinking_timeout=0.05)

    async def stream():
        yield {"type": "thinking", "delta": "Weighing the two layouts."}
        await asyncio.sleep(0.15)  # past the (short) thinking budget, within the idle budget
        yield {"type": "text", "delta": "ok"}

    assert collect(stream(), cfg)[-1] == {"type": "text", "delta": "ok"}


def _body(model, tools):
    return direct.build_body({}, {"model": model}, [], "hi", tools, None, None)


TOOLS = [{"name": "Write", "description": "write a file", "input_schema": {"type": "object"}}]


def test_retool_streams_tool_arguments_finely_for_the_models_its_registry_marks():
    assert _body("claude-opus-5-5", TOOLS)["enableFineGrainedStreaming"] is True
    assert "enableFineGrainedStreaming" not in _body("claude-opus-5-5", [])  # nothing to stream without tools
    # Models whose registry entry lacks the flag are asked as before.
    assert "enableFineGrainedStreaming" not in _body("claude-sonnet-5-5", TOOLS)
    assert "enableFineGrainedStreaming" not in _body("gpt-6.1-sol", TOOLS)
