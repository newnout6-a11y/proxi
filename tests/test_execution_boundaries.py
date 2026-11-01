"""Regression probes for the local execution and project-context trust boundary.

These tests describe the allowlist contract: the default policy must not be a
shell escape hatch, and project documentation must stay within the workspace.
The implementation fixes belong in executor.py/context.py/redact.py.
"""
import json
import os
import subprocess
import threading

import pytest

import executor
import config
from context import build_project_context
from redact import redact


@pytest.mark.parametrize("command", [
    # A single quote is a PowerShell quote, but cmd.exe treats it as ordinary
    # text. It therefore cannot make a separator safe for shell=True.
    "echo 'visible & del /q /s C:\\sensitive'",
    "echo 'visible | whoami'",
])
def test_single_quote_cannot_hide_cmd_separator(command):
    with pytest.raises(executor.PolicyError):
        executor.check_policy(command, "allowlist", [r"^echo\b"])


@pytest.mark.parametrize("command", [
    "powershell -File evil.ps1",
    "powershell -EncodedCommand SQBFAFgA",
    "pwsh -e SQBFAFgA",
    "cmd /c evil.bat",
])
def test_interpreter_file_and_encoded_modes_are_denied(command):
    with pytest.raises(executor.PolicyError):
        executor.check_policy(command, "allowlist", [r"^powershell\b", r"^pwsh\b", r"^cmd\b"])


@pytest.mark.parametrize("command", [
    'python -c "print(1)"', 'node -e "console.log(1)"', 'pip install example',
    'curl https://example.invalid', 'npm run start',
])
def test_default_policy_does_not_approve_interpreters_or_downloaders(command):
    with pytest.raises(executor.PolicyError):
        executor.check_policy(command, "allowlist", config.DEFAULTS["exec_allowlist"])


def test_example_policy_does_not_approve_python(tmp_path):
    with open(os.path.join(os.path.dirname(executor.__file__), "config.example.json"), encoding="utf-8") as handle:
        example = json.load(handle)
    with pytest.raises(executor.PolicyError):
        executor.check_policy('python -c "print(1)"', example["exec_policy"], example["exec_allowlist"])


@pytest.mark.parametrize("body", [
    "Write-Output (Start-Process calc)",
    "Write-Output ([System.IO.File]::WriteAllText('outside.txt','secret'))",
    "Write-Output $($env:COMSPEC)",
    "$x = (Get-Content secret.txt)",
    "foreach ($x in (Get-ChildItem)) { Write-Output $x }",
])
def test_powershell_nested_expressions_are_denied(body):
    with pytest.raises(executor.PolicyError):
        executor.check_policy('powershell -NoProfile -Command "' + body + '"', "allowlist",
                              [r"^powershell\b"])


@pytest.mark.parametrize("command", [
    "git -c core.sshCommand=calc status",
    "git -c core.fsmonitor=calc status",
    "git -c credential.helper=calc fetch origin",
    "git --exec-path=C:/untrusted status",
])
def test_git_helper_configuration_cannot_escape_allowlist(command):
    with pytest.raises(executor.PolicyError):
        executor.check_policy(command, "allowlist", [r"^git\b"])


@pytest.mark.parametrize("secret", [
    'refresh_token="correct horse battery staple"',
    'client_secret: "correct horse battery staple"',
    '{"refresh_token": "correct horse battery staple"}',
])
def test_redaction_masks_quoted_secret_assignments(secret):
    masked = redact(secret)
    assert "correct horse battery staple" not in masked
    assert "[redacted]" in masked


@pytest.mark.parametrize("name", ["refresh_token", "client_secret", "oauth-token", "API_KEY"])
def test_redaction_masks_named_compound_assignments(name):
    value = "abcdefghi1234567890"
    assert value not in redact(name + "=" + value)


def test_symlinked_project_document_outside_workspace_is_not_read(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside-secret.txt")
    outside.write_text("OUTSIDE_PRIVATE_CONTENT", encoding="utf-8")
    try:
        os.symlink(outside, tmp_path / "README.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    context = build_project_context(str(tmp_path), limit=6000)
    assert "OUTSIDE_PRIVATE_CONTENT" not in context
    assert "README.md points outside workspace" in context


class _FakeStream:
    def read(self, _size):
        return b""


class _FakeProcess:
    pid = 31337
    returncode = 0
    stdout = _FakeStream()
    stderr = _FakeStream()

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def test_cancelled_before_spawn_has_no_process_side_effect(monkeypatch, tmp_path):
    event = threading.Event()
    event.set()
    spawned = []

    def popen(*args, **kwargs):
        spawned.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)
    code, output = executor.run_command("echo should-not-run", str(tmp_path),
                                       policy="allowlist", allowlist=[r"^echo\b"],
                                       cancel_event=event)
    assert spawned == []
    assert code == -1
    assert "cancel" in output.lower()


def test_cancelled_running_process_kills_the_tree(monkeypatch, tmp_path):
    event = threading.Event()
    process = _FakeProcess()
    process.returncode = None
    checks = {"poll": 0}

    def poll():
        checks["poll"] += 1
        if checks["poll"] > 1:
            process.returncode = -9
            return process.returncode
        event.set()
        return None

    process.poll = poll
    killed = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(executor, "_kill_tree", lambda pid: killed.append(pid))
    code, output = executor.run_command("echo should-cancel", str(tmp_path),
                                       policy="allowlist", allowlist=[r"^echo\b"],
                                       timeout=2, cancel_event=event)
    assert killed == [31337]
    assert code == -1
    assert "cancelled" in output
