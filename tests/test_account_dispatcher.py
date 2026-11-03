"""Account dispatcher: staggered renewal, exclusion of exhausted/dead accounts, failover."""
import asyncio
import io
import json
import random
from types import SimpleNamespace

import pytest

import apierr
import client_context
import direct
from accounts import DEAD_AFTER, AccountDispatcher
from session import RelaySession
from test_native_context_reuse import FIRST, Backend, request, run, setup


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeRefresher:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.last_error = None
        self.last_failure_kind = None
        self.keepalive_owner = None

    def refresh(self, force=False, **_kw):
        self.calls += 1
        ok, kind = self.results.pop(0) if self.results else (True, None)
        self.last_failure_kind = kind
        self.last_error = None if ok else "synthetic"
        return ok


class FakeManager:
    def __init__(self, available):
        self.available = available
        self.fetches = 0

    def get_balance(self, force_refresh=False):
        self.fetches += 1
        return {"available": self.available}

    def balance_snapshot(self):
        return {"available": self.available}


class Account:
    def __init__(self, name, refresher=None):
        self.cfg = {"account_id": name}
        self.refresher = refresher or FakeRefresher([])


def dispatcher(accounts, managers=None, **cfg):
    settings = {"session_refresh_interval": 900, "min_account_balance": 20, "account_refresh_parallel": 3}
    settings.update(cfg)
    clock = Clock()
    value = AccountDispatcher(accounts, settings, managers or {}, clock=clock, rng=random.Random(1))
    return value, clock


def test_first_checks_are_staggered_and_parallel_is_bounded():
    accounts = [Account("a%d" % i) for i in range(20)]
    value, clock = dispatcher(accounts, parallel=3)
    assert len(value.due()) == 1  # only the first account is due at start
    clock.now += 60
    jobs = value.due()
    assert len(jobs) == 2  # 3 in flight at most (one still running)
    assert all(a.refresher.keepalive_owner is value for a in accounts)


def test_repeated_auth_failures_remove_account_and_success_restores_it(capsys):
    refresher = FakeRefresher([(False, "auth")] * DEAD_AFTER + [(True, None)])
    account = Account("dead", refresher)
    value, _ = dispatcher([account])
    for _ in range(DEAD_AFTER - 1):
        value.refresh(account)
        assert account.unavailable is None
    value.refresh(account)
    assert account.unavailable["reason"] == "auth_dead"
    value.refresh(account)
    assert account.unavailable is None
    output = capsys.readouterr().out
    assert "нужен новый вход" in output and "снова в пуле" in output


def test_network_failure_pauses_every_account_instead_of_marking_it_dead():
    first, second = Account("a", FakeRefresher([(False, "network")])), Account("b")
    value, clock = dispatcher([first, second])
    value.refresh(first)
    assert first.unavailable is None
    clock.now += 10
    assert value.due() == []  # the shared pause blocks the second account too
    clock.now += 30
    assert value.due()


def test_balance_below_threshold_excludes_and_recovery_restores():
    account = Account("a")
    manager = FakeManager(19.5)
    value, _ = dispatcher([account], {account: manager})
    assert value.check_balance(account) is True
    assert account.unavailable == {"available": 19.5, "threshold": 20.0, "reason": "exhausted"}
    manager.available = 500
    assert value.check_balance(account) is False
    assert account.unavailable is None


def test_local_debit_below_threshold_forces_a_server_check():
    account = Account("a")
    manager = FakeManager(10)
    value, clock = dispatcher([account], {account: manager}, session_refresh_interval=0)
    value.state[account]["next_balance"] = clock.now + 10_000
    assert value.due() == [(account, "balance")]


def test_pool_skips_exhausted_accounts_and_reports_402_when_none_left(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)
    other = Backend(tmp_path, provider_resource_name="other-account")
    pool.backends.append(other)
    pool.backends[0].unavailable = {"reason": "exhausted", "available": 3.0, "threshold": 20.0}

    async def scenario():
        lease = await pool.acquire("chat")
        assert lease.backend is other
        await pool.release(lease)
        other.unavailable = {"reason": "exhausted", "available": 1.0, "threshold": 20.0}
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire("new-chat")
        return caught.value

    error = asyncio.run(scenario())
    assert error.status == 402 and error.code == "insufficient_credits"
    assert "Баланс Retool закончился" in error.message and "20" in error.message


def test_signed_out_only_account_is_still_tried(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)
    pool.backends[0].unavailable = {"reason": "auth_dead"}

    async def scenario():
        lease = await pool.acquire("chat")
        await pool.release(lease)

    asyncio.run(scenario())


def test_confirmed_exhaustion_retries_the_turn_on_another_account(tmp_path, monkeypatch):
    _, pool, runner = setup(tmp_path, transport="direct", project_context=False)
    first = pool.backends[0]
    second = Backend(tmp_path, provider_resource_name="other-account")
    pool.backends.append(second)
    served = []

    async def stream(session, prompt, provider, tool_names=None):
        served.append(session.backend)
        if session.backend is first:
            raise apierr.upstream_error("synthetic upstream failure")
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    def check(backend):
        backend.unavailable = {"reason": "exhausted", "available": 4.0, "threshold": 20.0}
        return True

    monkeypatch.setattr(RelaySession, "stream", stream)
    runner.accounts = SimpleNamespace(check_balance_now=check)
    events = asyncio.run(run(runner, request(FIRST)))
    assert served == [first, second]
    assert {"type": "text", "delta": "ok"} in events
    assert not any(event["type"] == "error" for event in events)
    assert not pool._active


def test_unconfirmed_failure_is_returned_without_switching(tmp_path, monkeypatch):
    _, pool, runner = setup(tmp_path, transport="direct", project_context=False)
    pool.backends.append(Backend(tmp_path, provider_resource_name="other-account"))
    served = []

    async def stream(session, prompt, provider, tool_names=None):
        served.append(session.backend)
        raise apierr.upstream_error("empty turn")
        yield  # pragma: no cover

    monkeypatch.setattr(RelaySession, "stream", stream)
    runner.accounts = SimpleNamespace(check_balance_now=lambda backend: False)
    events = asyncio.run(run(runner, request(FIRST)))
    assert len(served) == 1
    assert events[-1]["type"] == "error" and events[-1]["error"].code == "upstream_error"


@pytest.mark.parametrize("kind", ["upstream", "local", None])
def test_non_auth_renewal_failures_never_mark_account_signed_out(kind):
    account = Account("a", FakeRefresher([(False, kind)] * (DEAD_AFTER + 2)))
    value, clock = dispatcher([account])
    for _ in range(DEAD_AFTER + 2):
        value.refresh(account)
        clock.now = value.pause_until
    assert account.unavailable is None


def test_parallel_network_failures_count_as_one_outage():
    accounts = [Account(n, FakeRefresher([(False, "network")])) for n in "abc"]
    value, clock = dispatcher(accounts)
    for account in accounts:
        value.refresh(account)
    assert value.pause_until == clock.now + 20.0


def test_request_path_renewal_returns_signed_out_account_to_pool():
    refresher = FakeRefresher([(False, "auth")] * DEAD_AFTER)
    refresher.last_success_at = None
    account = Account("a", refresher)
    value, _ = dispatcher([account])
    for _ in range(DEAD_AFTER):
        value.refresh(account)
    assert account.unavailable["reason"] == "auth_dead"
    assert value.revive_renewed() is False
    refresher.last_success_at = 12345.0  # a 401 retry adopted a new sign-in
    assert value.revive_renewed() is True
    assert account.unavailable is None


def test_lone_signed_out_account_keeps_its_conversation(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)

    async def scenario():
        lease = await pool.acquire("chat")
        first = lease.session
        await pool.release(lease)
        pool.backends[0].unavailable = {"reason": "auth_dead"}
        lease = await pool.acquire("chat")
        assert lease.session is first  # no cache rewrite on every request
        await pool.release(lease)

    asyncio.run(scenario())


def test_failed_migration_keeps_the_original_conversation(tmp_path):
    _, pool, _ = setup(tmp_path, transport="direct", project_context=False)

    async def scenario():
        lease = await pool.acquire("chat")
        first = lease.session
        await pool.release(lease)
        pool.backends[0].unavailable = {"reason": "exhausted", "available": 2.0, "threshold": 20.0}
        with pytest.raises(apierr.ApiError) as caught:
            await pool.acquire("chat")
        assert caught.value.status == 402
        assert pool._conversations["chat"] is first
        assert not pool._active and pool._slots._value == 8

    asyncio.run(scenario())


def test_top_up_is_adopted_before_answering_402(tmp_path, monkeypatch):
    _, pool, runner = setup(tmp_path, transport="direct", project_context=False)
    account = pool.backends[0]
    account.unavailable = {"reason": "exhausted", "available": 2.0, "threshold": 20.0}

    async def stream(session, prompt, provider, tool_names=None):
        yield {"type": "text", "delta": "ok"}
        yield {"type": "end", "reason": "stop"}

    def recheck():
        account.unavailable = None
        return True

    monkeypatch.setattr(RelaySession, "stream", stream)
    runner.accounts = SimpleNamespace(recheck_exhausted=recheck, check_balance_now=lambda b: False)
    events = asyncio.run(run(runner, request(FIRST)))
    assert {"type": "text", "delta": "ok"} in events
    assert not any(event["type"] == "error" for event in events)


def _empty_stream(calls):
    def opener(*_a, **_kw):
        calls.append(1)
        return io.BytesIO(b"\n".join(json.dumps(e).encode() for e in (
            {"event": "metadata", "usage": {"creditsUsed": 0.03, "inputTokens": 368, "outputTokens": 0}},
            {"event": "end_of_stream"})) + b"\n")
    return opener


@pytest.mark.parametrize("purpose,expected", [("auto-mode-check", 1), ("client-tools", 5)])
def test_service_empty_turn_is_not_resent_and_is_saved_once(tmp_path, monkeypatch, purpose, expected):
    calls = []
    monkeypatch.setattr(direct.urllib.request, "urlopen", _empty_stream(calls))
    cfg = {"upstream_error_dir": str(tmp_path), "turn_timeout": 2}
    body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": purpose}]}
    session = SimpleNamespace(backend=SimpleNamespace(headers=lambda: {}, cfg={"base": "https://example.invalid"}),
                              _cancelled=False, _request_tag="req-empty")

    async def scenario():
        token = client_context.bind({"request_purpose": purpose, "request_id": "req-empty"})
        try:
            return [event async for event in direct._stream_body(cfg, session, body)]
        finally:
            client_context.reset(token)

    asyncio.run(scenario())
    assert len(calls) == expected
    assert len(list(tmp_path.glob("*-body.json"))) == 1
