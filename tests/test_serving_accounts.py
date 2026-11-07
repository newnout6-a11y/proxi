"""Discovery, the HTTP layer's refusal and acquire() look at the same accounts, and a stream gets a real status."""
import asyncio
import http.client
import json
import threading
from types import SimpleNamespace

import pytest

import apierr
import config
import direct
import models
import orchestrator
import proxy
from session import Backend, SessionPool
from test_review_fixes_pr19 import END, response, text
from test_review_fixes_proxy import FakeManager as BalanceManager
from upstream_attempts import AttemptBudget

KEY = "serving-accounts-relay-key"
M = "__managed__"
GLM = "zai-org/GLM-5.3"
MESSAGE = [{"role": "user", "content": "hi"}]
SCHEMA = {"type": "object", "properties": {}}
DIALECTS = {
    "chat": ("/v1/chat/completions",
             lambda model, tools: {"model": model, "messages": MESSAGE,
                                   **({"tools": [{"type": "function", "function": {"name": "t", "parameters": SCHEMA}}]}
                                      if tools else {})}),
    "anthropic": ("/v1/messages",
                  lambda model, tools: {"model": model, "max_tokens": 32, "messages": MESSAGE,
                                        **({"tools": [{"name": "t", "description": "d", "input_schema": SCHEMA}]}
                                           if tools else {})}),
    "responses": ("/v1/responses",
                  lambda model, tools: {"model": model, "input": "hi",
                                        **({"tools": [{"type": "function", "name": "t", "parameters": SCHEMA}]}
                                           if tools else {})}),
}


def account(name, **resources):
    return {"account_id": name, "page_uuid": "page-" + name, "thread_id": "thread-" + name,
            "cookie": "accessToken=cookie-%s-123456; xsrfToken=q" % name, "xsrf": "q", **resources}


# The first account has no GLM (retool-fast) resource, the second has it.
FIRST_LACKS_GLM = [account("acc-a", provider_resource_name=M),
                   account("acc-b", provider_resource_name=M, retool_fast_provider_resource_name=M)]


@pytest.fixture
def relay_pool(tmp_path, monkeypatch):
    """The real relay over HTTP (real pool, runner and transport) on a scripted Retool."""
    monkeypatch.setattr(AttemptBudget, "wait", lambda self, delay, cancelled: None)
    monkeypatch.setattr(direct, "_EMPTY_DUMPED", set())
    started, posts = [], []

    def opener(request_, **_kwargs):
        who = "acc-a" if "cookie-acc-a" in (request_.get_header("Cookie") or "") else "acc-b"
        posts.append(who)
        return response(text("OK"), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)

    def start(accounts, **overrides):
        cfg = dict(config.DEFAULTS)
        cfg.update({"base": "https://example.invalid", "page_uuid": accounts[0]["page_uuid"], "thread_id": "thread-a",
                    "ua": "test-agent", "cookie": accounts[0]["cookie"], "xsrf": "q", "relay_api_key": KEY,
                    "workspace_root": str(tmp_path), "command_log": str(tmp_path / "c.log"), "host": "127.0.0.1",
                    "port": 0, "cookie_values": [], "client_metadata_home": str(tmp_path),
                    "cache_keepwarm_seconds": 0, "upstream_error_dir": str(tmp_path), "accounts": accounts})
        cfg.update(overrides)
        runtime = proxy.Runtime(cfg)
        for backend in runtime.accounts.backends:
            manager = BalanceManager(100.0)
            runtime.accounts.managers[backend] = manager
            backend.credits = manager
        runtime.credits = runtime.accounts.managers[runtime.accounts.backends[0]]
        server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        started.append((runtime, server))

        def call(path, payload, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            connection.request("POST", path, json.dumps(payload).encode(),
                               {"content-type": "application/json", "authorization": "Bearer " + KEY, **(headers or {})})
            reply = connection.getresponse()
            raw = reply.read().decode()
            connection.close()
            is_json = "json" in reply.headers.get("content-type", "")
            return reply.status, (json.loads(raw or "{}") if is_json else raw), reply.headers

        call.runtime, call.posts = runtime, posts
        return call

    yield start
    for runtime, server in started:
        server.shutdown()
        try:
            server.server_close()
        except OSError:
            pass
        runtime.close()


# -- one answer for stream and non-stream, wherever acquire() would look ---------------------------------------
# The first account cannot serve the model, the second can: it lacks only this model's resource, or any resource.
UNEVEN = [pytest.param(FIRST_LACKS_GLM, GLM, "retool_fast_provider_resource_name", id="first-lacks-the-resource"),
          pytest.param([account("acc-a"), account("acc-b", provider_resource_name=M)], "claude-opus-5-5",
                       "provider_resource_name", id="first-has-no-resource-at-all")]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("accounts,model,key", UNEVEN)
def test_shared_mode_tools_request_is_refused_with_a_real_404_when_only_the_second_account_has_the_resource(
        relay_pool, accounts, model, key, dialect, stream):
    call = relay_pool(accounts, session_mode="shared")
    path, build = DIALECTS[dialect]
    status, body, headers = call(path, dict(build(model, True), stream=stream))
    # Tools take the shared session, which only the first account serves.
    assert status == 404 and "json" in headers["content-type"], (status, body)
    assert body["error"]["type"] == "not_found_error" and key in body["error"]["message"]
    assert call.posts == [] and not call.runtime.pool._active


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("accounts,model,key", UNEVEN)
def test_shared_mode_request_without_tools_is_served_by_the_second_account(relay_pool, accounts, model, key,
                                                                            dialect, stream):
    call = relay_pool(accounts, session_mode="shared")
    path, build = DIALECTS[dialect]
    status, body, _ = call(path, dict(build(model, False), stream=stream))
    # Without tools the pool routes by account, as discovery's per-account facts allow.
    assert status == 200, (status, body)
    assert call.posts == ["acc-b"]


@pytest.mark.parametrize("tools", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("accounts,model,key", UNEVEN)
def test_per_conversation_mode_routes_to_the_account_that_has_the_resource(relay_pool, accounts, model, key,
                                                                          dialect, stream, tools):
    call = relay_pool(accounts, session_mode="per_conversation")
    path, build = DIALECTS[dialect]
    status, body, _ = call(path, dict(build(model, tools), stream=stream))
    assert status == 200, (status, body)
    assert call.posts == ["acc-b"]


@pytest.mark.parametrize("stream", [False, True])
def test_no_account_with_the_resource_is_a_404_in_both_modes(relay_pool, stream):
    accounts = [account("acc-a", provider_resource_name=M), account("acc-b", provider_resource_name=M)]
    for mode in ("shared", "per_conversation"):
        call = relay_pool(accounts, session_mode=mode)
        status, body, headers = call("/v1/chat/completions", dict(DIALECTS["chat"][1](GLM, True), stream=stream))
        assert status == 404 and body["error"]["type"] == "not_found_error", (mode, status)
        assert call.posts == []


# -- the single list of accounts ---------------------------------------------------------------------------
def pool_of(accounts, **cfg):
    settings = dict(config.DEFAULTS, base="https://root.invalid", ua="serving", cookie="synthetic-only",
                    xsrf="synthetic-only", cookie_values=[], page_uuid="p-root", session_refresh_interval=0,
                    cache_keepwarm_seconds=0, max_workers=8, thread_id="shared-thread", accounts=accounts)
    settings.update(cfg)
    return SessionPool(Backend(settings))


def entry(name):
    return models.resolve(name)[0]


def test_the_shared_main_path_has_only_the_first_account_and_every_other_path_has_all():
    pool = pool_of([account("a", provider_resource_name=M), account("b", provider_resource_name=M)], session_mode="shared")
    first, second = pool.backends
    assert pool._serving() == [first]
    assert pool._serving(isolated=False, context_scope=None) == [first]
    assert pool._serving(isolated=True) == [first, second]
    assert pool._serving(context_scope="text") == [first, second]
    per_conversation = pool_of([account("a"), account("b")], session_mode="per_conversation")
    assert per_conversation._serving() == per_conversation.backends


@pytest.mark.parametrize("mode", ["shared", "per_conversation"])
def test_discovery_model_refusal_and_acquire_agree_on_which_accounts_serve(mode):
    accounts = [account("a", provider_resource_name=M), account("b", provider_resource_name=M,
                                                                retool_fast_provider_resource_name=M)]
    pool = pool_of(accounts, session_mode=mode)
    glm = entry(GLM)
    published = models.catalog_routable(glm, pool.cfg, pool.backends)
    refusal = pool.model_refusal(glm)
    assert published is (mode == "per_conversation")
    assert (refusal is None) is (mode == "per_conversation")

    async def acquire():
        try:
            lease = await pool.acquire("chat", model_entry=glm)
        except apierr.ApiError as error:
            return error
        await pool.release(lease)
        return None

    error = asyncio.run(acquire())
    assert (error is None) is (mode == "per_conversation")
    if error is not None:
        assert (error.status, error.code) == (404, "not_found_error")
        assert error.message == refusal.message


def test_model_refusal_on_another_path_looks_at_that_paths_accounts():
    pool = pool_of(FIRST_LACKS_GLM, session_mode="shared")
    glm = entry(GLM)
    assert pool.model_refusal(glm).status == 404
    assert pool.model_refusal(glm, isolated=True) is None
    assert pool.model_refusal(glm, context_scope="text") is None
    assert pool.model_refusal(glm, context_scope=("subagent", "chat")) is None


# -- no resources in the pool: the same reading everywhere ----------------------------------------------------
def test_a_pool_with_a_resource_only_on_a_switched_off_account_is_configured_for_discovery_too():
    accounts = [account("a"), account("b", provider_resource_name=M, enabled=False)]
    pool = pool_of(accounts, session_mode="per_conversation")
    opus = entry("claude-opus-5-5")
    # model_refusal has always counted the disabled account: the pool is configured, the enabled one lacks the resource.
    assert pool.model_refusal(opus) is None  # the capable account is only switched off: 503, not a settled 404
    assert models.catalog_routable(opus, pool.cfg, pool.backends) is False
    gpt = entry("gpt-6-sol")
    assert pool.model_refusal(gpt).status == 404
    assert models.catalog_routable(gpt, pool.cfg, pool.backends) is False


def test_a_shared_pool_whose_first_account_has_nothing_refuses_what_the_shared_session_cannot_serve():
    accounts = [account("a"), account("b", provider_resource_name=M)]
    pool = pool_of(accounts, session_mode="shared")
    opus = entry("claude-opus-5-5")
    # The pool has a resource (on the second account), so no empty-pool explanation is left to acquire: the shared
    # session, which is the first account alone, cannot serve the model. Discovery hides it and a stream gets a 404.
    assert models.catalog_routable(opus, pool.cfg, pool.backends) is False
    assert pool.model_refusal(opus).status == 404
    # A text chat or a standalone job may use the second account.
    assert pool.model_refusal(opus, isolated=True) is None
    assert pool.model_refusal(opus, context_scope="text") is None
    assert pool.model_refusal(entry("gpt-6-sol"), isolated=True).status == 404


def test_a_pool_without_any_resource_is_one_pool_for_discovery_and_every_path():
    pool = pool_of([account("a"), account("b")], session_mode="shared")
    opus = entry("claude-opus-5-5")
    assert models.pool_configured([backend.cfg for backend in pool.backends]) is False
    assert models.catalog_routable(opus, pool.cfg, pool.backends) is True
    for isolated in (False, True):
        assert pool.model_refusal(opus, isolated=isolated) is None  # the request itself explains the missing resource
    assert pool.model_refusal(entry("gpt-5.3-codex")).status == 404  # a route fact holds without any resource


def test_a_published_model_is_never_refused_over_a_grid_of_pools():
    resources = [{}, {"provider_resource_name": M}, {"openai_provider_resource_name": M},
                 {"retool_fast_provider_resource_name": M},
                 {"provider_resource_name": M, "retool_fast_provider_resource_name": M}]
    checked = 0
    for mode in ("per_conversation", "shared"):
        for first in resources:
            for second in resources:
                for first_on in (True, False):
                    for second_on in (True, False):
                        pool = pool_of([account("a", enabled=first_on, **first),
                                        account("b", enabled=second_on, **second)], session_mode=mode)
                        for model in ("claude-opus-5-5", "gpt-6-sol", GLM, "gpt-5.3-codex"):
                            published = models.catalog_routable(entry(model), pool.cfg, pool.backends)
                            if published:
                                assert pool.model_refusal(entry(model)) is None, (mode, first, second, model)
                            checked += 1
    assert checked == 800


# -- the pool path of the orchestrator is the one the HTTP layer asks about -----------------------------------
def parsed_request(**fields):
    base = {"tools": [], "system": "", "structured_output": None, "request_purpose": "generation",
            "conversation_key": "chat", "parent_conversation_key": None}
    base.update(fields)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("mode", ["shared", "per_conversation", "per_request"])
@pytest.mark.parametrize("fields,kind", [
    ({"tools": [{"function": {"name": "t"}}], "request_purpose": "client-tools"}, "session"),
    ({}, "text"),
    ({"request_purpose": "session-title"}, "isolated"),
    ({"request_purpose": "client-tools", "tools": [{"function": {"name": "t"}}],
      "parent_conversation_key": "parent"}, "session"),
])
def test_the_plan_names_the_kind_of_session_a_turn_asks_for(mode, fields, kind):
    cfg = {"transport": "direct", "session_mode": mode, "exec_policy": "allowlist", "max_exec_turns": 0}
    plan = orchestrator.acquire_plan(cfg, parsed_request(**fields))
    expected = kind
    if kind == "text" and mode == "per_request":
        expected = "isolated"
    assert plan.kind == expected


def test_the_plan_gives_a_subagent_of_the_shared_session_its_own_scope():
    cfg = {"transport": "direct", "session_mode": "shared", "exec_policy": "allowlist", "max_exec_turns": 0}
    tools = [{"function": {"name": "t"}}]
    plain = orchestrator.acquire_plan(cfg, parsed_request(tools=tools, request_purpose="client-tools"))
    child = orchestrator.acquire_plan(cfg, parsed_request(tools=tools, request_purpose="client-tools",
                                                          parent_conversation_key="parent"))
    assert plain.kwargs == {} and child.kwargs == {"context_scope": ("subagent", "chat")}
    text_plan = orchestrator.acquire_plan(cfg, parsed_request(parent_conversation_key="parent"))
    assert text_plan.kwargs == {"context_scope": (("subagent", "chat"), "text")}
    isolated = orchestrator.acquire_plan(cfg, parsed_request(request_purpose="session-title"))
    assert isolated.kwargs == {"isolated": True}


def test_the_turn_acquires_with_the_plan_the_http_layer_checked(tmp_path):
    from test_native_context_reuse import FIRST, request, run, setup
    backend, pool, runner = setup(tmp_path, transport="direct", project_context=False, session_mode="shared")
    seen = []
    real = pool.acquire

    async def spy(key=None, **kwargs):
        seen.append(kwargs)
        return await real(key, **kwargs)

    pool.acquire = spy
    parsed = request(FIRST)
    asyncio.run(run(runner, parsed))
    plan = orchestrator.acquire_plan(runner.cfg, parsed)
    assert seen and {k: v for k, v in seen[0].items() if k not in ("model_entry",)} == plan.kwargs
