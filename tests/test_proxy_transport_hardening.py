"""End-to-end verification of proxy transport hardening across Minara, Retool, Universal drivers, Config, and Admin API."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from types import SimpleNamespace
import urllib.error

import httpx
import pytest

import accounts
import apierr
import auth
import client_context
import config as config_module
from catalog import discovery
from drivers import anthropic_messages, curl_pool, http_pool, openai_chat, openai_responses
import minara
import proxy
import retool_refresh
from test_admin_pool import clean_environment, pool  # noqa: F401 - clean env + two-account admin fixture


def _start_server(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_minara_uses_chrome152_fetch_headers_and_reuses_sockets_per_account(monkeypatch):
    # Opt out of conftest's api.minara.ai offline guard for this test; cfg points at 127.0.0.1.
    monkeypatch.setattr(accounts, "_minara_network_get", accounts._ORIG_MINARA_GET)
    seen_headers: list[dict[str, str]] = []
    seen_peers: list[tuple[str, int]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            seen_peers.append(self.connection.getpeername())
            seen_headers.append({k.lower(): v for k, v in self.headers.items()})
            body = json.dumps({"creditBalanceBreakdown": {"plan": "50", "extra": "0"}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(ln)
            seen_peers.append(self.connection.getpeername())
            seen_headers.append({k.lower(): v for k, v in self.headers.items()})
            frame = {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}
            body = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server, thread = _start_server(Handler)
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    cfg_a = {"account_id": "min-a", "upstream": "minara", "base": base_url, "bearer_token": "tok-a"}
    cfg_b = {"account_id": "min-b", "upstream": "minara", "base": base_url, "bearer_token": "tok-b"}
    try:
        # Account A: balance GET + completion POST share one TCP socket and send Fetch/CORS Chrome/152 headers.
        mgr_a = accounts.MinaraAccount(cfg_a)
        bal = mgr_a.get_balance(force_refresh=True)
        assert bal["available"] == 50.0

        async def run_turn(cfg):
            sess = SimpleNamespace(backend=SimpleNamespace(cfg=cfg), _cancelled=False)
            return [ev async for ev in minara.iter_events(cfg, sess, {"model": "minara/openai/x"}, "hi", "", None, None, 10)]

        events = asyncio.run(run_turn(cfg_a))
        assert any(ev.get("type") == "text" and ev.get("delta") == "OK" for ev in events)
        assert len(seen_peers) == 2
        assert len(set(seen_peers)) == 1, f"same account should reuse socket: {seen_peers}"

        for hdrs in seen_headers:
            assert "Chrome/152." in hdrs["user-agent"]
            assert 'v="152"' in hdrs["sec-ch-ua"]
            assert hdrs["sec-fetch-mode"] == "cors"
            assert hdrs["sec-fetch-dest"] == "empty"
            assert hdrs["sec-fetch-site"] == "same-site"
            assert "upgrade-insecure-requests" not in hdrs
            assert hdrs.get("cache-control") != "max-age=0"
            assert "zstd" in hdrs.get("accept-encoding", "")

        # Account B uses a completely isolated socket pool.
        asyncio.run(run_turn(cfg_b))
        assert len(seen_peers) == 3
        assert seen_peers[2] != seen_peers[0]
    finally:
        curl_pool.close_account("min-a")
        curl_pool.close_account("min-b")
        _stop_server(server, thread)


def test_retool_curl_pool_headers_reuse_isolation_and_error_contract():
    seen_headers: list[dict[str, str]] = []
    seen_peers: list[tuple[str, int]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            seen_peers.append(self.connection.getpeername())
            seen_headers.append({k.lower(): v for k, v in self.headers.items()})
            if self.path == "/refresh-fail":
                body = b"challenge"
                self.send_response(403)
                self.send_header("cf-mitigated", "challenge")
                self.send_header("Set-Cookie", "a=1; Path=/")
                self.send_header("Set-Cookie", "b=2; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b"console.log('ok')"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(ln)
            seen_peers.append(self.connection.getpeername())
            seen_headers.append({k.lower(): v for k, v in self.headers.items()})
            body = b'{"event":"content_block_delta","type":"text","partialText":"ok"}\n{"event":"end_of_stream"}\n'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server, thread = _start_server(Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    cfg_a = {"account_id": "rt-a", "base": base, "cookie": "accessToken=a", "xsrf": "xa"}
    cfg_b = {"account_id": "rt-b", "base": base, "cookie": "accessToken=b", "xsrf": "xb"}
    try:
        # Two streaming POSTs on account A share one socket and never send identity / Connection: close.
        for _ in range(2):
            with curl_pool.retool_open(
                cfg_a, base + "/api/gpt/copilotStreamGPT?origin=frontend",
                data=b"{}", headers=auth.upstream_headers(cfg_a), method="POST", timeout=10, stream=True,
            ) as resp:
                lines = list(resp)
            assert len(lines) == 2

        assert len(seen_peers) == 2 and len(set(seen_peers)) == 1
        assert "zstd" in seen_headers[0]["accept-encoding"]
        assert "identity" not in seen_headers[0]["accept-encoding"]
        assert "close" not in seen_headers[0].get("connection", "").lower()

        # Account B uses a separate socket.
        with curl_pool.retool_open(
            cfg_b, base + "/api/gpt/copilotStreamGPT?origin=frontend",
            data=b"{}", headers=auth.upstream_headers(cfg_b), method="POST", timeout=10, stream=True,
        ) as resp:
            list(resp)
        assert seen_peers[2] != seen_peers[0]

        # HTTP >= 400 raises urllib.error.HTTPError preserving get_all('set-cookie') and cf-mitigated.
        with pytest.raises(urllib.error.HTTPError) as raised:
            curl_pool.retool_open(cfg_a, base + "/refresh-fail", headers=auth.upstream_headers(cfg_a), method="GET", timeout=10)
        assert raised.value.code == 403
        assert auth.cloudflare_challenge(raised.value.code, raised.value.headers) is True
        assert raised.value.headers.get_all("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]
        assert raised.value.read() == b"challenge"
        raised.value.close()

        # retool_refresh.fetch no longer sends retool-relay-catalog in User-Agent.
        fetched = retool_refresh.fetch(base + "/script.js", 1024, cfg=cfg_a)
        assert fetched == "console.log('ok')"
        assert "retool-relay-catalog" not in seen_headers[-1]["user-agent"]
        assert "Chrome/152." in seen_headers[-1]["user-agent"]
    finally:
        curl_pool.close_account("rt-a")
        curl_pool.close_account("rt-b")
        _stop_server(server, thread)


def test_http_pool_defaults_http2_isolates_accounts_and_hides_httpx_ua():
    async def scenario():
        try:
            c_a = http_pool.get_async_client({"account_id": "acct-1"})
            c_b = http_pool.get_async_client({"account_id": "acct-2"})
            c_a2 = http_pool.get_async_client({"account_id": "acct-1"})
            assert c_a is c_a2
            assert c_a is not c_b
            # Verify HTTP/2 is enabled by default on httpx's async transport pool.
            assert getattr(c_a._transport._pool, "_http2", False) is True
            c_h1 = http_pool.get_async_client({"account_id": "acct-1", "http2": False})
            assert c_h1 is not c_a
            assert getattr(c_h1._transport._pool, "_http2", True) is False
        finally:
            await http_pool.close_loop_clients()

    asyncio.run(scenario())
    hdrs = discovery.build_auth_headers({"driver": "openai-chat", "api_key": "sk-test"})
    assert "python-httpx" not in hdrs["User-Agent"]
    assert hdrs["User-Agent"] == discovery.DEFAULT_USER_AGENT


@pytest.mark.parametrize("driver_cls,preset", [
    (openai_chat.OpenAIChatDriver, "openai"),
    (openai_responses.OpenAIResponsesDriver, "openai-responses"),
    (anthropic_messages.AnthropicMessagesDriver, "anthropic"),
])
def test_universal_drivers_abort_unblocks_stalled_stream_immediately(driver_cls, preset):
    release = threading.Event()
    started = threading.Event()

    class HangingSSE(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(ln)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            first = (
                b"event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n"
                if preset == "anthropic"
                else (b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n"
                      if preset == "openai-responses"
                      else b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"},\"finish_reason\":null}]}\n\n")
            )
            self.wfile.write(first)
            self.wfile.flush()
            started.set()
            release.wait(5)

        def log_message(self, *_args):
            pass

    server, thread = _start_server(HangingSSE)
    base = f"http://127.0.0.1:{server.server_port}"
    cfg = {
        "account_id": f"abort-{preset}",
        "preset": preset,
        "driver": driver_cls.driver_id,
        "base_url": base,
        "allow_insecure_http": True,
        "http2": False,
        "api_key": "sk-test",
        "turn_timeout": 30,
    }

    async def scenario():
        sess = SimpleNamespace(_cancelled=False)
        gen = driver_cls().stream_turn(cfg, sess, {"upstream_model": "m", "model": "m"}, "hi", "", None, None, 32)
        ev1 = await anext(gen)
        assert ev1["type"] == "usage_start"

        async def drain():
            async for _ in gen:
                pass

        task = asyncio.create_task(drain())
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        assert callable(sess._active_direct_abort)
        sess._active_direct_abort()
        with pytest.raises(Exception):
            await asyncio.wait_for(task, timeout=1.5)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        await gen.aclose()
        assert sess._active_direct_abort is None
        await http_pool.close_loop_clients()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        _stop_server(server, thread)


def test_anthropic_driver_forwards_only_allowlisted_betas():
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append({k.lower(): v for k, v in request.headers.items()})
        body = (
            b"event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":5}}}\n\n"
            b"event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"ok\"}}\n\n"
            b"event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":1}}\n\n"
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    cfg = {
        "account_id": "anth-beta-test",
        "preset": "anthropic",
        "driver": "anthropic-messages",
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "sk-ant-test",
        "cache_policy": "1h",
        "_http_transport": httpx.MockTransport(handler),
    }
    tok = client_context.bind_anthropic_betas({
        "anthropic-beta": "fine-grained-tool-streaming-2025-05-14,oauth-2025-04-20,claude-code-20250219,context-1m-2025-08-07"
    })

    async def scenario():
        try:
            sess = SimpleNamespace(_cancelled=False)
            evs = [e async for e in anthropic_messages.AnthropicMessagesDriver().stream_turn(
                cfg, sess, {"upstream_model": "claude-sonnet-4-6"}, "hi", "", None, None, 32
            )]
            assert any(e.get("type") == "end" for e in evs)
        finally:
            await http_pool.close_loop_clients()

    try:
        asyncio.run(scenario())
    finally:
        client_context.reset_anthropic_betas(tok)

    beta = seen_headers[0].get("anthropic-beta", "")
    assert "fine-grained-tool-streaming-2025-05-14" in beta
    assert "context-1m-2025-08-07" in beta
    assert "extended-cache-ttl-2025-04-11" in beta
    assert "oauth-2025-04-20" not in beta
    assert "claude-code-20250219" not in beta


def test_config_and_admin_validate_mask_and_invalidate_proxy_and_impersonate(pool, tmp_path):
    # Config rejects SSRF/metadata proxy_url and invalid impersonate profiles, masks proxy passwords.
    bad_cfg = tmp_path / "bad.json"
    bad_cfg.write_text(json.dumps({
        "relay_api_key": "test-relay-key-long-enough-123456",
        "providers": [],
        "proxy_url": "http://169.254.169.254:8080",
    }), encoding="utf-8")
    with pytest.raises(Exception, match="restricted"):
        config_module.load(str(bad_cfg))

    bad_imp = tmp_path / "bad_imp.json"
    bad_imp.write_text(json.dumps({
        "relay_api_key": "test-relay-key-long-enough-123456",
        "providers": [],
        "impersonate": "chrome999",
    }), encoding="utf-8")
    with pytest.raises(Exception, match="impersonate"):
        config_module.load(str(bad_imp))

    good_cfg = tmp_path / "good.json"
    good_cfg.write_text(json.dumps({
        "relay_api_key": "test-relay-key-long-enough-123456",
        "providers": [],
        "proxy_url": "socks5h://proxyuser:SuperSecretProxyPw@127.0.0.1:1080",
        "impersonate": "chrome150",
        "workspace_root": str(tmp_path),
    }), encoding="utf-8")
    loaded = config_module.load(str(good_cfg))
    assert "SuperSecretProxyPw" in loaded["_extra_secrets"]
    assert loaded["impersonate"] == "chrome150"
    assert loaded["http2"] is True

    # Admin PATCH sets proxy_url + impersonate, masks password, keeps real proxy on masked echo, and closes old pool.
    tag = pool.tags[0]
    secret_proxy = "socks5h://u1:AdminProxyPass999@127.0.0.1:9050"
    status, body = pool.call("/admin/api/accounts/" + tag, {"proxy_url": secret_proxy, "impersonate": "chrome150"}, method="PATCH")
    assert status == 200
    assert "AdminProxyPass999" not in json.dumps(body)
    assert body["account"]["proxy_url"] == "socks5h://•••@127.0.0.1:9050"
    assert body["account"]["impersonate"] == "chrome150"
    assert pool.backends[0].cfg["proxy_url"] == secret_proxy
    assert pool.backends[0].cfg["impersonate"] == "chrome150"

    # Re-sending the masked URL keeps the real secret URL intact.
    status2, _ = pool.call("/admin/api/accounts/" + tag, {"proxy_url": "socks5h://•••@127.0.0.1:9050"}, method="PATCH")
    assert status2 == 200
    assert pool.saved()["accounts"][0]["proxy_url"] == secret_proxy

    # Clearing resets to root default without leaving stale keys in saved config.
    status3, cleared = pool.call("/admin/api/accounts/" + tag, {"proxy_url": "", "impersonate": ""}, method="PATCH")
    assert status3 == 200
    assert "proxy_url" not in cleared["account"]
    assert "impersonate" not in cleared["account"]
    assert "proxy_url" not in pool.saved()["accounts"][0]
    assert "impersonate" not in pool.saved()["accounts"][0]
    assert proxy.Runtime is not None


def test_minara_legacy_claude_omits_ttl_and_403_distinguishes_banned_model_from_auth():
    session = SimpleNamespace(_native_client_messages=[
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ], _native_allow_cold_replay=True)

    legacy = minara.build_body({}, {"model": "anthropic/claude-opus-4.1"}, "SYS", "", None, None, 64, session=session)
    assert legacy["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert legacy["messages"][1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    modern = minara.build_body({}, {"model": "anthropic/claude-opus-5.5"}, "SYS", "", None, None, 64, session=session)
    assert modern["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    # Banned-model JSON 403 must not blame the account token or claim auth expiry.
    banned_body = (
        b'{"error":{"message":"Model google/gemini-4-carbon-20261110 is banned",'
        b'"type":"upstream_error","code":403,"metadata":{"previous_errors":[{"code":403}]}}}'
    )
    banned_err = minara._http_error(403, {"content-type": "application/json"}, banned_body)
    assert (banned_err.status, banned_err.code) == (502, "upstream_error")
    assert banned_err.unbilled is True
    assert "banned" in banned_err.message

    # Cloudflare HTML 403 also avoids blaming the account token.
    cf_err = minara._http_error(403, {"cf-mitigated": "challenge"}, b"<html>cloudflare</html>")
    assert (cf_err.status, cf_err.code) == (502, "upstream_error")
    assert cf_err.unbilled is True

    # Genuine token/auth or blank 403 keeps upstream_auth_error.
    token_err = minara._http_error(403, {"content-type": "application/json"}, b'{"error":{"message":"Invalid token"}}')
    assert (token_err.status, token_err.code) == (502, "upstream_auth_error")
    blank_err = minara._http_error(403, {}, b"")
    assert (blank_err.status, blank_err.code) == (502, "upstream_auth_error")


def test_minara_sse_rate_limit_fails_over_cleanly_and_output_only_usage_prices(tmp_path, monkeypatch):
    # Output-only Messages/Event usage still prices against catalog and falls back to upstream cost.
    msg_usage = minara.normalize_messages_usage(
        {"output_tokens": 100},
        pricing={"prompt": 2, "completion": 10, "cache_read": 0.2, "cache_write": 2.5},
        channel="msg_01",
    )
    assert msg_usage["provider_cost"] == pytest.approx(0.001)
    fallback_usage = minara.normalize_messages_usage({"output_tokens": 10, "cost": 0.0042}, pricing=None)
    assert fallback_usage["provider_cost"] == pytest.approx(0.0042)

    # Pre-output SSE 429 becomes unbilled 429 rate_limit_error; post-output stays billed/non-unbilled.
    sse_rl = json.dumps({"error": {"code": 429, "message": "openai/gpt-6.2-sol is temporarily rate-limited upstream."}})
    parser = minara.EventParser()
    with pytest.raises(apierr.ApiError) as pre_exc:
        parser.feed(sse_rl)
    assert (pre_exc.value.status, pre_exc.value.code, pre_exc.value.unbilled) == (429, "rate_limit_error", True)

    parser_mid = minara.EventParser()
    parser_mid.feed(json.dumps({"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}))
    with pytest.raises(apierr.ApiError) as mid_exc:
        parser_mid.feed(sse_rl)
    assert (mid_exc.value.status, mid_exc.value.code, mid_exc.value.unbilled) == (429, "rate_limit_error", False)

    # End-to-end failover when account 1 gets SSE 429 before any output: moves to account 2 cleanly.
    import http.client
    import journal
    import models

    prior = dict(models._MINARA_MODELS)
    key = "failover-sse-429-key-01234567890123"
    model = "minara/openai/sse-rl-test"
    for env_name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(env_name, raising=False)
    cfg_path = tmp_path / "minara_rl.json"
    cfg_path.write_text(json.dumps({
        "accounts": [
            {"account_id": "rl-%d" % n, "label": "Minara RL %d" % n, "upstream": "minara",
             "bearer_token": "tok-%d" % n, "minara_models": [{"id": "openai/sse-rl-test"}]}
            for n in (1, 2)
        ],
        "model": model,
        "relay_api_key": key,
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
    }), encoding="utf-8")
    runtime = proxy.Runtime(config_module.load(str(cfg_path)))
    seen_accounts = []

    class FakeResp:
        def __init__(self, body):
            self.status_code = 200
            self.headers = {"content-type": "text/event-stream"}
            self._body = body

        def iter_content(self, **_):
            yield self._body

        def close(self):
            pass

    ok_body = ("data: " + json.dumps({
        "choices": [{"index": 0, "delta": {"content": "MOVED_OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }) + "\n\ndata: [DONE]\n\n").encode()
    rl_body = ("data: " + sse_rl + "\n\n").encode()

    for idx, backend in enumerate(runtime.accounts.backends):
        def make_post(b, is_first):
            def post(*_a, **_kw):
                seen_accounts.append(b.cfg["account_id"])
                return FakeResp(rl_body if is_first else ok_body)
            return post
        backend._minara_post = make_post(backend, idx == 0)

    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        before = journal.JOURNAL.last_seq
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}),
            {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        reply = conn.getresponse()
        payload = json.loads(reply.read().decode())
        conn.close()
        assert reply.status == 200, payload
        assert payload["choices"][0]["message"]["content"] == "MOVED_OK"
        assert len(seen_accounts) == 2 and len(set(seen_accounts)) == 2
        failover_events = [e for e in journal.JOURNAL.query(since=before) if e["kind"] == "account.failover"]
        assert len(failover_events) == 1
        assert "Retool" not in failover_events[0]["message"]
        assert "Minara" in failover_events[0]["message"]
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
        models._MINARA_MODELS.clear()
        models._MINARA_MODELS.update(prior)
