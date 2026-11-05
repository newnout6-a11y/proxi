"""The balance report asks up to eight accounts at once, whatever account_refresh_parallel says about renewals."""
import threading
from types import SimpleNamespace

import pytest

import account_report
from accounts import AccountDispatcher
from test_account_dispatcher import Account, Clock


class Counting:
    """A balance source that records how many lookups are in flight and waits until `want` of them are."""

    def __init__(self, want, window=0.3):
        self.want, self.window = want, window
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.full = threading.Event()

    def manager(self, available):
        outer = self

        class Manager:
            def get_balance(self, force_refresh=False):
                with outer.lock:
                    outer.active += 1
                    outer.peak = max(outer.peak, outer.active)
                    if outer.active >= outer.want:
                        outer.full.set()
                # Held until enough lookups overlap, so a smaller pool shows up as a smaller peak.
                outer.full.wait(outer.window)
                with outer.lock:
                    outer.active -= 1
                return {"available": available, "monthly_allocation": 500.0}

            def balance_snapshot(self):
                return {"available": available}

        return Manager()


def runtime_of(count, source, **cfg):
    backends = [Account("acc-%d" % index) for index in range(count)]
    managers = {backend: source.manager(100.0 + index) for index, backend in enumerate(backends)}
    dispatcher = AccountDispatcher(backends, {"session_refresh_interval": 0, **cfg}, managers, clock=Clock())
    return SimpleNamespace(accounts=dispatcher, backend=backends[0], credits=managers[backends[0]], cfg=dict(cfg))


@pytest.mark.parametrize("refresh_parallel", [1, 3, 32])
def test_the_report_uses_eight_lookups_whatever_the_renewal_setting_is(refresh_parallel):
    source = Counting(want=8)
    runtime = runtime_of(12, source, account_refresh_parallel=refresh_parallel)
    entries = account_report.collect(runtime, force_refresh=True)
    assert len(entries) == 12 and [entry["available"] for entry in entries] == [100.0 + i for i in range(12)]
    assert source.peak == 8
    assert account_report.BALANCE_PARALLEL == 8


def test_a_small_pool_is_asked_all_at_once():
    source = Counting(want=5)
    entries = account_report.collect(runtime_of(5, source, account_refresh_parallel=1), force_refresh=True)
    assert len(entries) == 5 and source.peak == 5


def test_a_lone_account_is_not_given_a_pool():
    source = Counting(want=1)
    entries = account_report.collect(runtime_of(1, source, account_refresh_parallel=1), force_refresh=True)
    assert len(entries) == 1 and source.peak == 1


def test_the_setting_still_bounds_the_renewal_checks_of_the_dispatcher():
    runtime = runtime_of(12, Counting(want=1), account_refresh_parallel=2)
    assert runtime.accounts.parallel == 2
