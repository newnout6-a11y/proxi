"""Review fixes for the direct transport and the turn runner (PR #18 findings)."""
import asyncio
import os
import re
import time
from types import SimpleNamespace

import pytest

import apierr
import direct
import journal
import validate
from orchestrator import TurnRunner
from session import RelaySession
from test_native_context_reuse import FIRST, Backend, request, run, setup
from test_service_requests import capture_direct


@pytest.fixture(autouse=True)
def _fresh_dump_state(monkeypatch):
    monkeypatch.setattr(direct, "_DUMP_RECENT", {}, raising=False)
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())


_STEM = re.compile(r"^(\d{8}-\d{6}-[^\W_]*-[0-9a-f]{12})-(?:body\.json|error\.txt|metadata\.json)$")


def _sets(folder):
    return sorted(m.group(1) for p in folder.iterdir() if (m := _STEM.match(p.name)) and p.name.endswith("-body.json"))


def test_identical_rejected_body_is_stored_once_and_reuses_its_diagnostic_id(tmp_path):
    """18-8: 50 identical poison retries used to leave 150 files."""
    cfg = {"upstream_error_dir": str(tmp_path)}
    ids = {direct._dump_rejected_body(b'{"messages":["same"]}', "bad", cfg=cfg, request_id="req-%d" % i)
           for i in range(50)}
    assert len(ids) == 1 and None not in ids
    assert len(list(tmp_path.iterdir())) == 3


def test_deleted_dump_is_written_again_for_the_same_body(tmp_path):
    cfg = {"upstream_error_dir": str(tmp_path)}
    first = direct._dump_rejected_body(b"{}", "bad", cfg=cfg)
    for path in tmp_path.iterdir():
        path.unlink()
    second = direct._dump_rejected_body(b"{}", "bad", cfg=cfg)
    assert second and second != first
    assert len(list(tmp_path.iterdir())) == 3


def test_dump_sets_are_pruned_oldest_first_and_unrelated_files_survive(tmp_path):
    cfg = {"upstream_error_dir": str(tmp_path), "upstream_error_max_dumps": 4}
    old = []
    for index in range(3):
        stem = "20250101-00000%d-old%d-%012x" % (index, index, index)
        old.append(stem)
        for suffix in ("body.json", "error.txt", "metadata.json"):
            path = tmp_path / ("%s-%s" % (stem, suffix))
            path.write_text("old", encoding="utf-8")
            os.utime(path, (1_000_000 + index, 1_000_000 + index))
    foreign = [tmp_path / "notes.txt", tmp_path / "keep-body.json",
               tmp_path / "20250101-000000-x-body.json", tmp_path / "20250101-000000-x-0123456789ab-body.json.bak"]
    for path in foreign:
        path.write_text("mine", encoding="utf-8")
        os.utime(path, (1, 1))
    ids = [direct._dump_rejected_body(str(index).encode(), "bad", cfg=cfg) for index in range(3)]
    kept = _sets(tmp_path)
    assert len(kept) == 4
    assert set(ids) <= set(kept)  # the newest sets stay
    assert sum(1 for stem in old if stem in kept) == 1 and old[2] in kept  # oldest two went first
    for stem in kept:  # a set is deleted or kept as a whole
        assert all((tmp_path / ("%s-%s" % (stem, s))).exists() for s in ("body.json", "error.txt", "metadata.json"))
    assert all(path.exists() for path in foreign)


def test_default_retention_is_fifty_sets(tmp_path):
    cfg = {"upstream_error_dir": str(tmp_path)}
    ids = [direct._dump_rejected_body(str(index).encode(), "bad", cfg=cfg) for index in range(60)]
    assert len(_sets(tmp_path)) == 50
    assert ids[-1] in _sets(tmp_path)
    assert len(list(tmp_path.iterdir())) == 150


def test_invalid_retention_setting_never_breaks_the_dump(tmp_path):
    stamp = direct._dump_rejected_body(b"{}", "bad", cfg={"upstream_error_dir": str(tmp_path),
                                                          "upstream_error_max_dumps": "many"})
    assert stamp and (tmp_path / (stamp + "-body.json")).exists()


def test_empty_turn_is_marked_dumped_only_after_a_successful_write(tmp_path, monkeypatch):
    """18-22: a failed first write used to disable the diagnostic for the rest of the run."""
    attempts = []
    outcomes = iter([None, "stamp-ok"])

    def dump(*_args, **_kw):
        attempts.append(1)
        return next(outcomes)

    monkeypatch.setattr(direct, "_dump_rejected_body", dump)
    session = SimpleNamespace(_request_tag="r")
    for _ in range(3):
        direct._dump_empty_once(b"{}", {"last_usage": None}, {}, session, "model-x")
    assert len(attempts) == 2


def _run_idle_keepwarm(monkeypatch, seconds=10):
    clock = SimpleNamespace(now=1000.0)
    real_sleep = asyncio.sleep
    async def sleep(delay):
        clock.now += delay
        if clock.now >= 1000.0 + seconds:
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(direct.time, "time", lambda: clock.now)
    monkeypatch.setattr(direct.asyncio, "sleep", sleep)
    iterations = []
    real_bind = direct.client_context.bind
    monkeypatch.setattr(direct.client_context, "bind", lambda identity: iterations.append(1) or real_bind(identity))
    session = SimpleNamespace(_busy=False, _last_request_at=1000.0, _turn_seq=1)
    asyncio.run(direct._keepwarm(session, {"cache_keepwarm_seconds": 240, "cache_keepwarm_idle": 1800}))
    return iterations


def test_keepwarm_without_a_body_polls_about_once_a_second(monkeypatch):
    """18-15: a session with nothing to ping spun ~9 times a second."""
    iterations = _run_idle_keepwarm(monkeypatch)
    assert len(iterations) <= 12, "%d iterations in 10 simulated seconds" % len(iterations)


def test_insufficient_credits_from_a_persisted_error_is_402_like_the_pool():
    """18-17: the same error code must not carry two statuses."""
    messages = [{"senderRole": "sandpack", "content": {"type": "error", "error": "You've used all AI credits."}}]

    async def get_messages(_thread):
        return {"messages": messages}

    runner = TurnRunner({"workspace_root": ".", "project_context": False, "exec_policy": "off",
                         "context_max_bytes": 100, "max_exec_turns": 0, "cookie_values": ()}, None,
                        SimpleNamespace(backend=SimpleNamespace(get_messages=get_messages)))
    error = asyncio.run(runner._explain_empty({"session": SimpleNamespace(thread_id="t")}))
    pool_error = apierr.insufficient_credits(20)
    assert (error.status, error.code) == (pool_error.status, pool_error.code) == (402, "insufficient_credits")


@pytest.mark.parametrize("model,configured,expected_resolution", [
    ("zai-org/GLM-5.3", "max", "provider_managed"),
    ("claude-opus-5-5", "max", "exact"),
    ("gpt-5", "max", "compatibility"),
    ("zai-org/GLM-5.3", "auto", "model_default"),
])
def test_turn_begin_reports_provider_managed_effort(tmp_path, monkeypatch, model, configured, expected_resolution):
    """18-21: a provider-managed model with a configured default is not a 'compatibility' mapping."""
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                         reasoning_effort=configured, openai_provider_resource_name="o",
                         google_provider_resource_name="g", retool_fast_provider_resource_name="r")
    capture_direct(monkeypatch)
    parsed = validate.parse_chat({"model": model, "messages": [{"role": "user", "content": "OK"}],
                                  "max_tokens": 64}, {"strict_models": True, "model": "claude-opus-5-5"})
    before = journal.JOURNAL.last_seq
    asyncio.run(run(runner, parsed))
    begun = next(e for e in journal.JOURNAL.query(since=before) if e["kind"] == "turn.begin")
    assert begun["data"]["effort_resolution"] == expected_resolution


def _failing_runner(tmp_path, monkeypatch, error):
    _, pool, runner = setup(tmp_path, transport="direct", project_context=False)
    checks = []

    async def stream(session, prompt, provider, tool_names=None):
        raise error
        yield  # pragma: no cover

    def check(backend):
        checks.append(backend)
        return False

    monkeypatch.setattr(RelaySession, "stream", stream)
    runner.accounts = SimpleNamespace(check_balance_now=check)
    return runner, checks


@pytest.mark.parametrize("error,expected_checks", [
    (apierr.upstream_timeout(), 0),
    (apierr.too_many_requests(), 0),
    (apierr.bad_request("x"), 0),
    (apierr.ApiError(429, "insufficient_credits", "out"), 1),
    (apierr.ApiError(402, "insufficient_credits", "out"), 1),
    (apierr.upstream_error("boom"), 1),
])
def test_balance_is_fetched_only_for_errors_that_can_be_about_credits(tmp_path, monkeypatch, error, expected_checks):
    """18-24: a 504 timeout used to add a blocking balance fetch before the error reached the client."""
    runner, checks = _failing_runner(tmp_path, monkeypatch, error)
    events = asyncio.run(run(runner, request(FIRST)))
    assert events[-1]["type"] == "error" and events[-1]["error"].code == error.code
    assert len(checks) == expected_checks
