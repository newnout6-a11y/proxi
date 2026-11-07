"""Ways around the default allowlist found by the audits of 2026-11-07 and 2026-11-08: each ran any program the model
wrote."""
import json
from pathlib import Path

import pytest

import config
import executor

ALLOWLIST = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))[
    "exec_allowlist"]
DEFAULTS = config.DEFAULTS["exec_allowlist"]


def verdict(cmd, allowlist=ALLOWLIST):
    try:
        executor.check_policy(cmd, "allowlist", allowlist)
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


@pytest.mark.parametrize("allowlist", [DEFAULTS, ALLOWLIST], ids=["defaults", "example"])
@pytest.mark.parametrize("cmd", [
    "powershell -NoProfile -Command Get-Date ; dir (whoami.exe)",
    "powershell -NoProfile -Command Get-Date;dir (whoami.exe)",
    "pwsh -NoProfile -c Get-Date ; dir ([IO.File]::WriteAllText('pwned.txt','x'))",
    "pwsh -c Get-Date;type ([IO.File]::WriteAllText('pwned.txt','x'))",
    'cmd /c "powershell -Command Get-Date ; dir (whoami.exe)"',
    'cmd /c "pwsh -c Get-Date;dir (whoami.exe)"',
    "powershell -NoProfile -Command Get-Date\rdir (whoami.exe)",
    "pwsh -c Get-Date\rtype (whoami.exe)",
    'cmd /c "powershell -Command Get-Date\rdir (whoami.exe)"',
])
def test_a_powershell_statement_behind_a_semicolon_or_a_carriage_return_is_not_skipped(cmd, allowlist):
    """cmd.exe splits a line neither at ';' nor at a carriage return (it drops the CR), so powershell.exe got the whole
    line and ran `(whoami.exe)`, while the policy split there and judged Get-Date alone as the body and dir as a command
    of its own (audit of 2026-11-08: a file written by a statement the check never saw)."""
    assert verdict(cmd, allowlist) == "denied"


@pytest.mark.parametrize("cmd", [
    "git status\ngit log", "git status\r\ngit log", "powershell -Command Get-Date\ndir",
    'powershell -Command "Get-ChildItem\nGet-Date"', 'echo "a\nb"', 'cmd /c "dir\necho x"',
])
def test_a_line_break_is_refused_because_cmd_runs_only_the_first_line(cmd):
    """cmd.exe stops at the first line feed, inside quotes too: the policy judged lines that never ran."""
    assert verdict(cmd, DEFAULTS) == "denied"


@pytest.mark.parametrize("cmd", [
    'powershell -Command "Get-ChildItem; Get-Date"', 'pwsh -c "Get-ChildItem;Get-Date"',
    'powershell -NoProfile -Command "Get-Content a.txt ; Select-String foo a.txt"',
    'findstr "a;b" x.txt', 'echo "a ; b"', 'git log --grep="fix;" -1',
])
def test_a_semicolon_inside_quotes_still_runs(cmd):
    """Inside a quoted PowerShell body a ';' separates statements that the PowerShell rules judge one by one; in an
    argument it is text."""
    assert verdict(cmd, DEFAULTS) == "allowed"


@pytest.mark.parametrize("cmd", [
    'powershell -Command "Get-Date; echo (whoami.exe)"', 'pwsh -c "Get-Date;Start-Process calc"',
    'powershell -Command "Get-Date ; Remove-Item x.txt"',
])
def test_a_quoted_statement_after_a_semicolon_is_judged_by_the_powershell_rules(cmd):
    assert verdict(cmd, DEFAULTS) == "denied"
