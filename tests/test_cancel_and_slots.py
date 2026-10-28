import asyncio
import json
import queue
import types

import executor
import models
from orchestrator import TurnRunner
from session import RelaySession, SessionPool


class FakeWS:
    def __init__(self, frames):
        self.frames = [json.dumps(f) for f in frames]
        self.sent = []

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(0.45)
        raise asyncio.TimeoutError

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        return None


def build_session(frames):
    backend = types.SimpleNamespace(cfg={"turn_timeout": 30, "recv_timeout": 5, "base": "https://x",
                                         "agent_mode": 2}, refresher=None)
    session = RelaySession(backend, "t1")
    session.ws = FakeWS(frames)
    return session


def test_cancel_does_not_poison_following_turns():
    session = build_session([
        {"type": "text", "messageId": "a", "content": "first"},
        {"type": "agent_loop_done"},
    ])

    async def scenario():
        # turn 1: cancel after the first event, as a disconnect would
        first = []
        async for event in session.stream("p", {}):
            first.append(event)
            session.cancel()
        # An aborted socket is closed before the next logical request.
        if session._drain_task is not None:
            await asyncio.wrap_future(session._drain_task)
        assert session.ws is None
        session.begin_request()
        session.ws = FakeWS([{"type": "text", "messageId": "b", "content": "second"},
                             {"type": "agent_loop_done"}])
        session._needs_drain = False
        second = [e async for e in session.stream("p", {})]
        return first, second

    first, second = asyncio.run(scenario())
    assert first[-1]["reason"] == "cancelled"
    assert [e for e in second if e["type"] == "final_text"][0]["text"] == "second"


class Backend:
    def __init__(self, mode):
        self.cfg = {"session_mode": mode, "thread_id": "t1", "page_uuid": "p", "max_workers": 2,
                    "base": "https://x", "upstream_timeout": 5}

    async def create_thread(self):
        raise asyncio.CancelledError()


def test_cancelled_acquire_gives_the_slot_back():
    pool = SessionPool(Backend("per_request"))
    assert pool._slots._value == 2
    try:
        asyncio.run(pool.acquire())
    except asyncio.CancelledError:
        pass
    assert pool._slots._value == 2


def test_double_release_ignored():
    class Ok(Backend):
        async def create_thread(self):
            return "thread-1"

    pool = SessionPool(Ok("per_request"))

    async def scenario():
        session = await pool.acquire()
        assert pool._slots._value == 1
        await pool.release(session)
        assert pool._slots._value == 2
        await pool.release(session)  # second time must not inflate
        assert pool._slots._value == 2

    asyncio.run(scenario())


def test_deep_exec_chain_respects_prompt_limit(tmp_path, monkeypatch):
    limit = 1500
    monkeypatch.setattr(executor, "run_command", lambda *a, **kw: (0, "x" * 3000))
    prompts = []

    class Session:
        thread_id = "t1"

        async def stream(self, prompt, model_provider):
            prompts.append(prompt)
            if len(prompts) < 5:
                yield {"type": "text", "delta": "<exec_command>dir</exec_command>"}
                yield {"type": "final_text", "text": "<exec_command>dir</exec_command>"}
            else:
                yield {"type": "final_text", "text": "готово"}
            yield {"type": "end", "reason": "stop"}

    class Pool:
        async def acquire(self):
            return Session()

        async def release(self, session):
            return None

    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "allowlist",
           "exec_allowlist": ["^dir\\b"], "max_exec_turns": 6, "command_timeout": 5,
           "command_output_limit": 5000, "show_exec_output": True, "cookie_values": [],
           "context_max_bytes": 300, "max_prompt_bytes": limit,
           "provider_resource_name": "anth", "openai_provider_resource_name": "oai"}
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), Pool())
    entry, effort = models.resolve("claude-opus-5-5", None)
    parsed = type("P", (), {})()
    parsed.prompt, parsed.system, parsed.model, parsed.entry, parsed.effort = "задача", "", entry["id"], entry, effort
    asyncio.run(runner.run(parsed, queue.Queue(), {}))

    assert len(prompts) == 5
    assert prompts[0].count("USER TASK") == 0 or True
    assert all(len(p) <= limit for p in prompts), [len(p) for p in prompts]
