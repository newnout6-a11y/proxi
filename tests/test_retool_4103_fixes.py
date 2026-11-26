"""Regression tests for Retool 4.103.0 fixes:
1. Error A: HTTP 403 "Assist is not available for this plan." skips auth refresh/retry, maps to
   502 upstream_error (not upstream_auth_error), and sets plan_refusal=True and unbilled=True.
2. Error B: Retool 4.103.0 assistCopilotStreamGateEnabled 400 "Invalid request format" on
   metadata.sandpackAgent falls back once to unmarked Assist (_unmarked + account-scoped TTL
   _remember_assist_only) while preserving build_body / prompt_golden wire parity, allowing
   paid Assist orgs to succeed and Freetool orgs to hit Error A 403 -> clean failover to
   another Retool account or Minara route target (e.g. smart-opus).
"""
import asyncio
import io
import json
import types
import urllib.error
from email.message import Message

import pytest

import apierr
import client_context
import config
import direct
import journal
import models
import proxy
import upstream_attempts
import validate
from test_account_rate_limit import _events, _headers, retool  # noqa: F401
from test_minara_failover import Response as MinaraResponse, ok_stream
from test_native_context_reuse import run
from test_retool_plan_refusal import OPUS, errors, texts, turn
from test_review_fixes_pr19 import END, response, text


ASSIST_REFUSAL = "Assist is not available for this plan."
ASSIST_403_BODY = json.dumps(
    {"statusCode": 403, "error": "Forbidden", "message": ASSIST_REFUSAL, "data": None}
)
GATE_400_BODY = json.dumps(
    {"statusCode": 400, "error": "Bad Request", "message": "Invalid request format", "data": None}
)


@pytest.fixture(autouse=True)
def _reset_direct_learning(monkeypatch):
    monkeypatch.setattr(direct, "_ASSIST_ONLY", set(direct._ASSIST_SEEDS))
    monkeypatch.setattr(direct, "_ASSIST_LEARNED", {})
    monkeypatch.setattr(direct, "_TOOL_RESULT_HINTS", {"refused": False})
    monkeypatch.setattr(upstream_attempts.AttemptBudget, "wait", lambda self, delay, cancelled: None)


def _http_error(url, status, body):
    return urllib.error.HTTPError(
        url,
        status,
        "HTTP %d" % status,
        Message(),
        io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body),
    )


# -- Error A: 403 "Assist is not available for this plan." ------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    (ASSIST_REFUSAL, True),
    (ASSIST_REFUSAL.upper(), True),
    ("Assist  is not\navailable for   this plan.", True),
    (ASSIST_403_BODY, True),
    ("This model is not available for your plan.", False),
    ("<!DOCTYPE html><title>Just a moment...</title>", False),
    ("Forbidden", False),
    ("", False),
    (None, False),
])
def test_assist_plan_refusal_text_is_recognised_case_and_space_insensitively(payload, expected):
    assert apierr.is_assist_not_in_plan(payload) is expected
    assert apierr.is_not_in_plan(ASSIST_REFUSAL) is False


def test_http_403_assist_refusal_maps_to_unbilled_plan_refusal_not_auth_error():
    err = direct._api_error(403, ASSIST_403_BODY, _headers())
    assert (err.status, err.code, err.message) == (502, "upstream_error", ASSIST_REFUSAL)
    assert apierr.is_plan_refusal(err) is True
    assert getattr(err, "unbilled", False) is True
    assert not apierr.is_account_quota(err)

    # Genuine session 403 keeps upstream_auth_error and is NOT a plan refusal.
    auth_err = direct._api_error(403, json.dumps({"message": "session expired"}), _headers())
    assert (auth_err.status, auth_err.code) == (502, "upstream_auth_error")
    assert not apierr.is_plan_refusal(auth_err)
    assert getattr(auth_err, "unbilled", False) is False


def test_stream_event_403_assist_refusal_maps_to_unbilled_plan_refusal():
    err = direct._error_of_event({"event": "error", "statusCode": 403, "error": ASSIST_REFUSAL})
    assert (err.status, err.code, err.message) == (502, "upstream_error", ASSIST_REFUSAL)
    assert apierr.is_plan_refusal(err) is True
    assert getattr(err, "unbilled", False) is True


def test_stream_body_403_assist_refusal_skips_auth_refresh_and_retry(tmp_path, monkeypatch):
    posts = []
    renewals = []

    def opener(req, **_kw):
        posts.append(req)
        raise _http_error(req.full_url, 403, ASSIST_403_BODY)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path / "diag")}
    refresher = types.SimpleNamespace(refresh=lambda rejected_cookie=None: renewals.append(1) or True)
    session = types.SimpleNamespace(
        backend=types.SimpleNamespace(cfg=cfg, headers=lambda: {"cookie": "c=1"}, refresher=refresher),
        _cancelled=False,
    )

    async def go():
        with pytest.raises(apierr.ApiError) as caught:
            async for _ev in direct._stream_body(cfg, session, {"model": OPUS}):
                pass
        return caught.value

    err = asyncio.run(go())
    assert len(posts) == 1
    assert renewals == []
    assert err.upstream_status == 403
    assert (err.status, err.code) == (502, "upstream_error")
    assert apierr.is_plan_refusal(err) is True
    assert getattr(err, "unbilled", False) is True


def test_stream_body_plain_403_still_refreshes_session_once(tmp_path, monkeypatch):
    posts = []
    renewals = []

    def opener(req, **_kw):
        posts.append(1)
        if len(posts) == 1:
            raise _http_error(req.full_url, 403, json.dumps({"message": "session expired"}))
        return response(text("ok"), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path / "diag")}
    refresher = types.SimpleNamespace(refresh=lambda rejected_cookie=None: renewals.append(1) or True)
    session = types.SimpleNamespace(
        backend=types.SimpleNamespace(cfg=cfg, headers=lambda: {"cookie": "c=1"}, refresher=refresher),
        _cancelled=False,
    )
    events = asyncio.run(_collect(direct._stream_body(cfg, session, {"model": OPUS})))
    assert renewals == [1] and len(posts) == 2
    assert any(ev == {"type": "text", "delta": "ok"} for ev in events)


# -- Error B: Claude Desktop shapes + 400 "Invalid request format" unmarked Assist fallback --------------------------

@pytest.mark.parametrize("with_tools", [False, True], ids=["203328-session-title", "203323-client-tools"])
def test_build_body_preserves_claude_desktop_wire_shape_and_rit(with_tools):
    req = {
        "model": OPUS,
        "max_tokens": 64000,
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.330.e4b; cc_entrypoint=claude-desktop-3p;"},
            {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
             "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Title task", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "<system-reminder>\nEnv details\n</system-reminder>",
                     "cache_control": {"type": "ephemeral"}},
                ],
            }
        ],
    }
    tools = None
    if with_tools:
        req["tools"] = [{"name": "read_file", "description": "Read",
                         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                                          "required": ["path"]}}]
        tools = direct.tools_to_retool(req["tools"])

    parsed = validate.parse_anthropic(req, {"strict_models": True, "model": OPUS})
    body = direct.build_body(
        {"direct_cache": True},
        {"model": OPUS, "reasoningEffort": "medium"},
        parsed.system,
        parsed.prompt,
        tools,
        parsed.tool_choice,
        parsed.max_tokens,
        chunks=parsed.message_chunks,
    )
    assert body["messages"][0]["role"] == "system"
    assert len(body["messages"][0]["content"]) == 2
    assert body["messages"][0]["content"][0]["text"].startswith("x-anthropic-billing-header:")
    assert body["messages"][0]["content"][1]["cache"] is True
    assert body["metadata"] == {"sandpackAgent": True, "requestOrigin": "Browser"}
    assert body["rit"] == direct.make_rit(body["messages"], tools)
    assert body["openAICacheKey"] == direct.cache_key_for(parsed.system, OPUS, tools)


def test_iter_events_retries_unmarked_on_400_gate_rejection_and_learns_per_account(tmp_path, monkeypatch):
    posted = []

    def opener(req, **_kw):
        body = json.loads(req.data)
        posted.append(body)
        if "metadata" in body:
            raise _http_error(req.full_url, 400, GATE_400_BODY)
        return response(text("ASSIST_OK"), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path / "diag"), "turn_timeout": 10}
    sess_a = types.SimpleNamespace(
        backend=types.SimpleNamespace(cfg={**cfg, "account_id": "acc-paid"}, headers=lambda: {}),
        _cancelled=False,
    )
    sess_b = types.SimpleNamespace(
        backend=types.SimpleNamespace(cfg={**cfg, "account_id": "acc-other"}, headers=lambda: {}),
        _cancelled=False,
    )

    seq = journal.JOURNAL.last_seq
    events = asyncio.run(_collect(direct.iter_events(
        cfg, sess_a, {"model": OPUS, "reasoningEffort": "medium"}, "hi", "You are concise.", None, "auto", 64
    )))
    assert len(posted) == 2
    assert posted[0]["metadata"] == {"sandpackAgent": True, "requestOrigin": "Browser"}
    assert "metadata" not in posted[1]
    assert any(ev == {"type": "text", "delta": "ASSIST_OK"} for ev in events)
    assert any(ev["kind"] == "upstream.assist_retry" for ev in journal.JOURNAL.query(since=seq, limit=100))

    # Account-scoped TTL learning: next turn on acc-paid starts unmarked in 1 POST; acc-other still tries marked first.
    assert direct._is_assist_only(OPUS, sess_a) is True
    assert direct._is_assist_only(OPUS, sess_b) is False
    assert OPUS not in direct._ASSIST_ONLY

    posted.clear()
    events2 = asyncio.run(_collect(direct.iter_events(
        cfg, sess_a, {"model": OPUS, "reasoningEffort": "medium"}, "hi2", "You are concise.", None, "auto", 64
    )))
    assert len(posted) == 1 and "metadata" not in posted[0]
    assert any(ev == {"type": "text", "delta": "ASSIST_OK"} for ev in events2)


def test_iter_events_gate_400_to_assist_403_does_not_learn_and_surfaces_unbilled_plan_refusal(tmp_path, monkeypatch):
    posted = []
    renewals = []

    def opener(req, **_kw):
        body = json.loads(req.data)
        posted.append(body)
        if "metadata" in body:
            raise _http_error(req.full_url, 400, GATE_400_BODY)
        raise _http_error(req.full_url, 403, ASSIST_403_BODY)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path / "diag"), "turn_timeout": 10}
    refresher = types.SimpleNamespace(refresh=lambda rejected_cookie=None: renewals.append(1) or True)
    sess = types.SimpleNamespace(
        backend=types.SimpleNamespace(cfg={**cfg, "account_id": "acc-free"}, headers=lambda: {"cookie": "c=1"},
                                      refresher=refresher),
        _cancelled=False,
    )

    async def go():
        with pytest.raises(apierr.ApiError) as caught:
            async for _ev in direct.iter_events(
                cfg, sess, {"model": OPUS}, "hi", "You are concise.", None, "auto", 64
            ):
                pass
        return caught.value

    err = asyncio.run(go())
    assert len(posted) == 2
    assert "metadata" in posted[0] and "metadata" not in posted[1]
    assert renewals == []
    assert (err.status, err.code) == (502, "upstream_error")
    assert apierr.is_plan_refusal(err) is True
    assert getattr(err, "unbilled", False) is True
    assert direct._is_assist_only(OPUS, sess) is False


@pytest.mark.parametrize("outcome", ["rebuild_success", "assist_success", "assist_refused", "other_error"])
def test_iter_events_retool_gate_after_tool_result_hint_rebuild(tmp_path, monkeypatch, outcome):
    posted = []

    def opener(req, **_kw):
        body = json.loads(req.data)
        posted.append(body)
        if direct._has_tool_result_hint(body["messages"]):
            raise _http_error(req.full_url, 400, GATE_400_BODY)
        if "metadata" in body and outcome != "rebuild_success":
            detail = GATE_400_BODY if outcome != "other_error" else '{"message":"Invalid tool schema"}'
            raise _http_error(req.full_url, 400, detail)
        if "metadata" not in body and outcome == "assist_refused":
            raise _http_error(req.full_url, 403, ASSIST_403_BODY)
        return response(text("OK"), *END)

    monkeypatch.setattr(direct.urllib.request, "urlopen", opener)
    cfg = {"base": "https://retool.invalid", "upstream_error_dir": str(tmp_path / "diag"), "turn_timeout": 10,
           "direct_cache": True}
    tools = [{"name": "read", "description": "r", "params": {}}]
    client_msgs = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [{"type": "tool_call", "toolCallId": "c1", "toolName": "read", "toolInput": {}}]},
        {"role": "tool", "content": {"toolCallId": "c1", "toolResult": "result " * 20}},
    ]
    scope = direct.native_scope("conv-hint", {"model": OPUS}, "head " * 20, tools, "auto")
    history = direct.native_history.History()
    history.record(scope, client_msgs[:1], [{"role": "user", "content": "go"}],
                   [{"type": "tool_call", "toolCallId": "c1", "toolName": "read", "toolInput": {}}],
                   client_msgs[1], None)
    sess = types.SimpleNamespace(
        backend=types.SimpleNamespace(cfg=cfg, headers=lambda: {}),
        _cancelled=False,
        _native_conversation_key="conv-hint",
        _native_client_messages=client_msgs,
        _native_history=history,
    )
    token = client_context.bind({"client": "opencode", "request_id": "req-hint"})
    try:
        events = _collect(direct.iter_events(cfg, sess, {"model": OPUS}, "go", "head " * 20, tools, "auto", 64))
        if outcome in ("assist_refused", "other_error"):
            with pytest.raises(apierr.ApiError) as caught:
                asyncio.run(events)
            error = caught.value
            assert (error.status, error.message) == ((502, ASSIST_REFUSAL) if outcome == "assist_refused"
                                                     else (400, "Invalid tool schema"))
            assert apierr.is_plan_refusal(error) is (outcome == "assist_refused")
            if outcome == "assist_refused":
                assert error.unbilled is True
        else:
            assert {"type": "text", "delta": "OK"} in asyncio.run(events)
    finally:
        client_context.reset(token)
    expected = [(True, True), (True, False)]
    if outcome in ("assist_success", "assist_refused"):
        expected.append((False, False))
    assert [("metadata" in body, direct._has_tool_result_hint(body["messages"])) for body in posted] == expected
    assert direct._TOOL_RESULT_HINTS["refused"] is (outcome == "rebuild_success")
    assert direct._is_assist_only(OPUS, sess) is (outcome == "assist_success")


# -- End-to-end failover tests -----------------------------------------------------------------------------------------

def test_retool_gate_400_to_assist_403_fails_over_on_smart_opus_route_to_minara(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "relay_api_key": "relay-4103-test-key-0123456789",
        "ua": "Mozilla/5.0",
        "workspace_root": str(tmp_path),
        "project_context": False,
        "exec_policy": "off",
        "conversation_state": False,
        "cache_keepwarm_seconds": 0,
        "session_refresh_interval": 0,
        "account_balance_interval": 0,
        "minara_channel_retry_delays": [],
        "journal_path": str(tmp_path / "journal.jsonl"),
        "prompts_file": str(tmp_path / "absent.json"),
        "accounts": [
            {
                "account_id": "retool-freetool",
                "base": "https://retool.invalid",
                "page_uuid": "p-free",
                "cookie": "accessToken=ret-free-secret-123",
                "xsrf": "xsrf-free-123",
                "provider_resource_name": "__managed__",
            },
            {
                "account_id": "minara-healthy",
                "upstream": "minara",
                "bearer_token": "minara-bearer-secret-123",
                "minara_models": [{"id": "anthropic/claude-opus-5-5"}],
            },
        ],
        "routes": [{"id": "smart-opus", "targets": [OPUS, "minara/" + OPUS], "strategy": "priority"}],
    }), encoding="utf-8")
    cfg = config.load(str(cfg_path))
    runtime = proxy.Runtime(cfg)

    retool_posts = []
    retool_refreshes = []
    minara_posts = []
    retool_backend, minara_backend = runtime.accounts.backends
    retool_backend.refresher = types.SimpleNamespace(
        refresh=lambda rejected_cookie=None: retool_refreshes.append(1) or True,
        refresh_detailed=lambda force=True: (False, "auth", "unused"),
    )

    def fake_retool(req, **_kw):
        body = json.loads(req.data)
        retool_posts.append(body)
        if "metadata" in body:
            raise _http_error(req.full_url, 400, GATE_400_BODY)
        raise _http_error(req.full_url, 403, ASSIST_403_BODY)

    def fake_minara_post(url, **kwargs):
        minara_posts.append(kwargs.get("json") or {})
        return MinaraResponse(200, ok_stream("MINARA_FALLBACK_OK"))

    def fake_minara_get(url, **kwargs):
        return types.SimpleNamespace(status_code=200, json=lambda: {"creditBalanceBreakdown": {"plan": "200", "extra": "0"}})

    monkeypatch.setattr(direct.urllib.request, "urlopen", fake_retool)
    minara_backend._minara_post = fake_minara_post
    minara_backend._minara_get = fake_minara_get
    runtime.accounts.check_balance_now = lambda _backend: False

    alias_entry = models.resolve("smart-opus", cfg=cfg)[0]
    parsed = turn("conv-route-1")
    parsed.entry, parsed.model = alias_entry, "smart-opus"

    before = journal.JOURNAL.last_seq
    holder = {}
    events = asyncio.run(run(runtime.runner, parsed, holder))

    assert len(retool_posts) == 2
    assert "metadata" in retool_posts[0] and "metadata" not in retool_posts[1]
    assert retool_refreshes == []
    assert len(minara_posts) == 1
    assert texts(events) == [{"type": "text", "delta": "MINARA_FALLBACK_OK"}]
    assert errors(events) == []
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert set(retool_backend.refused_models) == {OPUS}
    assert retool_backend.unavailable is None
    assert holder["_attempt_budget"].refunds == 1

    moved = _events("account.failover", before)
    assert len(moved) == 1
    assert (moved[0]["data"]["reason"], moved[0]["data"]["model"], moved[0]["data"]["error"]) == (
        "plan_refused", OPUS, "upstream_error"
    )

    # A second conversation on smart-opus routes straight to Minara without touching Retool.
    parsed2 = turn("conv-route-2")
    parsed2.entry, parsed2.model = alias_entry, "smart-opus"
    events2 = asyncio.run(run(runtime.runner, parsed2, {}))
    assert len(retool_posts) == 2
    assert len(minara_posts) == 2
    assert texts(events2) == [{"type": "text", "delta": "MINARA_FALLBACK_OK"}]


def test_retool_freetool_fails_over_to_assist_enabled_retool_account(retool):
    retool_refreshes = []
    retool.a.refresher = types.SimpleNamespace(
        refresh=lambda rejected_cookie=None: retool_refreshes.append(1) or True,
    )
    retool.b.refresher = types.SimpleNamespace(
        refresh=lambda rejected_cookie=None: retool_refreshes.append(1) or True,
    )
    posted_by = []

    def freetool_retool(req):
        body = json.loads(req.data)
        posted_by.append(("acc-a", "metadata" in body))
        if "metadata" in body:
            raise _http_error(req.full_url, 400, GATE_400_BODY)
        raise _http_error(req.full_url, 403, ASSIST_403_BODY)

    def assist_retool(req):
        body = json.loads(req.data)
        posted_by.append(("acc-b", "metadata" in body))
        if "metadata" in body:
            raise _http_error(req.full_url, 400, GATE_400_BODY)
        return response(text("ASSIST_ACCOUNT_OK"), *END)

    retool.script["acc-a"] = freetool_retool
    retool.script["acc-b"] = assist_retool

    events = asyncio.run(run(retool.runner, turn("conv-retool-1")))
    assert posted_by == [("acc-a", True), ("acc-a", False), ("acc-b", True), ("acc-b", False)]
    assert retool_refreshes == []
    assert texts(events) == [{"type": "text", "delta": "ASSIST_ACCOUNT_OK"}]
    assert errors(events) == []
    assert set(retool.a.refused_models) == {OPUS}
    assert retool.b.refused_models == {}
    assert retool.pool._conversations["conv-retool-1"].backend is retool.b

    # Turn 2 on conv-retool-1 uses learned Assist mode for acc-b directly (1 unmarked POST).
    posted_by.clear()
    events2 = asyncio.run(run(retool.runner, turn("conv-retool-1")))
    assert posted_by == [("acc-b", False)]
    assert texts(events2) == [{"type": "text", "delta": "ASSIST_ACCOUNT_OK"}]


async def _collect(agen):
    return [ev async for ev in agen]
