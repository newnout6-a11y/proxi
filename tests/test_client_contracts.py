"""Client contracts shared by OpenCode, Claude Code and Responses/Codex."""
import json

import pytest

import apierr
import auth
import models
import validate

CFG = {"strict_models": True, "model": "claude-opus-5-5", "show_thinking": True}


def _body(dialect, **extra):
    if dialect == "responses":
        body = {"model": "gpt-6-sol", "input": "hello"}
    else:
        body = {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "hello"}]}
        if dialect == "anthropic":
            body["max_tokens"] = 1024
    return dict(body, **extra)


@pytest.mark.parametrize("raw", [b'{"text":"\xff"}', b'{"text":"\xc3("}'])
def test_invalid_utf8_is_rejected_without_replacement(raw):
    with pytest.raises(apierr.ApiError) as raised:
        validate.load_json(raw)
    assert raised.value.status == 400
    assert "UTF-8" in str(raised.value)


def test_valid_utf8_is_preserved():
    text = "Прочитай проект и ответь"
    assert validate.load_json(json.dumps({"text": text}, ensure_ascii=False).encode())["text"] == text


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_stream_must_be_a_boolean_for_every_client(dialect, value):
    with pytest.raises(apierr.ApiError) as raised:
        getattr(validate, "parse_" + dialect)(_body(dialect, stream=value), CFG)
    assert raised.value.status == 422
    assert raised.value.param == "stream"


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
def test_boolean_stream_is_retained(dialect):
    parser = getattr(validate, "parse_" + dialect)
    assert parser(_body(dialect, stream=True), CFG).stream is True
    assert parser(_body(dialect, stream=False), CFG).stream is False


def test_unidentified_requests_never_share_a_persistent_conversation():
    body = _body("chat")
    assert validate.conversation_key(body) is None
    assert validate.conversation_key(dict(body, messages=body["messages"] + [{"role": "user", "content": "next"}])) is None


def test_explicit_affinity_survives_history_changes_without_truncation_collision():
    a = "a" * 256 + "first"
    b = "a" * 256 + "second"
    assert validate.conversation_key({}, a) != validate.conversation_key({}, b)
    assert validate.conversation_key({"messages": []}, a) == validate.conversation_key(_body("chat"), a)
    assert validate.conversation_key({"conversation_id": "chat-1"}) == "conversation_id:chat-1"


@pytest.mark.parametrize("headers", [{"Authorization": "Bearer same-relay-key"},
                                     {"authorization": "Bearer same-relay-key"},
                                     {"x-api-key": "same-relay-key"},
                                     {"X-Api-Key": "same-relay-key"}])
def test_same_key_authenticates_openai_and_anthropic_headers(headers):
    auth.verify(headers, None, "same-relay-key")


def test_non_ascii_query_key_uses_utf8_not_header_latin1():
    auth.verify({}, "ключ", "ключ")
    auth.verify({"Authorization": "Bearer " + "ключ".encode().decode("latin-1")}, None, "ключ")


def test_client_headers_use_explicit_key_before_environment(monkeypatch):
    monkeypatch.setenv("RELAY_API_KEY", "environment-key")
    headers = auth.client_headers("explicit-key")
    assert headers["content-type"] == "application/json"
    auth.verify(headers, None, "explicit-key")


def test_client_headers_use_environment_before_file(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "__file__", str(tmp_path / "auth.py"))
    (tmp_path / "relay_key.txt").write_text("file-key\n", encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "environment-key")
    auth.verify(auth.client_headers(), None, "environment-key")


def test_client_headers_use_key_file_without_upstream_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "__file__", str(tmp_path / "auth.py"))
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    (tmp_path / "relay_key.txt").write_text("ключ\n", encoding="utf-8")
    auth.verify(auth.client_headers(), None, "ключ")
    assert not (tmp_path / "config.json").exists()


def test_client_headers_report_missing_credential_without_using_upstream_config(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "__file__", str(tmp_path / "auth.py"))
    monkeypatch.delenv("RELAY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="missing relay API key"):
        auth.client_headers()


@pytest.mark.parametrize("dialect,control", [
    ("chat", {"reasoning_effort": "low"}),
    ("responses", {"reasoning": {"effort": "low", "summary": "auto"}}),
    ("anthropic", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "low"}}),
])
def test_native_client_effort_controls_reach_the_normalized_provider_setting(dialect, control):
    parsed = getattr(validate, "parse_" + dialect)(_body(dialect, **control), CFG)
    assert parsed.effort == "low"
    assert parsed.show_thinking is True


def test_conflicting_effort_controls_are_not_silently_ignored():
    with pytest.raises(apierr.ApiError) as raised:
        validate.parse_chat(_body("chat", reasoning_effort="low", output_config={"effort": "high"}), CFG)
    assert raised.value.status == 422


@pytest.mark.parametrize("model,effort", [("gpt-5.5", "xhigh"), ("gpt-5.6-sol", "max"),
                                         ("gpt-6-astra", "none"), ("claude-opus-5-5", "xhigh")])
def test_unsupported_route_effort_is_rejected(model, effort):
    with pytest.raises(apierr.ApiError) as raised:
        models.resolve(model, effort)
    assert raised.value.status == 400
    assert model in str(raised.value)


def test_supported_efforts_are_real_per_model_capabilities():
    for effort in ("none", "low", "medium", "high", "xhigh", "max"):
        entry, selected = models.resolve("gpt-6-sol", effort)
        assert selected == effort
        assert effort in entry["reasoning_effort_levels"]
    entry, _ = models.resolve("gpt-5.5")
    assert entry["reasoning_effort_levels"] == ["low", "medium", "high"]


def test_codex_catalog_advertises_direct_client_tools_and_actual_route_capabilities():
    entry, _ = models.resolve("gpt-6-sol")
    catalog = models.codex_catalog([entry])
    assert list(catalog) == ["models"]
    native = catalog["models"][0]
    assert native["slug"] == "gpt-6-sol"
    assert native["tool_mode"] == "direct"
    assert native["shell_type"] == "unified_exec"
    assert native["apply_patch_tool_type"] == "freeform"
    assert native["input_modalities"] == ["text"]
    assert native["supports_reasoning_summary_parameter"] is True
    assert native["default_reasoning_summary"] == "detailed"
    assert [value["effort"] for value in native["supported_reasoning_levels"]] == entry["reasoning_effort_levels"]
    assert native["default_reasoning_level"] in entry["reasoning_effort_levels"]
    assert native["model_messages"]["instructions_template"] == native["base_instructions"]
    assert native["use_responses_lite"] is False


def test_codex_catalog_does_not_invent_reasoning_for_non_reasoning_model():
    entry, _ = models.resolve("zai-org/GLM-5.3")
    native = models.codex_catalog([entry])["models"][0]
    assert native["supported_reasoning_levels"] == []
    assert native["default_reasoning_level"] is None
    assert native["supports_reasoning_summary_parameter"] is False
    assert models.codex_catalog([]) == {"models": []}


def test_disabling_thinking_never_silently_falls_back_to_high():
    parsed = validate.parse_responses(_body("responses", thinking={"type": "disabled"}), CFG)
    assert parsed.effort == "none"
    with pytest.raises(apierr.ApiError):
        validate.parse_anthropic(_body("anthropic", thinking={"type": "disabled"}), CFG)


def test_anthropic_display_omitted_hides_thinking_without_changing_effort():
    parsed = validate.parse_anthropic(_body("anthropic", thinking={"type": "adaptive", "display": "omitted"},
                                          output_config={"effort": "high"}), CFG)
    assert parsed.effort == "high"
    assert not parsed.show_thinking


def test_tool_history_and_results_are_not_truncated_and_keep_call_ids():
    arguments = {"patch": "x" * 1500 + "FINAL_PATCH_MARKER"}
    output = "result" * 500 + "FINAL_RESULT_MARKER"
    parsed = validate.parse_anthropic(_body("anthropic", messages=[
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "provider-private", "signature": "opaque-signature"},
            {"type": "tool_use", "id": "call_17", "name": "apply_patch", "input": arguments}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_17", "content": [{"type": "text", "text": output}]}]},
    ]), CFG)
    assert parsed.prompt.count("call_17") == 2
    assert "FINAL_PATCH_MARKER" in parsed.prompt
    assert "FINAL_RESULT_MARKER" in parsed.prompt
    assert "provider-private" not in parsed.prompt
    assert "opaque-signature" not in parsed.prompt


def test_responses_function_call_ids_and_full_output_are_preserved():
    parsed = validate.parse_responses(_body("responses", instructions="Follow AGENTS.md", input=[
        {"type": "function_call", "id": "fc_internal", "call_id": "call_real", "name": "read_file", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_real", "output": "z" * 2000 + "END_OF_FILE"},
    ]), CFG)
    assert parsed.prompt.count("id=call_real") == 2
    assert "END_OF_FILE" in parsed.prompt
    assert parsed.system == "Follow AGENTS.md"


def test_codex_custom_tools_keep_grammar_input_call_id_and_result():
    grammar = {"type": "grammar", "syntax": "lark", "definition": 'start: "*** Begin Patch"'}
    patch = "*** Begin Patch\n" + "x" * 2000 + "\n*** End Patch"
    parsed = validate.parse_responses(_body("responses", tools=[{
        "type": "custom", "name": "apply_patch", "description": "Apply patch", "format": grammar}],
        tool_choice={"type": "custom", "name": "apply_patch"}, input=[
            {"type": "custom_tool_call", "call_id": "call_patch", "name": "apply_patch", "input": patch},
            {"type": "custom_tool_call_output", "call_id": "call_patch", "output": "Updated app.py"},
        ]), CFG)
    tool = parsed.tools[0]["function"]
    assert tool["x-client-type"] == "custom"
    assert tool["x-format"] == grammar
    assert tool["parameters"]["properties"]["input"] == {"type": "string"}
    assert parsed.prompt.count("id=call_patch") == 2
    assert "*** End Patch" in parsed.prompt
    assert "Updated app.py" in parsed.prompt


def test_codex_namespace_tools_keep_the_client_namespace_and_call_identity():
    parsed = validate.parse_responses(_body("responses", tools=[{
        "type": "namespace", "name": "multi_agent_v1", "tools": [
            {"type": "function", "name": "spawn_agent", "parameters": {"type": "object"}}]}],
        tool_choice={"type": "function", "namespace": "multi_agent_v1", "name": "spawn_agent"}, input=[
            {"type": "function_call", "namespace": "multi_agent_v1", "name": "spawn_agent",
             "call_id": "call_agent", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_agent", "output": "agent-id"},
        ]), CFG)
    definition = parsed.tools[0]["function"]
    assert definition["name"] == "multi_agent_v1.spawn_agent"
    assert definition["x-client-name"] == "spawn_agent"
    assert definition["x-namespace"] == "multi_agent_v1"
    assert parsed.tool_choice["function"]["name"] == definition["name"]
    assert "tool_call multi_agent_v1.spawn_agent id=call_agent" in parsed.prompt


def test_image_input_is_rejected_rather_than_replaced_with_a_text_placeholder():
    body = _body("responses", input=[{"role": "user", "content": [{"type": "input_image", "image_url": "https://example.invalid/image.png"}]}])
    with pytest.raises(apierr.ApiError) as raised:
        validate.parse_responses(body, CFG)
    assert raised.value.status == 422
    assert "attachments" in str(raised.value)


def test_unresolvable_item_reference_has_an_explicit_error():
    with pytest.raises(apierr.ApiError) as raised:
        validate.parse_responses(_body("responses", input=[{"type": "item_reference", "id": "msg_old"}]), CFG)
    assert raised.value.status == 422


def test_unknown_tool_choice_is_rejected_before_provider_request():
    with pytest.raises(apierr.ApiError) as raised:
        validate.parse_responses(_body("responses", tool_choice={"type": "function", "name": "missing"}), CFG)
    assert raised.value.status == 422
