"""Tests for R5/D2 unified structured logging, D5 thinking heartbeat safety, Minara flash
suggestion candidates, and proxy adapter/future failure handling.
"""

import asyncio
import concurrent.futures
import io
import json
import os
import re
import shutil
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import adapters
import apierr
import client_context
import config as config_module
import console_log
import console_records
import direct
import journal
import models
import proxy
import validate
from conftest import KEY, FakeCredits, FakeRunner
from test_prompt_suggestion_chain import suggestion


PS_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_relay.ps1"
STAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}\] ")

PS_LOADER = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tree = [System.Management.Automation.Language.Parser]::ParseFile($env:RUN_RELAY_SCRIPT, [ref]$null, [ref]$null)
$isFunction = { $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }
foreach ($function in $tree.FindAll($isFunction, $false)) {
    . ([ScriptBlock]::Create($function.Extent.Text))
}
"""


@pytest.fixture(autouse=True)
def _clean_context():
    token = client_context.bind({})
    try:
        yield
    finally:
        client_context.reset(token)


@pytest.fixture
def isolated_journal(monkeypatch, tmp_path):
    monkeypatch.setattr(models, "_MINARA_MODELS", dict(models._MINARA_MODELS))
    store = journal.Journal()
    cfg = {
        **config_module.DEFAULTS,
        "cookie": "accessToken=secret-token-xyz",
        "cookie_values": ["secret-token-xyz"],
        "xsrf": "secret-xsrf-token",
        "relay_api_key": "sk-relay-secret-value",
        "workspace_root": str(tmp_path),
        "journal_path": str(tmp_path / "journal.jsonl"),
        "_extra_secrets": ["message", "multi\nline-secret"],
    }
    store.configure(cfg, cfg["journal_path"])
    monkeypatch.setattr(journal, "JOURNAL", store)
    yield store, tmp_path


def test_console_records_mode_and_identity_backfill(monkeypatch):
    monkeypatch.delenv("RELAY_LOG_FORMAT", raising=False)
    assert console_records.mode() == "text"
    assert not console_records.enabled()
    assert not console_records.structured_enabled()

    for val, expected in (
        ("jsonl", "jsonl"),
        (" JSONL ", "jsonl"),
        ("structured", "structured"),
        ("structured-text", "structured"),
        ("structured_text", "structured"),
        ("unknown", "text"),
    ):
        monkeypatch.setenv("RELAY_LOG_FORMAT", val)
        assert console_records.mode() == expected

    buf = io.StringIO()
    token = client_context.bind({
        "request_id": "req-42",
        "turn_id": "turn-01",
        "attempt": 2,
        "conversation": "conv-abc",
        "client": "claude-code",
        "route": "anthropic",
        "upstream": "retool",
        "account": "acc-1",
        "model": "claude-opus-5-5",
        "upstream_model": "claude-opus-4-5",
        "effective_effort": "high",
    })
    try:
        console_records.write(
            "upstream.ttft",
            "debug",
            {"ttft_ms": 180, "phase": "first_token"},
            when=1700000000.0,
            file=buf,
        )
        record = json.loads(buf.getvalue().strip())
        for key in ("ts", "kind", "level", "phase", "request_id", "turn_id", "attempt",
                    "conversation", "client", "route", "upstream", "account", "model",
                    "upstream_model", "effective_effort"):
            assert key in record, f"missing {key} in {record}"
        assert record["kind"] == "upstream.ttft"
        assert record["phase"] == "first_token"
        assert record["ttft_ms"] == 180
        assert record["attempt"] == 2

        rot_buf = io.StringIO()
        console_records.write(
            "journal.rotation_failed",
            "warn",
            {"reason": "PermissionError"},
            when=1700000000.0,
            file=rot_buf,
        )
        rot_rec = json.loads(rot_buf.getvalue().strip())
        assert rot_rec["kind"] == "journal.rotation_failed"
        assert rot_rec["phase"] == "journal.rotation_failed"
        assert "request_id" not in rot_rec and "conversation" not in rot_rec
    finally:
        client_context.reset(token)


def test_parse_line_classifies_cache_balance_route_body_ttft_history_and_http():
    kind, level, fields = console_records.parse_line(
        "[req-1] [cache] in=1,200 read=60,000 write=52,000 credits=0.0500 out=320 "
        "channel=ws path=/messages provider=anthropic [keepwarm] [WRITE HIGH]"
    )
    assert kind == "cache.summary" and level == "warn"
    assert fields["request_id"] == "req-1"
    assert fields["input_tokens"] == 1200 and fields["cache_read"] == 60000
    assert fields["cache_write"] == 52000 and fields["output_tokens"] == 320
    assert fields["credits"] == 0.05 and fields["channel"] == "ws"
    assert fields["path"] == "/messages" and fields["provider"] == "anthropic"
    assert fields["keepwarm"] is True and fields["alerts"] == ["WRITE HIGH"]

    k_tail, l_tail, f_tail = console_records.parse_line(
        "[req-1] [cache.tail_unwritten] Minara ничего не записала в кэш: 4,200 токенов промпта оплачены по цене входа (0.1250 кредитов Minara)"
    )
    assert k_tail == "cache.tail_unwritten" and l_tail == "warn"
    assert f_tail["input_tokens"] == 4200 and f_tail["minara_credits"] == 0.125

    k_bal, l_bal, f_bal = console_records.parse_line(
        "[req-2] [balance] account=minara-1 balance=-1.5000 limit=100.0000 spent=2.2500 [UNREPORTED USAGE]"
    )
    assert k_bal == "account.balance" and l_bal == "warn"
    assert f_bal["account"] == "minara-1" and f_bal["available"] == -1.5
    assert f_bal["limit"] == 100.0 and f_bal["spent"] == 2.25 and f_bal["state"] == "UNREPORTED USAGE"

    k_rte, l_rte, f_rte = console_records.parse_line(
        "[req-3] [route] provider=Minara account=m1 model=minara/deepseek-v4-flash upstream_model=deepseek-v4-flash effort=none kind=subagent"
    )
    assert k_rte == "route.bound" and l_rte == "info"
    assert f_rte["provider"] == "Minara" and f_rte["child"] is True and f_rte["effort"] == "none"

    k_body, l_body, f_body = console_records.parse_line('[req-4] [body] {"model":"claude-opus-5-5","tools_count":3}')
    assert k_body == "upstream.body" and l_body == "debug"
    assert f_body["request_id"] == "req-4" and f_body["model"] == "claude-opus-5-5"
    assert f_body["body"] == {"model": "claude-opus-5-5", "tools_count": 3}

    k_ttft, l_ttft, f_ttft = console_records.parse_line("[req-5] [ttft] 0.42s")
    assert k_ttft == "upstream.ttft" and l_ttft == "debug"
    assert f_ttft == {"request_id": "req-5", "ttft_ms": 420}

    k_hist, l_hist, f_hist = console_records.parse_line(
        "[req-6] [history] forked native branch thinking=2 tool_calls=1"
    )
    assert k_hist == "native_history.continuation" and l_hist == "info"
    assert f_hist == {"request_id": "req-6", "mode": "forked", "thinking": 2, "tool_calls": 1}

    k_exc, l_exc, f_exc = console_records.parse_line("relay req-7 EXC ValueError: bad")
    assert k_exc == "http.exception" and l_exc == "error"
    assert f_exc == {"request_id": "req-7", "exception_type": "ValueError"}

    k_http, l_http, f_http = console_records.parse_line("relay req-8 POST /v1/messages 502 claude-opus-5-5 12ms")
    assert k_http == "http.request" and l_http == "error" and f_http["status"] == 502

    k_exp, l_exp, f_exp = console_records.parse_line("relay req-9 POST /v1/messages 400 claude-opus-5-5 4ms [expected]")
    assert k_exp == "http.request" and l_exp == "info" and f_exp["status"] == 400


def test_console_log_jsonl_structured_and_default_text_modes(monkeypatch, isolated_journal):
    token = client_context.bind({
        "request_id": "req-log-1",
        "turn_id": "t12345678",
        "attempt": 1,
        "conversation": "conv-log",
        "client": "claude-code",
        "route": "anthropic",
        "account": "acc-log",
        "model": "claude-opus-5-5",
    })
    try:
        # 1. JSONL mode: upgrades diagnostics, redacts secrets (even inside dict & when key matches secret!), drops text_only
        monkeypatch.setenv("RELAY_LOG_FORMAT", "jsonl")
        jsonl_buf = io.StringIO()
        console_log.write(
            "[req-log-1] [cache] in=100 read=500 write=50 credits=0.0100 out=25",
            file=jsonl_buf,
        )
        console_log.emit_body(
            "req-log-1",
            {"model": "claude-opus-5-5", "token": "secret-token-xyz", "multi": "multi\nline-secret"},
            model="claude-opus-5-5",
            account="acc-log",
            file=jsonl_buf,
        )
        console_log.write("human-only line", file=jsonl_buf, text_only=True)
        lines = [json.loads(item) for item in jsonl_buf.getvalue().splitlines()]
        assert len(lines) == 2  # text_only dropped in JSONL
        cache_rec, body_rec = lines
        assert cache_rec["kind"] == "cache.summary" and cache_rec["phase"] == "cache.summary"
        assert cache_rec["input_tokens"] == 100 and cache_rec["cache_read"] == 500
        assert cache_rec["turn_id"] == "t12345678" and cache_rec["conversation"] == "conv-log"
        assert "message" in cache_rec  # key name preserved even though 'message' is in _extra_secrets
        assert body_rec["kind"] == "upstream.body"
        serialized_body = json.dumps(body_rec)
        assert "secret-token-xyz" not in serialized_body and "line-secret" not in serialized_body

        # 2. Structured text mode: prints timestamp + req/conv/client/model/account/phase + metrics + message, keeps text_only
        monkeypatch.setenv("RELAY_LOG_FORMAT", "structured")
        struct_buf = io.StringIO()
        console_log.emit_ttft("req-log-1", 0.35, model="claude-opus-5-5", account="acc-log", file=struct_buf)
        console_log.write(
            "[req-log-1] [cache] in=100 read=500 write=50 credits=0.0100 out=25",
            file=struct_buf,
        )
        console_log.write("only-in-text-and-structured", file=struct_buf, text_only=True)
        struct_lines = struct_buf.getvalue().splitlines()
        assert len(struct_lines) == 3
        for line in struct_lines:
            assert STAMP_RE.match(line), f"missing timestamp on {line!r}"
            assert "req=req-log-1" in line and "conv=conv-log" in line and "account=acc-log" in line
        assert "phase=upstream.ttft" in struct_lines[0] and "ttft=350ms" in struct_lines[0]
        assert "tokens=in:100,read:500,write:50,out:25" in struct_lines[1] and "cost=0.0100" in struct_lines[1]
        assert "only-in-text-and-structured" in struct_lines[2]

        # Color rendering preserves structured prefix cleanly
        ansi_rendered = console_log.render(struct_lines[0], color=True)
        assert "\x1b[" in ansi_rendered and "phase=upstream.ttft" in re.sub(r"\x1b\[[0-9;]*m", "", ansi_rendered)

        # 3. Default text mode preserves plain stamped line without structured prefix
        monkeypatch.delenv("RELAY_LOG_FORMAT", raising=False)
        text_buf = io.StringIO()
        console_log.write("[req-log-1] plain diagnostic", file=text_buf)
        plain_line = text_buf.getvalue().strip()
        assert STAMP_RE.match(plain_line)
        assert "phase=" not in plain_line and plain_line.endswith("[req-log-1] plain diagnostic")
    finally:
        client_context.reset(token)


def test_helper_fields_builders():
    monitor = console_log.CacheMonitor()
    line, fields, level = monitor.fields(
        "r1",
        {"inputTokens": 200, "promptTokensReadFromCache": 1000, "promptTokensWrittenToCache": 100,
         "outputTokens": 50, "creditsUsed": 0.025},
        scope="s1", channel="sse", path="/v1/messages", provider="openrouter",
    )
    assert level == "info" and "[r1] [cache]" in line
    assert fields["input_tokens"] == 200 and fields["cache_read"] == 1000
    assert fields["cache_write"] == 100 and fields["output_tokens"] == 50
    assert fields["channel"] == "sse" and fields["provider"] == "openrouter"

    b_text, b_fields, b_level = console_log.balance_fields(
        "r1", {"available": 12.5, "monthly_allocation": 100.0}, spent=0.5, account="main", upstream="retool",
    )
    assert b_level == "info" and "balance=12.5000" in b_text
    assert b_fields["available"] == 12.5 and b_fields["unit"] == "retool_credits" and b_fields["state"] == "CACHED"

    r_text, r_fields, r_level = console_log.route_fields(
        "r1", upstream="minara", account="m1", model="minara/glm-5.3-flash",
        upstream_model="glm-5.3-flash", effort="minimal", child=True,
    )
    assert r_level == "info" and "kind=subagent" in r_text
    assert r_fields["provider"] == "Minara" and r_fields["child"] is True


def test_direct_drain_emits_ttft_in_jsonl_and_structured_without_direct_debug(monkeypatch, capsys, isolated_journal):
    async def run_drain():
        queue = asyncio.Queue()
        await queue.put(json.dumps({"event": "content_block_delta", "type": "text", "partialText": "hello"}))
        await queue.put(json.dumps({
            "event": "metadata",
            "usage": {"inputTokens": 10, "promptTokensReadFromCache": 20, "promptTokensWrittenToCache": 0,
                      "outputTokens": 5, "creditsUsed": 0.001},
        }))
        await queue.put(json.dumps({"event": "end_of_stream"}))
        session = types.SimpleNamespace(
            _cancelled=False,
            _request_tag="req-direct-1",
            backend=types.SimpleNamespace(cfg={"account_id": "acc-direct", "label": "acc-direct"}),
        )
        events = []
        async for ev in direct._drain({}, session, queue, [], [], flags={"model": "claude-opus-5-5"}):
            events.append(ev)
        return events

    monkeypatch.setenv("RELAY_LOG_FORMAT", "jsonl")
    capsys.readouterr()
    events = asyncio.run(run_drain())
    assert any(ev.get("type") == "text" and ev.get("delta") == "hello" for ev in events)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    ttft_recs = [rec for rec in records if rec.get("kind") == "upstream.ttft"]
    assert len(ttft_recs) == 1
    assert ttft_recs[0]["request_id"] == "req-direct-1"
    assert ttft_recs[0]["model"] == "claude-opus-5-5"
    assert ttft_recs[0]["account"] == journal.tag("acc-direct")
    assert isinstance(ttft_recs[0]["ttft_ms"], int) and ttft_recs[0]["ttft_ms"] >= 0
    assert any(rec.get("kind") == "cache.summary" and rec.get("output_tokens") == 5 for rec in records)

    monkeypatch.setenv("RELAY_LOG_FORMAT", "structured")
    capsys.readouterr()
    asyncio.run(run_drain())
    out = capsys.readouterr().out
    assert "phase=upstream.ttft" in out and "ttft=" in out and "model=claude-opus-5-5" in out
    assert "phase=cache.summary" in out and "tokens=in:10,read:20,write:0,out:5" in out


def test_direct_thinking_heartbeat_jsonl_and_no_client_synthetic_thinking(monkeypatch, capsys, isolated_journal):
    async def run_heartbeat():
        queue = asyncio.Queue()
        await queue.put(json.dumps({"event": "content_block_start", "type": "thinking"}))
        await queue.put(json.dumps({"event": "content_block_delta", "type": "text", "partialText": "done"}))
        await queue.put(json.dumps({"event": "end_of_stream"}))

        real_wait_for = asyncio.wait_for
        calls = {"n": 0}

        async def fake_wait_for(coro, timeout=None):
            calls["n"] += 1
            if calls["n"] == 2:
                coro.close()
                raise asyncio.TimeoutError()
            return await real_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        session = types.SimpleNamespace(
            _cancelled=False,
            _request_tag="req-hb-1",
            backend=types.SimpleNamespace(cfg={"label": "acc-hb"}),
        )
        out = []
        async for ev in direct._drain({}, session, queue, [], [], flags={"model": "claude-opus-5-5"}):
            out.append(ev)
        return out

    monkeypatch.setenv("RELAY_LOG_FORMAT", "jsonl")
    capsys.readouterr()
    events = asyncio.run(run_heartbeat())
    hb_events = [ev for ev in events if ev.get("type") == "thinking_heartbeat"]
    assert len(hb_events) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    hb_recs = [rec for rec in records if rec.get("kind") == "upstream.thinking_heartbeat"]
    assert len(hb_recs) == 1 and hb_recs[0]["request_id"] == "req-hb-1" and "elapsed_ms" in hb_recs[0]

    # All client adapters must ignore thinking_heartbeat without leaking synthetic thinking text
    cfg = {**config_module.DEFAULTS, "cookie": "c=1"}
    anthropic_parsed = validate.parse_anthropic(
        {"model": "claude-opus-5-5", "max_tokens": 128, "messages": [{"role": "user", "content": "hi"}]}, cfg
    )
    chat_parsed = validate.parse_chat(
        {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}, cfg
    )
    resp_parsed = validate.parse_responses(
        {"model": "claude-opus-5-5", "input": "hi"}, cfg
    )
    for dialect, parsed_req in (("anthropic", anthropic_parsed), ("chat", chat_parsed), ("responses", resp_parsed)):
        adapter = adapters.for_dialect(dialect, parsed_req)
        adapter.feed({"type": "thinking_heartbeat", "elapsed": 5.0})
        adapter.feed({"type": "text", "delta": "real reply"})
        payload = adapter.end("stop", "hi")
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "Waiting for upstream reasoning output" not in serialized
        assert "Модель думает" not in serialized and "real reply" in serialized


def test_direct_stream_body_emits_structured_body(monkeypatch, capsys, isolated_journal):
    async def run_stream_body():
        class FakeResp:
            def __iter__(self):
                return iter([
                    json.dumps({"event": "content_block_delta", "type": "text", "partialText": "ok"}).encode(),
                    json.dumps({"event": "end_of_stream"}).encode(),
                ])

            def close(self):
                pass

        monkeypatch.setattr(direct.curl_pool, "retool_open", lambda *a, **kw: FakeResp())
        session = types.SimpleNamespace(
            _cancelled=False,
            _request_tag="req-body-1",
            _abort_reason=None,
            backend=types.SimpleNamespace(
                cfg={"base": "https://example.invalid", "account_id": "acc-body", "label": "acc-body"},
                headers=lambda: {"cookie": "secret-token-xyz"},
            ),
        )
        cfg = {**config_module.DEFAULTS, "direct_debug": True}
        body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "secret-token-xyz"}],
                "tools": [{"name": "Read"}], "reasoningEffort": "low"}
        return [ev async for ev in direct._stream_body(cfg, session, body)]

    monkeypatch.setenv("RELAY_LOG_FORMAT", "jsonl")
    capsys.readouterr()
    events = asyncio.run(run_stream_body())
    assert any(ev.get("type") == "text" for ev in events)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    body_recs = [rec for rec in records if rec.get("kind") == "upstream.body"]
    assert len(body_recs) == 1
    rec = body_recs[0]
    assert rec["model"] == "claude-opus-5-5" and rec["account"] == journal.tag("acc-body")
    assert rec["body"]["tools_count"] == 1 and "messages" not in rec["body"]


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"), reason="needs Windows PowerShell")
def test_supervisor_script_supports_jsonl_and_structured_formats(tmp_path):
    def run_ps(scenario, log_format):
        harness = tmp_path / f"harness_{log_format}.ps1"
        harness.write_text(PS_LOADER + scenario, encoding="utf-8-sig")
        environ = {k: v for k, v in os.environ.items() if k != "RELAY_LOG_FORMAT"}
        environ["RUN_RELAY_SCRIPT"] = str(PS_SCRIPT)
        environ["RELAY_LOG_FORMAT"] = log_format
        done = subprocess_run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(harness),
        ], env=environ)
        return [line for line in done.splitlines() if line.strip()]

    relay_valid = json.dumps({"ts": "2026-11-24T12:00:00.000+03:00", "kind": "upstream.ttft",
                              "phase": "upstream.ttft", "level": "debug", "ttft_ms": 120})
    jsonl_lines = run_ps(f"""
Write-Output (Format-RelayLogLine 'supervisor ready' -Kind 'supervisor.started' -Level 'info' -Fields @{{port=8787; attempts=1}})
Write-Output (Format-RelayLogLine '{relay_valid}' -FromRelay)
Write-Output (Format-RelayLogLine 'unstructured python traceback accessToken=abc' -FromRelay)
""", "jsonl")
    sup_rec, passthrough_rec, bad_rec = [json.loads(line) for line in jsonl_lines]
    assert sup_rec["kind"] == "supervisor.started" and sup_rec["phase"] == "supervisor.started"
    assert sup_rec["port"] == 8787 and sup_rec["attempts"] == 1
    assert passthrough_rec["kind"] == "upstream.ttft" and passthrough_rec["ttft_ms"] == 120
    assert bad_rec["kind"] == "supervisor.unstructured_output" and "accessToken" not in json.dumps(bad_rec)

    struct_lines = run_ps("""
Write-Output (Format-RelayLogLine 'supervisor ready' -Kind 'supervisor.started' -Level 'info' -Fields @{port=8787})
Write-Output (Format-RelayLogLine '[2026-11-24 12:00:00.000 +03:00] phase=http.request | relay r1 GET / 200' -FromRelay)
""", "structured")
    assert STAMP_RE.match(struct_lines[0]) and "phase=supervisor.started" in struct_lines[0]
    assert "port=8787" in struct_lines[0] and "| supervisor ready" in struct_lines[0]
    assert struct_lines[1].startswith("[2026-11-24 12:00:00.000 +03:00] phase=http.request")


def subprocess_run(cmd, *, env):
    import subprocess
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_suggestion_models_include_minara_flash_candidates_after_retool_haiku(tmp_path, monkeypatch):
    candidates = proxy.make_handler(proxy.Runtime({
        **config_module.DEFAULTS,
        "_empty_pool": True,
        "relay_api_key": KEY,
        "workspace_root": str(tmp_path),
        "journal_path": str(tmp_path / "j0.jsonl"),
        "prompts_file": str(tmp_path / "absent.json"),
    })).SUGGESTION_MODELS
    assert candidates[0] == "claude-haiku-4-5-20251001"
    assert "minara/deepseek-v4-flash" in candidates
    assert "minara/glm-5.3-flash" in candidates
    assert candidates.index("minara/deepseek-v4-flash") < candidates.index("minara/claude-haiku-4-5")
    assert candidates.index("minara/glm-5.3-flash") < candidates.index("minara/claude-haiku-4-5")

    # In a Minara-only pool with auto prompt suggestions, fast Minara flash candidates are selected cleanly
    monkeypatch.setattr(models, "_MINARA_MODELS", dict(models._MINARA_MODELS))
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RELAY_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "minara_only.json"
    path.write_text(json.dumps({
        "accounts": [{
            "account_id": "m-flash", "label": "Minara Flash", "upstream": "minara",
            "bearer_token": "synthetic-bearer-flash",
            "minara_models": [
                {"id": "deepseek/deepseek-v4-flash"},
                {"id": "z-ai/glm-5.3-flash"},
                {"id": "openai/gpt-5.4"},
            ],
        }],
        "model": "minara/openai/gpt-5.4",
        "relay_api_key": KEY,
        "workspace_root": str(tmp_path),
        "project_context": False,
        "exec_policy": "off",
        "conversation_state": False,
        "cache_keepwarm_seconds": 0,
        "session_refresh_interval": 0,
        "account_balance_interval": 0,
        "minara_channel_retry_delays": [],
        "journal_path": str(tmp_path / "journal_minara.jsonl"),
        "prompts_file": str(tmp_path / "absent.json"),
        "prompt_suggestion_model": "auto",
    }), encoding="utf-8")
    runtime = proxy.Runtime(config_module.load(str(path)))
    runtime.runner = FakeRunner()
    runtime.credits = FakeCredits()
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def post_suggest():
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/v1/messages",
                data=json.dumps(suggestion(model="minara/openai/gpt-5.4")).encode(),
                method="POST",
            )
            req.add_header("content-type", "application/json")
            req.add_header("authorization", "Bearer " + KEY)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status

        assert post_suggest() == 200
        first = runtime.runner.seen[-1]
        assert first.model == "minara/deepseek-v4-flash" and first.effort == "none"

        real_unavail = runtime.pool.unavailable_error

        def skip_deepseek(entry, **kwargs):
            if entry.get("id") == "minara/deepseek-v4-flash":
                return apierr.accounts_rate_limited(60, 1, 1)
            return real_unavail(entry, **kwargs)

        monkeypatch.setattr(runtime.pool, "unavailable_error", skip_deepseek)
        assert post_suggest() == 200
        second = runtime.runner.seen[-1]
        assert second.model == "minara/glm-5.3-flash" and second.effort == "minimal"
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()


def _start_test_relay(tmp_path, monkeypatch, runner):
    cfg = dict(config_module.DEFAULTS)
    cfg.update({
        "base": "https://example.invalid",
        "page_uuid": "page-1",
        "thread_id": "thread-1",
        "ua": "test-agent",
        "cookie": "accessToken=abc123456789xyz; xsrfToken=q",
        "xsrf": "q",
        "relay_api_key": KEY,
        "workspace_root": str(tmp_path),
        "journal_path": str(tmp_path / "journal.jsonl"),
        "prompts_file": str(tmp_path / "absent.json"),
        "host": "127.0.0.1",
        "port": 0,
        "cookie_values": ["abc123456789xyz"],
        "exec_policy": "off",
        "project_context": False,
        "cache_keepwarm_seconds": 0,
        "session_refresh_interval": 0,
        "account_balance_interval": 0,
    })
    store = journal.Journal()
    store.configure(cfg, cfg["journal_path"])
    monkeypatch.setattr(journal, "JOURNAL", store)
    runtime = proxy.Runtime(cfg)
    runtime.credits = FakeCredits()
    runtime.runner = runner
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(runtime), threading.BoundedSemaphore(4))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return runtime, server, store


def test_adapter_feed_api_error_cancels_turn_without_marking_client_disconnected(tmp_path, monkeypatch, capsys):
    cancelled = []

    class InvalidStructuredOutputRunner:
        async def run(self, parsed, out, holder):
            holder["cancel_event"]
            holder["session"] = types.SimpleNamespace(cancel=lambda: cancelled.append(True))
            try:
                # Tool call under strict json_schema causes StructuredOutputAdapter.feed() to raise 502 upstream_invalid_output
                out.put({"type": "tool_call", "id": "c1", "name": "UnexpectedTool", "arguments": "{}", "done": True})
                await asyncio.sleep(0.05)
            finally:
                out.put(None)

    monkeypatch.setenv("RELAY_LOG_FORMAT", "jsonl")
    runtime, server, store = _start_test_relay(tmp_path, monkeypatch, InvalidStructuredOutputRunner())
    capsys.readouterr()
    schema_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "reply",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        },
    }
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
        # Non-streaming (_collect)
        req1 = urllib.request.Request(
            base_url,
            data=json.dumps({
                "model": "claude-opus-5-5",
                "response_format": schema_format,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }).encode(),
            method="POST",
        )
        req1.add_header("content-type", "application/json")
        req1.add_header("authorization", "Bearer " + KEY)
        with pytest.raises(urllib.error.HTTPError) as err1:
            urllib.request.urlopen(req1, timeout=10)
        assert err1.value.code == 502

        # Streaming (_pump)
        req2 = urllib.request.Request(
            base_url,
            data=json.dumps({
                "model": "claude-opus-5-5",
                "response_format": schema_format,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }).encode(),
            method="POST",
        )
        req2.add_header("content-type", "application/json")
        req2.add_header("authorization", "Bearer " + KEY)
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            sse_body = resp2.read().decode()
        assert "upstream_invalid_output" in sse_body

        assert len(cancelled) == 2
        con_records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        http_recs = [r for r in con_records if r.get("kind") == "http.request"]
        assert len(http_recs) == 2
        for rec in http_recs:
            assert rec["status"] == 502
            assert rec["level"] == "error"
            assert not rec.get("disconnected")
            assert rec.get("error") == "upstream_invalid_output"

        j_http = [ev for ev in store.query(limit=50) if ev.get("kind") == "http.request"]
        assert len(j_http) == 2
        for ev in j_http:
            assert ev["data"]["status"] == 502 and ev["level"] == "error"
            assert not ev["data"].get("disconnected")
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()


@pytest.mark.parametrize("put_sentinel", [False, True])
def test_unhandled_runner_exception_surfaces_error_instead_of_empty_200(tmp_path, monkeypatch, put_sentinel):
    runtime, server, _ = _start_test_relay(tmp_path, monkeypatch, FakeRunner())

    def exploding_submit_turn(parsed, events, holder):
        fut = concurrent.futures.Future()
        if put_sentinel:
            events.put(None)
        fut.set_exception(RuntimeError("runner crashed before _run_once"))
        return fut

    monkeypatch.setattr(runtime, "submit_turn", exploding_submit_turn)
    try:
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1/messages"
        # Non-stream must return 500 error envelope (NOT empty HTTP 200)
        req1 = urllib.request.Request(
            base_url,
            data=json.dumps({
                "model": "claude-opus-5-5",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }).encode(),
            method="POST",
        )
        req1.add_header("content-type", "application/json")
        req1.add_header("authorization", "Bearer " + KEY)
        with pytest.raises(urllib.error.HTTPError) as err1:
            urllib.request.urlopen(req1, timeout=10)
        assert err1.value.code == 500
        err_payload = json.loads(err1.value.read().decode())
        assert err_payload["error"]["type"] == "api_error"
        assert "runner crashed before _run_once" in err_payload["error"]["message"]

        # Stream must emit error event in SSE body (NOT silent stop with empty text)
        req2 = urllib.request.Request(
            base_url,
            data=json.dumps({
                "model": "claude-opus-5-5",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            }).encode(),
            method="POST",
        )
        req2.add_header("content-type", "application/json")
        req2.add_header("authorization", "Bearer " + KEY)
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            sse_body = resp2.read().decode()
        assert "event: error" in sse_body and "runner crashed before _run_once" in sse_body
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
