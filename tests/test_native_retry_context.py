"""Relay-only retry instructions must not survive into the next client turn."""
import asyncio
import json
import threading

import pytest

import apierr
import session as session_module
from test_native_context_reuse import FIRST, Socket, continued, request, run, setup


STOP = {"type": "end", "reason": "stop"}


@pytest.fixture
def scripted_runner(tmp_path, monkeypatch):
    backend, pool, runner = setup(tmp_path)
    backend.normalized_scripts = []

    class ScriptedSession(session_module.RelaySession):
        async def stream(self, prompt, model_provider, tool_names=None):
            if self._needs_drain:
                await self.aclose()
                self._needs_drain = False
            if self.ws is None:
                self.ws = Socket(backend, self.thread_id)
            backend.sent.append((self.thread_id, prompt, self.ws))
            script = backend.normalized_scripts.pop(0) if backend.normalized_scripts else [
                {"type": "text", "delta": "ready"}, STOP,
            ]
            for event in script:
                if callable(event):
                    event(self)
                elif isinstance(event, Exception):
                    self._needs_drain = True
                    raise event
                else:
                    yield event

    monkeypatch.setattr(session_module, "RelaySession", ScriptedSession)
    return backend, pool, runner


def retry_script(kind):
    if kind == "empty":
        return [STOP]
    # A final-snapshot transport can replace an insignificant whitespace delta.
    # The visible-text guard intentionally keeps this refusal retryable.
    return [{"type": "text", "delta": "\n"},
            {"type": "final_text", "text": "I cannot access the client tools."}, STOP]


def success_script(outcome):
    if outcome == "text":
        return [{"type": "text", "delta": "ready"}, STOP]
    call = {"id": "read-1", "name": "read_file", "arguments": {"path": "a"}}
    if outcome == "native_tool":
        return [{"type": "tool_call", **call}, STOP]
    return [{"type": "text", "delta": "<tool_call>" + json.dumps(call) + "</tool_call>"}, STOP]


def followup(messages, outcome):
    if outcome == "text":
        return continued(messages, "NEXT_CLIENT_REQUEST")
    return messages + [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "read-1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": "NEW_RETRY_TOOL_RESULT"},
    ]


@pytest.mark.parametrize("kind", ["empty", "refusal"])
@pytest.mark.parametrize("outcome", ["text", "envelope", "native_tool"])
@pytest.mark.parametrize("initial", ["cold", "warm"])
def test_successful_retry_keeps_pending_then_full_reset_and_warm_resume(scripted_runner, kind, outcome, initial):
    backend, pool, runner = scripted_runner
    messages = FIRST if initial == "cold" else continued(FIRST, "CURRENT_CLIENT_REQUEST")
    current = request(messages)
    following_messages = followup(messages, outcome)
    following = request(following_messages)
    resumed = request(continued(following_messages, "POST_RESET_WARM_REQUEST"))

    async def scenario():
        if initial == "warm":
            await run(runner, request(FIRST))
        previous_target = pool._conversations.get(current.conversation_key)
        previous_context = getattr(previous_target, "_client_context", None)
        backend.normalized_scripts.extend([retry_script(kind), success_script(outcome)])
        events = await run(runner, current)
        assert events[-1] == {"type": "end", "reason": "stop" if outcome == "text" else "tool_calls"}
        target = pool._conversations[current.conversation_key]
        assert target._client_context_pending is True
        assert getattr(target, "_client_context", None) is previous_context
        retry_socket = target.ws
        assert not pool._active and pool._conversation_refs[current.conversation_key] == 0

        events = await run(runner, following)
        assert events[-1] == STOP
        assert retry_socket.closed and target.thread_id == "thread-2"
        assert target._client_context_pending is False
        assert target._client_context["history"] == following.prompt
        fresh_socket = target.ws
        events = await run(runner, resumed)
        assert events[-1] == STOP and target.ws is fresh_socket
        assert target._client_context_pending is False
        assert not pool._active and pool._slots._value == backend.cfg["max_workers"]

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    correction = "[RETRY]" if kind == "empty" else "[CLIENT TOOL TRANSPORT CORRECTION]"
    assert correction in backend.sent[-3][1]
    assert backend.sent[-3][0] == "thread-1"
    assert backend.sent[-2][0] == backend.sent[-1][0] == "thread-2"
    assert following.prompt in backend.sent[-2][1] and 'tools=[' in backend.sent[-2][1]
    assert correction not in backend.sent[-2][1] and correction not in backend.sent[-1][1]
    assert "POST_RESET_WARM_REQUEST" in backend.sent[-1][1]
    assert "ORIGINAL_TASK" not in backend.sent[-1][1] and 'tools=[' not in backend.sent[-1][1]


@pytest.mark.parametrize("kind", ["empty", "refusal"])
@pytest.mark.parametrize("failure", ["error", "cancelled"])
def test_failed_or_cancelled_retry_keeps_previous_context_pending(scripted_runner, kind, failure):
    backend, pool, runner = scripted_runner
    first = request(FIRST)
    following = request(continued(FIRST, "RETRYING_CLIENT_REQUEST"))
    holder = {"cancel_event": threading.Event()}

    async def scenario():
        await run(runner, first)
        target = pool._conversations[first.conversation_key]
        previous = target._client_context
        if failure == "error":
            failed_script = [apierr.upstream_error("scripted retry failure")]
        else:
            def cancel(current):
                holder["cancel_event"].set()
                current.cancel()
            failed_script = [cancel, {"type": "end", "reason": "cancelled"}]
        backend.normalized_scripts.extend([retry_script(kind), failed_script])
        events = await run(runner, following, holder)
        if failure == "error":
            assert events[-1]["type"] == "error" and events[-1]["error"].status == 502
        else:
            assert events[-1] == {"type": "end", "reason": "cancelled"}
        assert target._client_context is previous and target._client_context_pending is True
        assert not pool._active and pool._conversation_refs[first.conversation_key] == 0
        assert pool._slots._value == backend.cfg["max_workers"]
        recovered = await run(runner, following)
        assert recovered[-1] == STOP
        assert target.thread_id == "thread-2" and target._client_context_pending is False

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    assert following.prompt in backend.sent[-1][1] and 'tools=[' in backend.sent[-1][1]


def test_native_success_without_retry_keeps_warm_context(scripted_runner):
    backend, pool, runner = scripted_runner
    first, following = request(FIRST), request(continued(FIRST, "NORMAL_NEXT_REQUEST"))

    async def scenario():
        await run(runner, first)
        target = pool._conversations[first.conversation_key]
        socket = target.ws
        await run(runner, following)
        assert target.ws is socket and not socket.closed
        assert target._client_context_pending is False
        assert target._client_context["history"] == following.prompt

    asyncio.run(scenario())
    assert backend.created == ["thread-1"]
    assert "NORMAL_NEXT_REQUEST" in backend.sent[-1][1] and "ORIGINAL_TASK" not in backend.sent[-1][1]
