"""Administration API and static build security, with an offline HTTP server."""
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

import pytest

import admin
import config
import proxy


@pytest.fixture
def admin_relay(tmp_path, monkeypatch):
    build = tmp_path / "build"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text('<html><script src="/admin/assets/app.js"></script></html>', encoding="utf-8")
    (build / "assets" / "app.js").write_text('console.log("admin")', encoding="utf-8")
    (build / "assets" / "app.css").write_text("body{margin:0}", encoding="utf-8")
    (build / "assets" / "hidden.json").write_text('{"secret":"private"}', encoding="utf-8")
    monkeypatch.setattr(admin, "BUILD_ROOT", build)
    monkeypatch.delenv("RELAY_SESSION_MODE", raising=False)
    saved = {"base": "https://example.invalid", "page_uuid": "private-page", "ua": "test-agent",
             "cookie": "accessToken=private-cookie-value", "xsrf": "private-xsrf-token",
             "relay_api_key": "private-relay-key", "provider_resource_name": "private-resource",
             "thread_id": "private-thread", "model": "claude-opus-5-5"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    cfg = dict(config.DEFAULTS, **saved, _config_path=str(path), host="127.0.0.1",
               cookie_values=["private-cookie-value"])
    runtime = proxy.Runtime(cfg)

    class Credits:
        def __init__(self):
            self.balance = {"available": 37.5, "monthly_allocation": 100, "stale": False}

        def get_balance(self, force_refresh=False):
            assert not force_refresh
            return dict(self.balance)

        def usage(self):
            return {"session_used": 2.5, "turns": 1,
                    "recent": [{"ts": 1, "turn_id": "turn-1", "credits": 2.5}]}

    runtime.credits = Credits()
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime),
                                 threading.BoundedSemaphore(8))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Client:
        def request(self, route, payload=None, *, key="private-relay-key", method="GET", headers=None):
            body = json.dumps(payload).encode() if payload is not None else None
            request = urllib.request.Request(base + route, data=body, method=method)
            if key:
                request.add_header("authorization", "Bearer " + key)
            if body is not None:
                request.add_header("content-type", "application/json")
            for name, value in (headers or {}).items():
                request.add_header(name, value)
            try:
                response = urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                raw = response.read()
                parsed = json.loads(raw) if "application/json" in response.headers.get("content-type", "") else raw
                time.sleep(0.01)
                return response.status, parsed, response.headers

        def patch(self, changes, **options):
            return self.request("/admin/api/settings", {"settings": changes}, method="PATCH",
                                headers={"x-admin-request": "1", "origin": base}, **options)

    client = Client()
    client.runtime, client.cfg, client.path, client.url, client.build = runtime, cfg, path, base, build
    yield client
    server.shutdown()
    server.server_close()
    runtime.loop.call_soon_threadsafe(runtime.loop.stop)


@pytest.mark.parametrize("route", ["overview", "models", "settings"])
@pytest.mark.parametrize("key", [None, "incorrect-key"])
def test_admin_api_requires_existing_header_key(admin_relay, route, key):
    status, body, headers = admin_relay.request("/admin/api/" + route, key=key)
    assert status == 401
    assert body["error"]["code"] == "unauthorized"
    assert headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in headers


def test_query_key_is_not_an_admin_login(admin_relay):
    status, _, _ = admin_relay.request("/admin/api/settings?key=private-relay-key", key=None)
    assert status == 401


@pytest.mark.parametrize("route,ctype", [("/admin", "text/html"), ("/admin/", "text/html"),
                                          ("/admin/assets/app.js", "application/javascript"),
                                          ("/admin/assets/app.css", "text/css")])
def test_public_build_contains_no_credentials_and_has_security_headers(admin_relay, route, ctype):
    status, body, headers = admin_relay.request(route, key=None)
    assert status == 200 and ctype in headers["content-type"]
    assert b"private-" not in body
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize("route", ["/admin/config.json", "/admin/relay_key.txt", "/admin/assets/hidden.json",
                                    "/admin/assets/../../secret.js", "/admin/assets/%2e%2e/%2e%2e/secret.js",
                                    "/admin/assets/%5c..%5csecret.js", "/admin/assets/app.js.map"])
def test_static_only_exposes_allowlisted_contained_build_files(admin_relay, route):
    (admin_relay.path.parent / "secret.js").write_text("credential", encoding="utf-8")
    status, _, _ = admin_relay.request(route, key=None)
    assert status == 404


def test_static_head_is_bodyless_and_methods_are_limited(admin_relay):
    status, body, headers = admin_relay.request("/admin", key=None, method="HEAD")
    assert status == 200 and body == b"" and int(headers["content-length"]) > 0
    assert admin_relay.request("/admin", key=None, method="POST")[0] == 405
    assert admin_relay.request("/admin/api/settings", method="POST")[0] == 405


def test_overview_reports_real_counters_routes_and_connection_without_secrets(admin_relay):
    admin_relay.runtime.pool._active["lease"] = object()
    admin_relay.runtime.pool._conversations["secret-conversation-key"] = object()
    admin_relay.runtime.credits.balance["stale_error"] = "private-relay-key private-xsrf-token private-cookie-value"
    status, body, _ = admin_relay.request("/admin/api/overview")
    assert status == 200
    assert body["balance"]["available"] == 37.5 and body["usage"]["turns"] == 1
    assert body["sessions"]["active"] == body["sessions"]["cached"] == 1
    assert 0 < body["models"]["routable"] < body["models"]["total"]
    assert body["models"]["available_verified"] is None
    assert body["connection"] == {"openai_base_url": admin_relay.url + "/v1", "anthropic_base_url": admin_relay.url}
    encoded = json.dumps(body)
    for secret in ("private-relay-key", "private-xsrf-token", "private-cookie-value", "private-resource",
                   "private-thread", "private-page", "secret-conversation-key"):
        assert secret not in encoded


def test_model_metadata_is_catalog_data_with_routing_separate_from_verification(admin_relay):
    status, body, _ = admin_relay.request("/admin/api/models")
    assert status == 200 and body["default_model"] == "claude-opus-5-5"
    by_id = {entry["id"]: entry for entry in body["data"]}
    assert by_id["claude-opus-5-5"]["routable"] is True
    assert by_id["gpt-6-sol"]["routable"] is False
    assert by_id["claude-opus-5-5"]["available_verified"] is None
    assert "reasoning_effort_levels" in by_id["claude-opus-5-5"]
    assert "metadata_checked_at" in by_id["claude-opus-5-5"]


def test_settings_are_whitelisted_and_changes_wait_for_restart(admin_relay):
    status, before, _ = admin_relay.request("/admin/api/settings")
    assert status == 200 and before["writable"] is True and before["pending_restart"] == []
    assert set(before["settings"]) == set(before["schema"])
    assert "cookie" not in before["settings"] and "relay_api_key" not in before["schema"]
    status, after, _ = admin_relay.patch({"show_thinking": False, "max_workers": 8})
    assert status == 200 and after["saved"] and after["restart_required"]
    assert set(after["pending_restart"]) == {"show_thinking", "max_workers"}
    assert after["applied"] == []
    assert admin_relay.cfg["show_thinking"] is True
    assert admin_relay.cfg["max_workers"] == config.DEFAULTS["max_workers"]
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    assert saved["show_thinking"] is False and saved["max_workers"] == 8
    assert saved["cookie"] == "accessToken=private-cookie-value" and saved["xsrf"] == "private-xsrf-token"
    assert not list(admin_relay.path.parent.glob(".admin-*.tmp"))
    assert admin_relay.request("/admin/api/settings")[1]["pending_restart"] == after["pending_restart"]


def test_rate_limit_changes_apply_to_new_admission_without_replacing_pool(admin_relay):
    pool = admin_relay.runtime.pool
    limiter = admin_relay.runtime.limiter
    status, body, _ = admin_relay.patch({"rate_limit_rps": 2.5, "rate_limit_burst": 5})
    assert status == 200 and not body["restart_required"]
    assert set(body["applied"]) == {"rate_limit_rps", "rate_limit_burst"}
    assert admin_relay.runtime.pool is pool and admin_relay.runtime.limiter is limiter
    assert limiter.rate == 2.5 and limiter.burst == 5 and limiter.tokens <= 5


@pytest.mark.parametrize("changes", [{"cookie": "replacement"}, {"relay_api_key": "replacement"},
                                     {"show_thinking": "false"}, {"max_workers": True}, {"max_workers": 0},
                                     {"max_frame_bytes": -1}, {"rate_limit_rps": float("inf")},
                                     {"rate_limit_burst": 0}, {"model": "unknown"},
                                     {"reasoning_effort": "xhigh"}, {"session_mode": "invalid"}])
def test_invalid_or_protected_settings_do_not_change_disk_or_runtime(admin_relay, changes):
    before = admin_relay.path.read_bytes()
    status, body, _ = admin_relay.patch(changes)
    assert status == 400 and "error" in body
    assert admin_relay.path.read_bytes() == before
    assert admin_relay.cfg["max_workers"] == config.DEFAULTS["max_workers"]


@pytest.mark.parametrize("headers", [{}, {"x-admin-request": "1", "origin": "http://attacker.invalid"},
                                     {"x-admin-request": "1", "origin": "null"},
                                     {"x-admin-request": "1", "sec-fetch-site": "cross-site"}])
def test_mutations_require_csrf_header_and_same_origin(admin_relay, headers):
    before = admin_relay.path.read_bytes()
    status, _, _ = admin_relay.request("/admin/api/settings", {"settings": {"max_workers": 4}},
                                       method="PATCH", headers=headers)
    assert status == 403 and admin_relay.path.read_bytes() == before


def test_atomic_save_failure_preserves_configuration_and_live_limiter(admin_relay, monkeypatch):
    before = admin_relay.path.read_bytes()

    def fail_replace(*_):
        raise OSError("disk unavailable")

    monkeypatch.setattr(admin.os, "replace", fail_replace)
    status, _, _ = admin_relay.patch({"rate_limit_rps": 3})
    assert status == 503 and admin_relay.path.read_bytes() == before
    assert admin_relay.cfg["rate_limit_rps"] == 0 and admin_relay.runtime.limiter.rate == 0
    assert not list(admin_relay.path.parent.glob(".admin-*.tmp"))


def test_environment_owned_session_mode_cannot_be_overwritten(admin_relay, monkeypatch):
    monkeypatch.setenv("RELAY_SESSION_MODE", "per_conversation")
    assert admin_relay.patch({"session_mode": "per_request"})[0] == 400


# -- journal ------------------------------------------------------------------
import journal
import socket


def test_journal_redacts_configured_secrets_and_known_shapes(admin_relay):
    journal.JOURNAL.reset()
    event = journal.emit("test.secret", "key private-relay-key cookie private-cookie-value", "info",
                         nested={"xsrf": "private-xsrf-token", "jwt": "eyJabcdefghij.abcdefghijk.abcdefghijk"})
    encoded = json.dumps(event)
    for secret in ("private-relay-key", "private-cookie-value", "private-xsrf-token", "eyJabcdefghij"):
        assert secret not in encoded
    assert event["category"] == "test" and event["seq"] == 1


def test_events_api_filters_by_level_category_text_and_since(admin_relay):
    journal.JOURNAL.reset()
    journal.emit("turn.begin", "alpha", "info", turn_id="t1", model="m")
    journal.emit("exec.result", "beta failed", "error", turn_id="t1", exit=1)
    journal.emit("auth.refresh", "gamma", "success")
    status, body, _ = admin_relay.request("/admin/api/events?level=error")
    assert status == 200 and [e["message"] for e in body["data"]] == ["beta failed"]
    _, body, _ = admin_relay.request("/admin/api/events?category=auth,turn")
    assert [e["kind"] for e in body["data"]] == ["turn.begin", "auth.refresh"]
    _, body, _ = admin_relay.request("/admin/api/events?q=GAMMA")
    assert len(body["data"]) == 1
    _, body, _ = admin_relay.request("/admin/api/events?since=2")
    assert [e["seq"] for e in body["data"]] == [3] and body["last_seq"] == 3
    assert admin_relay.request("/admin/api/events?limit=abc")[0] == 400


def test_admin_requests_are_logged_but_polling_is_not(admin_relay):
    journal.JOURNAL.reset()
    admin_relay.request("/admin/api/overview")
    admin_relay.request("/admin/api/overview", key="wrong")
    admin_relay.request("/health", key=None)
    kinds = [e["kind"] for e in journal.JOURNAL.query()]
    assert "admin.auth_failed" in kinds
    http = [e for e in journal.JOURNAL.query() if e["kind"] == "http.request"]
    assert [e["data"]["path"] for e in http] == ["/admin/api/overview", "/health"]
    assert http[0]["data"]["status"] == 401 and http[0]["level"] == "warn"


def test_turn_traces_and_metrics_aggregate_journal(admin_relay):
    journal.JOURNAL.reset()
    journal.emit("turn.begin", "", turn_id="abc", model="claude-opus-5-5", effort="high", transport="direct")
    journal.emit("upstream.first_token", "", turn_id="abc", ttft_ms=2100)
    journal.emit("upstream.usage", "", turn_id="abc", credits=0.25, cache_read=900, cache_write=100,
                 input_tokens=0)
    journal.emit("tool.call", "", turn_id="abc", name="read")
    journal.emit("turn.end", "", "success", turn_id="abc", reason="tool_calls", duration_ms=3400)
    journal.emit("http.request", "", "success", route="anthropic", status=200, duration_ms=3500)
    _, body, _ = admin_relay.request("/admin/api/turns")
    trace = body["data"][0]
    assert trace["turn_id"] == "abc" and trace["ttft_ms"] == 2100 and trace["tools"] == 1
    assert trace["status"] == "tool_calls" and trace["credits"] == 0.25 and trace["cache_read"] == 900
    _, metrics, _ = admin_relay.request("/admin/api/metrics?window=600")
    totals = metrics["totals"]
    assert totals["turns"] == 1 and totals["requests"] == 1 and totals["ttft_p50"] == 2100
    assert totals["cache_hit_ratio"] == 0.9 and metrics["models"] == {"claude-opus-5-5": 1}
    assert len(metrics["buckets"]) == 60


def test_live_stream_delivers_new_events_with_header_auth(admin_relay, monkeypatch):
    journal.JOURNAL.reset()
    monkeypatch.setattr(admin, "STREAM_MAX_SECONDS", 3)
    port = int(admin_relay.url.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"GET /admin/api/events/stream?since=0 HTTP/1.1\r\nHost: x\r\n"
                 b"Authorization: Bearer private-relay-key\r\n\r\n")
    data = b""
    while b"event: hello" not in data:
        data += sock.recv(4096)
    journal.emit("exec.start", "live-event-marker", "info")
    while b"live-event-marker" not in data:
        chunk = sock.recv(4096)
        assert chunk
        data += chunk
    sock.close()
    head = data.split(b"\r\n\r\n", 1)[0].decode()
    assert "200" in head.splitlines()[0] and "text/event-stream" in head
    assert "access-control-allow-origin" not in head.lower()


def test_stream_requires_key(admin_relay):
    assert admin_relay.request("/admin/api/events/stream", key=None)[0] == 401


def test_settings_change_and_session_actions_are_journaled(admin_relay):
    journal.JOURNAL.reset()
    admin_relay.patch({"rate_limit_rps": 1.5})
    event = journal.JOURNAL.query(categories={"admin"})[-1]
    assert event["kind"] == "admin.settings"
    assert event["data"]["changed"]["rate_limit_rps"] == {"from": 0.0, "to": 1.5}
    status, _, _ = admin_relay.request("/admin/api/session", {"action": "login"}, method="POST",
                                       headers={"x-admin-request": "1", "origin": admin_relay.url})
    assert status == 200 and journal.JOURNAL.query(categories={"admin"})[-1]["kind"] == "admin.login"
    assert journal.JOURNAL.query()[-1]["kind"] == "http.request"
    assert admin_relay.request("/admin/api/session", {"action": "login"}, method="POST")[0] == 403


def test_clear_resets_buffer_and_records_the_clear(admin_relay):
    journal.emit("x.y", "old")
    status, body, _ = admin_relay.request("/admin/api/events/clear", {}, method="POST",
                                          headers={"x-admin-request": "1", "origin": admin_relay.url})
    assert status == 200
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if len(journal.JOURNAL.query()) >= 2:
            break
        time.sleep(0.01)
    assert [e["kind"] for e in journal.JOURNAL.query()] == ["admin.journal_cleared", "http.request"]


def test_journal_persists_jsonl_and_restores_tail(tmp_path):
    path = tmp_path / "journal.jsonl"
    first = journal.Journal()
    first.configure({"relay_api_key": "secret-key-value"}, str(path))
    first.emit("relay.start", "boot secret-key-value", "success")
    first.emit("turn.end", "done", "success", turn_id="t9")
    first.configure(None, None)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and "secret-key-value" not in path.read_text(encoding="utf-8")
    second = journal.Journal()
    second.configure({}, str(path))
    restored = second.query()
    assert [e["kind"] for e in restored] == ["relay.start", "turn.end"] and restored[0]["restored"]
    assert second.emit("x.y", "next")["seq"] == 3


def test_journal_never_raises_on_odd_values():
    log = journal.Journal()
    assert log.emit("x.y", None, "bogus-level", value=float("nan"), obj=object(), deep=[[[[[[[[1]]]]]]]])["level"] == "info"
