"""Reject shell redirection without executing any file-writing commands."""
import pytest

import executor


ALLOW = [r"^(?:echo|type|findstr|cmd|powershell|pwsh)\b"]


@pytest.mark.parametrize("command", [
    "echo x > file",
    "echo x >> file",
    "type a >> b",
    "findstr x < file",
    "echo x 2>&1",
    "echo x 2>error.txt",
    "echo 'x > file'",
    'cmd /c "echo x > file"',
    'cmd /c "type a >> b"',
    'cmd /d /s /c "findstr x < file"',
    'powershell -Command "Write-Output x > file"',
    'pwsh -c "Get-Content a > b"',
    'powershell -NoProfile -Command "Write-Output x 2>&1"',
    'cmd /c "cmd /c echo x > file"',
    'cmd /c "powershell -Command Write-Output x > file"',
    # powershell.exe drops the double quotes of its command line before PowerShell reads the text, so this is a
    # redirection to PowerShell (`"a > b.txt"` wrote b.txt, audit of 2026-11-08); with single quotes cmd.exe redirects.
    'powershell -Command Write-Output "a>b"',
    "powershell -Command Write-Output 'a>b'",
])
def test_allowlist_rejects_unquoted_redirection(command):
    with pytest.raises(executor.PolicyError, match="redirection requires allow_all"):
        executor.check_policy(command, "allowlist", ALLOW)


@pytest.mark.parametrize("command", [
    'findstr "a>b" file.txt',
    'echo "x<y"',
    'echo "x>y" | findstr x',
    'cmd /c echo "x>y"',
    'powershell -Command "Write-Output \'a>b\'"',
    'powershell -Command "Write-Output \'a<b\'"',
    'pwsh -c "Write-Output \'a<b\' | Select-String a"',
])
def test_allowlist_preserves_quoted_literals_and_pipelines(command):
    executor.check_policy(command, "allowlist", ALLOW)


@pytest.mark.parametrize("command", [
    "echo x > file",
    "type a >> b",
    "findstr x < file",
    'powershell -Command "Write-Output x 2>&1"',
])
def test_allow_all_preserves_explicit_redirection_opt_in(command):
    executor.check_policy(command, "allow_all", [])


def test_segment_utility_keeps_default_behavior():
    assert executor.split_segments("echo x > file") == ["echo x > file"]
