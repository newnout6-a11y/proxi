"""A POST to another account is not a retry: the rejected attempt cost nothing, so it never waits."""
import asyncio
import io
import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest

import direct
import upstream_attempts
from accounts import AccountDispatcher
from test_account_rate_limit import HOURLY_BODY, serve_ok
from test_native_context_reuse import FIRST, Backend, request, run, setup
from upstream_attempts import AttemptBudget


class FakeTime:
    """Replaces the `time` module of upstream_attempts: sleeping moves the clock, nothing really waits."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(upstream_attempts, "time", fake)
    return fake


def take(budget, account=None):
    return budget.take(lambda: False, account)


def test_the_first_post_of_a_step_never_waits_and_a_retry_on_the_same_account_still_does(clock):
    budget = AttemptBudget()
    account = object()
    take(budget, account)
    assert clock.now == 100.0
    budget.last = {"outcome": "error"}
    take(budget, account)
    assert clock.now == pytest.approx(100.5)
    take(budget, account)
    assert clock.now == pytest.approx(101.5)  # 0.5, then 1


def test_a_post_to_another_account_does_not_wait_and_its_own_backoff_starts_over(clock):
    budget = AttemptBudget()
    first, second, third = object(), object(), object()
    take(budget, first)
    budget.last = {"outcome": "error"}
    take(budget, second)
    take(budget, third)
    assert clock.now == 100.0  # two failovers, no waiting at all
    take(budget, third)
    assert clock.now == pytest.approx(100.5)  # the first retry on the third account: 0.5, not 2
    take(budget, third)
    assert clock.now == pytest.approx(101.5)


def test_the_attempt_limit_still_counts_every_account(clock):
    from apierr import ApiError
    budget = AttemptBudget()
    budget.last = {"outcome": "error"}
    accounts = [object() for _ in range(5)]
    for account in accounts:
        take(budget, account)
    with pytest.raises(ApiError) as caught:
        take(budget, object())
    assert caught.value.code == "upstream_attempt_limit" and budget.used == 5
    assert clock.now == 100.0


def test_an_unknown_account_keeps_the_old_backoff(clock):
    budget = AttemptBudget()
    budget.last = {"outcome": "error"}
    take(budget)
    take(budget)
    take(budget)
    assert clock.now == pytest.approx(101.5)


def test_a_new_step_starts_without_waiting_again(clock):
    budget = AttemptBudget()
    account = object()
    budget.last = {"outcome": "error"}
    take(budget, account)
    take(budget, account)
    waited = clock.now
    budget.new_step()
    take(budget, account)
    assert clock.now == waited


# -- the same through the real runner, pool and transport ---------------------------------------------------
def limited(request_):
    raise urllib.error.HTTPError(request_.full_url, 429, "Too Many Requests", Message(), io.BytesIO(HOURLY_BODY.encode()))


@pytest.fixture
def four_accounts(tmp_path, monkeypatch):
    """Four accounts in priority order behind one pool and a real dispatcher; AttemptBudget.wait is the real one."""
    delays = []
    real_wait = AttemptBudget.wait

    def spy(self, delay, cancelled):
        delays.append(delay)
        return real_wait(self, delay, cancelled)

    monkeypatch.setattr(AttemptBudget, "wait", spy)
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    settings = dict(transport="direct", project_context=False, cookie_values=[], cache_keepwarm_seconds=0,
                    upstream_error_dir=str(tmp_path))
    first, pool, runner = setup(tmp_path, base="https://acc-a.invalid", account_id="acc-a", **settings)
    for name in ("b", "c", "d"):
        pool.backends.append(Backend(tmp_path, base="https://acc-%s.invalid" % name, account_id="acc-" + name,
                                     **settings))
    for index, backend in enumerate(pool.backends):
        backend.headers = lambda: {}
        backend.cfg["priority"] = index
    runner.accounts = AccountDispatcher(list(pool.backends), runner.cfg, {})
    runner.credits = SimpleNamespace(record_turn=lambda key, cost: None)
    script, posts = {}, []

    def opener(request_, **_kwargs):
        account = request_.full_url.split("/")[2].split(".")[0].replace("acc-", "")
        posts.append(account)
        return script.get(account, serve_ok)(request_)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    return SimpleNamespace(runner=runner, pool=pool, script=script, posts=posts, delays=delays)


def test_failing_over_past_three_limited_accounts_costs_no_waiting(four_accounts):
    four_accounts.script.update(a=limited, b=limited, c=limited)
    events = asyncio.run(run(four_accounts.runner, request(FIRST)))
    assert four_accounts.posts == ["a", "b", "c", "d"]
    assert {"type": "text", "delta": "OK"} in events and not any(e["type"] == "error" for e in events)
    # The retries used to add 0.5 + 1 + 2 seconds; the real wait now runs with a delay of zero every time.
    assert four_accounts.delays and not any(four_accounts.delays)


def test_a_balance_failover_does_not_wait_either(four_accounts):
    def down(request_):
        raise urllib.error.HTTPError(request_.full_url, 503, "down", {}, io.BytesIO(b"down"))

    four_accounts.script.update(a=down)

    def exhausted(backend):
        backend.unavailable = {"reason": "exhausted", "available": 1, "threshold": 20}
        return True

    four_accounts.runner.accounts = SimpleNamespace(check_balance_now=exhausted)
    events = asyncio.run(run(four_accounts.runner, request(FIRST)))
    assert four_accounts.posts[:2] == ["a", "b"] and {"type": "text", "delta": "OK"} in events
    assert not any(four_accounts.delays)
