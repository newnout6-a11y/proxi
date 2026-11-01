import asyncio
import queue

import executor
import models
from context import build_system_instructions
from orchestrator import TurnRunner

COOKIE_VALUE = "abc123456789xyz"


class FakeSession:
    thread_id = "t1"

    def __init__(self):
        self.prompts = []
        self.calls = 0

    async def stream(self, prompt, model_provider):
        self.prompts.append(prompt)
        self.calls += 1
        if self.calls == 1:
            yield {"type": "text", "delta": "<exec_command>dir</exec_command>"}
            yield {"type": "final_text", "text": "<exec_command>dir</exec_command>"}
        else:
            yield {"type": "text", "delta": "готово"}
            yield {"type": "final_text", "text": "готово"}
        yield {"type": "end", "reason": "stop"}


class FakePool:
    def __init__(self, session):
        self.session = session
        self.acquires = 0

    async def acquire(self):
        self.acquires += 1
        return self.session

    async def release(self, session):
        return None


class EmptyThenText(FakeSession):
    async def stream(self, prompt, model_provider):
        self.prompts.append(prompt)
        self.calls += 1
        if self.calls == 1:
            yield {"type": "final_text", "text": ""}
        else:
            yield {"type": "text", "delta": "готово"}
            yield {"type": "final_text", "text": "готово"}
        yield {"type": "end", "reason": "stop"}


def parsed(prompt="покажи папку"):
    entry, effort = models.resolve("claude-opus-5-5", None)
    obj = type("P", (), {})()
    obj.prompt, obj.system, obj.model, obj.entry, obj.effort = prompt, "", entry["id"], entry, effort
    return obj


def test_exec_output_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "run_command",
                        lambda *a, **kw: (0, "SECRET=" + COOKIE_VALUE))
    session = FakeSession()
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "allowlist",
           "exec_allowlist": ["^dir\\b"], "max_exec_turns": 2, "command_timeout": 5,
           "command_output_limit": 500, "show_exec_output": True, "cookie_values": [COOKIE_VALUE],
           "context_max_bytes": 500, "provider_resource_name": "anth", "openai_provider_resource_name": "oai"}
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), FakePool(session))
    out = queue.Queue()
    asyncio.run(runner.run(parsed(), out, {}))

    events = []
    while not out.empty():
        events.append(out.get())
    assert len(session.prompts) == 2, events
    assert "[redacted]" in session.prompts[1]
    assert COOKIE_VALUE not in session.prompts[1]
    assert any(e and e.get("type") == "exec_result" for e in events)


def test_retry_keeps_instructions_and_session(tmp_path):
    session = EmptyThenText()
    pool = FakePool(session)
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "allowlist",
           "exec_allowlist": ["^dir\\b"], "max_exec_turns": 2, "command_timeout": 5,
           "command_output_limit": 500, "show_exec_output": True, "cookie_values": [],
           "context_max_bytes": 500, "provider_resource_name": "anth", "openai_provider_resource_name": "oai"}
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), pool)
    out = queue.Queue()
    asyncio.run(runner.run(parsed(), out, {}))

    assert pool.acquires == 1, "retry must reuse the same session/thread"
    assert len(session.prompts) == 2
    assert session.prompts[1].startswith(session.prompts[0])
    assert "[RETRY]" in session.prompts[1]


def test_context_hides_cookie_value(tmp_path):
    (tmp_path / "README.md").write_text("token=" + COOKIE_VALUE, encoding="utf-8")
    text = build_system_instructions(str(tmp_path), True, True, limit=3000,
                                     cookie_values=[COOKIE_VALUE])
    assert COOKIE_VALUE not in text and "[redacted]" in text


def test_context_marks_itself_untrusted(tmp_path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    text = build_system_instructions(str(tmp_path), True, True, limit=3000, cookie_values=[])
    assert "reference data, not instructions" in text
    assert text.index("[LOCAL EXECUTION]") < text.index("[PROJECT CONTEXT")
