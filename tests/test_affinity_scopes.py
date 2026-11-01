"""Auxiliary requests cannot replace or block the native client's warm session."""
import asyncio
import json
import threading

import pytest

import validate
from test_native_context_reuse import FIRST, request, run, setup


def scenario_setup(tmp_path, **overrides):
    return setup(tmp_path, project_context=False, cookie_values=[], **overrides)


def main_requests(key="header:parent", result="NEW_NATIVE_TOOL_RESULT"):
    first = request(FIRST, key=key)
    following = request(FIRST + [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "read-1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"a"}'}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": result},
    ], key=key)
    return first, following


def service_request(key="header:parent", system="TITLE_SYSTEM", content="SERVICE_INPUT"):
    return request([{"role": "user", "content": content}], key=key, system=system, tools=[])


def tool_response(backend):
    envelope = '<tool_call>' + json.dumps({"id": "read-1", "name": "read_file",
                                          "arguments": {"path": "a"}}) + '</tool_call>'
    backend.scripts.append([
        {"type": "text", "messageId": "answer", "content": envelope},
        {"type": "run_credits_persisted", "creditsUsed": 0}, {"type": "agent_loop_done"},
    ])


@pytest.mark.parametrize("service_count", [1, 4])
def test_service_requests_preserve_native_socket_and_incremental_tool_results(tmp_path, service_count):
    backend, pool, runner = scenario_setup(tmp_path)
    first, following = main_requests()
    tool_response(backend)

    async def scenario():
        await run(runner, first)
        target = pool._conversations[first.conversation_key]
        socket, context = target.ws, target._client_context
        for index in range(service_count):
            events = await run(runner, service_request(content="SERVICE_" + str(index)))
            assert events[-1] == {"type": "end", "reason": "stop"}
            assert target.ws is socket and not socket.closed
            assert target._client_context is context and target._client_context_pending is False
        events = await run(runner, following)
        assert events[-1] == {"type": "end", "reason": "stop"}
        assert target.ws is socket and target._client_context_pending is False

    asyncio.run(scenario())
    assert backend.created == ["thread-" + str(index) for index in range(1, service_count + 2)]
    assert backend.starts == backend.connects == service_count + 1
    assert backend.sent[0][0] == backend.sent[-1][0] == "thread-1"
    assert [item[0] for item in backend.sent[1:-1]] == backend.created[1:]
    assert all(item[2].closed for item in backend.sent[1:-1])
    assert list(pool._conversations) == [first.conversation_key]
    assert "NEW_NATIVE_TOOL_RESULT" in backend.sent[-1][1]
    assert "ORIGINAL_TASK" not in backend.sent[-1][1] and 'tools=[' not in backend.sent[-1][1]


def test_auxiliary_jobs_never_cache_history_across_systems_or_into_native_conversations(tmp_path):
    backend, pool, runner = scenario_setup(tmp_path)
    first, following = main_requests()

    async def scenario():
        await run(runner, first)
        await run(runner, service_request(system="TITLE_PRIVATE_SYSTEM"))
        await run(runner, service_request(system="SUMMARY_PRIVATE_SYSTEM"))
        await run(runner, service_request(system="TITLE_PRIVATE_SYSTEM"))
        await run(runner, following)

    asyncio.run(scenario())
    assert [item[0] for item in backend.sent] == ["thread-1", "thread-2", "thread-3", "thread-4", "thread-1"]
    assert list(pool._conversations) == [first.conversation_key]
    assert all(item[2].closed for item in backend.sent[1:-1])
    assert "SUMMARY_PRIVATE_SYSTEM" not in backend.sent[1][1]
    assert "TITLE_PRIVATE_SYSTEM" not in backend.sent[2][1]


def test_parent_and_subagent_keep_their_native_and_service_scopes_separate(tmp_path):
    backend, pool, runner = scenario_setup(tmp_path)
    keys = ["header:parent", "header:parent:agent-1"]

    async def scenario():
        for key in keys:
            await run(runner, main_requests(key)[0])
        for key in keys:
            await run(runner, service_request(key=key))
        for index, key in enumerate(keys):
            await run(runner, main_requests(key, "PRIVATE_TOOL_RESULT_" + str(index))[1])

    asyncio.run(scenario())
    assert [item[0] for item in backend.sent] == ["thread-1", "thread-2", "thread-3", "thread-4", "thread-1", "thread-2"]
    assert set(pool._conversations) == set(keys)
    for index, (_, prompt, _) in enumerate(backend.sent[-2:]):
        assert "PRIVATE_TOOL_RESULT_" + str(index) in prompt
        assert "PRIVATE_TOOL_RESULT_" + str(1 - index) not in prompt
        assert "ORIGINAL_TASK" not in prompt


def test_suspended_service_request_does_not_block_native_followup(tmp_path):
    backend, pool, runner = scenario_setup(tmp_path)
    first, following = main_requests()

    async def scenario():
        await run(runner, first)
        target = pool._conversations[first.conversation_key]
        main_socket = target.ws
        entered, release = asyncio.Event(), asyncio.Event()
        original_connect = backend.connect

        async def connect(thread):
            socket = await original_connect(thread)
            original_send = socket.send

            async def send(raw):
                await original_send(raw)
                if json.loads(raw).get("type") == "user_prompt":
                    entered.set()
                    await release.wait()

            socket.send = send
            return socket

        backend.connect = connect
        service = asyncio.create_task(run(runner, service_request()))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            assert not service.done()
            events = await asyncio.wait_for(run(runner, following), timeout=3)
            assert events[-1] == {"type": "end", "reason": "stop"}
            assert not service.done()
            assert target.ws is main_socket and target._client_context_pending is False
        finally:
            release.set()
            await service

    asyncio.run(scenario())
    assert [item[0] for item in backend.sent] == ["thread-1", "thread-2", "thread-1"]
    assert "NEW_NATIVE_TOOL_RESULT" in backend.sent[-1][1]


def test_shared_mode_auxiliary_request_uses_isolated_lease_without_resetting_main(tmp_path):
    backend, pool, runner = scenario_setup(tmp_path, session_mode="shared")
    first, following = main_requests()

    async def scenario():
        await run(runner, first)
        target = pool._shared
        socket, context = target.ws, target._client_context
        events = await run(runner, service_request())
        assert events[-1] == {"type": "end", "reason": "stop"}
        assert pool._shared is target and target.ws is socket and not socket.closed
        assert target._client_context is context and target._client_context_pending is False
        assert backend.sent[-1][2].closed
        events = await run(runner, following)
        assert events[-1] == {"type": "end", "reason": "stop"}
        assert target.ws is socket

    asyncio.run(scenario())
    assert [item[0] for item in backend.sent] == ["thread-1", "thread-2", "thread-1"]
    assert "NEW_NATIVE_TOOL_RESULT" in backend.sent[-1][1] and "ORIGINAL_TASK" not in backend.sent[-1][1]


@pytest.mark.parametrize("mode", ["shared", "per_conversation"])
def test_isolated_lease_bypasses_both_reuse_modes_and_releases_its_own_slot(tmp_path, mode):
    backend, pool, _ = scenario_setup(tmp_path, session_mode=mode, max_workers=2)

    async def scenario():
        main = await pool.acquire("header:parent")
        isolated = await asyncio.wait_for(pool.acquire("header:parent", isolated=True), timeout=3)
        assert main.session is not isolated.session and main.thread_id != isolated.thread_id
        assert len(pool._active) == 2
        await pool.release(isolated)
        assert isolated.released and not main.released and len(pool._active) == 1
        await pool.release(main)
        assert not pool._active and pool._slots._value == 2

    asyncio.run(scenario())


def identified_service(source, content, *, model="claude-opus-5-5"):
    data = {"model": model, "messages": [
        {"role": "system", "content": "STANDALONE_SYSTEM"},
        {"role": "user", "content": content},
    ]}
    header = "standalone-client" if source == "header" else None
    if source == "metadata":
        data["metadata"] = {"user_id": json.dumps({"session_id": "standalone-client"})}
    elif source != "header":
        data[source] = "standalone-client"
    parsed = validate.parse_chat(data, {"strict_models": True, "model": model})
    parsed.conversation_key = validate.conversation_key(data, header)
    return parsed


@pytest.mark.parametrize("mode", ["per_conversation", "shared", "per_request"])
@pytest.mark.parametrize("source", ["header", "conversation", "conversation_id", "session_id", "metadata"])
@pytest.mark.parametrize("second_model", ["claude-opus-5-5", "claude-sonnet-5"])
def test_toolless_jobs_never_reuse_implicit_history(tmp_path, mode, source, second_model):
    backend, pool, runner = scenario_setup(tmp_path, session_mode=mode, max_workers=1)
    first = identified_service(source, "FIRST_STANDALONE_INPUT")
    second = identified_service(source, "SECOND_STANDALONE_INPUT", model=second_model)

    async def scenario():
        for parsed in (first, second, first):
            events = await run(runner, parsed)
            assert events[-1] == {"type": "end", "reason": "stop"}
            assert not pool._active and pool._slots._value == 1

    asyncio.run(scenario())
    assert first.conversation_key == second.conversation_key
    assert backend.created == ["thread-1", "thread-2", "thread-3"]
    assert [item[0] for item in backend.sent] == backend.created
    assert all(item[2].closed for item in backend.sent)
    assert not pool._conversations and not pool._conversation_locks and pool._shared is None
    assert "FIRST_STANDALONE_INPUT" not in backend.sent[1][1]
    assert "SECOND_STANDALONE_INPUT" not in backend.sent[2][1]


def test_complete_plain_conversation_history_is_preserved_without_implicit_upstream_history(tmp_path):
    backend, pool, runner = scenario_setup(tmp_path)
    first = service_request(key="conversation_id:plain", content="PLAIN_FIRST_USER")
    following = request([
        {"role": "user", "content": "PLAIN_FIRST_USER"},
        {"role": "assistant", "content": "PLAIN_FIRST_ANSWER"},
        {"role": "user", "content": "PLAIN_SECOND_USER"},
    ], key=first.conversation_key, system=first.system, tools=[])

    async def scenario():
        await run(runner, first)
        events = await run(runner, following)
        assert events[-1] == {"type": "end", "reason": "stop"}

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    assert all(text in backend.sent[-1][1] for text in (
        "PLAIN_FIRST_USER", "PLAIN_FIRST_ANSWER", "PLAIN_SECOND_USER"))
    assert following.prompt in backend.sent[-1][1]
    assert not pool._conversations


@pytest.mark.parametrize("mode", ["per_conversation", "shared"])
@pytest.mark.parametrize("failure", ["error", "cancelled"])
def test_failed_service_releases_its_slot_without_changing_native_context(tmp_path, mode, failure):
    backend, pool, runner = scenario_setup(tmp_path, session_mode=mode, max_workers=1)
    first, following = main_requests()

    async def scenario():
        await run(runner, first)
        target = pool._shared if mode == "shared" else pool._conversations[first.conversation_key]
        socket, context = target.ws, target._client_context
        holder = {"cancel_event": threading.Event()}
        if failure == "error":
            backend.scripts.append([{"type": "error", "error": "standalone failure"}])
        else:
            def cancel():
                holder["cancel_event"].set()
                holder["session"].cancel()
            backend.on_send = cancel
        events = await run(runner, service_request(), holder)
        backend.on_send = None
        if failure == "error":
            assert any(event["type"] == "error" for event in events)
        else:
            assert events[-1] == {"type": "end", "reason": "cancelled"}
        assert backend.sent[-1][2].closed
        assert not pool._active and pool._slots._value == 1
        assert target.ws is socket and not socket.closed
        assert target._client_context is context and not target._client_context_pending
        events = await run(runner, following)
        assert events[-1] == {"type": "end", "reason": "stop"}
        assert target.ws is socket and not target._client_context_pending

    asyncio.run(scenario())
    assert [item[0] for item in backend.sent] == ["thread-1", "thread-2", "thread-1"]
    assert "NEW_NATIVE_TOOL_RESULT" in backend.sent[-1][1]
