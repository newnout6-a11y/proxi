"""The admin's pages with hundreds of accounts: the same answers, found without gathering or scanning per account.

With 501 accounts (2026-11-13) the accounts page took 35 s and the overview 30 s: the credentials were gathered again for
each account with an error and replaced one by one in every string, every loaded account was compared with every saved
one, and every poll read all 60 Retool balances from the network."""
import random
import time
from types import SimpleNamespace

import account_report
import admin
import credits


def test_the_account_index_answers_what_same_account_does():
    rng = random.Random(20261113)
    pick = lambda *choices: rng.choice(choices)  # noqa: E731
    for _ in range(300):
        def item():
            value = {}
            if rng.random() < 0.5:
                value["account_id"] = pick("a", "b", "c", "")
            if rng.random() < 0.7:
                value["upstream"] = pick("retool", "minara")
            value["base"] = pick("https://x.retool.com", "https://x.retool.com/", "https://y.retool.com", None)
            value["page_uuid"] = pick("p1", "p2", None)
            return value
        items = [item() for _ in range(rng.randint(0, 6))]
        holds = admin.Admin._account_index(items)
        for _ in range(10):
            other = item()
            assert holds(other) == any(admin.Admin._same_account(record, other) for record in items), (items, other)
            assert admin.Admin._account_index([other])(items[0]) == admin.Admin._same_account(other, items[0]) \
                if items else True


class Clock:
    now = 1000.0


class Backend:
    def __init__(self, account_id):
        self.cfg, self.unavailable = {"account_id": account_id}, None


def test_a_recent_retool_balance_is_the_answer_of_a_poll_and_an_old_one_is_read_again(monkeypatch):
    monkeypatch.setattr(credits.time, "monotonic", lambda: Clock.now)
    reads = []
    manager = credits.CreditsManager("https://fixture.invalid", lambda: {})
    monkeypatch.setattr(manager, "_get_balance", lambda force: reads.append(force) or {"available": 9.0})
    manager._balance = {"available": 42.0, "monthly_allocation": 100.0}
    manager._cached_at = Clock.now
    backend = Backend("a")
    runtime = SimpleNamespace(cfg={"account_balance_interval": 300}, accounts=SimpleNamespace(
        backends=[backend], managers={backend: manager}, status=lambda: [], state={}))
    monkeypatch.setattr(account_report, "account_managers", lambda _runtime: [(backend, manager)])
    monkeypatch.setattr(account_report, "account_states", lambda _runtime: {})
    Clock.now += 400  # older than the cache's 30 s, read by the dispatcher within two of its intervals
    infos = [entry["info"] for entry in account_report.collect(runtime)]
    assert reads == [] and infos[0]["available"] == 42.0 and infos[0]["stale"] is False
    Clock.now += 400  # older than two intervals: read again
    account_report.collect(runtime)
    assert reads == [False]
    account_report.collect(runtime, force_refresh=True)  # an explicit check always reads
    assert reads == [False, True]


def test_the_accounts_page_gathers_the_credentials_once(monkeypatch):
    calls = []
    real = admin.Admin._account_secrets
    monkeypatch.setattr(admin.Admin, "_account_secrets", lambda self: calls.append(1) or real(self))
    page = admin.Admin.__new__(admin.Admin)
    page.cfg, page._tokens_cache, page._mask_cache = {"cookie_values": ["x" * 20]}, None, None
    page.runtime = SimpleNamespace(accounts=SimpleNamespace(backends=[]))
    entries = [{"info": {"error": "boom %d %s" % (n, "x" * 20)}, "backend": SimpleNamespace(cfg={"account_id": str(n)}),
                "state": "active", "routable": True, "reserve": False, "available": 1.0, "auth_failures": 0}
               for n in range(50)]
    started = time.monotonic()
    accounts = page._accounts(entries, {"accounts": [{} for _ in entries]})
    assert len(calls) == 1 and time.monotonic() - started < 5
    assert all(item["error"] == "boom %d [redacted]" % n for n, item in enumerate(accounts))
