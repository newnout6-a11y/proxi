"""PromptStore.preview: the head a request would get, from the real parsers and the same selection code as the runtime."""
import copy
import os
import tempfile

import pytest

import apierr
import client_context
import journal
import prompt_policy as pp
import validate
from test_prompt_policy_common import OPENCODE_SYSTEM, Clock, document, make_store, write_json

BILLING = "x-anthropic-billing-header: cc_version=2.1.310.a1b; cc_entrypoint=cli; cch=00000;"
TOOL = {"type": "function", "function": {"name": "bash", "description": "run", "parameters": {"type": "object"}}}
OPENCODE = {"x-proxi-client": "opencode", "user-agent": "ai-sdk/openai-compatible/1.0"}
CLAUDE = {"user-agent": "claude-cli/2.1.310 (external, cli)"}
CODEX = {"originator": "codex_cli_rs", "user-agent": "codex_cli_rs/0.164.0 (Windows 10.0.26100; x86_64) WindowsTerminal"}
FIELDS = {"rule", "profile", "rev", "protected", "purpose", "client", "model", "head", "parts", "history_changes",
          "tools_changes", "edits_unmatched", "warnings", "head_bytes", "tokens_estimate"}


def chat(system=OPENCODE_SYSTEM, tools=True, **extra):
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": "hi"}]
    body = {"model": "claude-opus-5-5", "stream": True, "messages": messages, **extra}
    if tools:
        body["tools"] = [copy.deepcopy(TOOL)]
    return body


def responses(instructions="INSTR", tools=True, **extra):
    body = {"model": "gpt-5.5", "input": "hello", **extra}
    if instructions:
        body["instructions"] = instructions
    if tools:
        body["tools"] = [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]
    return body


def anthropic(system=None, tools=True, **extra):
    body = {"model": "claude-sonnet-5-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}], **extra}
    if system is not None:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": "bash", "description": "run", "input_schema": {"type": "object"}}]
    return body


def doc(**extra):
    return document(
        texts={"rules": "My rules."},
        profiles={"mine": {"head": "{{text:rules}}\n\n{{client_system}}\n\n{{relay_task}}",
                           "client_system": {"edits": [{"op": "remove_section", "heading": "# Code comments"},
                                                       {"op": "remove_tag", "tag": "missing"}]}},
                  "exact": {"head": "EXACT"}},
        rules=[{"name": "opencode", "if": {"client": "opencode"}, "use": "mine"},
               {"name": "codex", "if": {"client": "codex"}, "use": "exact"}], **extra)


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path, doc())


# -- the shape ---------------------------------------------------------------------------------------------------------


def test_the_result_has_every_field_of_the_contract(store):
    result = store.preview(chat(), "chat", OPENCODE)
    assert FIELDS <= set(result)
    assert (result["rule"], result["profile"], result["rev"], result["protected"]) == ("opencode", "mine", 1, False)
    assert (result["purpose"], result["client"], result["model"]) == ("client-tools", "opencode", "claude-opus-5-5")
    assert result["history_changes"] == [] and result["tools_changes"] == []


def test_the_head_is_the_composed_template(store):
    result = store.preview(chat(), "chat", OPENCODE)
    expected = OPENCODE_SYSTEM.replace("# Code comments\nBy default, match the surrounding comment density.\n\n", "")
    assert result["head"] == "My rules.\n\n" + expected
    assert result["edits_unmatched"] == [{"scope": "client_system", "index": 1, "op": "remove_tag", "target": "missing"}]
    assert [e["matched"] for e in result["edits"]] == [True, False]
    assert any("client_system.edits[1] (remove_tag) matched nothing" == line for line in result["warnings"])
    assert any("{{relay_task}} is empty" in line for line in result["warnings"])


def test_parts_cover_the_head_exactly(store):
    result = store.preview(chat(), "chat", OPENCODE)
    head, parts = result["head"], result["parts"]
    assert parts[0]["start"] == 0 and parts[-1]["end"] == len(head)
    for before, after in zip(parts, parts[1:]):
        assert before["end"] == after["start"]
    for part in parts:
        chunk = head[part["start"]:part["end"]]
        assert part["chars"] == len(chunk) and part["sha8"] == pp.sha8(chunk)
    assert [part["source"] for part in parts] == ["text:rules", "literal", "client_edited"]


def test_sizes_are_counted_in_utf8_and_tokens_are_a_quarter_of_the_bytes(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"head": "Привет, мир! {{client_system}}"}},
                                          default_profile="p"))
    result = store.preview(chat(system="日本語"), "chat", OPENCODE)
    head = "Привет, мир! 日本語"
    assert result["head"] == head and result["head_bytes"] == len(head.encode("utf-8")) > len(head)
    assert result["tokens_estimate"] == (result["head_bytes"] + 3) // 4


def test_the_three_dialects(tmp_path):
    store = make_store(tmp_path, document(
        profiles={"p": {"head": "[{{client}}|{{purpose}}|{{model}}] {{client_system}}"}}, default_profile="p"))
    result = store.preview(chat(system="CHAT-SYS"), "chat", OPENCODE)
    assert result["head"] == "[opencode|client-tools|claude-opus-5-5] CHAT-SYS"
    result = store.preview(responses("RESP-SYS"), "responses", CODEX)
    assert result["head"] == "[codex|client-tools|gpt-5.5] RESP-SYS" and result["client"] == "codex"
    result = store.preview(anthropic(system="ANTH-SYS"), "anthropic", CLAUDE)
    assert result["head"] == "[claude-code|client-tools|claude-sonnet-5-5] ANTH-SYS"
    result = store.preview(chat(system="T", tools=False), "chat", {})
    assert result["head"] == "[unknown|generation|claude-opus-5-5] T" and result["client"] == "unknown"


def test_a_system_message_in_any_form_is_the_client_system(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"head": "<{{client_system}}>"}}, default_profile="p"))
    assert store.preview({"model": "gpt-5.5", "input": [
        {"role": "developer", "content": "DEV"}, {"role": "user", "content": "q"}], "instructions": "TOP"},
        "responses", {})["head"] == "<TOP\n\nDEV>"
    blocks = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
    assert store.preview(anthropic(system=blocks, tools=False), "anthropic", {})["head"] == "<A\nB>"


# -- I5, I6, I9, I10 ---------------------------------------------------------------------------------------------------


def test_the_billing_header_of_claude_code_stays_first(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"head": "{{text:r}}\n\n{{client_system}}"}},
                                          texts={"r": "RULES"}, default_profile="p"))
    system = [{"type": "text", "text": BILLING}, {"type": "text", "text": "You are Claude Code."}]
    result = store.preview(anthropic(system=system), "anthropic", CLAUDE)
    assert result["head"] == BILLING + "\nRULES\n\nYou are Claude Code."
    assert result["parts"][0]["source"] == "billing" and result["parts"][0]["end"] == len(BILLING) + 1


@pytest.mark.parametrize("headers, rule", [
    ({**CLAUDE, "x-claude-code-request-class": "compaction"}, "protected: Claude Code compaction"),
    ({**CLAUDE, "x-claude-code-request-class": "auxiliary"}, "protected: Claude Code compaction")])
def test_claude_code_service_requests_are_protected(store, headers, rule):
    result = store.preview(anthropic(system=[{"type": "text", "text": BILLING}, {"type": "text", "text": "S"}]),
                           "anthropic", headers)
    assert (result["profile"], result["rule"], result["protected"]) == ("passthrough", rule, True)
    assert result["head"] == BILLING + "\nS"


def test_a_title_request_is_protected_by_its_prompt(store):
    system = "You are naming a coding session so the user can pick it out of a long list of sessions.\nReply."
    result = store.preview(chat(system=system, tools=False), "chat", OPENCODE)
    assert (result["purpose"], result["profile"], result["protected"]) == ("session-title", "passthrough", True)
    assert result["head"] == system


def test_prompt_purpose_of_the_runtime_is_used_when_client_context_has_it(store, monkeypatch):
    monkeypatch.setattr(client_context, "prompt_purpose", lambda parsed, identity: "client-service", raising=False)
    result = store.preview(chat(system="whatever", tools=False), "chat", OPENCODE)
    assert (result["purpose"], result["protected"]) == ("client-service", True)


def test_relay_task_blocks_are_placeholders_for_a_task_the_relay_owns(tmp_path):
    store = make_store(tmp_path, doc(default_profile="mine"))
    result = store.preview(chat(system="", tools=False), "chat", {})
    assert result["purpose"] == "generation" and "[RELAY TASK" in result["head"]
    assert any("placeholders" in line for line in result["warnings"])
    assert result["parts"][-1]["source"] == "relay:task"
    result = store.preview(chat(), "chat", OPENCODE)
    assert "[RELAY TASK" not in result["head"] and not any("placeholders" in line for line in result["warnings"])
    assert any("{{relay_task}} is empty" in line for line in result["warnings"])


def test_a_structured_request_gets_the_contract(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"head": "{{client_system}}"}}, default_profile="p"))
    body = chat(system="SYS", tools=False, response_format={"type": "json_schema", "json_schema": {
        "name": "answer", "schema": {"type": "object", "properties": {"x": {"type": "string"}}}}})
    result = store.preview(body, "chat", {})
    assert result["head"].startswith("SYS\n\n[STRUCTURED OUTPUT FORMAT]") and "Schema name: answer" in result["head"]
    assert result["parts"][-1]["source"] == "relay:structured"
    wrapped = make_store(tmp_path / "w", document(
        profiles={"p": {"head": "{{client_system}}"}}, default_profile="p",
        relay_texts={"structured": "JSON for {{schema_name}} please: {{schema}}"}))
    assert wrapped.errors() == []
    assert wrapped.preview(body, "chat", {})["head"] == (
        'SYS\n\nJSON for answer please: {"type":"object","properties":{"x":{"type":"string"}}}')


def test_a_missing_text_is_not_confused_with_an_empty_one(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"head": "{{client_system}}"}}, default_profile="p"))
    result = store.preview(chat(system=""), "chat", OPENCODE)
    assert result["head"] == "" and result["parts"] == [] and result["head_bytes"] == 0


# -- drafts, pins and side effects --------------------------------------------------------------------------------------


def test_a_draft_is_judged_without_saving_it(store, tmp_path):
    draft = document(profiles={"d": {"head": "DRAFT {{client_system}}"}}, rules=[{"name": "r", "use": "d"}])
    since = journal.JOURNAL.last_seq
    before = (tmp_path / "prompts.json").read_bytes()
    result = store.preview(chat(system="S"), "chat", OPENCODE, document=draft)
    assert result["head"] == "DRAFT S" and result["rev"] is None and result["rule"] == "r"
    assert store.snapshot().rev == 1 and (tmp_path / "prompts.json").read_bytes() == before
    assert store.pins() == [] and store.recent() == [] and sorted(os.listdir(tmp_path)) == ["prompts.json"]
    assert [e for e in journal.JOURNAL.query(since=since, limit=500, order="asc") if e["category"] == "prompt"] == []
    assert store.preview(chat(system="S"), "chat", OPENCODE)["rev"] == 1


def test_a_draft_may_exist_without_any_saved_file(tmp_path):
    store = make_store(tmp_path)
    draft = document(profiles={"d": {"head": "DRAFT"}}, default_profile="d")
    assert store.preview(chat(), "chat", {}, document=draft)["head"] == "DRAFT"
    assert store.snapshot() is None and not (tmp_path / "prompts.json").exists()


def test_an_invalid_draft_is_reported_with_its_path(store):
    with pytest.raises(pp.PromptConfigError) as raised:
        store.preview(chat(), "chat", OPENCODE, document=document(profiles={"d": {"head": "{{nope}}"}}))
    assert raised.value.path == "profiles.d.head"


def test_a_draft_reads_its_text_files_next_to_the_prompts_file(tmp_path):
    store = make_store(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "t.md").write_text("FROM FILE", encoding="utf-8")
    draft = document(texts={"t": {"file": "prompts/t.md"}}, profiles={"d": {"head": "{{text:t}}"}},
                     default_profile="d")
    assert store.preview(chat(), "chat", {}, document=draft)["head"] == "FROM FILE"


def test_preview_writes_no_pins(tmp_path):
    store = make_store(tmp_path, doc())
    headers = {**OPENCODE, "x-session-affinity": "session-1"}
    for _ in range(3):
        store.preview(chat(), "chat", headers)
    assert store.pins() == []
    plan = store.plan(pp.PlanInput.from_request(validate.parse_chat(chat(), {"strict_models": True,
                                                                          "model": "claude-opus-5-5"}),
                                                {"client": "opencode"}, {}, "chat"))
    assert plan.rev == 1


def test_preview_shows_what_a_new_conversation_would_get_not_what_a_pinned_one_has(tmp_path):
    store = make_store(tmp_path, doc())
    headers = {**OPENCODE, "x-session-affinity": "session-1"}
    first = pp.PlanInput.from_request(validate.parse_chat(chat(), {"strict_models": True, "model": "x"}),
                                      {"client": "opencode"}, {}, "chat")
    pinned = pp.PlanInput(**{**first.__dict__, "conversation_key": "header:session-1"})
    store.plan(pinned)
    store.save(document(profiles={"n": {"head": "NEW"}}, default_profile="n"), 1)
    assert store.preview(chat(), "chat", headers)["head"] == "NEW"
    assert store.plan(pinned).rev == 1


def test_preview_sees_a_file_that_changed(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, doc(), clock=clock)
    write_json(tmp_path / "prompts.json", document(profiles={"n": {"head": "NEW"}}, default_profile="n"))
    info = (tmp_path / "prompts.json").stat()
    os.utime(tmp_path / "prompts.json", ns=(info.st_atime_ns, info.st_mtime_ns + 9_000_000_000))
    clock.advance(5)
    assert store.preview(chat(), "chat", OPENCODE)["head"] == "NEW"


# -- the identity is read from the request only -------------------------------------------------------------------------


def test_local_client_metadata_is_never_read(store, monkeypatch):
    seen = []
    real = client_context._local_title
    monkeypatch.setattr(client_context, "_local_title", lambda client, session, home: seen.append(str(home))
                        or real(client, session, home))
    monkeypatch.setattr(client_context.Path, "home", classmethod(lambda cls: pytest.fail("the real home is read")))
    store.preview(chat(), "chat", {**OPENCODE, "x-session-id": "ses_123"})
    assert all(not os.path.exists(path) for path in seen)
    assert all(path.startswith(os.path.realpath(tempfile.gettempdir())) or path.startswith(tempfile.gettempdir())
               for path in seen)


def test_the_calling_threads_identity_does_not_leak_in(store):
    token = client_context.bind({"client": "claude-code", "request_class": "compaction"})
    try:
        result = store.preview(chat(), "chat", OPENCODE)
    finally:
        client_context.reset(token)
    assert (result["client"], result["profile"], result["protected"]) == ("opencode", "mine", False)


def test_headers_are_matched_without_regard_to_case_and_type(store):
    result = store.preview(chat(), "chat", {"X-Proxi-Client": "OpenCode", "User-Agent": "x", "X-Count": 5})
    assert result["client"] == "opencode"
    assert store.preview(chat(), "chat", None)["client"] == "unknown"


def test_the_users_settings_apply_to_the_parse(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {"head": "x"}}, default_profile="p"), strict_models=False)
    result = store.preview(chat() | {"model": "some-new-model"}, "chat", OPENCODE)
    assert result["profile"] == "p"
    strict = make_store(tmp_path / "strict", document(profiles={"p": {"head": "x"}}, default_profile="p"))
    with pytest.raises(apierr.ApiError):
        strict.preview(chat() | {"model": "some-new-model"}, "chat", OPENCODE)


def test_remote_images_are_never_downloaded_by_a_preview(tmp_path, monkeypatch):
    store = make_store(tmp_path, document(profiles={"p": {"head": "{{client_system}}"}}, default_profile="p"))
    monkeypatch.setattr(validate, "_assert_public_http_url", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(validate._IMAGE_OPENER, "open", lambda *a, **k: pytest.fail("network"))
    body = {"model": "claude-opus-5-5", "messages": [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                     {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
                                     {"type": "image_url", "image_url": "http://example.invalid/b.png"},
                                     {"type": "input_image", "url": "https://example.invalid/c.png"}]}]}
    original = copy.deepcopy(body)
    assert store.preview(body, "chat", {})["head"] == "SYS"
    assert body == original                                       # the caller's body is not touched


def test_offline_copy_swaps_only_remote_images():
    body = {"a": [{"type": "image_url", "image_url": {"url": "https://x/y.png", "detail": "low"}},
                  {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
                  {"type": "text", "text": "https://x/y.png", "url": "https://keep.me"}]}
    out = pp._offline(body)
    assert out["a"][0] == {"type": "image_url", "image_url": {"url": pp._PIXEL, "detail": "low"}}
    assert out["a"][1] is body["a"][1] and out["a"][2] is body["a"][2]
    assert body["a"][0]["image_url"]["url"] == "https://x/y.png"
    untouched = {"k": [1, {"type": "text"}]}
    assert pp._offline(untouched) is untouched


# -- the virtual profile and errors ------------------------------------------------------------------------------------


def virtual(monkeypatch, profile):
    real = validate.parse_chat

    def parse(data, cfg):
        parsed = real(data, cfg)
        parsed.virtual_profile = profile
        parsed.requested_model = data["model"]
        return parsed

    monkeypatch.setattr(validate, "parse_chat", parse)


def test_a_virtual_profile_is_followed_as_at_runtime(store, monkeypatch):
    virtual(monkeypatch, "exact")
    result = store.preview(chat(), "chat", OPENCODE)
    assert (result["profile"], result["rule"], result["head"]) == ("exact", "virtual model", "EXACT")


def test_a_virtual_profile_that_does_not_exist_is_refused_as_at_runtime(store, monkeypatch):
    virtual(monkeypatch, "ghost")
    with pytest.raises(apierr.ApiError) as raised:
        store.preview(chat(), "chat", OPENCODE)
    assert raised.value.status == 404 and raised.value.param == "model"


def test_a_header_that_names_no_profile_is_refused_as_at_runtime(tmp_path):
    store = make_store(tmp_path, document(profiles={"p": {}}, rules=[{"name": "h", "use": "$header"}]))
    with pytest.raises(apierr.ApiError) as raised:
        store.preview(chat(), "chat", {"x-proxi-profile": "ghost"})
    assert raised.value.status == 400 and raised.value.param == "x-proxi-profile"
    assert store.preview(chat(), "chat", {"X-Proxi-Profile": "p"})["profile"] == "p"


def test_a_bad_dialect_or_body_is_a_client_error(store):
    for dialect in ("grpc", "", None):
        with pytest.raises(apierr.ApiError) as raised:
            store.preview(chat(), dialect, {})
        assert raised.value.status == 400 and raised.value.param == "dialect"
    for body in (None, [], "text", 5):
        with pytest.raises(apierr.ApiError) as raised:
            store.preview(body, "chat", {})
        assert raised.value.status == 400 and raised.value.param == "request"
    with pytest.raises(apierr.ApiError):
        store.preview({"model": "claude-opus-5-5"}, "chat", {})        # no messages: the parser's own error


# -- edits of the body (P3) --------------------------------------------------------------------------------------------


def body_doc():
    return document(profiles={"p": {"head": "{{client_system}}", "history": {"mid_system": "drop"}}},
                    default_profile="p")


def test_without_the_body_editor_the_preview_says_so_and_still_works(tmp_path, monkeypatch):
    import prompt_body

    def missing(*args):
        raise NotImplementedError

    monkeypatch.setattr(prompt_body, "rewrite", missing)
    store = make_store(tmp_path, body_doc())
    result = store.preview(chat(system="S"), "chat", {})
    assert result["head"] == "S" and result["history_changes"] == [] and result["tools_changes"] == []
    assert any("not available" in line for line in result["warnings"])


def test_the_real_body_editor_reports_its_changes_and_the_rules_that_found_nothing(tmp_path):
    doc = document(profiles={"p": {"head": "{{client_system}}",
                                   "history": {"mid_system": "drop",
                                               "user_blocks": [{"op": "drop_tag", "tag": "absent-tag"}]},
                                   "tools": {"edits": [{"tool": "nope*", "op": "append", "text": "x"}]}}},
                   default_profile="p")
    store = make_store(tmp_path, doc)
    body = chat(system="S")
    body["messages"].insert(2, {"role": "system", "content": "MID"})
    body["messages"].append({"role": "user", "content": "again"})
    result = store.preview(body, "chat", {})
    assert [change["op"] for change in result["history_changes"]] == ["mid_system"]
    assert result["tools_changes"] == []
    assert {"scope": "history", "op": "drop_tag", "edit": 0} in result["edits_unmatched"]
    assert {"scope": "tools", "op": "append", "edit": 0} in result["edits_unmatched"]
    assert not any("not available" in line for line in result["warnings"])


def test_changes_from_the_body_editor_are_split_into_history_and_tools(tmp_path, monkeypatch):
    import prompt_body
    store = make_store(tmp_path, body_doc())
    calls = []

    def rewrite(data, dialect, profile, snapshot):
        calls.append((copy.deepcopy(data), dialect, profile, snapshot))
        data = copy.deepcopy(data)
        data["messages"][-1]["content"] = "edited"
        return data, [{"index": 1, "op": "drop_tag", "removed_chars": 12}, {"tool": "bash", "op": "append"},
                      {"section": "tools", "op": "hide", "tool": "x"}, {"index": 3, "op": "remove_regex"}]

    monkeypatch.setattr(prompt_body, "rewrite", rewrite)
    result = store.preview(chat(system="S"), "chat", {})
    assert result["history_changes"] == [{"index": 1, "op": "drop_tag", "removed_chars": 12},
                                         {"index": 3, "op": "remove_regex"}]
    assert result["tools_changes"] == [{"tool": "bash", "op": "append"}, {"section": "tools", "op": "hide", "tool": "x"}]
    assert not any("not available" in line for line in result["warnings"])
    (data, dialect, profile, snapshot), = calls
    assert dialect == "chat" and profile["history"]["mid_system"] == "drop" and snapshot is store.snapshot()
    assert data["messages"][-1]["content"] == "hi"


def test_a_profile_without_body_edits_never_calls_the_body_editor(store, monkeypatch):
    import prompt_body
    monkeypatch.setattr(prompt_body, "rewrite", lambda *args: pytest.fail("called"))
    store.preview(chat(), "chat", OPENCODE)


def test_the_head_is_composed_from_the_second_parse(tmp_path, monkeypatch):
    import prompt_body
    store = make_store(tmp_path, body_doc())

    def rewrite(data, dialect, profile, snapshot):
        data = copy.deepcopy(data)
        data["messages"][0]["content"] = "SYSTEM AFTER THE EDIT"
        return data, []

    monkeypatch.setattr(prompt_body, "rewrite", rewrite)
    assert store.preview(chat(system="S"), "chat", {})["head"] == "SYSTEM AFTER THE EDIT"


# -- the legacy path ---------------------------------------------------------------------------------------------------


def test_without_a_file_the_preview_shows_the_head_of_the_existing_code(tmp_path):
    store = make_store(tmp_path)
    result = store.preview(chat(system="CLIENT"), "chat", OPENCODE)
    assert (result["profile"], result["rule"], result["rev"], result["protected"]) == (None, None, None, False)
    assert result["head"] == "CLIENT" and [part["source"] for part in result["parts"]] == ["client"]
    assert any("legacy path" in line for line in result["warnings"])
    assert result["purpose"] == "client-tools" and FIELDS <= set(result)


def test_a_request_no_rule_caught_shows_the_legacy_head_of_the_relay_settings(tmp_path):
    store = make_store(tmp_path, doc(), relay_system_prompt="POLICY", relay_system_prompt_mode="append")
    result = store.preview(chat(system="CLIENT"), "chat", {"user-agent": "aider"})
    assert result["profile"] is None and result["rev"] == 1
    assert result["head"] == "CLIENT\n\nPOLICY" and any("no rule matched" in line for line in result["warnings"])
    replace = make_store(tmp_path / "r", doc(), relay_system_prompt="POLICY", relay_system_prompt_mode="replace")
    assert replace.preview(chat(system="CLIENT"), "chat", {})["head"] == "POLICY"


def test_a_title_keeps_its_prompt_in_the_legacy_preview_whatever_the_mode(tmp_path):
    store = make_store(tmp_path, relay_system_prompt="POLICY", relay_system_prompt_mode="replace")
    system = "You are naming a coding session so the user can pick it out of a long list of sessions.\nReply."
    assert store.preview(chat(system=system, tools=False), "chat", OPENCODE)["head"] == system


def test_a_relay_owned_legacy_request_shows_the_role_and_says_what_is_left_out(tmp_path):
    store = make_store(tmp_path)
    import context
    import orchestrator
    result = store.preview(chat(system="", tools=False), "chat", {})
    assert result["head"] == context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert any("local execution" in line for line in result["warnings"])
    result = store.preview(chat(system=""), "chat", OPENCODE)
    assert result["head"] == orchestrator.DEFAULT_NATIVE_TOOLS_ROLE
    own = make_store(tmp_path / "own", relay_system_prompt="MY ROLE")
    assert own.preview(chat(system=""), "chat", OPENCODE)["head"] == "MY ROLE"


def test_a_legacy_preview_carries_the_structured_contract(tmp_path):
    store = make_store(tmp_path)
    body = chat(system="SYS", tools=False, response_format={"type": "json_object"})
    result = store.preview(body, "chat", {})
    assert result["head"].startswith("SYS\n\n[STRUCTURED OUTPUT FORMAT]")
    assert [part["source"] for part in result["parts"]] == ["client", "literal", "relay:structured"]


def test_a_legacy_rule_names_itself(tmp_path):
    store = make_store(tmp_path, document(rules=[{"name": "hands off", "use": "$legacy"}]))
    result = store.preview(chat(system="CLIENT"), "chat", {})
    assert result["profile"] is None and result["rule"] == "hands off" and "hands off" in " ".join(result["warnings"])


# -- Codex: the system is the leading developer messages --------------------------------------------------------------------

CODEX_BASE = "You are Codex, a coding agent based on GPT-5."
CODEX_PERMISSIONS = "<permissions instructions>\nFilesystem sandboxing: workspace-write.\n</permissions instructions>"
CODEX_SKILLS = "<skills_instructions>\n## Skills\n- review\n</skills_instructions>"


def codex_body(*developer):
    """A Responses request as codex-rs 0.164 sends it: no `instructions`, the leading developer messages are the prompt."""
    messages = [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}
                for text in (developer or (CODEX_BASE, CODEX_PERMISSIONS, CODEX_SKILLS))]
    messages += [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "<environment_context>\n  <cwd>C:\\work</cwd>\n</environment_context>"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]}]
    return {"model": "gpt-5.5", "stream": True, "input": messages,
            "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}]}


def test_the_client_system_of_codex_is_the_join_of_its_leading_developer_messages(tmp_path):
    store = make_store(tmp_path, document(
        texts={"rules": "My rules."},
        profiles={"mine": {"head": "{{text:rules}}\n\n{{client_system}}", "client_system": {"edits": [
            {"op": "remove_tag", "tag": "skills_instructions"}]}}},
        rules=[{"name": "codex", "if": {"client": "codex"}, "use": "mine"}]))
    assert "instructions" not in codex_body()
    result = store.preview(codex_body(), "responses", CODEX)
    assert (result["client"], result["profile"], result["purpose"]) == ("codex", "mine", "client-tools")
    assert result["head"] == "My rules.\n\n" + CODEX_BASE + "\n\n" + CODEX_PERMISSIONS
    assert [part["source"] for part in result["parts"]] == ["text:rules", "literal", "client_edited"]
    assert result["edits_unmatched"] == [] and not any("not part of the head" in line for line in result["warnings"])
    raw = make_store(tmp_path / "raw", document(profiles={"p": {}}, default_profile="p"))
    assert raw.preview(codex_body(), "responses", CODEX)["head"] == "\n\n".join(
        [CODEX_BASE, CODEX_PERMISSIONS, CODEX_SKILLS])


def test_a_codex_request_without_developer_messages_has_no_client_system(tmp_path):
    store = make_store(tmp_path, document(
        profiles={"p": {"head": "[{{client_system}}]"}},
        rules=[{"name": "bare", "if": {"client": "codex", "has_client_system": False}, "use": "p"}]))
    body = codex_body()
    body["input"] = body["input"][3:]
    assert store.preview(body, "responses", CODEX)["head"] == "[]"
    assert store.preview(codex_body(), "responses", CODEX)["profile"] is None


def test_edits_of_the_client_system_work_across_the_developer_messages_of_codex(tmp_path):
    store = make_store(tmp_path, document(
        profiles={"p": {"head": "{{client_system}}", "client_system": {"edits": [
            {"op": "remove_section", "heading": "## Skills"}, {"op": "replace_text", "find": "workspace-write",
                                                                "with": "read-only"}]}}},
        default_profile="p"))
    result = store.preview(codex_body(CODEX_BASE, CODEX_PERMISSIONS, "<skills_instructions>\n## Skills\n- review\n</skills_instructions>"),
                           "responses", CODEX)
    assert "read-only" in result["head"] and "workspace-write" not in result["head"]
    assert result["head"].startswith(CODEX_BASE + "\n\n<permissions instructions>")
    assert [e["matched"] for e in result["edits"]] == [True, True]
