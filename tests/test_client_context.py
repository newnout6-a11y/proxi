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
               "output_config": {"effort": "minimal"}}
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
    assert error["data"]["effort"] == "minimal"
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
    state = tmp_path / "state"
    seen = []
    monkeypatch.setattr(proxy.config_module, "load", lambda path: seen.append(path) or
                        {"secret": "preserved", "journal_path": "old-path", "command_log": "old-commands",
                         "upstream_error_dir": "old-dir"})
    cfg = proxy.startup_config(["--config", str(config_file), "--state-dir", str(state)])
    assert seen == [str(config_file)]
    assert cfg["secret"] == "preserved"
    assert Path(cfg["journal_path"]) == state / "journal.jsonl"
    assert Path(cfg["command_log"]) == state / "commands.log"
    assert Path(cfg["upstream_error_dir"]) == state / "logs/upstream-400"
    assert state.is_dir() and not config_file.exists()
    # Without --state-dir an already configured path is kept as is.
    kept = proxy.startup_config(["--config", str(config_file)])
    assert kept["journal_path"] == "old-path" and kept["command_log"] == "old-commands"


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
    relay.cfg["client_metadata_home"] = str(relay.workspace)
    relay.json("/v1/chat/completions", chat, headers={"x-proxi-client": "opencode", "x-proxi-session-id": "ses_title"})
    assert relay.fake.seen[-1].conversation_key == "header:ses_title"
    relay.json("/v1/chat/completions", chat, headers={"x-proxi-client": "opencode",
               "x-proxi-chat-title": "display only", "x-proxi-agent": "explore"})
    assert relay.fake.seen[-1].conversation_key is None


ATTRIBUTION = "x-anthropic-billing-header: cc_version=2.1.317.1a2; cc_entrypoint=cli; cch=e87ef;"
CLI_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
SDK_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
CURRENT_TITLE = client_context.SDK_TITLE_PREFIX + " The title is a name for what the session is about.\nReturn JSON."
LEGACY_TITLE = ("Generate a concise, sentence-case title (3-7 words) that captures the main topic or goal of "
                "this coding session.\n\nReturn JSON with a single \"title\" field.")
DESKTOP_TITLE = "You write short session titles. Reply with only the tagged fields the prompt asks for."
HARNESS_TITLE = ("Create a concise title for an AI coding-assistant session from the supplied human messages.\nReturn only "
                 "the title on one line, **in plain text of natural language**, with no quotes, prefix, explanation.")


def kind_of(system, client="claude-code", **extra):
    from types import SimpleNamespace
    parsed = SimpleNamespace(system=system, prompt="User: x", max_tokens=64, tools=[], **extra)
    return client_context.request_kind(parsed, {"client": client} if client else {})


@pytest.mark.parametrize("wording", [CURRENT_TITLE, LEGACY_TITLE, DESKTOP_TITLE, HARNESS_TITLE],
                         ids=["current", "legacy", "desktop", "harness"])
@pytest.mark.parametrize("preamble", [
    "", "\n  ", ATTRIBUTION + "\n", ATTRIBUTION + "\n" + CLI_IDENTITY + "\n", CLI_IDENTITY + "\n",
    ATTRIBUTION + "\n" + SDK_IDENTITY + "\n\n", ATTRIBUTION + "\r\n" + CLI_IDENTITY + "\r\n",
], ids=["bare", "blank", "attribution", "attribution+cli", "cli", "attribution+sdk", "crlf"])
def test_title_prompt_is_recognised_when_it_opens_its_block(wording, preamble):
    assert kind_of(preamble + wording) == "session-title"
    assert kind_of(preamble + wording, client="") == "session-title"


@pytest.mark.parametrize("system", [
    "Be concise.\n" + CURRENT_TITLE, "You are a helpful assistant.\n" + CURRENT_TITLE,
    ATTRIBUTION + "\nBe concise.\n" + CURRENT_TITLE, ATTRIBUTION + "\n" + CLI_IDENTITY + "\nBe concise.\n" + LEGACY_TITLE,
    'The SDK title prompt starts with "' + client_context.SDK_TITLE_PREFIX + '" and nothing else.',
    "> " + CURRENT_TITLE, "Rule one.\n" * 300 + CURRENT_TITLE + "\nRule two.\n" * 5,
    "Quote:\n\n" + LEGACY_TITLE, "  Notes:\n" + DESKTOP_TITLE,
], ids=["quoted-line", "role-line", "after-rule-attribution", "after-rule-legacy", "inline-quote", "markdown-quote",
        "deep-in-long-system", "legacy-after-label", "desktop-after-label"])
def test_a_system_that_merely_quotes_the_title_wording_is_an_ordinary_request(system):
    assert kind_of(system) == "client-text"
    assert kind_of(system, client="") == "generation"
    assert not client_context.is_sdk_title_system(system)


def test_title_is_recognised_by_its_schema_when_the_prompt_is_unrecognisable():
    schema_only = {"title": True, "kind": "json_schema"}
    assert kind_of("Name this session in two words.", structured_output=schema_only) == "session-title"
    assert kind_of("Name this session in two words.", structured_output={"title": False}) == "client-text"
    assert kind_of("Name this session in two words.") == "client-text"
    from types import SimpleNamespace
    with_tools = SimpleNamespace(system="Name it.", prompt="", tools=[{"type": "function"}], structured_output=schema_only)
    assert client_context.request_kind(with_tools, {"client": "claude-code"}) == "client-tools"


def test_title_wording_still_classifies_when_structured_outputs_are_switched_off():
    # CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1 sends the prompt but no output_config.format.
    import validate
    from test_client_contracts import CFG
    body = {"model": "claude-haiku-4-5-20251001", "max_tokens": 64, "thinking": {"type": "disabled"},
            "system": [{"type": "text", "text": ATTRIBUTION}, {"type": "text", "text": CLI_IDENTITY},
                       {"type": "text", "text": LEGACY_TITLE}],
            "messages": [{"role": "user", "content": "<session>fix the login bug</session>"}]}
    parsed = validate.parse_anthropic(body, CFG)
    assert parsed.structured_output is None
    assert client_context.request_kind(parsed, {"client": "claude-code"}) == "session-title"


@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("system,expected", [
    (ATTRIBUTION + "\n" + CLI_IDENTITY + "\n" + LEGACY_TITLE, "session-title"),
    (CURRENT_TITLE, "session-title"),
    ("Be concise.\n" + CURRENT_TITLE, "client-text"),
], ids=["legacy-title", "current-title", "quote"])
def test_relay_policy_follows_the_classification_not_a_quoted_phrase(tmp_path, monkeypatch, mode, system, expected):
    from test_native_context_reuse import request, run, setup
    from test_service_requests import capture_direct
    policy = "RELAY ROLE AND RULES"
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, exec_policy="off",
                         cookie_values=[], relay_system_prompt=policy, relay_system_prompt_mode=mode)
    sent = capture_direct(monkeypatch)
    parsed = request([{"role": "user", "content": "hi"}], tools=[], system=system)
    token = client_context.bind({"client": "claude-code"})
    try:
        asyncio.run(run(runner, parsed))
    finally:
        client_context.reset(token)
    head = sent[0][1]["messages"][0]["content"]
    text = "\n".join(block["text"] for block in head)
    assert client_context.request_kind(parsed, {"client": "claude-code"}) == expected
    if expected == "session-title":
        assert policy not in text and system.split("\n", 1)[-1] in text
    else:
        assert policy in text
