import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading
import time
from types import SimpleNamespace
import urllib.error

import pytest

import apierr
import client_context
import direct
import journal
import session
import stream_watchdog
from upstream_attempts import AttemptBudget
from upstream_diagnostics import AttemptTrace


@pytest.mark.parametrize("status", [400, 422])
def test_rejection_has_unique_correlated_dump_and_context_in_executor(tmp_path, monkeypatch, status):
    cfg = {"upstream_error_dir": str(tmp_path), "turn_timeout": 2}
    body = {"model": "claude-opus-5-5", "reasoningEffort": "high", "messages": [{"role": "user", "content": "hi"}]}
    session = SimpleNamespace(backend=SimpleNamespace(headers=lambda: {"cookie": "never-store-this-header"},
                              cfg={"base": "https://example.invalid"}), _cancelled=False, _request_tag="req123")

    def reject(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", status, "invalid", {"request-id": "upstream123"},
                                     io.BytesIO(b'{"message":"Invalid request format"}'))
    monkeypatch.setattr(direct.urllib.request, "urlopen", reject)

    async def run():
        token = client_context.bind({"client": "opencode", "chat_title": "Проверка ошибок", "request_id": "req123"})
        try:
            with pytest.raises(apierr.ApiError) as caught:
                async for _ in direct._stream_body(cfg, session, body):
                    pass
            return caught.value
        finally:
            client_context.reset(token)

    error = asyncio.run(run())
    assert error.status == 400
    assert error.upstream_status == status
    files = list(tmp_path.glob("*-body.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_bytes()) == body
    metadata = json.loads((tmp_path / (error.diagnostic_id + "-metadata.json")).read_text(encoding="utf-8"))
    assert metadata["chat_title"] == "Проверка ошибок"
    assert metadata["request_id"] == "req123"
    assert metadata["upstream_status"] == status
    assert "never-store-this-header" not in "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    event = next(e for e in reversed(journal.JOURNAL.query()) if e["kind"] == "upstream.rejected")
    assert event["request_id"] == "req123"
    assert event["data"]["upstream_status"] == status


def test_same_second_concurrent_dumps_do_not_overwrite(tmp_path):
    def dump(index):
        return direct._dump_rejected_body(str(index).encode(), "bad", cfg={"upstream_error_dir": str(tmp_path)},
                                          request_id="../same-id")
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(dump, range(16)))
    assert len(set(ids)) == 16
    assert len(list(tmp_path.glob("*-body.json"))) == 16
    assert {p.read_bytes() for p in tmp_path.glob("*-body.json")} == {str(i).encode() for i in range(16)}


def test_diagnostic_disk_failure_keeps_original_error(tmp_path):
    target = tmp_path / "file"
    target.write_text("cannot mkdir under a file")
    assert direct._dump_rejected_body(b"{}", "bad", cfg={"upstream_error_dir": str(target / "dir")}) is None


# -- A09-03: an attempt the client cancelled is no incident; one the watchdog cut is a timeout ----------------------------

def attempt_events(since):
    return [event for event in journal.JOURNAL.query(since=since, limit=500) if event["kind"] == "upstream.attempt"]


@pytest.mark.parametrize("outcome, level, saved", [("cancelled", "info", False), ("error", "warn", True),
                                                   ("timeout", "warn", True), ("content", "debug", False)])
def test_only_a_failed_attempt_is_a_warning_and_a_summary_on_disk(tmp_path, outcome, level, saved):
    """A client that leaves mid-answer (Esc in Claude Code or Codex) is no upstream failure: its summary took one of the
    200 places of logs/upstream-400 from a real one, and its journal line was a warning (audit A09-03)."""
    budget = AttemptBudget()
    budget.take(lambda: False)
    seq = journal.JOURNAL.last_seq
    summary = AttemptTrace({"upstream_error_dir": str(tmp_path)}, budget, "req", "claude-opus-5-5", b"{}").finish(outcome)
    (event,) = attempt_events(seq)
    assert event["level"] == level and event["data"]["outcome"] == outcome
    assert summary["diagnostic_saved"] is saved and bool(list(tmp_path.iterdir())) is saved


class Silent:
    """An upstream answer that sends nothing until it is closed."""
    status = 200
    headers = {}

    def __init__(self):
        self.entered, self.released = threading.Event(), threading.Event()

    def __iter__(self):
        self.entered.set()
        assert self.released.wait(5)
        return iter(())

    def close(self):
        self.released.set()


def cut_attempt(tmp_path, monkeypatch, by_watchdog):
    answer = Silent()
    monkeypatch.setattr(direct.urllib.request, "urlopen", lambda *_a, **_kw: answer)
    backend = SimpleNamespace(cfg={"base": "https://upstream.invalid"}, headers=lambda: {})
    target = SimpleNamespace(backend=backend, thread_id="test-thread", _cancelled=False, _request_tag="owner-request")
    cfg = {"upstream_error_dir": str(tmp_path)}
    seq = journal.JOURNAL.last_seq

    async def consume():
        stream = direct._stream_body(cfg, target, {"model": "claude-opus-5-5"})
        if by_watchdog:
            stream = stream_watchdog.watch(stream, 0.3, 0.3, lambda: session.RelaySession.abort_active_turn(target),
                                           tool_timeout=None, thinking_timeout=None)
        task = asyncio.create_task(_collect(stream))
        try:
            if not by_watchdog:
                deadline = time.monotonic() + 3
                while not answer.entered.is_set():
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.01)
                target._cancelled = True
                target._active_direct_abort()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        finally:
            answer.released.set()

    asyncio.run(consume())
    events = attempt_events(seq)
    assert len(events) == 1, events
    return events[0], list(tmp_path.iterdir())


async def _collect(stream):
    return [event async for event in stream]


def test_an_attempt_the_client_cancelled_is_info_and_writes_no_incident(tmp_path, monkeypatch):
    event, files = cut_attempt(tmp_path, monkeypatch, by_watchdog=False)
    assert (event["data"]["outcome"], event["level"], files) == ("cancelled", "info", [])


def test_an_attempt_the_watchdog_cut_is_a_timeout_not_a_cancel(tmp_path, monkeypatch):
    """The watchdog aborts the attempt with a CancelledError, so its summary read like a client that left (8c30ec65492e on
    2026-11-18: "Попытка 1/5: cancelled" right before turn.error upstream_timeout)."""
    event, files = cut_attempt(tmp_path, monkeypatch, by_watchdog=True)
    assert (event["data"]["outcome"], event["level"]) == ("timeout", "warn") and len(files) == 1
