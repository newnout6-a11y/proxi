import asyncio
import queue

import executor
import models
from orchestrator import TurnRunner


class StubbornSession:
    """Always answers with a command, even after the summary request."""

    thread_id = "t1"

    def __init__(self):
        self.prompts = []

    async def stream(self, prompt, model_provider):
        self.prompts.append(prompt)
        yield {"type": "text", "delta": "<exec_command>dir</exec_command>"}
        yield {"type": "final_text", "text": "<exec_command>dir</exec_command>"}
        yield {"type": "end", "reason": "stop"}


class Pool:
    def __init__(self, session):
        self.session = session

    async def acquire(self, conversation_key=None, *, isolated=False, model_entry=None):
        return self.session

    async def release(self, session):
        return None


def run(session, tmp_path, monkeypatch, **overrides):
    executions = []

    def fake_run(cmd, *a, **kw):
        executions.append(cmd)
        return 0, "same"

    monkeypatch.setattr(executor, "run_command", fake_run)
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "allowlist",
           "exec_allowlist": ["^dir\\b"], "max_exec_turns": 0, "command_timeout": 5,
           "command_output_limit": 500, "show_exec_output": False, "cookie_values": [],
           "context_max_bytes": 300, "max_prompt_bytes": 5000,
           "provider_resource_name": "a", "openai_provider_resource_name": "o"}
    cfg.update(overrides)
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), Pool(session))
    entry, effort = models.resolve("claude-opus-5-5", None)
    parsed = type("P", (), {})()
    parsed.prompt, parsed.system, parsed.model, parsed.entry, parsed.effort = "задача", "", entry["id"], entry, effort
    asyncio.run(runner.run(parsed, queue.Queue(), {}))
    return executions


def test_loop_guard_stops_execution_even_for_a_stubborn_model(tmp_path, monkeypatch):
    session = StubbornSession()
    executions = run(session, tmp_path, monkeypatch)

    assert len(executions) == 3, "the loop must stop after three identical runs"
    assert "[ИТОГ]" in session.prompts[-1]
    # turns: 3 executed + the turn whose command triggered the guard + the summary
    assert len(session.prompts) == 5


def test_loop_guard_ignores_case_and_spacing(tmp_path, monkeypatch):
    class Mixed(StubbornSession):
        async def stream(self, prompt, model_provider):
            self.prompts.append(prompt)
            cmd = ["DIR", " dir ", "Dir", "DIR"][min(len(self.prompts) - 1, 3)]
            text = "<exec_command>" + cmd + "</exec_command>"
            yield {"type": "final_text", "text": text}
            yield {"type": "end", "reason": "stop"}

    session = Mixed()
    executions = run(session, tmp_path, monkeypatch)
    assert len(executions) == 3
    assert "[ИТОГ]" in session.prompts[-1]


def test_explicit_step_cap_still_works(tmp_path, monkeypatch):
    session = StubbornSession()
    executions = run(session, tmp_path, monkeypatch, max_exec_turns=2)
    assert len(executions) == 2
    assert "[ИТОГ]" in session.prompts[-1]
