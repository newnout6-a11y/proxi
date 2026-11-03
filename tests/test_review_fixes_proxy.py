"""Review fixes for PR #18 (proxy side): per-account reporting, private chat labels,
bounded metadata lookups and a UTF-8 relay log."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import client_context
import config as config_module
import journal
import proxy

KEY = "test-key-123"
SESSION = "ac247765-c122-4cca-ba05-ef006343ee93"
ANTH = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}


# -- two real accounts behind one relay -------------------------------------
class FakeManager:
    def __init__(self, available, allocation=500.0):
        self.balance = {"available": available, "monthly_allocation": allocation, "reset_date": "2026-12-01"}
        self.used = []

    def get_balance(self, force_refresh=False):
        if self.balance["available"] is None:
            return {"available": None, "error": "boom"}
        return dict(self.balance, session_used=sum(cost for _, cost in self.used))

    def balance_snapshot(self):
        return dict(self.balance, estimated=bool(self.used))

    def record_turn(self, turn_id, cost):
        self.used.append((turn_id, cost))
        if self.balance["available"] is not None:
            self.balance["available"] = round(self.balance["available"] - cost, 4)

    def usage(self):
        return {"session_used": sum(cost for _, cost in self.used), "turns": len(self.used),
                "recent": [{"ts": 100 + index, "turn_id": turn, "credits": cost}
                           for index, (turn, cost) in enumerate(self.used)],
                "available": self.balance["available"]}


class ServingRunner:
    """Serves each turn on one chosen account and debits that account only."""

    def __init__(self, runtime, managers):
        self.runtime, self.managers, self.serving, self.cost = runtime, managers, 0, 1.0

    async def run(self, parsed, out, holder):
        backend = self.runtime.accounts.backends[self.serving]
        holder["session"] = SimpleNamespace(session=SimpleNamespace(backend=backend))
        self.managers[self.serving].record_turn("turn-%s" % parsed.request_id, self.cost)
        for event in ({"type": "credits", "value": self.cost}, {"type": "text", "delta": "ok"},
                      {"type": "end", "reason": "stop"}):
            out.put(event)
        out.put(None)


@pytest.fixture
def multi(monkeypatch, tmp_path):
    def account(name):
        return {"account_id": name, "page_uuid": "page-" + name, "thread_id": "thread-" + name,
                "cookie": "accessToken=cookie-%s-123456; xsrfToken=q" % name, "xsrf": "q"}

    cfg = dict(config_module.DEFAULTS)
    cfg.update({"base": "https://example.invalid", "page_uuid": "page-acc-a", "thread_id": "thread-a",
                "ua": "test-agent", "cookie": "accessToken=cookie-acc-a-123456; xsrfToken=q", "xsrf": "q",
                "relay_api_key": KEY, "workspace_root": str(tmp_path), "command_log": str(tmp_path / "c.log"),
                "host": "127.0.0.1", "port": 0, "cookie_values": [], "client_metadata_home": str(tmp_path),
                "accounts": [account("acc-a"), account("acc-b")]})
    runtime = proxy.Runtime(cfg)
    managers = [FakeManager(100.0), FakeManager(7.0)]
    for backend, manager in zip(runtime.accounts.backends, managers):
        runtime.accounts.managers[backend] = manager
        backend.credits = manager
    runtime.credits = managers[0]
    runner = ServingRunner(runtime, managers)
    runtime.runner = runner
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    class Ctx:
        url, managers_, runner_, backends = base, managers, runner, runtime.accounts.backends
        tags = [journal.tag("acc-a"), journal.tag("acc-b")]

        def call(self, path, payload=None, *, key=KEY):
            body = json.dumps(payload).encode() if payload is not None else None
            request = urllib.request.Request(base + path, data=body)
            request.add_header("content-type", "application/json")
            if key:
                request.add_header("authorization", "Bearer " + key)
            try:
                response = urllib.request.urlopen(request, timeout=20)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = response.read().decode()
                is_json = "json" in response.headers.get("content-type", "")
                return response.status, (json.loads(raw or "{}") if is_json else raw), response.headers

    yield Ctx()
    server.shutdown()
    try:
        server.server_close()
    except OSError:
        pass


def _wait_logged(request_id):
    deadline = time.monotonic() + 2
    while not any(event["kind"] == "http.request" and event.get("request_id") == request_id
                  for event in journal.JOURNAL.query()):
        assert time.monotonic() < deadline, "HTTP completion was not journaled"
        time.sleep(0.01)


# -- 18-6: per-account balance reporting ------------------------------------
@pytest.mark.parametrize("serving", [0, 1])
def test_balance_line_reports_the_account_that_served_the_request(multi, capsys, monkeypatch, serving):
    monkeypatch.setenv("NO_COLOR", "1")
    multi.runner_.serving = serving
    status, _, headers = multi.call("/v1/messages", ANTH)
    assert status == 200
    request_id = headers["request-id"]
    _wait_logged(request_id)
    output = capsys.readouterr().out
    expected = {0: "99.0000", 1: "6.0000"}[serving]
    other = {0: "6.0000", 1: "99.0000"}[serving]
    line = next(item for item in output.splitlines() if "[%s] [balance]" % request_id in item)
    assert "account=%s" % multi.tags[serving] in line
    assert "balance=%s" % expected in line and "spent=1.0000" in line
    assert other not in line and "balance=100.0000" not in line


def test_balance_endpoint_lists_every_account_and_sums_known_balances(multi):
    multi.backends[1].unavailable = {"reason": "exhausted", "available": 7.0}
    status, body, _ = multi.call("/v1/balance")
    assert status == 200
    assert body["available"] == 107.0
    assert body["monthly_allocation"] == 1000.0 and body["reset_date"] == "2026-12-01"
    accounts = body["accounts"]
    assert [item["account"] for item in accounts] == multi.tags
    assert [item["available"] for item in accounts] == [100.0, 7.0]
    assert [item["state"] for item in accounts] == ["active", "exhausted"]
    assert accounts[0]["reset_date"] == "2026-12-01" and accounts[0]["monthly_allocation"] == 500.0


def test_balance_total_skips_accounts_with_unknown_balance(multi):
    multi.managers_[1].balance["available"] = None
    status, body, _ = multi.call("/v1/balance")
    assert status == 200 and body["available"] == 100.0
    assert body["accounts"][1]["available"] is None and body["accounts"][1]["error"] == "boom"
    multi.managers_[0].balance["available"] = None
    status, body, _ = multi.call("/v1/balance")
    assert status == 503 and body["available"] is None


def test_usage_endpoint_aggregates_all_accounts(multi):
    multi.managers_[0].record_turn("a1", 1.5)
    multi.managers_[1].record_turn("b1", 2.0)
    multi.managers_[1].record_turn("b2", 0.5)
    status, body, _ = multi.call("/v1/usage")
    assert status == 200
    assert body["session_used"] == 4.0 and body["turns"] == 3
    assert [item["session_used"] for item in body["accounts"]] == [1.5, 2.5]
    assert [item["turns"] for item in body["accounts"]] == [1, 2]
    assert {item["turn_id"] for item in body["recent"]} == {"a1", "b1", "b2"}


def test_health_is_ok_while_one_account_is_routable_and_healthy(multi):
    multi.managers_[0].balance["available"] = None  # first account cannot report a balance
    status, body, _ = multi.call("/health", key=None)
    assert status == 200 and body["ok"] is True
    multi.managers_[0].balance["available"] = 100.0
    multi.backends[0].unavailable = {"reason": "auth_dead"}
    status, body, _ = multi.call("/health", key=None)
    assert status == 200 and body["ok"] is True
    assert body["accounts"] == {"total": 2, "routable": 1, "reserve": 0, "states": {"auth_dead": 1, "active": 1}}
    assert "available" not in body and "monthly_allocation" not in body


def test_health_fails_without_a_routable_healthy_account(multi):
    multi.backends[0].unavailable = {"reason": "exhausted"}
    multi.managers_[1].balance["stale"] = True
    status, body, _ = multi.call("/health", key=None)
    assert status == 503 and body["ok"] is False and body["upstream"] == "degraded"
    multi.managers_[1].balance["stale"] = False
    multi.backends[0].unavailable = {"reason": "exhausted", "available": 0.0}
    multi.backends[1].unavailable = {"reason": "auth_dead"}
    status, body, _ = multi.call("/health", key=None)
    assert status == 503 and body["accounts"]["routable"] == 0


def _exhausted(available):
    return {"reason": "exhausted", "available": available, "threshold": 20.0}


def test_health_counts_exhausted_accounts_with_credits_as_reserve(multi):
    for backend in multi.backends:
        backend.unavailable = _exhausted(15.0)
    status, body, _ = multi.call("/health", key=None)
    assert status == 200 and body["ok"] is True and body["upstream"] == "ok"
    assert body["accounts"] == {"total": 2, "routable": 2, "reserve": 2, "states": {"exhausted": 2}}


def test_health_treats_unknown_reserve_balance_as_serving(multi):
    multi.backends[0].unavailable = _exhausted(None)
    multi.backends[1].unavailable = _exhausted(0.0)
    status, body, _ = multi.call("/health", key=None)
    assert status == 200
    assert body["accounts"]["routable"] == 1 and body["accounts"]["reserve"] == 1


def test_health_fails_when_exhausted_accounts_have_no_credits(multi):
    for backend in multi.backends:
        backend.unavailable = _exhausted(0.0)
    status, body, _ = multi.call("/health", key=None)
    assert status == 503 and body["ok"] is False
    assert body["accounts"] == {"total": 2, "routable": 0, "reserve": 0, "states": {"exhausted": 2}}


def test_health_ignores_reserve_while_an_account_is_active(multi):
    multi.backends[0].unavailable = _exhausted(15.0)
    status, body, _ = multi.call("/health", key=None)
    assert status == 200
    assert body["accounts"] == {"total": 2, "routable": 1, "reserve": 0, "states": {"exhausted": 1, "active": 1}}


# -- 18-9: chat titles come only from real titles ---------------------------
def test_first_user_message_is_never_a_chat_title(tmp_path):
    transcript = tmp_path / ".claude/projects/project" / (SESSION + ".jsonl")
    transcript.parent.mkdir(parents=True)
    secret = "my password is hunter2-SuperSecret please fix"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": secret}}) + "\n", encoding="utf-8")
    history = tmp_path / ".claude/history.jsonl"
    history.write_text(json.dumps({"sessionId": SESSION, "display": secret}) + "\n", encoding="utf-8")
    info = client_context.describe({"metadata": {"user_id": json.dumps({"session_id": SESSION})}},
                                   {"user-agent": "claude-cli/2.0"}, {"client_metadata_home": str(tmp_path)})
    assert info["client"] == "claude-code" and info["client_session_id"] == SESSION
    assert "chat_title" not in info and "title_source" not in info
    assert client_context.label(info) == "Claude Code"
    assert "hunter2" not in json.dumps(info)


def test_summary_title_is_still_used(tmp_path):
    transcript = tmp_path / ".claude/projects/project" / (SESSION + ".jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "private prompt"}}) + "\n" +
                          json.dumps({"type": "summary", "summary": "Починка прокси"}), encoding="utf-8")
    info = client_context.describe({"metadata": {"user_id": json.dumps({"session_id": SESSION})}},
                                   {"user-agent": "claude-cli/2.0"}, {"client_metadata_home": str(tmp_path)})
    assert info["chat_title"] == "Починка прокси" and info["title_source"] == "claude-summary"


def test_chat_title_secrets_are_scrubbed_in_journal_prefix_and_console(monkeypatch):
    instance = journal.Journal()
    instance.configure({"relay_api_key": "secret123456"})
    monkeypatch.setattr(journal, "JOURNAL", instance)
    token = client_context.bind({"client": "opencode", "chat_title": "deploy secret123456 now"})
    try:
        event = instance.emit("turn.begin", "hello")
        import console_log
        printed = console_log.render("[req] " + client_context.label())
    finally:
        client_context.reset(token)
    assert "secret123456" not in json.dumps(event, ensure_ascii=False)
    assert "secret123456" not in printed and "[redacted]" in event["message"]


# -- 18-11: no client lookups outside completion routes ---------------------
LABEL_HEADERS = {"x-proxi-client": "opencode", "x-proxi-session-id": "ses_probe"}


@pytest.mark.parametrize("path,authenticated,method", [
    ("/health", False, None), ("/v1/models", True, None), ("/codex/models", True, None),
    ("/v1/balance", True, None), ("/v1/usage", True, None), ("/v1/models", False, None),
    ("/v1/messages", False, "OPTIONS")])
def test_non_completion_routes_never_describe_the_client(relay, monkeypatch, path, authenticated, method):
    calls = []
    monkeypatch.setattr(client_context, "describe", lambda *args, **kw: calls.append(path) or {})
    relay.status_of(path, key=relay.key if authenticated else None, method=method, headers=LABEL_HEADERS)
    assert calls == []


@pytest.mark.parametrize("path,payload", [("/v1/messages", ANTH), ("/v1/messages/count_tokens", ANTH)])
def test_completion_routes_describe_the_client_exactly_once(relay, monkeypatch, path, payload):
    real, calls = client_context.describe, []
    monkeypatch.setattr(client_context, "describe", lambda *args, **kw: calls.append(1) or real(*args, **kw))
    relay.cfg["client_metadata_home"] = str(relay.workspace)
    assert relay.status_of(path, payload, headers=LABEL_HEADERS) == 200
    assert len(calls) == 1


@pytest.fixture
def clean_cache():
    client_context._CACHE.clear()
    yield
    client_context._CACHE.clear()


def test_title_cache_evicts_oldest_entries_instead_of_clearing(clean_cache, monkeypatch, tmp_path):
    monkeypatch.setattr(client_context, "_desktop_title", lambda *_a: {})
    monkeypatch.setattr(client_context, "_claude_title", lambda *_a: {})
    ids = ["ac247765-c122-4cca-ba05-%012d" % index for index in range(1030)]
    for session_id in ids:
        client_context._local_title("claude-code", session_id, tmp_path)
    keys = {key[2] for key in client_context._CACHE}
    assert len(keys) == 1024
    assert ids[-1] in keys and ids[-2] in keys and ids[6] in keys
    assert ids[0] not in keys and ids[5] not in keys


def test_recently_used_title_survives_eviction(clean_cache, monkeypatch, tmp_path):
    scans = []
    monkeypatch.setattr(client_context, "_desktop_title", lambda *_a: {})
    monkeypatch.setattr(client_context, "_claude_title", lambda *_a: scans.append(1) or {})
    ids = ["ac247765-c122-4cca-ba05-%012d" % index for index in range(1100)]
    for index, session_id in enumerate(ids):
        client_context._local_title("claude-code", session_id, tmp_path)
        client_context._local_title("claude-code", ids[0], tmp_path)  # keep the first one hot
    assert len(scans) == 1100


# -- 18-14: hostile metadata cannot break describe --------------------------
@pytest.mark.parametrize("user_id", [
    pytest.param("[" * 200000, id="huge-brackets"), pytest.param("[" * 4000, id="deep-brackets"),
    pytest.param('{"a":' * 3000, id="deep-objects")])
def test_hostile_user_id_does_not_raise(tmp_path, user_id):
    info = client_context.describe({"metadata": {"user_id": user_id}}, {"user-agent": "claude-cli/2.0"},
                                   {"client_metadata_home": str(tmp_path)})
    assert info["client"] == "claude-code"
    assert "client_session_id" not in info


def test_oversized_user_id_json_is_ignored(tmp_path):
    user_id = json.dumps({"session_id": SESSION, "pad": "x" * 5000})
    info = client_context.describe({"metadata": {"user_id": user_id}}, {}, {"client_metadata_home": str(tmp_path)})
    assert "client_session_id" not in info


def test_non_string_identity_session_does_not_break_header_fallback(tmp_path):
    info = client_context.describe({"metadata": {"user_id": json.dumps({"session_id": 12345, "agent_id": 7})}},
                                   {"user-agent": "claude-cli/2.1", "X-Claude-Code-Session-Id": SESSION,
                                    "x-claude-code-agent-id": "agent_child"},
                                   {"client_metadata_home": str(tmp_path)})
    assert info["client_session_id"] == SESSION
    assert info["agent"] == "agent_child"


# -- 18-10: relay.log is UTF-8 under Windows PowerShell ---------------------
@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"), reason="needs Windows PowerShell")
def test_run_relay_writes_utf8_log_without_nul_bytes(tmp_path):
    fake = tmp_path / "fake_python.cmd"
    code = r"import sys; print('\u041f\u0440\u0438\u0432\u0435\u0442 relay'); print('\u041e\u0448\u0438\u0431\u043a\u0430', file=sys.stderr)"
    fake.write_text('@echo off\r\n"%s" -c "%s"\r\n' % (sys.executable, code), encoding="ascii")
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_relay.ps1"
    state, log = tmp_path / "state", tmp_path / "state" / "relay.log"
    proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                             "-Python", str(fake), "-StateDir", str(state)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if b"restart" in log.read_bytes().replace(b"\x00", b""):
                    break
            except OSError:
                pass
            time.sleep(0.25)
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        proc.wait(15)
    raw = log.read_bytes()
    assert b"\x00" not in raw, raw[:60].hex(" ")
    assert not raw.startswith((b"\xff\xfe", b"\xef\xbb\xbf"))
    text = raw.decode("utf-8")
    assert "Привет relay" in text and "Ошибка" in text and "restart" in text
