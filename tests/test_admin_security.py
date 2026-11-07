"""Security hardening: /health redaction, wrong-key throttling, settings bounds."""


def test_health_hides_financials_and_error_text(relay):
    relay.credits.balance.update(stale=True, stale_error="HTTP Error 401: Unauthorized")
    status, body = relay.json_any("/health", key=None)
    assert status == 503 and body["ok"] is False
    assert body["upstream"] == "degraded"
    assert "error" not in body, "raw upstream error text must not leak unauthenticated"
    assert "available" not in body and "monthly_allocation" not in body


def test_health_keeps_readiness_signals(relay):
    status, body = relay.json_any("/health", key=None)
    assert status == 200 and body["ok"] is True
    assert body["balance_known"] is True
    assert body["upstream"] == "ok"
    assert "error" not in body


def test_wrong_key_attempts_are_throttled(relay, monkeypatch):
    import auth as auth_module
    monkeypatch.setattr(auth_module, "_FAILURE_LIMIT", 5)
    codes = []
    for _ in range(12):
        status, _body = relay.json_any("/v1/models", key="definitely-wrong-key")
        codes.append(status)
        if status == 429:
            break
    assert 429 in codes, "presented wrong keys must hit the always-on brute-force budget"
    assert codes[-1] == 429 and codes.index(429) == len(codes) - 1, "429 must come after 401s"


def test_missing_key_is_not_throttled_as_bruteforce(relay):
    # Health-check style keyless probes stay 401 and must never trip the budget.
    for _ in range(35):
        status, _body = relay.json_any("/v1/models", key=None)
        assert status == 401
