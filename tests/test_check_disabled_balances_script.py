"""Offline checks for the account balance diagnostic."""
import io
import json
import urllib.error

import pytest

from scripts import check_disabled_balances as script


ACCOUNT = {"base": "https://tenant.invalid/", "cookie": "accessToken=synthetic-account-cookie", "xsrf": "synthetic-xsrf"}


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    replies = {}

    def open_balance(cfg, url, *, headers, timeout):
        calls.append((cfg, url, headers, timeout))
        reply = replies[cfg["base"]]
        if isinstance(reply, BaseException):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(script.curl_pool, "retool_open", open_balance)
    return calls, replies


@pytest.mark.parametrize(("raw", "expected", "state"), [
    ("12.00456", 12.0046, "ok"), (" 4.50 ", 4.5, "low"), ("0", 0, "empty"),
    ("-0.5", -0.5, "empty"), (5, 5, "ok"), (False, None, "unknown"),
    ("nan", None, "unknown"), ("Infinity", None, "unknown"), (float("inf"), None, "unknown"),
    ("1e309", None, "unknown"), (10 ** 400, None, "unknown"), ({}, None, "unknown"), (None, None, "unknown"),
])
def test_retool_only_reports_finite_numbers(raw, expected, state, upstream):
    _, replies = upstream
    replies[ACCOUNT["base"]] = {"org": {"balance": raw}}
    row = script.check_retool(ACCOUNT)
    assert row["balance"] == expected
    assert row["state"] == state


@pytest.mark.parametrize("reply", [[], {"org": ["unexpected"]}, {"org": {}}, {"status": "ok"}])
def test_missing_balance_is_unknown(reply, upstream):
    _, replies = upstream
    replies[ACCOUNT["base"]] = reply
    row = script.check_retool(ACCOUNT)
    assert (row["state"], row["balance"]) == ("unknown", None)


@pytest.mark.parametrize(("status", "state"), [(401, "dead"), (403, "dead"), (502, "error")])
def test_http_auth_failures_stay_distinct_without_printing_the_response(status, state, upstream):
    calls, replies = upstream
    replies[ACCOUNT["base"]] = urllib.error.HTTPError(ACCOUNT["base"], status, "secret response", {}, None)
    row = script.check_retool(ACCOUNT)
    assert (row["state"], row["error"]) == (state, f"HTTP {status}")
    assert len(calls) == 1  # no token refresh or retry


def test_diagnostic_uses_the_effective_proxy_and_leaves_saved_credentials_unchanged(tmp_path, monkeypatch, capsys,
                                                                                upstream):
    calls, replies = upstream
    config_path, output = tmp_path / "config.json", tmp_path / "balances.json"
    default_proxy = "http://global-user:global-password@global-proxy.invalid:8080"
    account_proxy = "socks5://account-user:account-password@account-proxy.invalid:1080"
    records = [
        dict(ACCOUNT, label="retained", enabled=False),
        dict(ACCOUNT, base="https://second.invalid", label="override", enabled=False, proxy_url=account_proxy),
        dict(ACCOUNT, base="https://enabled.invalid", label="enabled"),
    ]
    config_path.write_text(json.dumps({"proxy_url": default_proxy, "cookie": "root-cookie", "accounts": records}),
                           encoding="utf-8")
    before = config_path.read_bytes()
    replies.update({r["base"]: {"org": {"balance": "25.5"}} for r in records})
    monkeypatch.setattr(script.sys, "argv", ["check_disabled_balances", "--config", str(config_path),
                                            "--json", str(output), "--groups"])

    script.main()

    request_by_label = {cfg["label"]: (cfg, url, headers) for cfg, url, headers, _ in calls}
    assert set(request_by_label) == {"retained", "override"}
    cfg, url, headers = request_by_label["retained"]
    assert (cfg["proxy_url"], url) == (default_proxy, "https://tenant.invalid/api/credits/balance")
    assert headers["cookie"] == ACCOUNT["cookie"]
    assert request_by_label["override"][0]["proxy_url"] == account_proxy
    assert config_path.read_bytes() == before
    saved_output = output.read_text(encoding="utf-8")
    rows = json.loads(saved_output)
    assert [(row["label"], row["balance"], row["state"]) for row in rows] == [
        ("retained", 25.5, "ok"), ("override", 25.5, "ok"),
    ]
    printed = capsys.readouterr().out
    for secret in (default_proxy, account_proxy, ACCOUNT["cookie"], "root-cookie"):
        assert secret not in printed + saved_output


def test_legacy_urllib_backend_cannot_silently_bypass_an_explicit_proxy(upstream):
    calls, _ = upstream
    row = script.check_retool(dict(ACCOUNT, retool_http_backend="urllib", proxy_url="http://proxy.invalid:8080"))
    assert row["state"] == "error"
    assert "curl_cffi" in row["error"]
    assert not calls


def test_existing_report_path_is_rejected_before_any_requests(tmp_path, monkeypatch, capsys, upstream):
    calls, _ = upstream
    config = tmp_path / "config.json"
    original = b'{"accounts": [{"enabled": false}]}'
    config.write_bytes(original)
    monkeypatch.setattr(script.sys, "argv", ["check_disabled_balances", "--config", str(config), "--json", str(config)])

    with pytest.raises(SystemExit, match="2"):
        script.main()

    assert not calls
    assert config.read_bytes() == original
    assert "already exists" in capsys.readouterr().err


def test_file_created_between_preflight_and_report_is_preserved(tmp_path, monkeypatch, upstream):
    config, report = tmp_path / "config.json", tmp_path / "balances.json"
    config.write_text(json.dumps({"accounts": [dict(ACCOUNT, enabled=False)]}), encoding="utf-8")

    def finish(_record):
        report.write_text("other writer", encoding="utf-8")
        return {"upstream": "retool", "label": "race", "state": "unknown", "balance": None,
                "enabled": False, "expires_at": None, "error": None}

    monkeypatch.setattr(script, "check", finish)
    monkeypatch.setattr(script.sys, "argv", ["check_disabled_balances", "--config", str(config), "--json", str(report)])

    with pytest.raises(SystemExit, match="2"):
        script.main()

    assert report.read_text(encoding="utf-8") == "other writer"
