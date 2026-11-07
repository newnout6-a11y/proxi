"""Ways around the default allowlist found by the audit of 2026-11-07: each ran any program the model wrote."""
import json
from pathlib import Path

import pytest

import executor

ALLOWLIST = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))[
    "exec_allowlist"]


def verdict(cmd):
    try:
        executor.check_policy(cmd, "allowlist", ALLOWLIST)
    except executor.PolicyError:
        return "denied"
    return "allowed"


@pytest.mark.parametrize("cmd", [
    'git rebase -x "echo pwned" HEAD~1',
    'git rebase --exec "echo pwned" HEAD~1',
    'git ls-remote --exec="echo pwned" .',
    'git archive --remote=. --exec="echo pwned" HEAD',
    'git clone --no-local -u "echo pwned" . d',
    'git clone --config core.sshCommand="echo pwned" ssh://x/y d',
    'git fetch-pack --exec=x .',
    'git push --exec=x origin',
    'git fetch --exec=x origin',
    'git send-pack --exec=x .',
    'git instaweb',
    'git commit -am x',
    'git reset --hard',
    'git clean -fdx',
    'git branch -D main',
    'git tag -d v1',
    'powershell -Command "Write-Output $ExecutionContext.InvokeCommand | ForEach-Object -MemberName InvokeScript -ArgumentList x"',
    'powershell -Command "Get-ChildItem x.txt | ForEach-Object -MemberName Delete"',
    'powershell -Command "Get-Process | ForEach-Object Kill"',
    'powershell -Command "Get-Item variable:ExecutionContext"',
    'powershell -Command "Get-ChildItem env:"',
    r'powershell -Command "Get-ItemProperty HKCU:\Environment"',
])
def test_a_way_to_run_any_program_or_change_the_work_is_refused(cmd):
    assert verdict(cmd) == "denied"


@pytest.mark.parametrize("cmd", [
    'git fetch --upload="calc" .', 'git fetch --upl=calc .', 'git push --exe="calc" .', 'git push --rece="calc" .',
    'git push --receive=calc .', 'git grep --open-files-in-p="calc" -e x', 'git grep --open=calc -e x',
    'git log --outp=x.txt', 'git push "--exe=calc" .', 'git fetch --UPLOAD=calc .',
])
def test_an_abbreviated_option_that_git_completes_to_one_that_runs_a_program_is_refused(cmd):
    """git takes any unique prefix of a long option, so a list of whole option names let these run any program (the
    verifiers of the audit of 2026-11-07 ran each of them)."""
    assert verdict(cmd) == "denied"


@pytest.mark.parametrize("cmd", ["git log --output-indicator-new=+ -p", "git diff --exit-code", "git log --reverse",
                                 "git grep --extended-regexp foo", "git log --remotes", "git fetch --prune origin",
                                 "git diff --relative", "git log --oneline --", "git grep --count foo"])
def test_ordinary_long_options_are_not_taken_for_abbreviations(cmd):
    assert verdict(cmd) == "allowed"


@pytest.mark.parametrize("cmd", [
    "git status", "git log --oneline -5", "git diff HEAD~1", "git show HEAD", "git blame a.py", "git grep -n foo",
    "git ls-files", "git rev-parse HEAD", "git branch --list", "git branch -a", "git tag --list", "git describe --tags",
    "git push -u origin main", "git fetch origin",
    'powershell -Command "Get-ChildItem -Recurse | Select-Object -ExpandProperty Name"',
    'powershell -Command "Get-Content a.txt | Select-String foo"',
    'powershell -Command "Get-ChildItem | Where-Object Length -gt 10"',
    r'powershell -Command "Get-ChildItem C:\work"',
])
def test_reading_commands_still_run(cmd):
    assert verdict(cmd) == "allowed"
