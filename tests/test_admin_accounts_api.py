"""Admin overview and metrics for several accounts: per-account state, aggregation, no secrets."""
import json

import pytest

import journal
from test_admin import admin_relay  # noqa: F401 - single-account fixture
from test_review_fixes_proxy import multi  # noqa: F401 - two-account relay fixture

ACCOUNT_KEYS = {"account", "state", "routable", "reserve", "available", "monthly_allocation", "used_month",
                "reset_date", "stale", "session_used", "turns", "auth_failures", "last_renewed_at", "error"}


def overview(multi):
    status, body, _ = multi.call("/admin/api/overview")
    assert status == 200
    return body


def test_overview_aggregates_balances_and_lists_each_account(multi):
    for manager, used in zip(multi.managers_, (30.0, 5.0)):
        manager.balance["used_month"] = used
    multi.managers_[0].record_turn("a1", 1.5)
    multi.managers_[1].record_turn("b1", 2.0)
    multi.managers_[1].record_turn("b2", 0.5)
    body = overview(multi)

    balance = body["balance"]
    assert balance["available"] == 100.0 - 1.5 + 7.0 - 2.5
    assert balance["monthly_allocation"] == 1000.0 and balance["used_month"] == 35.0
    assert balance["reset_date"] == "2026-12-01" and balance["stale"] is False
    assert balance["session_used"] == 4.0

    accounts = body["accounts"]
    assert [item["account"] for item in accounts] == multi.tags
    assert all(set(item) == ACCOUNT_KEYS for item in accounts)
    first, second = accounts
    assert (first["available"], second["available"]) == (98.5, 4.5)
    assert (first["monthly_allocation"], first["used_month"]) == (500.0, 30.0)
    assert (first["session_used"], first["turns"]) == (1.5, 1)
    assert (second["session_used"], second["turns"]) == (2.5, 2)
    assert first["state"] == second["state"] == "active"
    assert first["routable"] is second["routable"] is True
    assert first["reserve"] is second["reserve"] is False
    assert first["error"] is None and first["auth_failures"] == 0 and first["stale"] is False

    assert body["accounts_summary"] == {"total": 2, "routable": 2, "reserve": 0, "states": {"active": 2}}
    assert body["health"]["ok"] is True and body["health"]["upstream"] == "ok"
    assert body["usage"]["session_used"] == 4.0 and body["usage"]["turns"] == 3
    assert [item["turns"] for item in body["usage"]["accounts"]] == [1, 2]
    assert {item["account"] for item in body["usage"]["recent"]} == set(multi.tags)
    assert body["limits"] == {"min_account_balance": 20, "account_balance_interval": 300,
                              "upstream_empty_retries": 1, "max_workers": body["sessions"]["max_workers"],
                              "max_conversations": body["sessions"]["max_conversations"]}
    for key in ("uptime_seconds", "session", "sessions", "models", "resources", "connection"):
        assert key in body


def test_exhausted_accounts_with_credits_are_reserve_when_none_is_active(multi):
    multi.backends[0].unavailable = {"reason": "exhausted", "available": 12.0}
    multi.backends[1].unavailable = {"reason": "exhausted", "available": 0.0}
    body = overview(multi)
    first, second = body["accounts"]
    assert (first["state"], first["routable"], first["reserve"]) == ("exhausted", True, True)
    assert (second["state"], second["routable"], second["reserve"]) == ("exhausted", False, False)
    assert body["accounts_summary"] == {"total": 2, "routable": 1, "reserve": 1, "states": {"exhausted": 2}}
    assert body["health"]["ok"] is True
    status, health, _ = multi.call("/health", key=None)
    assert health["accounts"] == body["accounts_summary"] and status == 200


def test_active_account_keeps_exhausted_ones_out_of_the_reserve(multi):
    multi.backends[0].unavailable = {"reason": "exhausted", "available": 12.0}
    body = overview(multi)
    assert [(a["routable"], a["reserve"]) for a in body["accounts"]] == [(False, False), (True, False)]
    assert body["accounts_summary"]["routable"] == 1 and body["accounts_summary"]["reserve"] == 0


def test_health_needs_a_routable_account_with_a_fresh_balance(multi):
    multi.backends[0].unavailable = {"reason": "auth_dead"}
    multi.managers_[1].balance["stale"] = True
    body = overview(multi)
    assert body["health"]["ok"] is False and body["health"]["upstream"] == "degraded"
    assert body["accounts"][0]["state"] == "auth_dead" and body["accounts"][0]["routable"] is False
    assert body["accounts"][1]["stale"] is True and body["balance"]["stale"] is True
    multi.managers_[1].balance["stale"] = False
    assert overview(multi)["health"]["ok"] is True


def test_unknown_balance_is_null_and_skipped_in_the_sum(multi):
    multi.managers_[1].balance["available"] = None
    body = overview(multi)
    assert body["balance"]["available"] == 100.0
    assert body["accounts"][1]["available"] is None and body["accounts"][1]["error"] == "boom"
    assert body["health"]["error"] == "boom"


def test_overview_never_exposes_account_secrets(multi):
    multi.managers_[0].balance["stale_error"] = "failed accessToken=cookie-acc-a-123456 for page-acc-a"
    multi.managers_[1].balance["stale_error"] = "xsrfToken=q cookie-acc-b-123456 " + "x" * 400
    multi.managers_[0].record_turn("a1", 1.0)
    encoded = json.dumps(overview(multi))
    for secret in ("cookie-acc-a-123456", "cookie-acc-b-123456", "page-acc-a", "page-acc-b", "thread-a",
                   "thread-acc-b", "acc-a", "acc-b", "example.invalid", "test-key-123", "xsrfToken=q"):
        assert secret not in encoded, secret
    accounts = json.loads(encoded)["accounts"]
    assert len(accounts[1]["error"]) <= 200


def test_single_account_overview_has_the_same_contract(admin_relay):
    status, body, _ = admin_relay.request("/admin/api/overview")
    assert status == 200
    assert body["balance"]["available"] == 37.5 and body["health"]["ok"] is True
    assert len(body["accounts"]) == 1 and set(body["accounts"][0]) == ACCOUNT_KEYS
    account = body["accounts"][0]
    assert (account["state"], account["routable"], account["reserve"]) == ("active", True, False)
    assert account["available"] == 37.5 and account["session_used"] == 2.5 and account["turns"] == 1
    assert body["accounts_summary"] == {"total": 1, "routable": 1, "reserve": 0, "states": {"active": 1}}
    assert body["usage"]["session_used"] == 2.5 and body["usage"]["accounts"][0]["turns"] == 1
    assert body["limits"]["upstream_empty_retries"] == 1


@pytest.mark.parametrize("window,bucket,count", [(3600, 60, 60), (21600, 300, 72), (86400, 900, 96),
                                                 (7200, 300, 24), (600, 10, 60)])
def test_metrics_choose_the_bucket_from_the_window(multi, window, bucket, count):
    status, body, _ = multi.call("/admin/api/metrics?window=%d" % window)
    assert status == 200
    assert body["window_seconds"] == window and body["bucket_seconds"] == bucket
    assert len(body["buckets"]) == count


def test_metrics_accept_an_explicit_bucket_and_bound_it(multi):
    _, body, _ = multi.call("/admin/api/metrics?window=3600&bucket=300")
    assert body["bucket_seconds"] == 300 and len(body["buckets"]) == 12
    _, body, _ = multi.call("/admin/api/metrics?window=86400&bucket=1")
    assert len(body["buckets"]) <= 1440


@pytest.mark.parametrize("query", ["window=abc", "window=1.5", "bucket=abc", "window=3600&bucket=x"])
def test_metrics_reject_garbage_parameters(multi, query):
    status, body, _ = multi.call("/admin/api/metrics?" + query)
    assert status == 400 and body["error"]["code"] == "invalid_request_error"


def test_metrics_buckets_carry_token_columns_and_output_total(multi):
    journal.JOURNAL.reset()
    journal.emit("upstream.usage", "", turn_id="t1", credits=0.5, cache_read=900, cache_write=100,
                 input_tokens=20, output_tokens=40)
    _, body, _ = multi.call("/admin/api/metrics?window=21600")
    assert body["totals"]["output_tokens"] == 40 and body["totals"]["input_tokens"] == 20
    filled = [slot for slot in body["buckets"] if slot["credits"]]
    assert len(filled) == 1
    assert (filled[0]["cache_read"], filled[0]["cache_write"], filled[0]["input"], filled[0]["output"]) == (
        900, 100, 20, 40)
