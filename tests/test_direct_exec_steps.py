"""On the direct transport, a relay exec step adds to the body only the reply that asked for its command and the result.

The follow-up of a step is the agent transport's, where it replaces the whole prompt: it starts with "USER TASK:" and the
client's conversation. The direct body keeps that conversation as the client's own messages (client_chunks), with every
earlier step after them, so each follow-up chunk brought the conversation once more: after three commands the body held it
four times, and each step wrote another full copy of it to the provider cache (a 110 KB conversation went upstream as
440 KB in the fourth request)."""
import asyncio
import json
import queue

import direct
import executor
import models
from orchestrator import TurnRunner
from test_native_context_reuse import request, run, setup
from test_review_fixes_pr19 import END, response, text

CONVERSATION = [{"role": "user", "content": "Inspect ORIGINAL_TASK."},
                {"role": "assistant", "content": "Which folder?"},
                {"role": "user", "content": "The LATEST_FOLDER, please."}]


def exec_turn(tmp_path, monkeypatch, steps, reply="Listing it.\n<exec_command>dir step%d</exec_command>", **cfg):
    """(the bodies a relay task, no client tools and no system, posts upstream on the direct transport, in order; its parse)
    when its model asks for `steps` commands, each with `reply`, and then answers."""
    backend, _, runner = setup(tmp_path, **{
        "transport": "direct", "base": "https://example.invalid", "project_context": False, "cookie_values": [],
        "cache_keepwarm_seconds": 0, "upstream_error_dir": str(tmp_path / "diag"), "exec_policy": "allow_all",
        "exec_allowlist": [], "command_timeout": 10, "command_output_limit": 4000, "show_exec_output": False, **cfg})
    backend.headers = lambda: {}
    bodies = []

    def opener(req, **_kw):
        bodies.append(json.loads(req.data))
        step = len(bodies)
        return response(text(reply % step if step <= steps else "The folder holds a.txt."), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    monkeypatch.setattr(executor, "run_command", lambda command, *a, **kw: (0, "a.txt"))
    parsed = request(CONVERSATION, tools=[], system="")
    events = asyncio.run(run(runner, parsed))
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert len(bodies) == steps + 1
    return bodies, parsed


def messages(body):
    """What upstream reads of a body: each message's role and texts, without the relay's cache marks."""
    return [(message["role"], [block["text"] for block in message["content"]]) for message in body["messages"]]


def test_each_direct_exec_step_sends_the_client_conversation_once(tmp_path, monkeypatch):
    for body in exec_turn(tmp_path, monkeypatch, steps=3)[0]:
        sent = json.dumps(body["messages"])
        assert sent.count("ORIGINAL_TASK") == 1 and sent.count("LATEST_FOLDER") == 1


def test_a_direct_exec_step_adds_its_reply_and_result_to_the_end_of_the_body_before_it(tmp_path, monkeypatch):
    """A request that repeats the one before it and adds to its end reads what that one wrote to the cache: the relay marks
    the end of the request before again (direct.build_body), and only the new messages are written."""
    bodies, _ = exec_turn(tmp_path, monkeypatch, steps=3)
    for step, (before, after) in enumerate(zip(bodies, bodies[1:]), start=1):
        kept = len(before["messages"])
        assert messages(after)[:kept] == messages(before)
        assert messages(after)[kept:] == [
            ("user", ["Assistant: Listing it.\n<exec_command>dir step%d</exec_command>" % step]),
            ("user", [executor.format_command_followup("dir step%d" % step, 0, "a.txt")])]
        assert after["messages"][kept - 1]["content"][-1].get("cache") is True


def test_the_reply_a_direct_exec_step_sends_back_is_masked_like_its_command(tmp_path, monkeypatch):
    bodies, _ = exec_turn(tmp_path, monkeypatch, steps=1, cookie_values=["SECRET_COOKIE_VALUE"],
                          reply="With the session: <exec_command>dir SECRET_COOKIE_VALUE %d</exec_command>")
    assert messages(bodies[1])[-2] == ("user", ["Assistant: With the session: <exec_command>dir [redacted] 1</exec_command>"])
    assert "SECRET_COOKIE_VALUE" not in json.dumps(bodies[1])


def test_the_token_estimate_of_a_direct_exec_step_reads_the_body_the_step_sends(tmp_path, monkeypatch):
    """A reply whose upstream reports no input tokens gives the client the relay's estimate (parsed._full_prompt_text). A
    follow-up step's prompt held the conversation only through the task text it no longer carries: the estimate of a step
    reads the body that step sends."""
    bodies, parsed = exec_turn(tmp_path, monkeypatch, steps=2)
    assert parsed._full_prompt_text == "\n".join(text for _, texts in messages(bodies[-1]) for text in texts)


def test_the_agent_transport_follow_up_still_carries_the_task_and_the_earlier_steps(tmp_path, monkeypatch):
    """The agent transport sends one prompt per step and nothing of the steps before it: its follow-up keeps the task text
    once and the trail of the earlier commands."""
    class Session:  # no set_direct: the prompts of the agent transport
        thread_id = "t1"

        def __init__(self):
            self.prompts = []

        async def stream(self, prompt, model_provider):
            self.prompts.append(prompt)
            step = len(self.prompts)
            yield {"type": "text", "delta": "<exec_command>dir step%d</exec_command>" % step if step <= 2 else "done"}
            yield {"type": "end", "reason": "stop"}

    class Pool:
        async def acquire(self, conversation_key=None, **_kw):
            return session

        async def release(self, _session):
            return None

    monkeypatch.setattr(executor, "run_command", lambda command, *a, **kw: (0, "a.txt"))
    session = Session()
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "allow_all", "exec_allowlist": [],
           "max_exec_turns": 0, "command_timeout": 5, "command_output_limit": 500, "show_exec_output": False,
           "cookie_values": [], "context_max_bytes": 300, "provider_resource_name": "a",
           "openai_provider_resource_name": "o"}
    runner = TurnRunner(cfg, type("C", (), {"record_turn": lambda *a: None})(), Pool())
    entry, effort = models.resolve("claude-opus-5-5", None)
    parsed = type("P", (), {})()
    parsed.prompt, parsed.system, parsed.model, parsed.entry, parsed.effort = "ORIGINAL_TASK", "", entry["id"], entry, effort
    asyncio.run(runner.run(parsed, queue.Queue(), {}))
    assert len(session.prompts) == 3
    assert session.prompts[2].count("ORIGINAL_TASK") == 1
    assert "USER TASK:\nORIGINAL_TASK" in session.prompts[2]
    assert "Previous steps: dir step1 -> exit 0" in session.prompts[2]
