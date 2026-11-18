"""Stage 5 tests: Admin Gateway CRUD for universal providers, dry-run connection test, live model ping,
route aliases CRUD, virtual client keys CRUD, and secret masking."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.error
import urllib.request

import pytest

import admin
import config
import proxy


@pytest.fixture
def mock_upstream():
    """Local OpenAI-compatible mock upstream serving `/v1/models` and `/v1/chat/completions`."""

    class UpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            auth_hdr = self.headers.get("authorization") or ""
            if auth_hdr != "Bearer sk-live-secret-provider-key-9999":
                body = json.dumps({"error": {"message": f"bad key {auth_hdr}"}}).encode()
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/v1/models":
                body = json.dumps({
                    "object": "list",
                    "data": [
                        {
                            "id": "deepseek/deepseek-chat-v3",
                            "name": "DeepSeek Chat V3",
                            "context_length": 128000,
                            "pricing": {"prompt": "0.00000027", "completion": "0.0000011"},
                        },
                        {
                            "id": "anthropic/claude-sonnet-4-5",
                            "name": "Claude Sonnet 4.5",
                            "context_length": 200000,
                            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                        },
                    ],
                }).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            req_data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/v1/chat/completions":
                model = req_data.get("model", "unknown")
                if req_data.get("stream"):
                    chunks = [
                        {"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}}]},
                        {
                            "id": "c1",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 2, "cost": 0.0025},
                        },
                    ]
                    payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                    raw = payload.encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    raw = json.dumps({
                        "id": "c1",
                        "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 2, "cost": 0.0025},
                    }).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def gateway_admin(tmp_path, monkeypatch):
    """Fresh bootstrap relay (zero Retool/Minara accounts) managed exclusively through Admin APIs."""
    for env_var in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)
    build = tmp_path / "build"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(admin, "BUILD_ROOT", build)

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"relay_api_key": "master-admin-secret-key-0123456789", "workspace_root": str(tmp_path), "providers": []}),
        encoding="utf-8",
    )
    cfg = config.load(str(cfg_path), bootstrap=True)
    cfg["host"], cfg["port"] = "127.0.0.1", 0
    runtime = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(8))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Client:
        url = base

        def admin_call(self, route: str, payload=None, *, method: str = "GET", key: str = "master-admin-secret-key-0123456789"):
            data = json.dumps(payload).encode() if payload is not None else (b"{}" if method in ("POST", "PATCH", "DELETE") else None)
            req = urllib.request.Request(base + route, data=data, method=method)
            if key:
                req.add_header("authorization", f"Bearer {key}")
            if data is not None:
                req.add_header("content-type", "application/json")
                req.add_header("x-admin-request", "1")
                req.add_header("origin", base)
            try:
                resp = urllib.request.urlopen(req, timeout=15)
            except urllib.error.HTTPError as err:
                resp = err
            with resp:
                return resp.status, json.loads(resp.read().decode() or "{}")

        def api_call(self, route: str, payload=None, *, method: str = "GET", key: str = "master-admin-secret-key-0123456789"):
            data = json.dumps(payload).encode() if payload is not None else None
            req = urllib.request.Request(base + route, data=data, method=method)
            if key:
                req.add_header("authorization", f"Bearer {key}")
            if data is not None:
                req.add_header("content-type", "application/json")
            try:
                resp = urllib.request.urlopen(req, timeout=15)
            except urllib.error.HTTPError as err:
                resp = err
            with resp:
                return resp.status, json.loads(resp.read().decode() or "{}")

    client = Client()
    client.runtime = runtime
    client.cfg_path = cfg_path
    yield client
    server.shutdown()
    server.server_close()
    runtime.close()


def test_empty_bootstrap_admin_endpoints_work_cleanly(gateway_admin):
    st, ov = gateway_admin.admin_call("/admin/api/overview")
    assert st == 200
    assert ov["accounts"] == []
    assert ov["providers"] == []
    assert ov["accounts_summary"]["total"] == 0

    st, accs = gateway_admin.admin_call("/admin/api/accounts")
    assert st == 200
    assert accs["accounts"] == []
    assert accs["restart_required"] is False

    st, provs = gateway_admin.admin_call("/admin/api/providers")
    assert st == 200
    assert provs["providers"] == []
    assert any(p["id"] == "openrouter" for p in provs["presets"])
    assert any(p["id"] == "ollama" for p in provs["presets"])


def test_provider_crud_live_adopt_ping_routes_and_client_keys(gateway_admin, mock_upstream):
    secret_key = "sk-live-secret-provider-key-9999"

    # 1. Dry-run test with wrong key -> ok: False, secret masked
    st, bad_probe = gateway_admin.admin_call(
        "/admin/api/providers/test",
        {"name": "Local Hub", "driver": "openai-chat", "base_url": mock_upstream, "api_key": "sk-wrong-secret-key-0000"},
        method="POST",
    )
    assert st == 200
    assert bad_probe["ok"] is False
    assert "sk-wrong-secret-key-0000" not in json.dumps(bad_probe)

    # 2. Dry-run test with valid key -> ok: True, discovers 2 models!
    st, good_probe = gateway_admin.admin_call(
        "/admin/api/providers/test",
        {"name": "Local Hub", "driver": "openai-chat", "base_url": mock_upstream, "api_key": secret_key},
        method="POST",
    )
    assert st == 200
    assert good_probe["ok"] is True
    assert good_probe["models_count"] == 2
    assert good_probe["base_url"] == mock_upstream  # Public base_url must NOT be redacted!

    # 3. Create provider live (no restart needed!)
    st, created = gateway_admin.admin_call(
        "/admin/api/providers",
        {
            "id": "myhub",
            "name": "My Universal Hub",
            "driver": "openai-chat",
            "base_url": mock_upstream,
            "api_key": secret_key,
            "routing_strategy": "fill-first",
            "cache_policy": "auto",
            "auto_discover": True,
        },
        method="POST",
    )
    assert st == 200
    assert created["restart_required"] is False
    prov = created["provider"]
    assert prov["id"] == "myhub"
    assert prov["routable"] is True
    assert prov["published_models_count"] == 2
    assert prov["base_url"] == mock_upstream
    assert secret_key not in json.dumps(created)

    # Verify /v1/models immediately serves discovered models!
    st, v1_models = gateway_admin.api_call("/v1/models")
    assert st == 200
    published_ids = {m["id"] for m in v1_models["data"]}
    assert "myhub/deepseek/deepseek-chat-v3" in published_ids
    assert "myhub/anthropic/claude-sonnet-4-5" in published_ids

    # 4. 1-click synthetic ping on discovered model
    st, ping = gateway_admin.admin_call(
        "/admin/api/models/ping",
        {"model": "myhub/deepseek/deepseek-chat-v3"},
        method="POST",
    )
    assert st == 200
    assert ping["ok"] is True
    assert ping["reply"] == "OK"
    assert ping["cost_usd"] == 0.0025

    # 5. PATCH provider without resending api_key -> preserves stored key & keeps working!
    st, patched = gateway_admin.admin_call(
        "/admin/api/providers/myhub",
        {"name": "My Hub Updated", "api_key": "", "cache_policy": "1h"},
        method="PATCH",
    )
    assert st == 200
    assert patched["provider"]["name"] == "My Hub Updated"
    assert patched["provider"]["cache_policy"] == "1h"
    assert secret_key not in json.dumps(patched)

    # Dry-run test of existing saved provider without passing api_key
    st, retest = gateway_admin.admin_call(
        "/admin/api/providers/test",
        {"provider_id": "myhub"},
        method="POST",
    )
    assert st == 200 and retest["ok"] is True and retest["models_count"] == 2

    # 6. Create route alias (`coding-best`) and ping it live!
    st, route_created = gateway_admin.admin_call(
        "/admin/api/routes",
        {
            "id": "coding-best",
            "label": "Best Coding Chain",
            "targets": ["myhub/anthropic/claude-sonnet-4-5", "myhub/deepseek/deepseek-chat-v3"],
            "strategy": "priority",
        },
        method="POST",
    )
    assert st == 200
    assert route_created["route"]["routable"] is True

    st, alias_ping = gateway_admin.admin_call(
        "/admin/api/models/ping",
        {"model": "coding-best"},
        method="POST",
    )
    assert st == 200 and alias_ping["ok"] is True and alias_ping["reply"] == "OK"

    # Update & list route
    st, _ = gateway_admin.admin_call(
        "/admin/api/routes/coding-best",
        {"label": "Updated Chain", "strategy": "least-inflight"},
        method="PATCH",
    )
    assert st == 200
    st, routes_list = gateway_admin.admin_call("/admin/api/routes")
    assert st == 200 and routes_list["routes"][0]["strategy"] == "least-inflight"

    # 7. Create virtual Client Key (`AccessKey`), use it on /v1/chat/completions, inspect usage, disable, delete!
    st, ck_created = gateway_admin.admin_call(
        "/admin/api/client-keys",
        {
            "label": "Cursor Laptop",
            "allowed_models": ["coding-best", "myhub/deepseek/*"],
            "rpm_limit": 30,
            "max_budget_usd": 5.0,
        },
        method="POST",
    )
    assert st == 200
    raw_subkey = ck_created["raw_key"]
    ck_id = ck_created["client_key"]["id"]
    assert raw_subkey.startswith("sk-proxi-")

    # Subsequent GET /admin/api/client-keys NEVER exposes raw_subkey
    st, ck_list = gateway_admin.admin_call("/admin/api/client-keys")
    assert st == 200
    assert raw_subkey not in json.dumps(ck_list)
    assert ck_list["client_keys"][0]["id"] == ck_id

    # Sub-key can call allowed alias `coding-best`, and cannot call disallowed `myhub/anthropic/claude-sonnet-4-5` directly
    st, forbidden = gateway_admin.api_call(
        "/v1/chat/completions",
        {"model": "myhub/anthropic/claude-sonnet-4-5", "messages": [{"role": "user", "content": "hi"}]},
        method="POST",
        key=raw_subkey,
    )
    assert st == 403

    st, ok_chat = gateway_admin.api_call(
        "/v1/chat/completions",
        {"model": "coding-best", "messages": [{"role": "user", "content": "hi"}]},
        method="POST",
        key=raw_subkey,
    )
    assert st == 200
    assert ok_chat["choices"][0]["message"]["content"] == "OK"

    # Usage stats updated on client key!
    st, ck_after = gateway_admin.admin_call("/admin/api/client-keys")
    assert ck_after["client_keys"][0]["requests"] == 1
    assert ck_after["client_keys"][0]["spent_usd"] == pytest.approx(0.0025)

    # Disable client key -> 401
    st, _ = gateway_admin.admin_call(f"/admin/api/client-keys/{ck_id}", {"enabled": False}, method="PATCH")
    assert st == 200
    st, _ = gateway_admin.api_call("/v1/models", key=raw_subkey)
    assert st == 401

    # Delete client key & route & provider
    assert gateway_admin.admin_call(f"/admin/api/client-keys/{ck_id}", method="DELETE")[0] == 200
    assert gateway_admin.admin_call("/admin/api/routes/coding-best", method="DELETE")[0] == 200
    # The provider is the only backend of this relay: it is not removed while nothing else would answer (audit A14-13, as
    # for the last enabled account); beside a second one it is.
    assert gateway_admin.admin_call("/admin/api/providers/myhub", method="DELETE")[0] == 409
    st, _ = gateway_admin.admin_call(
        "/admin/api/providers",
        {"id": "spare", "driver": "openai-chat", "base_url": mock_upstream, "api_key": secret_key, "discover_now": False},
        method="POST",
    )
    assert st == 200
    assert gateway_admin.admin_call("/admin/api/providers/myhub", method="DELETE")[0] == 200

    st, provs_end = gateway_admin.admin_call("/admin/api/providers")
    assert st == 200 and [p["id"] for p in provs_end["providers"]] == ["spare"]
