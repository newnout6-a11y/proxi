"""Claude Code's JSON metadata identity gives its own conversation affinity."""
import asyncio
import copy
import json

import pytest

import validate
from test_affinity_scopes import scenario_setup, tool_response


def metadata(session_id="claude-main", **extra):
    return {"metadata": {"user_id": json.dumps({
        "device_id": "test-device", "account_uuid": "test-account",
        "session_id": session_id, **extra,
    })}}


@pytest.mark.parametrize("value", [
    None, "", "opaque-user", "user_device_account_account_session_legacy", "{broken",
    "null", "false", "42", '"opaque-user"', "[]", '[{"session_id":"nested"}]',
    "{}", '{"device_id":"device","account_uuid":"account","parent_session_id":"parent"}',
    '{"session_id":null}', '{"session_id":false}', '{"session_id":123}',
    '{"session_id":[]}', '{"session_id":{}}', '{"session_id":""}', '{"session_id":"  \\n"}',
    {"session_id": "not-a-json-string"}, ["not-a-json-string"], 123, False,
])
def test_opaque_malformed_or_missing_metadata_session_has_no_affinity(value):
    assert validate.conversation_key({"metadata": {"user_id": value}}) is None


@pytest.mark.parametrize("value", [None, "string", [], 123, False, {"session_id": "wrong-field"}])
def test_nonobject_metadata_and_metadata_without_user_id_are_ignored(value):
    assert validate.conversation_key({"metadata": value}) is None


def test_claude_session_is_stable_and_account_device_parent_are_not_session_keys():
    first = metadata("  claude-main \n", parent_session_id="parent-one")
    changed = metadata("claude-main", device_id="another-device", account_uuid="another-account",
                       parent_session_id="parent-two")
    assert validate.conversation_key(first) == validate.conversation_key(changed) == "claude-metadata:claude-main"
    child = metadata("claude-child", parent_session_id="claude-main")
    assert validate.conversation_key(child) == "claude-metadata:claude-child"
    assert validate.conversation_key(child) != validate.conversation_key(first)


def test_existing_header_and_top_level_precedence_are_preserved():
    body = dict(metadata(), conversation="conversation", conversation_id="conversation-id", session_id="session")
    assert validate.conversation_key(body, " header-id ") == "header:header-id"
    assert validate.conversation_key(body, " ") == "conversation:conversation"
    del body["conversation"]
    assert validate.conversation_key(body) == "conversation_id:conversation-id"
    del body["conversation_id"]
    assert validate.conversation_key(body) == "session_id:session"
    del body["session_id"]
    assert validate.conversation_key(body) == "claude-metadata:claude-main"


def test_metadata_affinity_namespace_cannot_collide_with_existing_sources():
    keys = {validate.conversation_key(metadata("same")), validate.conversation_key({}, "same")}
    keys.update(validate.conversation_key({name: "same"}) for name in ("conversation", "conversation_id", "session_id"))
    assert len(keys) == 5


FIRST = [{"role": "user", "content": "Read ORIGINAL_CLAUDE_TASK."}]


def body(session_id, *, service=False, followup=False, parent=None):
    messages = copy.deepcopy(FIRST)
    if followup:
        messages += [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "read-1",
                                                "name": "read_file", "input": {"path": "a"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-1",
                                           "content": "NEW_CLAUDE_RESULT_" + session_id}]},
        ]
    data = {"model": "claude-opus-5-5", "max_tokens": 64, "messages": messages,
            "system": "TITLE_SYSTEM" if service else "CLIENT_SYSTEM", **metadata(session_id)}
    if parent:
        data.update(metadata(session_id, parent_session_id=parent))
    if not service:
        data["tools"] = [{"name": "read_file", "input_schema": {"type": "object"}}]
    return data


def test_actual_anthropic_endpoint_derives_affinity_from_metadata(relay):
    for data in (body("main"), body("main", followup=True), body("child", parent="main")):
        assert relay.json("/v1/messages", data)[0] == 200
    first, following, child = relay.fake.seen
    assert first.conversation_key == following.conversation_key == "claude-metadata:main"
    assert child.conversation_key == "claude-metadata:child"


def test_real_pool_keeps_claude_parent_child_and_auxiliary_scopes_separate(tmp_path):
    from test_native_context_reuse import run

    backend, pool, runner = scenario_setup(tmp_path)
    parse_cfg = {"strict_models": True, "model": "claude-opus-5-5"}

    async def send(data):
        parsed = validate.parse_anthropic(data, parse_cfg)
        parsed.conversation_key = validate.conversation_key(data)
        events = await run(runner, parsed)
        assert events[-1]["type"] == "end"

    async def scenario():
        for identity, parent in (("main", None), ("child", "main")):
            tool_response(backend)
            await send(body(identity, parent=parent))
            target = pool._conversations["claude-metadata:" + identity]
            socket, context = target.ws, target._client_context
            await send(body(identity, service=True, parent=parent))
            assert target.ws is socket and not socket.closed
            assert target._client_context is context and target._client_context_pending is False
        await send(body("main", followup=True))
        await send(body("child", followup=True, parent="main"))

    asyncio.run(scenario())
    assert set(pool._conversations) == {"claude-metadata:main", "claude-metadata:child"}
    assert [item[0] for item in backend.sent] == ["thread-1", "thread-2", "thread-3", "thread-4", "thread-1", "thread-3"]
    assert backend.sent[1][2].closed and backend.sent[3][2].closed
    assert backend.starts == backend.connects == 4
    for index, identity in enumerate(("main", "child")):
        prompt = backend.sent[-2 + index][1]
        assert "NEW_CLAUDE_RESULT_" + identity in prompt
        assert "NEW_CLAUDE_RESULT_" + ("child" if identity == "main" else "main") not in prompt
        assert "ORIGINAL_CLAUDE_TASK" not in prompt and 'tools=[' not in prompt


def test_moving_claude_cache_breakpoints_preserves_warm_native_conversation(tmp_path):
    from test_native_context_reuse import run

    backend, pool, runner = scenario_setup(tmp_path)
    first = body("cache-main")
    original = first["messages"][0]["content"]
    first["messages"][0]["content"] = [{"type": "text", "text": original,
                                         "cache_control": {"type": "ephemeral"}}]
    first["messages"][0]["cache_control"] = {"type": "ephemeral"}
    following = body("cache-main", followup=True)
    following["messages"][0]["content"] = [{"type": "text", "text": original}]
    following["messages"][-1]["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    parse_cfg = {"strict_models": True, "model": "claude-opus-5-5"}

    async def scenario():
        for index, data in enumerate((first, following)):
            parsed = validate.parse_anthropic(data, parse_cfg)
            parsed.conversation_key = validate.conversation_key(data)
            if index == 0:
                tool_response(backend)
            events = await run(runner, parsed)
            assert events[-1]["type"] == "end"
        target = pool._conversations[validate.conversation_key(first)]
        assert target._client_context_pending is False

    asyncio.run(scenario())
    assert backend.created == ["thread-1"] and backend.starts == backend.connects == 1
    assert backend.sent[0][2] is backend.sent[1][2]
    assert "NEW_CLAUDE_RESULT_cache-main" in backend.sent[-1][1]
    assert "ORIGINAL_CLAUDE_TASK" not in backend.sent[-1][1] and 'tools=[' not in backend.sent[-1][1]


@pytest.mark.parametrize("change", ["text", "tool_id", "tool_result_id", "argument_cache_control",
                                     "result_cache_control", "argument_signature"])
def test_bridge_visible_content_ids_and_tool_data_still_reset_context(tmp_path, change):
    from test_native_context_reuse import run

    backend, _, runner = scenario_setup(tmp_path)
    first = body("semantic-main", followup=True)
    tool = first["messages"][1]["content"][0]
    tool["input"].update(cache_control="ORIGINAL_TOOL_ARGUMENT", signature="ORIGINAL_SIGNATURE_ARGUMENT")
    first["messages"][2]["content"][0]["content"] = '{"cache_control":"ORIGINAL_TOOL_RESULT"}'
    following = copy.deepcopy(first)
    if change == "text":
        following["messages"][0]["content"] += "\nEDITED_LAST_MESSAGE"
    elif change == "tool_id":
        following["messages"][1]["content"][0]["id"] = "changed-call-id"
    elif change == "tool_result_id":
        following["messages"][2]["content"][0]["tool_use_id"] = "changed-result-id"
    elif change == "argument_cache_control":
        following["messages"][1]["content"][0]["input"]["cache_control"] = "CHANGED_TOOL_ARGUMENT"
    elif change == "result_cache_control":
        following["messages"][2]["content"][0]["content"] = '{"cache_control":"CHANGED_TOOL_RESULT"}'
    else:
        following["messages"][1]["content"][0]["input"]["signature"] = "CHANGED_SIGNATURE_ARGUMENT"
    following["messages"].append({"role": "user", "content": "ACTUAL_NEW_MESSAGE"})
    parse_cfg = {"strict_models": True, "model": "claude-opus-5-5"}
    parsed = [validate.parse_anthropic(data, parse_cfg) for data in (first, following)]
    assert parsed[1].message_fingerprints[:len(parsed[0].message_fingerprints)] != parsed[0].message_fingerprints

    async def scenario():
        for data, current in zip((first, following), parsed):
            current.conversation_key = validate.conversation_key(data)
            events = await run(runner, current)
            assert events[-1] == {"type": "end", "reason": "stop"}

    asyncio.run(scenario())
    assert backend.created == ["thread-1", "thread-2"]
    assert parsed[-1].prompt in backend.sent[-1][1] and 'tools=[' in backend.sent[-1][1]


def test_fingerprints_match_existing_bridge_rendering_of_thinking_signatures():
    first = body("thinking-metadata", followup=True)
    first["messages"][1]["content"].insert(0, {
        "type": "thinking", "thinking": "provider reasoning", "signature": "opaque-signature-one"})
    following = copy.deepcopy(first)
    following["messages"][1]["content"][0]["signature"] = "opaque-signature-two"
    parse_cfg = {"strict_models": True, "model": "claude-opus-5-5"}
    old, new = [validate.parse_anthropic(data, parse_cfg) for data in (first, following)]
    # The bridge already omits thinking blocks from its input prompt. This
    # projection must follow that existing wire representation, not erase a
    # signature occurring in actual tool input (covered separately above).
    assert old.prompt == new.prompt
    assert old.message_fingerprints == new.message_fingerprints
