"""Stage 4 tests: Unified USD ledger, live provider balance checkers, /health & /v1/balance, and Client AccessKeys."""
from __future__ import annotations

import http.client
import json
import threading

import httpx
import pytest

import config as config_module
from drivers import balance_checkers
import journal
import proxy


def _sse_chat_ok(text: str, *, prompt_tokens: int = 1000, completion_tokens: int = 500, cost: float | None = None) -> bytes:
    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    if cost is not None:
        usage["cost"] = cost
    frames = [
        f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': text}}]})}\n\n",
        f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage})}\n\n",
        "data: [DONE]\n\n",
    ]
    return "".join(frames).encode("utf-8")


def test_balance_parsers_for_openrouter_deepseek_moonshot_and_siliconflow():
    or_bal = balance_checkers._parse_balance_response(
        "openrouter",
        "https://openrouter.ai/api/v1/key",
        {"data": {"usage": 12.5, "limit": 50.0, "limit_remaining": 37.5}},
    )
    assert or_bal["available"] == 37.5
    assert or_bal["monthly_allocation"] == 50.0
    assert or_bal["unit"] == "usd"

    ds_bal = balance_checkers._parse_balance_response(
        "deepseek",
        "https://api.deepseek.com/user/balance",
        {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "18.25"}]},
    )
    assert ds_bal["available"] == 18.25
    assert ds_bal["unit"] == "usd"

    ms_bal = balance_checkers._parse_balance_response(
        "moonshot",
        "https://api.moonshot.ai/v1/users/me/balance",
        {"data": {"available_balance": 9.75}},
    )
    assert ms_bal["available"] == 9.75

    sf_bal = balance_checkers._parse_balance_response(
        "siliconflow",
        "https://api.siliconflow.com/v1/user/info",
        {"data": {"totalBalance": "42.10"}},
    )
    assert sf_bal["available"] == 42.1


def test_usd_journal_ledger_health_balance_and_client_access_keys(monkeypatch, tmp_path):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    journal.JOURNAL.reset()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/key"):
            return httpx.Response(200, json={"data": {"usage": 5.0, "limit": 25.0, "limit_remaining": 20.0}})
        # Chat completion costing $0.05 USD
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse_chat_ok("USD_TURN_OK", prompt_tokens=2000, completion_tokens=1000, cost=0.05),
        )

    transport = httpx.MockTransport(mock_handler)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({
            "relay_api_key": "master-secret-key-stage4-0000000000",
            "conversation_state": False,
            "project_context": False,
            "exec_policy": "off",
            "providers": [
                {
                    "id": "openrouter",
                    "preset": "openrouter",
                    "api_key": "sk-or-v1-stage4-secret12345",
                    "models": ["anthropic/claude-sonnet-4.6", "openai/gpt-5.4"],
                }
            ],
            "client_keys": [
                {
                    "id": "cursor-dev",
                    "label": "Cursor Dev Key",
                    "key": "sk-proxi-cursor-subkey-123456",
                    "enabled": True,
                    "allowed_models": ["openrouter/anthropic/*"],
                    "rpm_limit": 2,
                    "max_budget_usd": 0.08,
                }
            ],
        }),
        encoding="utf-8",
    )

    cfg = config_module.load(str(cfg_path))
    # Client sub-key secret is registered for redaction too!
    assert "sk-proxi-cursor-subkey-123456" in cfg["cookie_values"]
    cfg["journal_path"] = str(tmp_path / "journal.jsonl")
    cfg["accounts"][0]["_http_transport"] = transport

    rt = proxy.Runtime(cfg)
    server = proxy.BoundedServer(("127.0.0.1", 0), proxy.make_handler(rt), threading.BoundedSemaphore(4))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        # 1. /health reports 200 OK for healthy universal provider pool
        conn.request("GET", "/health")
        h_resp = conn.getresponse()
        h_body = json.loads(h_resp.read().decode("utf-8"))
        assert h_resp.status == 200, h_body
        assert h_body["ok"] is True

        # 2. /v1/balance reports live OpenRouter USD balance ($20.00)
        conn.request("GET", "/v1/balance", headers={"Authorization": "Bearer master-secret-key-stage4-0000000000"})
        bal_resp = conn.getresponse()
        bal_body = json.loads(bal_resp.read().decode("utf-8"))
        assert bal_resp.status == 200, bal_body
        assert bal_body["usd"]["available"] == pytest.approx(20.0)

        # 3. Client sub-key sees ONLY its allowed_models in /v1/models
        conn.request("GET", "/v1/models", headers={"Authorization": "Bearer sk-proxi-cursor-subkey-123456"})
        m_body = json.loads(conn.getresponse().read().decode("utf-8"))
        assert [m["id"] for m in m_body["data"]] == ["openrouter/anthropic/claude-sonnet-4.6"]

        # 4. Client sub-key CANNOT access disallowed model (`openrouter/openai/gpt-5.4`) -> 403 forbidden
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "openrouter/openai/gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}),
            {"Authorization": "Bearer sk-proxi-cursor-subkey-123456", "Content-Type": "application/json"},
        )
        r_forbid = conn.getresponse()
        _ = r_forbid.read()
        assert r_forbid.status == 403

        # 5. Client sub-key CANNOT access /admin/api/overview -> 401 unauthorized
        conn.request("GET", "/admin/api/overview", headers={"Authorization": "Bearer sk-proxi-cursor-subkey-123456"})
        r_admin = conn.getresponse()
        _ = r_admin.read()
        assert r_admin.status == 401

        # 6. Turn 1 on allowed model succeeds and debits $0.05 USD both in Journal AND on the sub-key!
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "openrouter/anthropic/claude-sonnet-4.6", "messages": [{"role": "user", "content": "hi"}]}),
            {"Authorization": "Bearer sk-proxi-cursor-subkey-123456", "Content-Type": "application/json"},
        )
        r_ok1 = conn.getresponse()
        b_ok1 = json.loads(r_ok1.read().decode("utf-8"))
        assert r_ok1.status == 200, b_ok1
        assert b_ok1["choices"][0]["message"]["content"] == "USD_TURN_OK"

        # Verify journal metrics & turns track cost_usd with ZERO false `credits_unknown`!
        metrics = journal.JOURNAL.metrics(window=3600, bucket=60)
        assert metrics["totals"]["cost_usd"] == pytest.approx(0.05)
        assert metrics["totals"]["credits_unknown"] == 0
        assert metrics["totals"]["credits_complete"] is True
        turns = journal.JOURNAL.turns(limit=5)
        assert turns[0]["cost_usd"] == pytest.approx(0.05)
        assert turns[0]["credits_unknown"] == 0

        # 7. Turn 2 succeeds ($0.05 + $0.05 = $0.10 >= $0.08 budget), then Turn 3 is blocked by `max_budget_usd` (402)!
        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "openrouter/anthropic/claude-sonnet-4.6", "messages": [{"role": "user", "content": "hi 2"}]}),
            {"Authorization": "Bearer sk-proxi-cursor-subkey-123456", "Content-Type": "application/json"},
        )
        r_ok2 = conn.getresponse()
        _ = r_ok2.read()
        assert r_ok2.status == 200

        conn.request(
            "POST",
            "/v1/chat/completions",
            json.dumps({"model": "openrouter/anthropic/claude-sonnet-4.6", "messages": [{"role": "user", "content": "hi 3"}]}),
            {"Authorization": "Bearer sk-proxi-cursor-subkey-123456", "Content-Type": "application/json"},
        )
        r_budget = conn.getresponse()
        b_budget = json.loads(r_budget.read().decode("utf-8"))
        assert r_budget.status == 402, b_budget
        assert "budget" in b_budget["error"]["message"].lower()

        # Public key stats show exact usage without exposing raw secret!
        pub_keys = rt.client_keys.list_public()
        assert len(pub_keys) == 1
        assert pub_keys[0]["spent_usd"] == pytest.approx(0.10)
        assert "sk-proxi-cursor-subkey-123456" not in json.dumps(pub_keys)
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        rt.close()
