import asyncio
import io
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

import console_log
import credits
import direct
import journal
from scripts import watch_relay


def usage(read=0, write=0):
    return {"inputTokens": 4, "promptTokensReadFromCache": read, "promptTokensWrittenToCache": write, "creditsUsed": 0.25}


def test_model_effort_and_cost_use_light_green_without_purple():
    line = "[req] MODEL: claude-opus-5-5 | EFFORT: HIGH credits=0.2500 spent=0.2500"
    colored = console_log.render(line, color=True)
    for field in ("MODEL: claude-opus-5-5", "EFFORT: HIGH", "credits=0.2500", "spent=0.2500"):
        assert "\x1b[92m" + field + "\x1b[0m" in colored
    assert "\x1b[35m" not in colored and "\x1b[95m" not in colored


def test_big_read_is_cyan_without_red_write_warning():
    line = console_log.CacheMonitor().line("req", usage(read=462838))
    colored = console_log.render(line, color=True)
    assert "\x1b[36mread=462,838\x1b[0m" in colored
    assert "WRITE HIGH" not in colored and "WRITE SPIKE" not in colored
    assert "\x1b[1;91m" not in colored


def test_large_initial_write_is_red_and_keeps_warning_without_color():
    line = console_log.CacheMonitor().line("req", usage(write=50000))
    assert "[WRITE HIGH]" in line
    assert "\x1b[1;91mwrite=50,000\x1b[0m" in console_log.render(line, color=True)
    assert "[WRITE HIGH]" in console_log.render(line, color=False)
    assert "\x1b" not in console_log.render(line, color=False)


def test_write_jump_compares_previous_request_not_metadata_in_same_request():
    monitor = console_log.CacheMonitor()
    monitor.line("req-A", usage(write=100), scope=("opus", "key"))
    monitor.line("req-B", usage(write=9000), scope=("opus", "key"))
    repeated = monitor.line("req-B", usage(write=9100), scope=("opus", "key"))
    assert "[WRITE SPIKE +9,000]" in repeated
    assert "SPIKE" not in monitor.line("req-C", usage(write=9101), scope=("opus", "key"))


def test_comparisons_do_not_cross_chats_or_model_cache_scopes():
    first, second = console_log.CacheMonitor(), console_log.CacheMonitor()
    first.line("req-A", usage(), scope=("opus", "key1"))
    assert "SPIKE" not in second.line("req-B", usage(write=9000), scope=("opus", "key1"))
    assert "SPIKE" not in first.line("req-C", usage(write=9000), scope=("sonnet", "key2"))


def test_jump_has_absolute_and_ratio_floors():
    monitor = console_log.CacheMonitor()
    monitor.line("req-A", usage(write=0))
    assert "SPIKE" not in monitor.line("req-B", usage(write=8191))
    monitor.line("req-C", usage(write=6000))
    assert "SPIKE" not in monitor.line("req-D", usage(write=16000))


@pytest.mark.parametrize("invalid", [None, True, -3, "invalid", "inf", "nan", [], {}])
def test_missing_or_invalid_counters_are_unknown_and_do_not_create_alerts(invalid):
    line = console_log.CacheMonitor().line("req", usage(read=invalid, write=invalid))
    assert "read=? write=?" in line and "WRITE " not in line


def test_pipe_is_plain_no_color_wins_and_force_is_explicit(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert console_log.color_enabled(io.StringIO()) is False
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert console_log.color_enabled(io.StringIO()) is True
    monkeypatch.setenv("NO_COLOR", "")
    assert console_log.color_enabled(io.StringIO()) is False


def test_render_redacts_before_styling_and_strips_injected_ansi(monkeypatch):
    instance = journal.Journal()
    instance.configure({"relay_api_key": "synthetic-secret-123"})
    monkeypatch.setattr(journal, "JOURNAL", instance)
    line = "relay req POST /v1/messages 400 synthetic-secret-123 \x1b[31m"
    colored = console_log.render(line, color=True)
    assert "synthetic-secret-123" not in colored
    assert "[redacted]" in colored
    assert "\x1b[31m" not in colored
    assert "\x1b[1;91m400\x1b[0m" in colored


def test_balance_snapshot_applies_debit_without_network_and_marks_estimate(monkeypatch):
    manager = credits.CreditsManager("https://example.invalid", lambda: {})
    manager._balance = {"available": 100.0, "monthly_allocation": 500.0}
    manager._cached_at = time.monotonic()
    monkeypatch.setattr(credits.urllib.request, "urlopen", lambda *_args, **_kw: pytest.fail("console must not fetch"))
    manager.record_turn("turn-A", 0.25)
    manager.record_turn("turn-A", 0.25)
    snapshot = manager.balance_snapshot()
    assert snapshot["available"] == 99.75 and snapshot["estimated"] is True
    assert snapshot["stale"] is False
    line = console_log.balance_line("req", snapshot, 0.25)
    assert "balance=99.7500" in line and "spent=0.2500" in line
    assert "LOCAL ESTIMATE" in line
    manager.last_error = "synthetic fetch error"
    assert "STALE" in console_log.balance_line("req", manager.balance_snapshot())


def test_balance_unknown_negative_and_expired_are_not_misrepresented():
    manager = credits.CreditsManager("https://example.invalid", lambda: {})
    assert "balance=?" in console_log.balance_line("req", manager.balance_snapshot())
    manager._balance = {"available": -0.5}
    manager._cached_at = time.monotonic() - credits.CACHE_TTL - 1
    assert "balance=-0.5000" in console_log.balance_line("req", manager.balance_snapshot())
    assert manager.balance_snapshot()["stale"] is True


@pytest.mark.parametrize("stream", [False, True])
def test_request_completion_prints_total_balance_and_summed_cost(relay, capsys, monkeypatch, stream):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(relay.credits, "balance_snapshot", lambda: {
        "available": 100 - sum(cost for _, cost in relay.credits.used), "estimated": True}, raising=False)
    relay.fake.script = [{"type": "credits", "value": 0.25}, {"type": "credits", "value": 0.75},
                         {"type": "text", "delta": "ok"}, {"type": "end", "reason": "stop"}]
    before = journal.JOURNAL.last_seq
    payload = {"model": "claude-opus-5-5", "max_tokens": 64, "stream": stream,
               "messages": [{"role": "user", "content": "hi"}]}
    with relay.request("/v1/messages", payload) as response:
        response.read()
        request_id = response.headers["request-id"]
    deadline = time.monotonic() + 2
    while not any(event["kind"] == "http.request" and event.get("request_id") == request_id
                  for event in journal.JOURNAL.query(since=before)):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    output = capsys.readouterr().out
    assert "[%s] [balance] balance=99.0000" % request_id in output
    assert "spent=1.0000" in output and "\x1b" not in output


def test_cache_rendering_does_not_change_credit_events(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    session = SimpleNamespace(_cancelled=False, _request_tag="req-cache")
    metadata = usage(read=0, write=190000)

    async def run():
        queue = asyncio.Queue()
        queue.put_nowait(json.dumps({"event": "metadata", "usage": metadata}))
        queue.put_nowait(json.dumps({"event": "end_of_stream"}))
        return [event async for event in direct._drain({}, session, queue, [], [])]

    events = asyncio.run(run())
    assert events == [{"type": "credits", "value": 0.25, "usage": {
        "input_tokens": 4, "output_tokens": None, "cache_read": 0, "cache_write": 190000}}]
    assert "write=190,000" in capsys.readouterr().out


def test_viewer_reads_last_lines_without_modifying_plain_file(tmp_path):
    path = tmp_path / "relay.log"
    content = "first\nsecond\n[req] [cache] read=1 write=50000 [WRITE HIGH]\n"
    path.write_text(content, encoding="utf-8")
    assert list(watch_relay.lines(path, tail=2, follow=False)) == content.splitlines(keepends=True)[1:]
    assert path.read_text(encoding="utf-8") == content


def test_viewer_follows_truncation(tmp_path, monkeypatch):
    path = tmp_path / "relay.log"
    path.write_text("old-long-line\n", encoding="utf-8")
    iterator = watch_relay.lines(path)
    assert next(iterator) == "old-long-line\n"
    monkeypatch.setattr(watch_relay.time, "sleep", lambda _: path.write_text("new\n", encoding="utf-8"))
    assert next(iterator) == "new\n"
    iterator.close()
