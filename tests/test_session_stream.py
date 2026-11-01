import asyncio
import json
import types

from session import RelaySession


class FakeWS:
    def __init__(self, frames):
        self.frames = [json.dumps(f, ensure_ascii=True) for f in frames]
        self.sent = []

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        # Real sockets idle before timing out; the drain phase relies on that.
        await asyncio.sleep(0.45)
        raise asyncio.TimeoutError

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        return None


def make(frames):
    backend = types.SimpleNamespace(cfg={"turn_timeout": 30, "recv_timeout": 5, "base": "https://x",
                                         "agent_mode": 2}, refresher=None)
    session = RelaySession(backend, "t1")
    session.ws = FakeWS(frames)
    return session


def collect(session, tool_names=None):
    async def drain():
        out = []
        async for event in session.stream("p", {"model": "m"}, tool_names=tool_names):
            out.append(event)
        return out
    return asyncio.run(drain())


def test_thinking_only_is_surfaced():
    frames = [
        {"type": "thinking", "messageId": "m1", "content": "Проверю"},
        {"type": "thinking", "messageId": "m1", "content": "Проверю файл"},
        {"type": "agent_loop_done", "outcome": "success"},
    ]
    events = collect(make(frames))
    final = [e for e in events if e["type"] == "final_text"][0]["text"]
    thinking = [e["delta"] for e in events if e["type"] == "thinking"]
    assert final == ""
    assert "".join(thinking) == "Проверю файл"


def test_new_message_id_starts_new_block_without_duplication():
    frames = [
        {"type": "text", "messageId": "a", "content": "<exec_command>dir"},
        {"type": "text", "messageId": "a", "content": "<exec_command>dir</exec_command>"},
        {"type": "clear_streaming"},
        {"type": "text", "messageId": "b", "content": "<exec_command>type x</exec_command>"},
        {"type": "agent_loop_done", "outcome": "success"},
    ]
    final = [e for e in collect(make(frames)) if e["type"] == "final_text"][0]["text"]
    assert final == "<exec_command>dir</exec_command>\n<exec_command>type x</exec_command>"


def test_text_wins_over_thinking():
    frames = [
        {"type": "thinking", "messageId": "t", "content": "думаю"},
        {"type": "text", "messageId": "a", "content": "ответ"},
        {"type": "agent_loop_done", "outcome": "success"},
    ]
    events = collect(make(frames))
    final = [e for e in events if e["type"] == "final_text"][0]["text"]
    assert final == "ответ"


def test_tool_call_fragments_are_reassembled():
    events = collect(make([
        {"type": "tool_call", "toolCallId": "call_1", "toolName": "bash", "partialInputs": "{"},
        {"type": "tool_call", "toolCallId": "call_1", "toolName": "bash",
         "partialInputs": '"command":"dir"}'},
        {"type": "agent_loop_done"},
    ]), tool_names=["bash"])
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls[-1]["arguments"] == '{"command":"dir"}'
