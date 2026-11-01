"""Native history is incremental only on its own successfully completed session."""
import asyncio
import copy
import json
import queue
import threading
from types import SimpleNamespace

import pytest

import validate
from orchestrator import TurnRunner
from session import SessionPool, StartResult


TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read the client's file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
}}]
FIRST = [{"role": "user", "content": "Inspect ORIGINAL_TASK."}]
PARSE_CFG = {"strict_models": True, "model": "claude-opus-5-5"}


def request(messages, *, key="header:client-a", system="CLIENT_SYSTEM", tools=None, dialect="chat"):
    tools = copy.deepcopy(TOOLS if tools is None else tools)
    data = {"model": "claude-opus-5-5", "tools": tools}
    if dialect == "responses":
        data.update(input=copy.deepcopy(messages), instructions=system)
        data["tools"] = [{"type": "function", **tool["function"]} for tool in tools]
        parsed = validate.parse_responses(data, PARSE_CFG)
    elif dialect == "anthropic":
        data.update(messages=copy.deepcopy(messages), system=system, max_tokens=64)
        data["tools"] = [{"name": tool["function"]["name"],
                          "description": tool["function"].get("description", ""),
                          "input_schema": tool["function"]["parameters"]} for tool in tools]
        parsed = validate.parse_anthropic(data, PARSE_CFG)
    else:
        data["messages"] = [{"role": "system", "content": system}] + copy.deepcopy(messages)
        parsed = validate.parse_chat(data, PARSE_CFG)
    parsed.conversation_key = key
    return parsed


def continued(messages, label="NEXT_RESULT"):
    return copy.deepcopy(messages) + [{"role": "assistant", "content": "ready"},
                                      {"role": "user", "content": label}]


class Socket:
    def __init__(self, backend, thread):
        self.backend, self.thread = backend, thread
        self.frames = asyncio.Queue()
        self.closed = False

    async def send(self, raw):
        data = json.loads(raw)
        if data.get("type") != "user_prompt":
            return
        self.backend.sent.append((self.thread, data["prompt"], self))
        script = self.backend.scripts.pop(0) if self.backend.scripts else [
            {"type": "text", "messageId": "answer", "content": "ready"},
            {"type": "run_credits_persisted", "creditsUsed": 0},
            {"type": "agent_loop_done"},
        ]
        for frame in script:
            self.frames.put_nowait(json.dumps(frame))
        if self.backend.on_send:
            self.backend.on_send()

    async def recv(self):
        return await self.frames.get()

    async def close(self):
        self.closed = True


class Backend:
    def __init__(self, tmp_path, **overrides):
        self.cfg = {
            "session_mode": "per_conversation", "thread_id": "legacy", "max_workers": 8,
            "workspace_root": str(tmp_path), "exec_policy": "off", "context_max_bytes": 0,
            "max_prompt_bytes": 0, "provider_resource_name": "test-provider",
            "show_thinking": False, "turn_timeout": 5, "recv_timeout": 5,
            "show_exec_output": False, "max_exec_turns": 0,
        }
        self.cfg.update(overrides)
        self.created, self.sent, self.scripts = [], [], []
        self.on_send = self.on_create = None
        self.starts = self.connects = 0

    async def create_thread(self):
        thread = "thread-" + str(len(self.created) + 1)
        self.created.append(thread)
        if self.on_create:
            self.on_create()
        return thread

    async def start_run(self, thread):
        self.starts += 1
        return StartResult(thread, "test-sandbox")

    async def connect(self, thread):
        self.connects += 1
        return Socket(self, thread)

    async def get_messages(self, thread):
        return {"messages": []}


def setup(tmp_path, **overrides):
    backend = Backend(tmp_path, **overrides)
    pool = SessionPool(backend)
    runner = TurnRunner(backend.cfg, SimpleNamespace(record_turn=lambda *_: None), pool)
    return backend, pool, runner


async def run(runner, parsed, holder=None):
    out = queue.Queue()
    await runner.run(parsed, out, holder if holder is not None else {})
    return [event for event in list(out.queue) if event is not None]


def test_next_native_turn_sends_only_new_history_and_all_tool_results(tmp_path):
    backend, pool, runner = setup(tmp_path)
    calls = [{"id": "read-" + name, "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": name})}} for name in ("a", "b")]
    envelope = "\n".join('<tool_call>' + json.dumps({"id": call["id"], "name": "read_file",
                         "arguments": {"path": name}}) + '</tool_call>'
                         for call, name in zip(calls, ("a", "b")))
    backend.scripts.append([
        {"type": "text", "messageId": "answer", "content": envelope},
        {"type": "run_credits_persisted", "creditsUsed": 0}, {"type": "agent_loop_done"},
    ])
    next_messages = FIRST + [{"role": "assistant", "tool_calls": calls, "content": ""}] + [
        {"role": "tool", "tool_call_id": "read-a", "content": "FILE_A_RESULT"},
        {"role": "tool", "tool_call_id": "read-b", "content": "FILE_B_RESULT"},
    ]
    first, following = request(FIRST), request(next_messages)

    async def scenario():
        initial = await run(runner, first)
        target = pool._conversations[first.conversation_key]
        completed = await run(runner, following)
        return target, initial, completed

    target, initial, completed = asyncio.run(scenario())
    assert initial[-1] == {"type": "end", "reason": "tool_calls"}
    assert completed[-1] == {"type": "end", "reason": "stop"}
    first_prompt, next_prompt = [item[1] for item in backend.sent]
    assert all(text in first_prompt for text in (first.prompt, "CLIENT_SYSTEM", 'tools=[', 'read_file'))
    assert all(text in next_prompt for text in ("FILE_A_RESULT", "FILE_B_RESULT", "read-a", "read-b",
                                                "[CONTINUE THE CURRENT CLIENT TURN]"))
    assert all(text not in next_prompt for text in ("ORIGINAL_TASK", "CLIENT_SYSTEM", 'tools=[',
                                                    "Read the client's file"))
    assert backend.created == ["thread-1"] and backend.starts == backend.connects == 1
    assert backend.sent[0][2] is backend.sent[1][2]
    assert target._client_context["history"] == following.prompt, (
        target._cancelled, target._needs_drain, target._client_context_pending)
    assert target._client_context_pending is False


@pytest.mark.parametrize("change", ["system", "tool_schema", "edited", "truncated", "identical", "no_newline"])
def test_changed_context_rotates_thread_before_sending_full_history(tmp_path, change):
    backend, pool, runner = setup(tmp_path)
    first = request(continued(FIRST, "FIRST_RESULT"))
    following = request(continued(continued(FIRST, "FIRST_RESULT")))
    if change == "system":
        following.system = "NEW_CLIENT_SYSTEM"
    elif change == "tool_schema":
        following.tools[0]["function"]["parameters"]["properties"]["path"]["description"] = "Changed schema"
    elif change == "edited":
        following.prompt = following.prompt.replace("ORIGINAL_TASK", "EDITED_TASK")
    elif change == "truncated":
        following.prompt = "User: Truncated current task"
    elif change == "identical":
        following.prompt = first.prompt
    else:
        following.prompt = first.prompt + "-appended-without-a-message-boundary"

    async def scenario():
        await run(runner, first)
        target = pool._conversations[first.conversation_key]
        old_socket = target.ws
        events = await run(runner, following)
        return target, old_socket, events

    target, old_socket, events = asyncio.run(scenario())
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert backend.created == ["thread-1", "thread-2"]
    assert old_socket.closed and target.thread_id == "thread-2"
    assert backend.sent[-1][0] == "thread-2"
    assert following.prompt in backend.sent[-1][1] and 'tools=[' in backend.sent[-1][1]
    assert following.system in backend.sent[-1][1]


def test_conversations_and_subagents_keep_independent_session_histories(tmp_path):
    backend, pool, runner = setup(tmp_path)
    keys = ["header:client-a", "header:client-b", "header:client-a:subagent-1"]

    async def scenario():
        for key in keys:
            await run(runner, request(FIRST, key=key))
        for index, key in enumerate(keys):
            await run(runner, request(continued(FIRST, "PRIVATE_RESULT_" + str(index)), key=key))

    asyncio.run(scenario())
    assert len(backend.created) == 3
    for index, (thread, prompt, _) in enumerate(backend.sent[3:]):
        assert thread == backend.created[index]
        assert "PRIVATE_RESULT_" + str(index) in prompt and "ORIGINAL_TASK" not in prompt
        assert all("PRIVATE_RESULT_" + str(other) not in prompt for other in range(3) if other != index)
    assert len({id(pool._conversations[key]) for key in keys}) == 3


@pytest.mark.parametrize("mode,key", [("per_conversation", None), ("per_request", "header:client-a")])
def test_fresh_target_never_reuses_another_session_cache(tmp_path, mode, key):
    backend, _, runner = setup(tmp_path, session_mode=mode)

    async def scenario():
        await run(runner, request(FIRST, key=key))
        await run(runner, request(continued(FIRST), key=key))

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    assert all("ORIGINAL_TASK" in prompt and 'tools=[' in prompt for _, prompt, _ in backend.sent)
    assert backend.sent[0][2] is not backend.sent[1][2]


@pytest.mark.parametrize("failure", ["error", "timeout", "cancelled"])
def test_failed_turn_does_not_commit_history_and_retry_uses_fresh_thread(tmp_path, failure):
    backend, pool, runner = setup(tmp_path)
    first, following = request(FIRST), request(continued(FIRST))
    holder = {"cancel_event": threading.Event()}

    async def scenario():
        await run(runner, first)
        target = pool._conversations[first.conversation_key]
        previous = target._client_context
        if failure == "error":
            backend.scripts.append([{"type": "error", "error": "scripted upstream failure"}])
        elif failure == "timeout":
            backend.cfg["turn_timeout"] = 0.01
            backend.scripts.append([])
        else:
            def cancel():
                holder["cancel_event"].set()
                target.cancel()
            backend.on_send = cancel
        failed = await run(runner, following, holder)
        assert target._client_context is previous
        assert target._client_context_pending is True
        if failure == "error":
            assert any(event["type"] == "error" for event in failed)
        else:
            assert failed[-1] == {"type": "end", "reason": failure}
        backend.cfg["turn_timeout"] = 5
        backend.on_send = None
        recovered = await run(runner, following)
        return target, recovered

    target, recovered = asyncio.run(scenario())
    assert recovered[-1] == {"type": "end", "reason": "stop"}
    assert backend.created == ["thread-1", "thread-2"]
    assert backend.sent[-1][0] == "thread-2"
    assert following.prompt in backend.sent[-1][1] and 'tools=[' in backend.sent[-1][1]
    assert target._client_context["history"] == following.prompt
    assert target._client_context_pending is False


def test_cancellation_during_context_rotation_is_not_reset(tmp_path):
    backend, pool, runner = setup(tmp_path)
    first, following = request(FIRST), request(continued(FIRST), system="CHANGED_SYSTEM")
    holder = {"cancel_event": threading.Event()}

    async def scenario():
        await run(runner, first)
        target = pool._conversations[first.conversation_key]
        previous = target._client_context
        def cancel():
            holder["cancel_event"].set()
            target.cancel()
        backend.on_create = cancel
        with pytest.raises(asyncio.CancelledError):
            await run(runner, following, holder)
        assert target._cancelled is True
        assert target._client_context is previous and target._client_context_pending is True

    asyncio.run(scenario())
    assert len(backend.sent) == 1


def test_shared_target_cannot_reuse_a_different_client_history(tmp_path):
    backend, _, runner = setup(tmp_path, session_mode="shared")

    async def scenario():
        await run(runner, request(FIRST, key="header:client-a"))
        await run(runner, request(continued(FIRST, "OTHER_CLIENT_RESULT"), key="header:client-b"))

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    assert "ORIGINAL_TASK" in backend.sent[-1][1] and 'tools=[' in backend.sent[-1][1]
    assert backend.sent[0][0] != backend.sent[1][0]


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
@pytest.mark.parametrize("role", ["user", "assistant", "tool"])
@pytest.mark.parametrize("append_new_message", [False, True])
def test_editing_last_message_with_text_prefix_rotates_thread(
        tmp_path, dialect, separator, role, append_new_message):
    backend, _, runner = setup(tmp_path)
    messages = copy.deepcopy(FIRST)
    if role == "assistant":
        messages.append({"role": "assistant", "content": "ORIGINAL_ASSISTANT"})
    elif role == "tool":
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "read-1", "type": "function", "function": {
                    "name": "read_file", "arguments": '{"path":"a"}'}}]},
            {"role": "tool", "tool_call_id": "read-1", "content": "ORIGINAL_TOOL_RESULT"},
        ]
    edited = copy.deepcopy(messages)
    edited[-1]["content"] += separator + "EDITED_LAST_MESSAGE"
    if append_new_message:
        edited.append({"role": "user", "content": "ACTUAL_NEW_MESSAGE"})
    first, following = request(messages, dialect=dialect), request(edited, dialect=dialect)
    assert following.prompt.startswith(first.prompt + separator)

    async def scenario():
        await run(runner, first)
        await run(runner, following)

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    assert backend.sent[-1][0] == "thread-2"
    assert following.prompt in backend.sent[-1][1] and 'tools=[' in backend.sent[-1][1]


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
def test_appending_complete_messages_keeps_warm_session_in_every_dialect(tmp_path, dialect):
    backend, pool, runner = setup(tmp_path)
    first = request(FIRST, dialect=dialect)
    following = request(continued(FIRST, "NEW_MESSAGE_RESULT"), dialect=dialect)

    async def scenario():
        await run(runner, first)
        await run(runner, following)

    asyncio.run(scenario())
    assert backend.created == ["thread-1"] and backend.starts == backend.connects == 1
    assert backend.sent[0][2] is backend.sent[1][2]
    assert "NEW_MESSAGE_RESULT" in backend.sent[-1][1] and "ORIGINAL_TASK" not in backend.sent[-1][1]
    assert isinstance(following.message_fingerprints, tuple)
    assert all(isinstance(value, bytes) and len(value) == 32 for value in following.message_fingerprints)
    assert following.message_fingerprints[:len(first.message_fingerprints)] == first.message_fingerprints
    assert pool._conversations[first.conversation_key]._client_context["message_fingerprints"] == (
        following.message_fingerprints)


@pytest.mark.parametrize("missing", ["first", "following"])
def test_missing_structural_fingerprints_never_enable_incremental_history(tmp_path, missing):
    backend, _, runner = setup(tmp_path)
    first, following = request(FIRST), request(continued(FIRST))
    (first if missing == "first" else following).message_fingerprints = None

    async def scenario():
        await run(runner, first)
        await run(runner, following)

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    assert following.prompt in backend.sent[-1][1] and 'tools=[' in backend.sent[-1][1]
