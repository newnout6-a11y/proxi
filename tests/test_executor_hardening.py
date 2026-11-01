"""Executor allowlist hardening: git bypasses, preprocessing flags, audit redaction."""
import json

import pytest

import executor
from executor import PolicyError, check_policy, clean_env, split_segments

DEFAULTS = [
    r"^dir\b", r"^type\b", r"^git\b", r"^ping\b", r"^more\b", r"^findstr\b", r"^where\b",
    r"^rg\b", r"^powershell\b", r"^pwsh\b", r"^cmd\b", r"^echo\b", r"^cls\b",
    r"^Get-Content\b", r"^Get-ChildItem\b", r"^Select-String\b",
]


def _deny(cmd):
    with pytest.raises(PolicyError):
        check_policy(cmd, "allowlist", DEFAULTS)


def _allow(cmd):
    check_policy(cmd, "allowlist", DEFAULTS)


@pytest.mark.parametrize("cmd", [
    "git -ccore.pager=calc log",
    'git "-c" core.fsmonitor=calc status',
    "git -C/elsewhere/.git log",
    "git fetch --upload-pack=calc origin",
    "git push --receive-pack=calc origin main",
    'git clone "ext::calc.exe"',
    "git bisect run calc",
    "git filter-branch --msg-filter calc HEAD",
    "git difftool --extcmd calc HEAD~ HEAD",
    "git apply evil.patch",
    "git am evil.mbox",
    "git fast-import",
    "git daemon --export-all --port=9418",
    "git --exec-path=C:/evil upload-pack x",
    "@git -c x=y log",
])
def test_git_bypasses_are_denied(cmd):
    _deny(cmd)


@pytest.mark.parametrize("cmd", [
    "rg --pre calc pattern .",
    "rg --pre=calc pattern .",
])
def test_preprocessor_flags_are_denied(cmd):
    _deny(cmd)


@pytest.mark.parametrize("cmd", [
    "git status",
    "git log --pretty=oneline",
    "git diff --color",
    "git push -u origin main",
    "git fetch origin",
    "rg pattern .",
])
def test_legitimate_git_and_rg_still_allowed(cmd):
    _allow(cmd)


def test_head_match_contract():
    """The allowlist matches the command head (README contract), not any
    substring of the segment: `calc` mentioned mid-segment cannot smuggle a
    different head through."""
    _deny("echo calc & calc")  # second segment head is calc
    _allow("git status")


def test_clean_env_blocks_current_directory_exe_shadowing():
    env = clean_env(r"C:\workspace")
    assert env.get("NoDefaultCurrentDirectoryInExePath") == "1"


def test_audit_redacts_command_and_records_error(tmp_path):
    path = tmp_path / "commands.log"
    secret_cmd = 'git remote add origin https://user:ghp_Abcdefghijklmnopqrst@github.com/x/y.git'
    executor._audit(str(path), secret_cmd, -1, 0.0, 0, error="WinError 2")
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "ghp_Abcdefghijklmnopqrst" not in record["cmd"], "raw secret must not reach commands.log"
    assert record["error"] == "WinError 2"


def test_split_segments_still_uses_quote_awareness():
    assert split_segments('findstr "a|b"') == ['findstr "a|b"']
