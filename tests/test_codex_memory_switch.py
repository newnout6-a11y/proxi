"""Codex's memory jobs can be switched off in the admin (codex_memory_jobs): such a job is answered with a 400 before anything
is spent, and every other request of Codex is served as before. The owner's choice of 2026-11-07: memory off, with a way to
switch it on again in the admin."""
import pytest

import admin
import client_context
import config
from prompt_support import prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_background_jobs import CODEX, DESKTOP, job, run_job, tasks
from test_compaction_cache import Chat


def refused(relay):
    mark, sent = relay.mark(), len(relay.posts)
    status, payload, _ = relay.post("responses", job("old-thread"), DESKTOP)
    return status, payload, len(relay.posts) - sent, relay.events(mark, "codex.memory_job_refused")


def test_a_memory_job_is_refused_before_anything_is_spent_when_the_jobs_are_off(prompt_relay):
    relay = prompt_relay(codex_memory_jobs=False)
    status, payload, posted, events = refused(relay)
    assert status == 400 and "codex_memory_jobs" in str(payload)
    assert posted == 0 and len(events) == 1


def test_the_switch_is_read_at_every_request_and_is_live_in_the_admin(prompt_relay):
    relay = prompt_relay()
    mark = relay.mark()
    run_job(relay, "t1")
    assert tasks(relay, mark) == [client_context.BACKGROUND]
    relay.runtime.cfg["codex_memory_jobs"] = False  # what the admin does for a live setting
    assert refused(relay)[0] == 400
    relay.runtime.cfg["codex_memory_jobs"] = True
    run_job(relay, "t2")
    assert "codex_memory_jobs" in admin.LIVE_SETTINGS and "codex_memory_jobs" in admin._schema()


def test_codex_turns_with_tools_are_served_when_the_jobs_are_off(prompt_relay):
    relay = prompt_relay(codex_memory_jobs=False)
    chat = Chat(relay, "responses", effort="high", headers={**CODEX, "session-id": "chat"}, model="claude-sonnet-5-5")
    chat.work(2)
    assert len(relay.posts) == 2


def test_the_default_serves_the_jobs_and_a_wrong_value_stops_the_relay(tmp_path, monkeypatch):
    import apierr
    from test_client_adapt_config import load_with
    assert config.DEFAULTS["codex_memory_jobs"] is True
    with pytest.raises(apierr.ApiError, match="codex_memory_jobs must be a boolean"):
        load_with(tmp_path, monkeypatch, codex_memory_jobs="no")
