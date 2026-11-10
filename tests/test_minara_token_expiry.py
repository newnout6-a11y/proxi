"""A Minara token's expiry: the balance answer names it, the dispatcher acts on it without asking Minara.

Every token is a synthetic JWT (test_minara_accounts.jwt); no test reaches api.minara.ai."""
import json
import time

import pytest

import accounts
import config
import journal
import models
import proxy
from accounts import AccountDispatcher, MinaraAccount
from test_minara_accounts import DAY, NOW, jwt

BALANCE = {"creditBalanceBreakdown": {"plan": "120.5", "extra": "99.5"}, "creditBalance": {"total": "300", "reward": "200"}}


class Backend:
    def __init__(self, token, **cfg):
        self.cfg = {"upstream": "minara", "bearer_token": token, "account_id": "acc-" + str(id(self)), **cfg}
        self.unavailable = None


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def account(token, reads=None, **cfg):
    backend = Backend(token, **cfg)

    def fetch():
        if reads is not None:
            reads.append(backend)
        return json.loads(json.dumps(BALANCE))
    return backend, MinaraAccount(backend.cfg, fetch=fetch, backend=backend)


def dispatcher(*pairs, wall=None, **cfg):
    wall = wall or Clock(NOW)
    value = AccountDispatcher([backend for backend, _ in pairs], cfg, dict(pairs), clock=lambda: 5000.0, wall=wall)
    return value, wall


def _events(kind, since):
    return [event for event in journal.JOURNAL.query(since=since, order="asc") if event["kind"] == kind]


# -- the balance answer (judged on the real clock: the tokens are made relative to it) ---------------------------------
def test_every_balance_answer_names_the_token_expiry():
    expiry = int(time.time()) + 9 * DAY
    reads = []
    backend, manager = account(jwt(exp=expiry), reads)
    assert manager.balance_snapshot()["expires_at"] == expiry  # before the first read
    info = manager.get_balance()
    assert info["expires_at"] == expiry and info["available"] == 220 and info["unit"] == "minara_credits"
    assert manager.get_balance()["expires_at"] == expiry and len(reads) == 1  # the cached answer too
    assert manager.balance_snapshot()["expires_at"] == expiry
    backend.cfg["bearer_token"] = jwt(exp=expiry + 20 * DAY)  # a new token is read again
    assert manager.get_balance(force_refresh=True)["expires_at"] == expiry + 20 * DAY


def test_an_expired_token_is_never_sent_for_the_balance():
    expiry = int(time.time()) - 1
    reads = []
    _, manager = account(jwt(exp=expiry), reads)
    info = manager.get_balance(force_refresh=True)
    assert reads == [] and info["error"] == "token_expired" and info["available"] is None
    assert info["expires_at"] == expiry and manager.last_error == "token_expired"


def test_a_token_that_is_no_jwt_is_read_as_before():
    reads = []
    _, manager = account("opaque-bearer-token-value", reads)
    info = manager.get_balance()
    assert reads and info["available"] == 220 and info["expires_at"] is None and "error" not in info


# -- the dispatcher ----------------------------------------------------------------------------------------------------
def test_an_expired_account_leaves_the_pool_without_a_request(capsys):
    reads = []
    dead_backend, dead = account(jwt("dead-user", exp=NOW - 60), reads)
    live_backend, live = account(jwt("live-user", exp=NOW + 20 * DAY), reads)
    value, _ = dispatcher((dead_backend, dead), (live_backend, live))
    before = journal.JOURNAL.last_seq
    assert value.check_expiry() == [dead_backend]
    assert dead_backend.unavailable == {"reason": "auth_dead", "error": "token_expired", "expires_at": NOW - 60}
    assert live_backend.unavailable is None
    events = _events("account.auth_dead", before)
    assert len(events) == 1 and events[0]["data"]["error"] == "token_expired"
    assert events[0]["data"]["expires_at"] == NOW - 60 and events[0]["data"]["account"] == accounts.account_tag(dead_backend)
    out = capsys.readouterr().out
    assert out.count("токен Minara истёк") == 1 and "нужен новый токен" in out
    # Judged again every tick: nothing more is said, and the balance is never asked for.
    assert value.check_expiry() == [dead_backend] and not _events("account.auth_dead", events[0]["seq"])
    assert value.check_balance(dead_backend) is False and reads == []
    assert not [job for job in value.due(now=10_000_000.0) if job[0] is dead_backend]
    assert value.status()[0]["state"] == "auth_dead"


def test_a_token_with_less_than_three_days_left_is_announced_once_a_day(capsys):
    soon_backend, soon = account(jwt("soon", exp=NOW + 2 * DAY + 3 * 3600))
    later_backend, later = account(jwt("later", exp=NOW + 3 * DAY + 23 * 3600 + 60))
    off_backend, off = account(jwt("off", exp=NOW + DAY), enabled=False)
    value, wall = dispatcher((soon_backend, soon), (later_backend, later), (off_backend, off))
    before = journal.JOURNAL.last_seq
    assert value.check_expiry() == []
    events = _events("account.token_expiring", before)
    assert [event["data"]["account"] for event in events] == [accounts.account_tag(soon_backend)]
    assert events[0]["level"] == "warn" and events[0]["data"]["expires_at"] == NOW + 2 * DAY + 3 * 3600
    assert events[0]["data"]["days_left"] == pytest.approx(2.125, abs=0.01)
    out = capsys.readouterr().out
    assert out.count("токен Minara истекает") == 1 and "через 2 дн 3 ч" in out
    assert soon_backend.unavailable is None  # still serving until it expires
    wall.now = NOW + 23 * 3600
    value.check_expiry()
    assert len(_events("account.token_expiring", before)) == 1 and capsys.readouterr().out == ""
    wall.now = NOW + DAY
    value.check_expiry()  # a day later: once more, and now the second account is under three days too
    assert [event["data"]["account"] for event in _events("account.token_expiring", before)] == [
        accounts.account_tag(soon_backend), accounts.account_tag(soon_backend), accounts.account_tag(later_backend)]


def test_an_unknown_expiry_changes_nothing(capsys):
    reads = []
    pairs = [account(token, reads) for token in ("opaque-bearer-token-value", jwt(exp=None), "eyJ.not-base64!.x")]
    value, _ = dispatcher(*pairs)
    before = journal.JOURNAL.last_seq
    assert value.check_expiry() == []
    assert all(backend.unavailable is None for backend, _ in pairs)
    assert not [event for event in journal.JOURNAL.query(since=before) if event["kind"].startswith("account.")]
    assert capsys.readouterr().out == ""
    assert value.check_balance(pairs[0][0]) is False and len(reads) == 1  # read as before


def test_an_account_that_gets_a_valid_token_comes_back(capsys):
    backend, manager = account(jwt(exp=NOW - 1))
    value, _ = dispatcher((backend, manager), session_refresh_interval=900)
    value.check_expiry()
    assert backend.unavailable["reason"] == "auth_dead"
    value.state[backend]["next_balance"] = 9e9
    backend.cfg["bearer_token"] = jwt(exp=NOW + 30 * DAY)
    assert value.check_expiry() == []
    assert backend.unavailable is None and value.state[backend]["next_balance"] == 5000.0
    assert "снова в пуле" in capsys.readouterr().out


def test_an_exhausted_account_whose_token_expired_is_not_reported_as_topped_up():
    reads = []
    backend, manager = account(jwt(exp=NOW + DAY), reads)
    value, wall = dispatcher((backend, manager), minara_min_credits=500)
    assert value.check_balance(backend) is True and backend.unavailable["reason"] == "exhausted"
    wall.now = NOW + DAY
    # recheck_exhausted takes False for "restored": the account is still below the threshold, and out as auth_dead.
    assert value.check_balance(backend) is True and len(reads) == 1
    assert backend.unavailable["reason"] == "auth_dead"


def test_a_real_relay_routes_around_an_expired_minara_account(tmp_path, monkeypatch):
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    tokens = [jwt("expired-user", exp=int(time.time()) - DAY), jwt("live-user", exp=int(time.time()) + 30 * DAY)]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "minara/claude-opus-5-5", "relay_api_key": "relay-key-for-tests-123",
                                "workspace_root": str(tmp_path), "conversation_state": False,
                                "accounts": [{"label": "Minara %d" % n, "upstream": "minara", "bearer_token": token}
                                             for n, token in enumerate(tokens, 1)]}), encoding="utf-8")
    runtime = proxy.Runtime(config.load(str(path)))
    try:
        expired, live = runtime.accounts.backends
        assert runtime.accounts.check_expiry() == [expired]
        entry = models.resolve("minara/claude-opus-5-5", cfg=runtime.cfg)[0]
        assert {runtime.pool._next_backend(entry) for _ in range(4)} == {live}
        assert runtime.accounts.managers[expired].get_balance()["error"] == "token_expired"
    finally:
        runtime.close()
