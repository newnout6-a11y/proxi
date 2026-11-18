"""A route alias (routes[] in config.json) runs as one of its targets: the pool picks the account for a target, the turn is
bound to that target, and a refusal or failure is that target's.

Audits of 2026-11-18: a Retool plan refusal of a smart-opus turn was kept under "smart-opus", which no account choice saw,
so the alias got a settled 404 while its Minara target was free (A08-01, A12-02); the turn was bound to the first target
its account could serve by configuration, whatever target the pool had picked the account for (A08-02); and a failover
took the whole key out of the request, so a fallback model of the same provider was never tried (A17-01, A12-01)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import config as config_module
import journal
import models
import orchestrator
from accounts import AccountDispatcher
from catalog import router
from session import Backend, SessionPool
from test_account_dispatcher import Clock
from test_account_rate_limit import _events, retool, serve_ok, tag, turn  # noqa: F401
from test_native_context_reuse import run
from test_retool_plan_refusal import OPUS, SONNET, refused, refuses, texts, errors

MINARA_OPUS = "minara/claude-opus-5-5"


def load(tmp_path, monkeypatch, accounts, routes):
    """config.load of a config.json in tmp_path (fake credentials, no network), as the relay builds it."""
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"relay_api_key": "relay-test-key-0123456789abcdef", "ua": "Mozilla/5.0",
                                "workspace_root": str(tmp_path), "conversation_state": False,
                                "accounts": accounts, "routes": routes}), encoding="utf-8")
    return config_module.load(str(path))


RETOOL = {"base": "https://t1.retool.invalid", "page_uuid": "p1", "cookie": "accessToken=AAAAAAAAAAAA",
          "xsrf": "BBBBBBBBBB", "provider_resource_name": "__managed__"}
MINARA = {"upstream": "minara", "bearer_token": "minara-token-0123456789"}


def alias_pool(tmp_path, monkeypatch, targets, minara=True):
    cfg = load(tmp_path, monkeypatch, [RETOOL, MINARA] if minara else [RETOOL],
               [{"id": "smart-opus", "targets": targets, "strategy": "priority"}])
    pool = SessionPool(Backend(cfg))
    for backend in pool.backends:
        backend.unavailable, backend.refused_models = None, {}
    return cfg, pool, models.resolve("smart-opus", cfg=cfg)[0]


def acquired(pool, entry, key="conv-x", **kwargs):
    async def scenario():
        lease = await pool.acquire(key, model_entry=entry, **kwargs)
        await pool.release(lease)
        return lease
    return asyncio.run(scenario())


# -- holds are kept under the target the turn ran as ---------------------------------------------------------------------
def test_a_refusal_of_an_alias_turn_is_kept_under_its_target_and_the_alias_moves_to_minara(tmp_path, monkeypatch):
    cfg, pool, alias = alias_pool(tmp_path, monkeypatch, [OPUS, MINARA_OPUS])
    retool_account, minara_account = pool.backends
    lease = acquired(pool, alias, "conv-1")
    assert lease.session.backend is retool_account and lease.bound_entry["id"] == OPUS
    # orchestrator.mark_plan_refusal after Retool's 402 for this turn: the hold is the target's, never "smart-opus".
    parsed = SimpleNamespace(entry=alias, model=alias["id"])
    assert orchestrator._model_id(parsed, lease) == OPUS
    AccountDispatcher(pool.backends, {"session_refresh_interval": 0}, {}, clock=Clock()).mark_plan_refusal(
        retool_account, orchestrator._model_id(parsed, lease))
    assert set(retool_account.refused_models) == {OPUS}
    # The next request of the alias goes to Minara at once: no settled 404 while the fallback target is free.
    assert pool.model_refusal(alias) is None and pool.unavailable_error(alias) is None
    for key in ("conv-1", "conv-2", None):
        lease = acquired(pool, alias, key)
        assert lease.session.backend is minara_account and lease.bound_entry["id"] == MINARA_OPUS
    # A direct request of the target is held the same way.
    assert pool.unavailable_error(models.resolve(OPUS, cfg=cfg)[0]).status == 404


def test_an_alias_is_held_on_an_account_only_while_each_target_it_serves_there_is_held(tmp_path, monkeypatch):
    _, pool, alias = alias_pool(tmp_path, monkeypatch, [OPUS, MINARA_OPUS, "claude-opus-5"])
    retool_account, minara_account = pool.backends
    retool_account.refused_models = {OPUS: {"until_at": 2e9, "pause": 300.0}}
    assert SessionPool._plan_hold(retool_account, alias) is None  # claude-opus-5 is still served there
    retool_account.refused_models["claude-opus-5"] = {"until_at": 1.9e9, "pause": 300.0}
    assert SessionPool._plan_hold(retool_account, alias)["until_at"] == 1.9e9  # the hold that ends first
    assert SessionPool._plan_hold(minara_account, alias) is None
    # A hold under the alias's own id is no hold of anything (it was what A08-01 found the relay keeping).
    retool_account.refused_models = {"smart-opus": {"until_at": 2e9, "pause": 300.0}}
    assert SessionPool._plan_hold(retool_account, alias) is None


# -- the turn is bound to the target the pool chose ------------------------------------------------------------------------
def test_the_turn_is_bound_to_the_target_the_pool_chose_the_account_for(tmp_path, monkeypatch):
    _, pool, alias = alias_pool(tmp_path, monkeypatch, [OPUS, MINARA_OPUS, "claude-opus-5"], minara=False)
    (retool_account,) = pool.backends
    # A direct claude-opus-5-5 request was refused by this account's plan a moment ago.
    retool_account.refused_models = {OPUS: {"until_at": 2e9, "pause": 300.0}}
    lease = acquired(pool, alias)
    assert lease.session.backend is retool_account
    # Bound by configuration alone it went out as claude-opus-5-5 again, to the 402 it had just got (A08-02).
    assert lease.bound_entry["id"] == "claude-opus-5" and lease.bound_entry["alias_id"] == "smart-opus"


def test_a_failed_target_of_an_account_leaves_its_other_targets_to_the_request(tmp_path, monkeypatch):
    _, pool, alias = alias_pool(tmp_path, monkeypatch, [OPUS, "claude-opus-5"], minara=False)
    (retool_account,) = pool.backends
    failed = ((retool_account, OPUS),)
    assert pool.unavailable_error(alias, exclude=failed) is None
    lease = acquired(pool, alias, exclude=failed)
    assert lease.session.backend is retool_account and lease.bound_entry["id"] == "claude-opus-5"
    # Once each target failed on the one account, nothing is left.
    assert pool.unavailable_error(alias, exclude=(*failed, (retool_account, "claude-opus-5"))) is not None


# -- end to end: the turn moves to the alias's next target ------------------------------------------------------------------
def alias_turn(runner, targets, conversation="conv-1"):
    runner.cfg["routes"] = [{"id": "smart", "targets": targets}]
    runner.cfg["_route_aliases"] = router.build_route_aliases(runner.cfg, models)
    parsed = turn(conversation)
    parsed.entry, parsed.model = runner.cfg["_route_aliases"]["smart"], "smart"
    return parsed


def test_an_alias_whose_first_target_no_plan_includes_is_served_by_its_next_target(retool):
    retool.script["acc-a"] = retool.script["acc-b"] = refuses(OPUS)
    sent = []
    original = retool.script["acc-a"]

    def recording(request_):
        sent.append(json.loads(request_.data)["model"])
        return original(request_)

    retool.script["acc-a"] = retool.script["acc-b"] = recording
    before = journal.JOURNAL.last_seq

    async def scenario():
        first = await run(retool.runner, alias_turn(retool.runner, [OPUS, SONNET]))
        posts = list(retool.posts)
        return first, posts, await run(retool.runner, alias_turn(retool.runner, [OPUS, SONNET], "conv-2"))

    first, posts, second = asyncio.run(scenario())
    assert texts(first) == [{"type": "text", "delta": "OK"}] and errors(first) == []
    # Each account refused the first target once; the turn then ran as the second target on an account of the pool.
    assert posts == ["acc-a", "acc-b", "acc-a"] and sent[:3] == [OPUS, OPUS, SONNET]
    assert set(retool.a.refused_models) == set(retool.b.refused_models) == {OPUS}
    held = _events("account.model_refused", before)
    assert [event["data"]["model"] for event in held] == [OPUS, OPUS]
    moved = _events("account.failover", before)
    assert [event["data"]["model"] for event in moved] == [OPUS, OPUS]
    # The next conversation of the alias goes straight to the target the plans include: no 404, no refusal asked again.
    assert texts(second) == [{"type": "text", "delta": "OK"}] and errors(second) == []
    assert len(retool.posts) == 4 and sent[3] == SONNET


def test_a_direct_request_still_holds_and_moves_as_before(retool):
    retool.script["acc-a"] = refused
    events = asyncio.run(run(retool.runner, turn()))
    assert texts(events) == [{"type": "text", "delta": "OK"}] and retool.posts == ["acc-a", "acc-b"]
    assert set(retool.a.refused_models) == {OPUS}


@pytest.mark.parametrize("keys", [1, 2])
def test_a_route_to_another_model_of_the_same_provider_reaches_it(monkeypatch, tmp_path, keys):
    """A17-01: route [solo/model-a, solo/model-b] on one provider; model-a answers 5xx and model-b is alive."""
    import http.client
    import threading

    import apierr
    import drivers
    import proxy

    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    calls = []

    class Solo(drivers.UpstreamDriver):
        driver_id = "test-solo-route"
        capabilities = drivers.DriverCapabilities(driver_id="test-solo-route")

        async def stream_turn(self, cfg, session, model_provider, prompt, head, tools, tool_choice, max_tokens,
                              chunks=None):
            calls.append(model_provider.get("model"))
            if model_provider.get("model") == "model-a":
                raise apierr.upstream_error("model-a is down (HTTP 503)")
            yield {"type": "text", "delta": "B_OK"}
            yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 1, "cost_usd": 0.0001}}
            yield {"type": "end", "reason": "stop"}

    drivers.register_driver(Solo())
    try:
        provider = {"id": "solo", "preset": "openrouter", "models": ["model-a", "model-b"]}
        if keys == 1:
            provider["api_key"] = "sk-or-v1-solo-0123456789"
        else:
            provider["credentials"] = [{"id": "k1", "api_key": "sk-or-v1-solo-k1-0123456789"},
                                       {"id": "k2", "api_key": "sk-or-v1-solo-k2-0123456789"}]
        path = tmp_path / "config.json"
        path.write_text(json.dumps({
            "relay_api_key": "test-route-secret-key", "conversation_state": False, "project_context": False,
            "exec_policy": "off", "upstream_rate_limit_retries": 0, "workspace_root": str(tmp_path),
            "providers": [provider], "routes": [{"id": "chain", "targets": ["solo/model-a", "solo/model-b"]}],
        }), encoding="utf-8")
        cfg = config_module.load(str(path))
        cfg["journal_path"] = str(tmp_path / "journal.jsonl")
        for account in cfg["accounts"]:
            account["upstream"] = account["driver"] = "test-solo-route"
        for entry in cfg["_provider_models"].values():
            entry["upstream"] = "test-solo-route"
        cfg["_route_aliases"] = router.build_route_aliases(cfg, models)
        runtime = proxy.Runtime(cfg)
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            conn.request("POST", "/v1/chat/completions",
                         json.dumps({"model": "chain", "messages": [{"role": "user", "content": "hi"}]}),
                         {"Authorization": "Bearer test-route-secret-key", "Content-Type": "application/json"})
            response = conn.getresponse()
            body = json.loads(response.read().decode("utf-8"))
        finally:
            conn.close()
            server.shutdown()
            server.server_close()
            runtime.close()
    finally:
        drivers.DRIVERS.pop("test-solo-route", None)
    assert response.status == 200, body
    assert body["choices"][0]["message"]["content"] == "B_OK"
    # model-a is asked once on each key, then the route's next model of the same provider answers.
    assert calls == ["model-a"] * keys + ["model-b"]
