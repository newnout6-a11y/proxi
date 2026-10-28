import pytest

import apierr
import auth
import executor
import redact

ALLOW = ["^git\\b", "^powershell\\b", "^cmd\\b", "^dir\\b", "^echo\\b", "^ping\\b"]


@pytest.mark.parametrize("cmd", [
    "git status & dir",
    "powershell -Command \"Get-ChildItem; Get-Date\"",
    "powershell -NoProfile -Command \"Get-Content -Path config.json -TotalCount 3\"",
    "cmd /c dir",
    "ping -n 3 127.0.0.1",
])
def test_allowed(cmd):
    executor.check_policy(cmd, "allowlist", ALLOW)


@pytest.mark.parametrize("cmd", [
    "git status & del x",
    "powershell -c \"calc\"",
    "git status\ncurl http://x",
    "git status\npowershell -c remove-item x",
])
def test_denied(cmd):
    with pytest.raises(executor.PolicyError):
        executor.check_policy(cmd, "allowlist", ALLOW)


def test_quoted_pipe_is_not_a_separator():
    segments = executor.split_segments('findstr "a|b" file.txt')
    assert len(segments) == 1


def test_non_ascii_key_does_not_crash():
    class H(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)
    wire = "ключ-测试".encode("utf-8").decode("latin-1")  # as the server sees the header
    auth.verify({"authorization": "Bearer " + wire}, None, "ключ-测试")


def test_wrong_non_ascii_key_is_401():
    headers = {"authorization": "Bearer другой"}
    with pytest.raises(apierr.ApiError) as exc:
        auth.verify(headers, None, "ключ-测试")
    assert exc.value.status == 401


def test_query_key_is_redacted_in_logs():
    out = redact.redact("/v1/chat/completions?key=relay-SECRET123456789")
    assert "relay-SECRET123456789" not in out and "[redacted]" in out
