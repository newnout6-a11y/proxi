"""The cost guard prices a Minara request before it is sent, with the formula of its debit (design_routing_errors.md §6)."""
import http.client
import json
import threading
import types

import pytest

import apierr
import config
import cost_guard
import journal
import minara
import minara_catalog
import models
import proxy
from test_minara_failover import Response, ok_stream

CATALOG = {entry["id"]: entry for entry in json.load(open(minara_catalog.__file__.replace("minara_catalog.py",
                                                                                          "minara_models.json"),
                                                         encoding="utf-8"))["models"]}
OPUS = CATALOG["minara/claude-opus-5-5"]       # flat price, explicit cache
HAIKU = CATALOG["minara/claude-haiku-5-5"]     # tiered: len > 100000 costs five times as much
ASTRA = CATALOG["minara/gpt-6-astra"]          # tiered: len > 272000 doubles; automatic cache


def debit(entry, prompt, read, written, output):
    """What Minara debits for a reply with these tokens: its usage as Minara reports it, through normalize_usage."""
    details = {"cached_tokens": read, "cache_write_tokens": written}
    usage = minara.normalize_usage({"prompt_tokens": prompt, "completion_tokens": output,
                                    "prompt_tokens_details": details}, entry["pricing"])
    return usage["provider_cost"]


@pytest.mark.parametrize("entry,prompt,warm", [
    (OPUS, 470_000, 0), (OPUS, 470_000, 460_000),
    (HAIKU, 90_000, 0), (HAIKU, 120_000, 0), (HAIKU, 120_000, 110_000),
    (ASTRA, 250_000, 0), (ASTRA, 300_000, 0), (ASTRA, 780_000, 775_000)])
def test_the_estimate_is_the_debit_of_the_same_tokens(entry, prompt, warm):
    estimate = cost_guard.estimate(entry, prompt_tokens=prompt, basis="estimate", warm_prefix=warm, max_tokens=4000,
                                   effort="high")
    explicit = entry.get("cache") == "explicit"
    new = prompt - warm
    # Minara's prompt_tokens include what was read and written; the price tiers read the whole prompt.
    expected = debit(entry, prompt, warm, new if explicit else 0, 4000)
    assert estimate.usd == pytest.approx(expected, rel=1e-3)
    assert estimate.credits == pytest.approx(expected * minara_catalog.CREDITS_PER_USD, abs=0.01)  # cents of a credit
    assert (estimate.cache_read, estimate.written, estimate.warm) == (warm, new, warm > 0)


def test_a_cold_fork_of_a_huge_session_costs_thousands_and_its_warm_turn_does_not():
    cold = cost_guard.estimate(ASTRA, prompt_tokens=973_619, basis="estimate", warm_prefix=0, max_tokens=None,
                               effort="max")
    warm = cost_guard.estimate(ASTRA, prompt_tokens=780_000, basis="estimate", warm_prefix=775_000, max_tokens=None,
                               effort="max")
    assert cold.credits > 3000 and warm.credits < 1500  # the agents' fork against the owner's running session


def test_the_output_is_priced_at_the_levels_budget_or_the_requests_cap():
    assert cost_guard.output_budget(None, "max") == 32000 and cost_guard.output_budget(None, "low") == 2000
    assert cost_guard.output_budget(500, "max") == 500 and cost_guard.output_budget(None, None) == 8000


def test_a_model_without_a_readable_price_is_not_guarded():
    assert cost_guard.estimate({"id": "minara/x"}, prompt_tokens=10, basis="estimate", warm_prefix=0, max_tokens=1,
                               effort=None) is None


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_dialog_is_warm_after_a_priced_turn_and_a_first_contact_is_cold():
    clock = Clock()
    guard = cost_guard.Guard(clock=clock)
    assert guard.warm_prefix("conv", model="m", account="a", shared=True) == 0  # a fork: never seen
    assert guard.warm_prefix("conv", model="m", account="a", shared=False, session_prefix=5000) == 5000  # restored
    guard.spent(1.5, conversation="conv", account="a", model="m", prompt_tokens=300_000)
    assert guard.warm_prefix("conv", model="m", account="b", shared=True) == 300_000  # Claude: any account reads it
    assert guard.warm_prefix("conv", model="m", account="b", shared=False) == 0  # an account's own cache
    assert guard.warm_prefix("conv", model="m", account="a", shared=False) == 300_000
    assert guard.warm_prefix("conv", model="other", account="a", shared=True) == 0
    clock.now += cost_guard.WARM_SHARED + 1
    assert guard.warm_prefix("conv", model="m", account="a", shared=True) == 0


CFG = {"cost_guard": "block", "cost_guard_request_minara": 1500, "cost_guard_hour_minara": 12000,
       "cost_guard_warn_minara": 300}


def priced(credits, warm=False):
    return cost_guard.Estimate(credits, credits / 220, 470_000, 0, 470_000, 32000, warm, "estimate")


def test_a_request_above_the_ceiling_is_refused_with_a_final_error_that_names_the_estimate():
    error, warn = cost_guard.Guard().check(CFG, priced(1820), entry=ASTRA, account="3c8545cb")
    assert warn and (error.status, error.code) == (400, "cost_limit_exceeded")
    assert "1,820 Minara credits" in error.message and "cost_guard_request_minara = 1,500" in error.message
    assert "x-proxi-cost-confirm: 1900" in error.message and "account 3c8545cb" in error.message
    assert "cold cache" not in error.message and "without a cache" in error.message  # automatic cache: billed as input
    # Final in every dialect: Codex stops, Claude Code gets an invalid request, a retry header says no.
    assert apierr.responses_code(error.code, error.status, error.headers) == "invalid_prompt"
    assert apierr.anthropic_type(error.code, error.status) == "invalid_request_error"
    assert error.headers == {"x-should-retry": "false"}


def test_below_the_ceiling_a_dear_request_is_named_and_a_cheap_one_is_quiet():
    guard = cost_guard.Guard()
    assert guard.check(CFG, priced(1200), entry=OPUS, account="a") == (None, True)
    assert guard.check(CFG, priced(12), entry=OPUS, account="a") == (None, False)


def test_the_client_may_allow_one_dear_request_up_to_a_sum():
    guard = cost_guard.Guard()
    assert guard.check(CFG, priced(1820), entry=OPUS, account="a", confirm="2000")[0] is None
    assert guard.check(CFG, priced(1820), entry=OPUS, account="a", confirm="1000")[0].code == "cost_limit_exceeded"
    assert guard.check(CFG, priced(1820), entry=OPUS, account="a", confirm="soon")[0].code == "cost_limit_exceeded"


@pytest.mark.parametrize("mode,refused", [("warn", False), ("off", False), ("block", True)])
def test_warn_only_names_and_off_prices_nothing(mode, refused):
    error, _ = cost_guard.Guard().check(dict(CFG, cost_guard=mode), priced(5000), entry=OPUS, account="a")
    assert (error is not None) is refused


def test_a_ceiling_of_zero_is_none():
    assert cost_guard.Guard().check(dict(CFG, cost_guard_request_minara=0), priced(5000), entry=OPUS,
                                    account="a")[0] is None


def test_the_hour_is_a_guard_against_loops_with_a_retry_after():
    clock = Clock()
    guard = cost_guard.Guard(clock=clock)
    for _ in range(119):
        guard.spent(100, conversation="loop", account="a", model="m", prompt_tokens=10)
        clock.now += 10
    error, _ = guard.check(CFG, priced(200), entry=OPUS, account="a")
    assert (error.status, error.code) == (429, "rate_limit_error") and "cost_guard_hour_minara" in error.message
    assert 0 < error.retry_after <= 3600
    clock.now += 3600
    assert guard.check(CFG, priced(200), entry=OPUS, account="a")[0] is None


def test_a_keys_own_ceilings_and_its_day_which_outlives_a_restart(tmp_path):
    clock = Clock()
    path = str(tmp_path / cost_guard.STATE_NAME)
    key = {"id": "agents", "max_request_minara": 100, "max_day_minara": 500}
    guard = cost_guard.Guard(path, clock=clock)
    error, _ = guard.check(CFG, priced(150), entry=OPUS, account="a", key=key)
    assert error.code == "cost_limit_exceeded" and "max_request_minara of client key 'agents'" in error.message
    guard.spent(450, key_id="agents", conversation="c", account="a", model="m", prompt_tokens=1)
    restarted = cost_guard.Guard(path, clock=clock)
    assert restarted.key_spent("agents") == 450
    error, _ = restarted.check(CFG, priced(80), entry=OPUS, account="a", key=key)
    assert error.code == "cost_limit_exceeded" and "max_day_minara" in error.message
    clock.now += 86400  # the next day
    assert restarted.check(CFG, priced(80), entry=OPUS, account="a", key=key)[0] is None


@pytest.mark.parametrize("settings", [{"cost_guard": "on"}, {"cost_guard_request_minara": -1},
                                      {"cost_guard_hour_minara": True}, {"cost_guard_warn_minara": "300"}])
def test_the_config_refuses_a_bad_value(tmp_path, monkeypatch, settings):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua",
                                "cookie": "accessToken=fake-token", "xsrf": "fake-xsrf",
                                "workspace_root": str(tmp_path), **settings}), encoding="utf-8")
    with pytest.raises(apierr.ApiError):
        config.load(str(path))


KEY = "cost-guard-local-key-0123456789012345678"


@pytest.fixture
def minara_relay(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "_MINARA_MODELS", dict(models._MINARA_MODELS))
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def start(**settings):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "accounts": [{"account_id": "guard-1", "label": "Minara 1", "upstream": "minara",
                          "bearer_token": "synthetic-bearer-1"}],
            "model": "minara/claude-opus-5-5", "relay_api_key": KEY, "workspace_root": str(tmp_path),
            "project_context": False, "exec_policy": "off", "conversation_state": False, "cache_keepwarm_seconds": 0,
            "session_refresh_interval": 0, "account_balance_interval": 0, "minara_channel_retry_delays": [],
            "journal_path": str(tmp_path / "journal.jsonl"), "prompts_file": str(tmp_path / "absent.json"),
            **settings}), encoding="utf-8")
        runtime = proxy.Runtime(config.load(str(path)))
        posts = []
        for backend in runtime.accounts.backends:
            backend._minara_post = lambda url, **kwargs: posts.append(kwargs["json"]) or Response(200, ok_stream("ok"))
            backend._minara_get = lambda url, **kwargs: types.SimpleNamespace(
                status_code=200, json=lambda: {"creditBalanceBreakdown": {"plan": "2000", "extra": "0"}})
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def ask(headers=None, max_tokens=4000):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request("POST", "/v1/chat/completions", json.dumps({
                "model": "minara/claude-opus-5-5", "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": "hi " * 2000}]}),
                {"Authorization": "Bearer " + KEY, "Content-Type": "application/json", **(headers or {})})
            reply = connection.getresponse()
            body = json.loads(reply.read().decode())
            connection.close()
            return reply.status, body

        started.append((server, runtime))
        return ask, posts

    started = []
    yield start
    for server, runtime in started:
        server.shutdown()
        server.server_close()
        runtime.close()


def test_a_refused_request_never_reaches_minara(minara_relay):
    # The default is "warn" until the estimate is calibrated; "block" is the operator's choice.
    ask, posts = minara_relay(cost_guard="block", cost_guard_request_minara=1)
    before = journal.JOURNAL.last_seq
    status, body = ask()
    assert status == 400 and body["error"]["code"] == "cost_limit_exceeded" and posts == []
    assert "Minara credits" in body["error"]["message"]
    blocked = [event for event in journal.JOURNAL.query(since=before, limit=2000) if event["kind"] == "cost.blocked"]
    assert len(blocked) == 1 and blocked[0]["level"] == "warn" and blocked[0]["data"]["credits"] > 1
    # The master key may allow it.
    status, _ = ask({cost_guard.CONFIRM_HEADER: "100"})
    assert status == 200 and len(posts) == 1


def test_an_ordinary_request_goes_through_and_its_debit_is_kept(minara_relay):
    ask, posts = minara_relay()
    before = journal.JOURNAL.last_seq
    status, _ = ask()
    assert status == 200 and len(posts) == 1
    kinds = [event["kind"] for event in journal.JOURNAL.query(since=before, limit=2000)]
    assert "cost.estimate" in kinds and "cost.blocked" not in kinds
