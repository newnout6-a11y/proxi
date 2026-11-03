"""Verify prompt budgets and latency without depending on network timing."""
import asyncio
import io
import queue
from types import SimpleNamespace

import pytest

import apierr
import context
import executor
import orchestrator
from orchestrator import TurnRunner


def runner(tmp_path, session=None, **overrides):
    cfg = {"workspace_root": str(tmp_path), "project_context": False, "exec_policy": "off",
           "context_max_bytes": 120000, "max_prompt_bytes": 0,
           "openai_provider_resource_name": "provider", "show_exec_output": False,
           "max_exec_turns": 0}
    cfg.update(overrides)

    class Pool:
        calls = 0

        async def acquire(self, conversation_key=None, *, isolated=False, model_entry=None):
            self.calls += 1
            return session

        async def release(self, current):
            pass

    return TurnRunner(cfg, SimpleNamespace(record_turn=lambda *_: None), Pool())


def parsed(prompt="question", tools=None):
    return SimpleNamespace(prompt=prompt, system="client-system", model="gpt-6-sol",
                           entry={"id": "gpt-6-sol", "provider": "openai", "reasoning": True},
                           effort="low", tools=tools or [], tool_choice="auto", conversation_key=None)


TOOL = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]


def test_native_text_reaches_client_before_upstream_turn_completes(tmp_path):
    out = queue.Queue()

    class Session:
        async def stream(self, prompt, provider, tool_names=None):
            yield {"type": "text", "delta": "The file is ready."}
            # This assertion runs before the upstream end/final_text event.
            assert any(e.get("type") == "text" and e["delta"] == "The file is ready."
                       for e in list(out.queue))
            yield {"type": "final_text", "text": "The file is ready."}
            yield {"type": "end", "reason": "stop"}

    asyncio.run(runner(tmp_path, Session()).run(parsed(tools=TOOL), out, {}))
    text = "".join(e["delta"] for e in list(out.queue) if e and e.get("type") == "text")
    assert text == "The file is ready."
    assert not any(e and e.get("type") == "error" for e in list(out.queue))


@pytest.mark.parametrize("chunks", [
    ["<tool_call>", '{"name":"read_file","arguments":{}}', "</tool_call>"],
    ["\n", "<too", "l_call name=\"read_file\">", '{"arguments":{}}', "</tool_", "call>", "\n"],
    ["<exec_", "command>dir", "</exec_command>"],
])
def test_native_envelope_fragments_never_leak_to_text(chunks):
    stream = orchestrator._ClientText()
    text = "".join(stream.feed(chunk) for chunk in chunks) + stream.feed("", final=True)
    assert text == ""


def test_native_text_around_tool_envelopes_remains_streamable():
    stream = orchestrator._ClientText()
    assert stream.feed("Reading it. <too") == "Reading it. "
    assert stream.feed('l_call>{"name":"read_file"}</tool_call>Finished.') == "Finished."


def test_final_snapshot_without_text_deltas_is_delivered_once(tmp_path):
    class Session:
        async def stream(self, *args, **kwargs):
            yield {"type": "final_text", "text": "Final-only answer"}
            yield {"type": "end", "reason": "stop"}

    out = queue.Queue()
    asyncio.run(runner(tmp_path, Session()).run(parsed(tools=TOOL), out, {}))
    assert [e["delta"] for e in list(out.queue) if e and e.get("type") == "text"] == ["Final-only answer"]


def test_malformed_tool_markup_is_an_error_instead_of_an_empty_success(tmp_path):
    class Session:
        async def stream(self, *args, **kwargs):
            yield {"type": "text", "delta": '<tool_call>{"name":"unlisted","arguments":{}}</tool_call>'}
            yield {"type": "end", "reason": "stop"}

    out = queue.Queue()
    asyncio.run(runner(tmp_path, Session()).run(parsed(tools=TOOL), out, {}))
    assert not any(e and e.get("type") == "text" for e in list(out.queue))
    assert next(e["error"] for e in list(out.queue) if e and e.get("type") == "error").status == 502


def test_native_ordinary_refusal_does_not_trigger_an_extra_model_turn(tmp_path):
    class Session:
        calls = 0

        async def stream(self, *args, **kwargs):
            self.calls += 1
            text = "The optional plugin is not available; the requested file is ready."
            yield {"type": "text", "delta": text}
            yield {"type": "final_text", "text": text}
            yield {"type": "end", "reason": "stop"}

    session = Session()
    asyncio.run(runner(tmp_path, session).run(parsed(tools=TOOL), queue.Queue(), {}))
    assert session.calls == 1


def test_unlimited_prompt_preserves_full_native_client_history(tmp_path):
    history = "ж" * 100000 + "LATEST_NATIVE_TOOL_RESULT"

    class Session:
        async def stream(self, prompt, provider, tool_names=None):
            assert history in prompt
            yield {"type": "text", "delta": "done"}
            yield {"type": "end", "reason": "stop"}

    out = queue.Queue()
    asyncio.run(runner(tmp_path, Session()).run(parsed(history, TOOL), out, {}))
    assert not any(e and e.get("type") == "error" for e in list(out.queue))


@pytest.mark.parametrize("recommended,explicit,expected", [
    ("low", None, "low"), ("high", None, "high"), ("high", "low", "low"),
])
def test_auto_reasoning_uses_model_recommendation_without_overriding_client(tmp_path, recommended, explicit, expected):
    providers = []

    class Session:
        async def stream(self, prompt, provider, tool_names=None):
            providers.append(provider)
            yield {"type": "text", "delta": "done"}
            yield {"type": "end", "reason": "stop"}

    request = parsed()
    request.entry["default_reasoning_effort"] = recommended
    request.effort = explicit
    out = queue.Queue()
    asyncio.run(runner(tmp_path, Session(), reasoning_effort="auto").run(request, out, {}))
    assert providers[0]["reasoningEffort"] == expected
    assert not any(e and e.get("type") == "error" for e in list(out.queue))


def test_auto_reasoning_falls_back_to_balanced_supported_level(tmp_path):
    providers = []

    class Session:
        async def stream(self, prompt, provider, tool_names=None):
            providers.append(provider)
            yield {"type": "text", "delta": "done"}
            yield {"type": "end", "reason": "stop"}

    request = parsed()
    request.entry["reasoning_effort_levels"] = ("low", "medium", "high")
    request.effort = None
    asyncio.run(runner(tmp_path, Session(), reasoning_effort="auto").run(request, queue.Queue(), {}))
    assert providers[0]["reasoningEffort"] == "medium"


def test_positive_prompt_limit_rejects_whole_request_before_acquiring_upstream(tmp_path):
    instance = runner(tmp_path, max_prompt_bytes=1000)
    out = queue.Queue()
    asyncio.run(instance.run(parsed("ж" * 2000, TOOL), out, {}))
    error = next(e["error"] for e in list(out.queue) if e and e.get("type") == "error")
    assert error.status == 413
    assert instance.pool.calls == 0


def test_followup_budget_counts_all_fixed_content_and_utf8_bytes(tmp_path):
    instance = runner(tmp_path, max_prompt_bytes=500)
    prompt = instance._followup("instructions", "reminder", "dir", 0, "ж" * 1000, "USER TASK: preserve")
    assert len(prompt.encode("utf-8")) <= 500
    assert "USER TASK: preserve" in prompt
    assert "truncated to configured prompt byte limit" in prompt


def test_followup_never_silently_discards_required_task_or_instructions(tmp_path):
    instance = runner(tmp_path, max_prompt_bytes=200)
    with pytest.raises(apierr.ApiError) as raised:
        instance._followup("instruction" * 100, "reminder", "dir", 0, "output", "USER TASK")
    assert raised.value.status == 413


@pytest.mark.parametrize("step_cap", [0, 2])
def test_summary_turn_rebudgets_only_command_output_to_preserve_prompt_cap(tmp_path, monkeypatch, step_cap):
    prompts = []

    class Session:
        async def stream(self, prompt, provider, **kwargs):
            prompts.append(prompt)
            text = "summary" if "[ИТОГ]" in prompt else "<exec_command>dir</exec_command>"
            yield {"type": "text", "delta": text}
            yield {"type": "final_text", "text": text}
            yield {"type": "end", "reason": "stop"}

    executions = []

    def execute(*args, **kwargs):
        executions.append(args[0])
        return 0, "ж" * 2000

    monkeypatch.setattr(executor, "run_command", execute)
    instance = runner(tmp_path, Session(), max_prompt_bytes=1500, exec_policy="allowlist",
                      max_exec_turns=step_cap, command_timeout=300, command_output_limit=64000,
                      exec_allowlist=[r"^dir\b"])
    out = queue.Queue()
    asyncio.run(instance.run(parsed("REQUIRED ORIGINAL TASK"), out, {}))
    assert len(executions) == (step_cap or 3)
    assert "[ИТОГ]" in prompts[-1]
    assert all(len(prompt.encode("utf-8")) <= 1500 for prompt in prompts)
    assert "REQUIRED ORIGINAL TASK" in prompts[-1]
    assert "client-system" in prompts[-1]
    assert not any(e and e.get("type") == "error" for e in list(out.queue))


def test_zero_prompt_limit_does_not_trim_command_output(tmp_path):
    output = "ж" * 100000
    prompt = runner(tmp_path)._followup("instructions", "reminder", "dir", 0, output, "USER TASK")
    assert output in prompt


def test_project_docs_are_not_silently_clipped_at_1500_characters(tmp_path):
    (tmp_path / "README.md").write_text("x" * 1600 + "IMPORTANT_PROJECT_DETAIL", encoding="utf-8")
    assert "IMPORTANT_PROJECT_DETAIL" in context.build_project_context(str(tmp_path), limit=6000)


def test_context_reuses_unchanged_commit_metadata_but_refreshes_dirty_status(tmp_path, monkeypatch):
    git_dir = tmp_path / ".git"
    ref = git_dir / "refs" / "heads" / "main"
    ref.parent.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    ref.write_text("first", encoding="utf-8")
    calls = []

    def git(workspace, *args):
        calls.append(args)
        if args[0] == "status":
            return "## main\n M file-v" + str(sum(c[0] == "status" for c in calls))
        return ref.read_text() + " commit"

    monkeypatch.setattr(context, "_git", git)
    first = context.build_project_context(str(tmp_path))
    second = context.build_project_context(str(tmp_path))
    assert "file-v1" in first and "file-v2" in second
    assert [call[0] for call in calls] == ["status", "log", "status"]
    ref.write_text("second-new-ref", encoding="utf-8")
    third = context.build_project_context(str(tmp_path))
    assert "second-new-ref commit" in third
    assert [call[0] for call in calls] == ["status", "log", "status", "status", "log"]


def test_command_output_zero_limit_retains_complete_output(tmp_path, monkeypatch):
    output = b"x" * 100000

    class Process:
        stdout, stderr = io.BytesIO(output), io.BytesIO()
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: Process())
    code, text = executor.run_command("echo example", str(tmp_path), policy="allow_all", limit=0)
    assert code == 0
    assert text == output.decode()


def test_bounded_command_output_keeps_the_configured_number_of_utf8_characters(tmp_path, monkeypatch):
    output = ("😀" * 1000).encode("utf-8")

    class Process:
        stdout, stderr = io.BytesIO(output), io.BytesIO()
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: Process())
    code, text = executor.run_command("echo example", str(tmp_path), policy="allow_all", limit=1000)
    assert code == 0
    assert text == "😀" * 1000


def test_truncated_utf8_capture_does_not_switch_the_valid_prefix_to_cp866(tmp_path, monkeypatch):
    class Process:
        stdout, stderr = io.BytesIO(("€" * 3000).encode("utf-8")), io.BytesIO()
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: Process())
    code, text = executor.run_command("echo example", str(tmp_path), policy="allow_all", limit=1000)
    assert code == 0
    assert text.startswith("€" * 1000)
    assert "truncated" in text


def test_prose_mentioning_exec_command_does_not_fail(tmp_path):
    class Session:
        async def stream(self, *args, **kwargs):
            yield {"type": "text", "delta": "Here is the code. Note: `<exec_command>` should not be used directly."}
            yield {"type": "end", "reason": "stop"}

    out = queue.Queue()
    asyncio.run(runner(tmp_path, Session()).run(parsed(tools=TOOL), out, {}))
    items = list(out.queue)
    assert not any(e and e.get("type") == "error" for e in items)
    deltas = [e["delta"] for e in items if e and e.get("type") == "text"]
    assert "`<exec_command>`" in "".join(deltas)


def test_prose_mentioning_tool_call_does_not_fail(tmp_path):
    class Session:
        async def stream(self, *args, **kwargs):
            yield {"type": "text", "delta": "Here is an example: `<tool_call>` should have valid JSON."}
            yield {"type": "end", "reason": "stop"}

    out = queue.Queue()
    asyncio.run(runner(tmp_path, Session()).run(parsed(tools=TOOL), out, {}))
    items = list(out.queue)
    assert not any(e and e.get("type") == "error" for e in items)
    deltas = [e["delta"] for e in items if e and e.get("type") == "text"]
    assert "`<tool_call>`" in "".join(deltas)
