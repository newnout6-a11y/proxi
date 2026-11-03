import asyncio
import http.client
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

import pytest
import client_context
import journal
import proxy


SESSION = "ac247765-c122-4cca-ba05-ef006343ee93"


def test_unicode_header_is_bounded_and_cannot_inject_log_lines():
    info = client_context.describe({}, {"X-Proxi-Client": "opencode", "X-Proxi-Chat-Title":
                                      quote("Русский чат\n\x1b[31m\u202e" + "x" * 300)})
    assert info["chat_title"].startswith("Русский чат ")
    assert len(info["chat_title"]) == 240
    assert all(c not in info["chat_title"] for c in "\n\r\x1b\u202e")


def test_claude_real_custom_title_and_legacy_identity(tmp_path):
    transcript = tmp_path / ".claude/projects/project" / (SESSION + ".jsonl")
    transcript.parent.mkdir(parents=True)
    rows = [{"type": "user", "message": {"content": "old prompt"}},
            {"type": "custom-title", "customTitle": "Починка 400"}]
    transcript.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    for user_id in (json.dumps({"session_id": SESSION}), "user_abc_account_def_session_" + SESSION):
        info = client_context.describe({"metadata": {"user_id": user_id}}, {}, {"client_metadata_home": str(tmp_path)})
        assert info["client"] == "claude-code"
        assert info["client_session_id"] == SESSION
        assert info["chat_title"] == "Починка 400"
        assert info["title_source"] == "claude-custom-title"


@pytest.mark.parametrize("message", ["unexpected-shape", ["unexpected-shape"], 123])
def test_malformed_local_message_metadata_does_not_reject_model_turn(relay, tmp_path, monkeypatch, message):
    transcript = tmp_path / ".claude/projects/project" / (SESSION + ".jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "message": message}) + "\n" +
                          json.dumps({"type": "custom-title", "customTitle": "Рабочий чат"}), encoding="utf-8")
    monkeypatch.setattr(client_context.Path, "home", classmethod(lambda cls: tmp_path))
    payload = {"model": "claude-opus-5-5", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}],
               "metadata": {"user_id": json.dumps({"session_id": SESSION})}}
    status, _ = relay.json_any("/v1/messages", payload)
    assert status == 200
    assert len(relay.fake.seen) == 1
    assert client_context.describe(payload, {})["chat_title"] == "Рабочий чат"


def test_desktop_code_session_name_matches_cli_session(tmp_path):
    path = tmp_path / "AppData/Roaming/Claude/claude-code-sessions/account/org/local_1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cliSessionId": SESSION, "title": "Мой Desktop"}), encoding="utf-8")
    info = client_context.describe({"metadata": {"user_id": json.dumps({"session_id": SESSION})}},
                                   {"x-proxi-client": "claude-code"}, {"client_metadata_home": str(tmp_path)})
    assert info["chat_title"] == "Мой Desktop"
    assert info["client"] == "claude-desktop"


@pytest.mark.parametrize("table", ["session", "session_v2"])
def test_opencode_read_only_database_and_parent(tmp_path, table):
    db = tmp_path / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE %s (id TEXT, title TEXT, parent_id TEXT)" % table)
        conn.execute("INSERT INTO %s VALUES (?,?,?)" % table, ("ses_child", "Исследование", "ses_parent"))
    before = db.read_bytes()
    info = client_context.describe({}, {"x-proxi-client": "opencode", "x-proxi-session-id": "ses_child"},
                                   {"client_metadata_home": str(tmp_path)})
    assert info["chat_title"] == "Исследование"
    assert info["parent_session_id"] == "ses_parent"
    assert db.read_bytes() == before
    missing = tmp_path / "another"
    assert "chat_title" not in client_context.describe({},
        {"x-proxi-client": "opencode", "x-proxi-session-id": "none"}, {"client_metadata_home": str(missing)})
    assert not missing.exists()


def test_unknown_client_does_not_read_local_metadata(monkeypatch):
    def fail(*args):
        raise AssertionError("unrelated client metadata was accessed")
    monkeypatch.setattr(client_context, "_local_title", fail)
    assert client_context.describe({}, {"x-proxi-client": "codex", "x-proxi-session-id": SESSION})["client"] == "codex"


def test_opencode_v2_child_identity_is_distinct_from_parent_affinity(tmp_path):
    info = client_context.describe({}, {"user-agent": "opencode/latest/2.2.5/cli",
        "x-session-id": "ses_parent", "x-session-affinity": "ses_parent", "x-opencode-session": "ses_parent",
        "x-opencode-session-id": "ses_child", "x-opencode-parent-session-id": "ses_parent"},
        {"client_metadata_home": str(tmp_path)})
    assert info["client"] == "opencode"
    assert info["client_session_id"] == "ses_child"
    assert info["parent_session_id"] == "ses_parent"


def test_claude_subagent_headers_work_without_body_metadata(tmp_path):
    info = client_context.describe({}, {"user-agent": "claude-cli/2.1.310", "X-Claude-Code-Session-Id": SESSION,
                                       "x-claude-code-agent-id": "agent_child"}, {"client_metadata_home": str(tmp_path)})
    assert info["client_session_id"] == SESSION
    assert info["agent"] == "agent_child"


def test_labels_are_scoped_across_async_requests_and_redacted():
    instance = journal.Journal()
    instance.configure({"relay_api_key": "secret123456"})

    async def turn(title):
        token = client_context.bind({"client": "opencode", "chat_title": title, "request_id": title})
        try:
            await asyncio.sleep(0)
            event = instance.emit("turn.begin", "hello")
            assert event["data"]["chat_title"] == ("[redacted]" if title == "secret123456" else title)
            return event
        finally:
            client_context.reset(token)

    async def run():
        return await asyncio.gather(turn("Первый"), turn("secret123456"))
    events = asyncio.run(run())
    assert "Первый" in events[0]["message"]
    assert "secret123456" not in json.dumps(events, ensure_ascii=False)
    assert client_context.current() == {}


def test_invalid_effort_has_chat_title_and_correlated_error(relay):
    payload = {"model": "claude-opus-5-5", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}],
               "output_config": {"effort": "xhigh"}}
    before = journal.JOURNAL.last_seq
    conn = http.client.HTTPConnection(relay.url.removeprefix("http://"))
    headers = {"authorization": "Bearer " + relay.key, "content-type": "application/json",
               "x-proxi-client": "claude-code", "x-proxi-chat-title": quote("Сломанный чат")}
    conn.request("POST", "/v1/messages", json.dumps(payload), headers)
    response = conn.getresponse()
    body = json.loads(response.read())
    assert response.status == 400
    assert response.getheader("request-id") == response.getheader("x-request-id") == body["request_id"]
    # Reuse this socket, which reuses the HTTP handler, without any client labels.
    conn.request("GET", "/health")
    response = conn.getresponse()
    response.read()
    conn.close()
    deadline = time.monotonic() + 2
    while True:
        events = journal.JOURNAL.query(since=before)
        if any(event["kind"] == "http.request" and event["data"]["path"] == "/health" for event in events):
            break
        assert time.monotonic() < deadline, "HTTP completion was not journaled"
        time.sleep(0.01)
    error = next(event for event in events if event["kind"] == "http.error")
    assert error["data"]["chat_title"] == "Сломанный чат"
    assert error["data"]["effort"] == "xhigh"
    assert error["data"]["stage"] == "relay"
    assert error["request_id"] == body["request_id"]
    health = next(event for event in events if event["kind"] == "http.request" and event["data"]["path"] == "/health")
    assert "chat_title" not in health["data"]


@pytest.mark.parametrize("field", ["model", "effort"])
def test_invalid_model_and_effort_are_redacted_in_console(relay, capsys, field):
    payload = {"model": "claude-opus-5-5", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]}
    if field == "model":
        payload["model"] = relay.key
    else:
        payload["output_config"] = {"effort": relay.key}
    before = journal.JOURNAL.last_seq
    status, body = relay.json_any("/v1/messages", payload)
    assert status in (400, 404)
    deadline = time.monotonic() + 2
    while not any(row["kind"] == "http.request" and row.get("request_id") == body["request_id"]
                  for row in journal.JOURNAL.query(since=before)):
        assert time.monotonic() < deadline, "HTTP completion was not journaled"
        time.sleep(0.01)
    output = capsys.readouterr().out
    assert "relay " in output and "[redacted]" in output
    assert relay.key not in output


def test_startup_config_uses_existing_credentials_and_separate_state(tmp_path, monkeypatch):
    config_file = tmp_path / "external.json"
    seen = []
    monkeypatch.setattr(proxy.config_module, "load", lambda path: seen.append(path) or
                        {"secret": "preserved", "journal_path": "old-path"})
    cfg = proxy.startup_config(["--config", str(config_file), "--state-dir", str(tmp_path / "state")])
    assert seen == [str(config_file)]
    assert cfg["secret"] == "preserved"
    assert Path(cfg["journal_path"]) == tmp_path / "state/journal.jsonl"
    assert Path(cfg["upstream_error_dir"]) == tmp_path / "state/logs/upstream-400"


def test_external_config_uses_its_own_existing_relay_key(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    old_key = tmp_path / "source-key.txt"
    old_key.write_text("other-checkout-key", encoding="utf-8")
    monkeypatch.setattr(proxy.config_module, "KEY_PATH", str(old_key))
    external = tmp_path / "external"
    external.mkdir()
    (external / "relay_key.txt").write_text("original-client-key\n", encoding="utf-8")
    cfg = {"_config_path": str(external / "config.json")}
    assert proxy.config_module._ensure_relay_key(cfg) == "original-client-key"
    assert old_key.read_text(encoding="utf-8") == "other-checkout-key"


def test_new_external_relay_key_stays_with_config(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    cfg = {"_config_path": str(tmp_path / "config.json")}
    generated = proxy.config_module._ensure_relay_key(cfg)
    assert (tmp_path / "relay_key.txt").read_text(encoding="utf-8").strip() == generated


def test_session_id_routes_affinity_while_display_labels_do_not(relay):
    chat = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]}
    relay.json("/v1/chat/completions", chat, headers={"x-proxi-client": "opencode", "x-proxi-session-id": "ses_title"})
    assert relay.fake.seen[-1].conversation_key == "header:ses_title"
    relay.json("/v1/chat/completions", chat, headers={"x-proxi-client": "opencode",
               "x-proxi-chat-title": "display only", "x-proxi-agent": "explore"})
    assert relay.fake.seen[-1].conversation_key is None
