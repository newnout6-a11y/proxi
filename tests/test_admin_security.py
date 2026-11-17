"""Security hardening: /health redaction, wrong-key throttling, settings bounds."""
import pytest

import auth as auth_module


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


@pytest.fixture
def budget(monkeypatch):
    """A wrong-key budget of 5 failures (auth.py), with no failure recorded yet."""
    monkeypatch.setattr(auth_module, "_FAILURES", {})
    monkeypatch.setattr(auth_module, "_FAILURE_LIMIT", 5)


@pytest.fixture
def from_another_host(monkeypatch):
    """The test relay is reached over loopback, which has no budget: its clients are taken for another host's."""
    monkeypatch.setattr(auth_module, "is_loopback", lambda client_ip: False, raising=False)


def test_wrong_key_attempts_are_throttled(relay, budget, from_another_host):
    codes = []
    for _ in range(12):
        status, _body = relay.json_any("/v1/models", key="definitely-wrong-key")
        codes.append(status)
        if status == 429:
            break
    assert 429 in codes, "presented wrong keys must hit the always-on brute-force budget"
    assert codes[-1] == 429 and codes.index(429) == len(codes) - 1, "429 must come after 401s"


@pytest.mark.parametrize("path", ["/v1/models", "/admin/api/overview"])
def test_a_source_past_its_budget_is_refused_before_its_key_is_compared(relay, budget, from_another_host, path):
    """The budget was looked at only after a wrong key: a source past it still had every key compared, so an attacker who
    ignored the 429 guessed on at full speed, and the right guess was let in."""
    for _ in range(6):
        relay.json_any("/v1/models", key="definitely-wrong-key")
    status, body = relay.json_any(path)  # the right key
    assert status == 429 and body["error"]["message"] == "too many auth failures"


def test_wrong_keys_from_loopback_stay_a_final_401(relay, budget):
    """Every local client shares 127.0.0.1. Once any local process had spent the budget, a local client with a stale key
    got 429 with Retry-After, which Claude Code and the OpenAI SDK retry (each retry kept the budget spent), instead of the
    401 they stop at."""
    codes = [relay.json_any("/v1/models", key="definitely-wrong-key")[0] for _ in range(12)]
    assert codes == [401] * 12
    assert relay.json_any("/v1/models")[0] == 200
    assert auth_module._FAILURES == {}


def test_missing_key_is_not_throttled_as_bruteforce(relay):
    # Health-check style keyless probes stay 401 and must never trip the budget.
    for _ in range(35):
        status, _body = relay.json_any("/v1/models", key=None)
        assert status == 401


def test_a_flood_of_wrong_keys_keeps_only_the_failures_the_budget_looks_at(budget):
    """A source kept every failure of its window, and each failure filtered them all under the lock: a flood levelled off
    near 30,000 stamps at about 2 ms of CPU a wrong key, and the requests with the right key waited behind it."""
    for _ in range(5000):
        auth_module.register_auth_failure("203.0.113.7")
    assert len(auth_module._FAILURES["203.0.113.7"]) == auth_module._FAILURE_LIMIT + 1


def test_the_wait_ends_when_the_failures_of_the_window_are_back_within_the_budget(budget, monkeypatch):
    """Retry-After was counted from the oldest failure of the window, so a source that had failed more often than the
    budget and waited as told was refused again."""
    clock = [1000.0]
    monkeypatch.setattr(auth_module.time, "time", lambda: clock[0])
    for second in range(8):  # a wrong key a second: the sixth spends the budget of 5
        clock[0] = 1000.0 + second
        wait = auth_module.register_auth_failure("203.0.113.7")
    assert wait == 55.0  # the six last failures are 1002 to 1007, and 1002 leaves the window at 1062
    clock[0] += wait
    assert auth_module.auth_failure_wait("203.0.113.7") is None


@pytest.mark.parametrize("address, loopback", [("127.0.0.1", True), ("127.8.9.10", True), ("::1", True),
                                               ("::ffff:127.0.0.1", True), ("10.0.0.1", False),
                                               ("192.168.1.20", False), ("::ffff:10.0.0.1", False),
                                               ("203.0.113.7", False), ("", False)])
def test_loopback_is_127_0_0_0_8_and_1_also_as_an_ipv4_mapped_address(address, loopback):
    assert auth_module.is_loopback(address) is loopback
