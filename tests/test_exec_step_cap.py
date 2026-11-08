"""At the max_exec_turns cap, a reply that asks for no other command is the answer, and the turn ends with it.

The reply after the last allowed command went to the cap's summary branch whatever it held: the relay asked upstream again
with "[ИТОГ] ...", a paid request whose answer was streamed after the one the client already had, so the client got two
answers in one reply. Only a reply that asks for another command needs that summary."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import direct
import executor
import models
from orchestrator import TurnRunner
from test_native_context_reuse import FIRST, request, run, setup
from test_review_fixes_pr19 import END, response, text

COMMAND, ANSWER = "<exec_command>dir</exec_command>", "The folder holds a.txt."


class AgentSession:
    """The agent transport: one prompt per step, its reply priced like an upstream one."""

    thread_id = "t1"

    def __init__(self, replies):
        self.replies, self.prompts = replies, []

    async def stream(self, prompt, model_provider):
        self.prompts.append(prompt)
        reply = self.replies[len(self.prompts) - 1]
        yield {"type": "text", "delta": reply}
        yield {"type": "final_text", "text": reply}
        yield {"type": "credits", "value": 0.25, "usage": {"input_tokens": 1000, "output_tokens": 10}}
        yield {"type": "end", "reason": "stop"}


class Pool:
    def __init__(self, session):
        self.session = session

    async def acquire(self, conversation_key=None, **_kw):
        return self.session

    async def release(self, _session):
        return None


def agent_turn(tmp_path, monkeypatch, replies, cap):
    """(the prompts sent upstream, the credits recorded, the commands run, the events) of a relay task on the agent transport."""
    commands, paid = [], []
    monkeypatch.setattr(executor, "run_command", lambda command, *a, **kw: (commands.append(command) or 0, "a.txt"))
    session = AgentSession(replies)
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "allow_all", "exec_allowlist": [],
           "max_exec_turns": cap, "command_timeout": 5, "command_output_limit": 500, "show_exec_output": False,
           "cookie_values": [], "context_max_bytes": 300, "provider_resource_name": "a",
           "openai_provider_resource_name": "o"}
    runner = TurnRunner(cfg, SimpleNamespace(record_turn=lambda turn_id, value: paid.append(value)), Pool(session))
    entry, effort = models.resolve("claude-opus-5-5", None)
    parsed = SimpleNamespace(prompt="User: list the folder", system="", model=entry["id"], entry=entry, effort=effort)
    events = asyncio.run(run(runner, parsed))
    return session.prompts, paid, commands, events


def direct_turn(tmp_path, monkeypatch, replies, cap):
    """(the bodies sent upstream, the credits recorded, the commands run, the events) of a relay task on the direct
    transport."""
    backend, _, runner = setup(tmp_path, transport="direct", base="https://example.invalid", project_context=False,
                               cookie_values=[], cache_keepwarm_seconds=0, upstream_error_dir=str(tmp_path / "diag"),
                               exec_policy="allow_all", exec_allowlist=[], command_timeout=10,
                               command_output_limit=4000, show_exec_output=False, max_exec_turns=cap)
    backend.headers = lambda: {}
    bodies, commands, paid = [], [], []
    runner.credits = SimpleNamespace(record_turn=lambda turn_id, value: paid.append(value))

    def opener(req, **_kw):
        bodies.append(json.loads(req.data))
        return response(text(replies[len(bodies) - 1]), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    monkeypatch.setattr(executor, "run_command", lambda command, *a, **kw: (commands.append(command) or 0, "a.txt"))
    events = asyncio.run(run(runner, request(FIRST, tools=[], system="")))
    return bodies, paid, commands, events


def client_text(events):
    return "".join(event["delta"] for event in events if event["type"] == "text")


@pytest.mark.parametrize("turn", [agent_turn, direct_turn], ids=["agent", "direct"])
def test_at_the_step_cap_an_answer_without_a_command_ends_the_turn(tmp_path, monkeypatch, turn):
    sent, paid, commands, events = turn(tmp_path, monkeypatch, [COMMAND, ANSWER, "A SECOND ANSWER"], cap=1)
    assert commands == ["dir"]
    assert len(sent) == len(paid) == 2
    assert "[ИТОГ]" not in json.dumps(sent, ensure_ascii=False)
    assert client_text(events) == COMMAND + ANSWER
    assert events[-1] == {"type": "end", "reason": "stop"}


@pytest.mark.parametrize("turn", [agent_turn, direct_turn], ids=["agent", "direct"])
def test_at_the_step_cap_a_reply_that_asks_for_another_command_gets_the_summary(tmp_path, monkeypatch, turn):
    sent, paid, commands, events = turn(tmp_path, monkeypatch, [COMMAND, "<exec_command>dir again</exec_command>", ANSWER],
                                        cap=1)
    assert commands == ["dir"]
    assert len(sent) == len(paid) == 3
    assert "[ИТОГ]" in json.dumps(sent[-1], ensure_ascii=False)
    assert client_text(events).endswith(ANSWER)
    assert events[-1] == {"type": "end", "reason": "stop"}


@pytest.mark.parametrize("turn, replies", [(agent_turn, [COMMAND, "", ANSWER]), (direct_turn, [COMMAND, "", "", ANSWER])],
                         ids=["agent", "direct"])
def test_at_the_step_cap_an_empty_reply_still_gets_the_summary(tmp_path, monkeypatch, turn, replies):
    """An empty reply is no answer (the direct transport has asked once more already): at the cap the relay asks for the
    summary, as it did before."""
    sent, _, commands, events = turn(tmp_path, monkeypatch, replies, cap=1)
    assert commands == ["dir"]
    assert len(sent) == len(replies)
    assert "[ИТОГ]" in json.dumps(sent[-1], ensure_ascii=False)
    assert client_text(events) == COMMAND + ANSWER
    assert events[-1] == {"type": "end", "reason": "stop"}
