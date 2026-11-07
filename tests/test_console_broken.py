"""The console is best effort: a stdout that cannot be written (its supervisor died) must not break a request."""
import io
import sys

import console_log


class Broken(io.TextIOBase):
    def write(self, text):
        raise OSError(22, "Invalid argument")

    def flush(self):
        raise OSError(22, "Invalid argument")


def test_a_write_to_a_broken_stdout_does_not_raise(monkeypatch):
    monkeypatch.setattr(sys, "stdout", Broken())
    console_log.write("relay 0123456789ab GET /v1/models 200 - 1ms")


def test_a_request_is_answered_while_stdout_is_broken(relay, monkeypatch):
    monkeypatch.setattr(sys, "stdout", Broken())
    status, _ = relay.json_any("/v1/nothing-here")
    assert status == 404  # it used to end with no response: the console line before the error response raised
    status, _ = relay.json("/v1/chat/completions", {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
