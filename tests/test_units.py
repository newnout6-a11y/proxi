import pytest

import apierr
import executor
import models
import redact
from context import build_project_context, inside_workspace
from credits import CreditsManager

FENCE = chr(96) * 3


def test_extract_command_prefers_last_tag():
    text = "echo " + chr(60) + "exec_command>old</exec_command> and " + chr(60) + "exec_command>new</exec_command>"
    assert executor.extract_command(text) == "new"


def test_extract_command_strips_fences():
    assert executor.extract_command("<exec_command>" + FENCE + "bash\ngit status\n" + FENCE + "</exec_command>") == "git status"
    assert executor.extract_command("no command") is None


@pytest.mark.parametrize("cmd,allowed", [
    ("git status", True),
    ("git log | findstr fix", True),
    ("echo hi && dir", True),
    ("powershell -c 1", False),
])
def test_policy(cmd, allowed):
    allowlist = ["^git\\b", "^echo\\b", "^dir\\b", "^findstr\\b"]
    if allowed:
        executor.check_policy(cmd, "allowlist", allowlist)
    else:
        with pytest.raises(executor.PolicyError):
            executor.check_policy(cmd, "allowlist", allowlist)


def test_policy_allow_all():
    executor.check_policy("powershell -c 1", "allow_all", [])


def test_run_command_captures_output(tmp_path):
    code, out = executor.run_command("echo hello", str(tmp_path), policy="allow_all")
    assert code == 0 and "hello" in out.lower()


def test_run_command_truncates(tmp_path):
    _, out = executor.run_command("python -c \"print('x'*5000)\"", str(tmp_path),
                                  policy="allow_all", limit=100)
    assert "truncated" in out


def test_redact_masks_tokens_and_cookies():
    text = "accessToken=abcdef123456; sk-abcdefghijklmnopqrst"
    out = redact.redact(text, cookie_values=["abcdef123456"])
    assert "abcdef123456" not in out and "sk-abcdefghijklmnopqrst" not in out


def test_redact_keeps_ordinary_text():
    assert redact.redact("hello world") == "hello world"


def test_sensitive_names():
    assert redact.is_sensitive_path(".env")
    assert not redact.is_sensitive_path("main.py")


def test_context_marks_sensitive_files(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "app.py").write_text("x", encoding="utf-8")
    text = build_project_context(str(tmp_path))
    assert "sensitive" in text and ".env" in text


def test_inside_workspace(tmp_path):
    child = tmp_path / "sub"
    child.mkdir()
    assert inside_workspace(str(child), str(tmp_path))
    assert not inside_workspace(str(tmp_path.parent), str(tmp_path))


def test_effort_from_budget():
    assert models.effort_from_budget(1024) == "low"
    assert models.effort_from_budget(8192) == "medium"
    with pytest.raises(apierr.ApiError):
        models.effort_from_budget(10)


def test_alias_resolution():
    entry, _ = models.resolve("claude-opus-5.5")
    assert entry["id"] == "claude-opus-5-5"


def test_unknown_model_strict_vs_lenient():
    with pytest.raises(apierr.ApiError):
        models.resolve("mystery", strict=True)
    entry, _ = models.resolve("mystery", strict=False)
    assert entry["id"] == "mystery"


def test_credits_dedupe_by_turn():
    manager = CreditsManager("https://x.invalid", lambda: {})
    manager.record_turn("t1", 1.0)
    manager.record_turn("t1", 1.0)
    assert manager.usage()["session_used"] == 1.0
