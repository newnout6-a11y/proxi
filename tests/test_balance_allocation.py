"""A balance without a monthly allocation says so: the allocation is unknown, not zero, and the pool total is partial."""
import io
import json
import urllib.request

import pytest

import account_report
import console_log
import proxy
from credits import CreditsManager
from test_review_fixes_proxy import multi  # noqa: F401 - fixture


def manager_for(monkeypatch, org):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_kw: io.BytesIO(json.dumps({"org": org}).encode()))
    return CreditsManager("https://upstream.invalid", lambda: {})


@pytest.mark.parametrize("org", [{"balance": 42}, {"balance": 42, "monthlyAllocation": None}])
def test_a_missing_or_null_allocation_is_unknown_and_never_zero(monkeypatch, org):
    manager = manager_for(monkeypatch, org)
    info = manager.get_balance()
    assert info["available"] == 42.0
    assert info["monthly_allocation"] is None and info["used_month"] is None
    # The cached answer and the console snapshot say the same.
    assert manager.get_balance()["monthly_allocation"] is None
    assert manager.balance_snapshot()["monthly_allocation"] is None
    assert "limit=?" in console_log.balance_line("req", manager.balance_snapshot())


def test_an_explicit_zero_allocation_is_a_known_zero(monkeypatch):
    info = manager_for(monkeypatch, {"balance": 42, "monthlyAllocation": 0}).get_balance()
    assert info["monthly_allocation"] == 0.0 and info["used_month"] == 0.0
    assert account_report.aggregate_balance([info])["partial"] is False


def test_a_reported_allocation_is_used_as_before(monkeypatch):
    info = manager_for(monkeypatch, {"balance": 42, "monthlyAllocation": 100, "resetDate": "2026-12-01"}).get_balance()
    assert (info["monthly_allocation"], info["used_month"], info["reset_date"]) == (100.0, 58.0, "2026-12-01")


@pytest.mark.parametrize("org", [{"balance": 42, "monthlyAllocation": "invalid"}, {"balance": 42, "monthlyAllocation": True},
                                 {"balance": 42, "monthlyAllocation": "NaN"}, {"monthlyAllocation": 100}])
def test_a_malformed_balance_or_allocation_is_still_an_error(monkeypatch, org):
    info = manager_for(monkeypatch, org).get_balance()
    assert info["available"] is None and info["error"]


def test_the_pool_total_is_partial_when_a_known_balance_has_no_allocation(monkeypatch):
    known = manager_for(monkeypatch, {"balance": 100, "monthlyAllocation": 500}).get_balance()
    unknown = manager_for(monkeypatch, {"balance": 50}).get_balance()
    report = account_report.aggregate_balance([known, unknown])
    assert (report["available"], report["monthly_allocation"]) == (150.0, 500.0)
    assert (report["partial"], report["unknown_accounts"], report["accounts_total"]) == (True, 0, 2)
    # Nothing known about any allocation: no total at all, and the figures are still partial.
    both = account_report.aggregate_balance([unknown, unknown])
    assert both["monthly_allocation"] is None and both["partial"] is True and both["available"] == 100.0


def test_the_relay_reports_the_unknown_allocation_over_http_and_in_the_banner(multi, monkeypatch):
    runtime = multi.runner_.runtime
    second = runtime.accounts.backends[1]
    original = urllib.request.urlopen

    def route(request, **kwargs):
        if request.full_url.startswith("https://upstream.invalid"):
            return io.BytesIO(json.dumps({"org": {"balance": 7}}).encode())
        return original(request, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", route)
    real = CreditsManager("https://upstream.invalid", lambda: {})
    runtime.accounts.managers[second] = second.credits = real
    status, body, _ = multi.call("/v1/balance")
    assert status == 200 and body["partial"] is True and body["unknown_accounts"] == 0
    assert (body["available"], body["monthly_allocation"]) == (107.0, 500.0)
    first, last = body["accounts"]
    assert first["monthly_allocation"] == 500.0 and last["monthly_allocation"] is None

    balance = proxy.startup_balance(runtime)
    line = next(item for item in proxy.banner_lines(runtime.cfg, balance) if item.startswith(" balance:"))
    assert line == " balance:       107.0 / 500.0  (partial: monthly allocation unknown for some accounts)"


def test_a_banner_with_no_known_allocation_does_not_print_none(multi):
    cfg = multi.runner_.runtime.cfg
    report = {"available": 7.0, "monthly_allocation": None, "partial": True, "unknown_accounts": 0, "accounts_total": 1}
    line = next(item for item in proxy.banner_lines(cfg, report) if item.startswith(" balance:"))
    assert line == " balance:       7.0 / ?  (partial: monthly allocation unknown for some accounts)"
    assert "None" not in line
