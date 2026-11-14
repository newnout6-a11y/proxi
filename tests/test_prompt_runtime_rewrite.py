"""The runtime against the real body rewrite (prompt_body.rewrite, work package P3) behind the fake prompt store.

In a tree where the rewrite is not merged yet every test here is skipped. After the merge they pin that the hooks of
proxy and the rewrite agree: the shape of its changes, the summaries the journal takes, the notes the client is told
and a signed history that goes on resuming.
"""
import copy
import json

import pytest

import direct
from prompt_bodies import (CLAUDE_SDK_HEADERS, CODEX_BASE, CODEX_COLLABORATION, CODEX_HEADERS, CODEX_PERMISSIONS,
                           codex_body)
from prompt_support import (client_request, fake_relay, head_of, profile, prompt_relay,  # noqa: F401
                            rewrite_ready, runner_turns)
from test_native_history import tool_reply
from test_prompt_runtime_compat import (BY_REFERENCE, BY_VALUE, REMINDER, TEXT, TOOL_CHANGES, compat,
                                        conversation)

pytestmark = pytest.mark.skipif(not rewrite_ready(), reason="prompt_body.rewrite is not merged yet (work package P3)")

DIALECTS = ["chat", "anthropic", "responses"]
TEMPLATE = "{{text:rules}}\n\n{{client_system}}"
RULES = "MY_RULES"
PLAN_MODE = "<system-reminder>\nPlan mode is active. Do not edit files.\n</system-reminder>"
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK\n\n" + PLAN_MODE}]
DROP_REMINDERS = [{"op": "drop_tag", "tag": "system-reminder"}]
NOTE_ON_READ_FILE = [{"tool": "read_file", "op": "append", "text": " TOOL_NOTE"}]
CALLS_READ_FILE = {"chat": {"type": "function", "function": {"name": "read_file"}},
                   "anthropic": {"type": "tool", "name": "read_file"},
                   "responses": {"type": "function", "name": "read_file"}}


def publish(relay, **sections):
    relay.prompts.publish(1, {"mine": profile(TEMPLATE, texts={"rules": RULES}, **sections)}, default_profile="mine")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_real_rewrite_edits_what_the_model_sees_and_the_hooks_report_it(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    publish(relay, user_blocks=DROP_REMINDERS, tool_edits=NOTE_ON_READ_FILE)
    request = client_request(dialect, USER, system="CLIENT_SYSTEM")
    before, mark = copy.deepcopy(request), relay.mark()
    assert relay.post(dialect, request, {"x-session-affinity": "dialog-1"})[0] == 200
    body = relay.posts[0]
    assert "ORIGINAL_TASK" in json.dumps(body["messages"]) and "Plan mode" not in json.dumps(body["messages"])
    assert "TOOL_NOTE" in json.dumps(body["tools"]) and head_of(body) == RULES + "\n\nCLIENT_SYSTEM"
    assert body["rit"] == direct.make_rit(body["messages"], body["tools"])
    (entry,) = relay.prompts.records
    assert entry["request"]["body"] == before and "Plan mode" in json.dumps(entry["request"]["body"])
    assert [(c["scope"], c["op"], c["target"]) for c in entry["history_changes"]] == [("history", "drop_tag", "user")]
    assert [(c["scope"], c["op"], c["tool"]) for c in entry["tools_changes"]] == [("tools", "append", "read_file")]
    assert all("Plan mode" not in json.dumps(change) and "TOOL_NOTE" not in json.dumps(change)
               for change in entry["history_changes"] + entry["tools_changes"])
    (event,) = relay.events(mark, "prompt.plan")
    (history,), (tools,) = event["data"]["history_changes"], event["data"]["tools_changes"]
    assert (history["op"], history["count"], history["edit"]) == ("drop_tag", 1, 0) and history["removed_chars"] > 0
    assert (tools["op"], tools["tool"], tools["count"]) == ("append", "read_file", 1)
    assert "Plan mode" not in json.dumps(event) and "TOOL_NOTE" not in json.dumps(event)
    assert not relay.events(mark, "prompt.edit_skipped") and not relay.events(mark, "prompt.runtime_error")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_edit_the_real_rewrite_refuses_is_a_warning_and_the_request_goes_on(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    publish(relay, tool_edits=[{"tool": "read_file", "hide": True}])
    request = client_request(dialect, USER, system="CLIENT_SYSTEM", tool_choice=CALLS_READ_FILE[dialect])
    mark = relay.mark()
    relay.scripts.append(tool_reply("c1", "opaque-one"))  # the client forced a call of read_file: the answer is one
    assert relay.post(dialect, request)[0] == 200
    assert "read_file" in json.dumps(relay.posts[0]["tools"])  # the tool the client forced is not hidden
    (skipped,) = relay.events(mark, "prompt.edit_skipped")
    data = skipped["data"]
    assert skipped["level"] == "warn" and skipped["request_id"]
    assert (data["scope"], data["op"], data["tool"], data["edit"], data["count"]) == ("tools", "hide", "read_file", 0, 1)
    assert isinstance(data["reason"], str) and data["reason"]
    (event,) = relay.events(mark, "prompt.plan")
    (group,) = event["data"]["tools_changes"]
    assert group["skipped"] is True and group["reason"] == data["reason"] and event["data"]["history_changes"] == []
    (entry,) = relay.prompts.records
    assert [change.get("skipped") for change in entry["tools_changes"]] == [True]


@pytest.mark.parametrize("mode", ["wrap", "plain"])
@pytest.mark.parametrize("dialect", DIALECTS)
def test_reminders_the_real_rewrite_turned_into_user_text_are_still_what_the_client_is_told(
        prompt_relay, dialect, mode):
    relay = fake_relay(prompt_relay)
    data = conversation(dialect, [*TEXT, BY_VALUE, BY_REFERENCE], temperature=1)
    mark = relay.mark()
    status, _, legacy = relay.post(dialect, data)
    assert status == 200
    publish(relay, mid_system=mode)
    status, _, through = relay.post(dialect, data)
    assert status == 200 and relay.prompts.plans[-1].body_edits
    assert legacy["x-proxi-system-messages"] == through["x-proxi-system-messages"] == "inline-as-user-text"
    assert through["x-proxi-ignored-parameters"] == legacy["x-proxi-ignored-parameters"]
    assert set(TOOL_CHANGES) <= set(through["x-proxi-ignored-parameters"].split(","))
    assert compat(relay, mark, through) == compat(relay, mark, legacy) and len(compat(relay, mark, through)) == 2
    sent = relay.posts[1]
    assert REMINDER in json.dumps(sent["messages"]) and "db_query" not in json.dumps(sent)
    assert ("<system-reminder>" in json.dumps(sent["messages"])) == (mode == "wrap")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_reminders_the_real_rewrite_dropped_are_not_passed_and_the_tool_changes_are_still_named(prompt_relay, dialect):
    relay = fake_relay(prompt_relay)
    data = conversation(dialect, [*TEXT, BY_VALUE, BY_REFERENCE], temperature=1)
    mark = relay.mark()
    _, _, legacy = relay.post(dialect, data)
    publish(relay, mid_system="drop")
    status, _, through = relay.post(dialect, data)
    assert status == 200
    assert "x-proxi-system-messages" not in through and legacy["x-proxi-system-messages"] == "inline-as-user-text"
    assert through["x-proxi-ignored-parameters"] == legacy["x-proxi-ignored-parameters"]
    assert [control for _, control, _ in compat(relay, mark, through)] == [None]
    assert REMINDER not in json.dumps(relay.posts[1]["messages"]) and "db_query" not in json.dumps(relay.posts[1])
    (plan,) = [e for e in relay.events(mark, "prompt.plan") if e["request_id"] == through["request-id"]]
    assert [(g["op"], g["mode"], g["count"]) for g in plan["data"]["history_changes"]] == [("mid_system", "drop", 1)]


def signatures(body):
    return [block["signature"] for message in body["messages"] if isinstance(message.get("content"), list)
            for block in message["content"] if isinstance(block, dict) and block.get("type") == "thinking"]


@pytest.mark.parametrize("mode", ["drop", "wrap", "plain"])
def test_the_desktop_runner_keeps_resuming_whatever_the_profile_does_with_its_reminders(prompt_relay, mode):
    relay = fake_relay(prompt_relay, allow_effort_downgrade=True)
    publish(relay, mid_system=mode)
    mark = relay.mark()
    results = runner_turns(relay, CLAUDE_SDK_HEADERS)
    sent = [body for _, _, _, body in results]
    assert [status for status, _, _, _ in results] == [200, 200, 200]
    # the first request already carries an inline system message, and so does every later one: that is the note
    assert ["x-proxi-system-messages" in headers for _, _, headers, _ in results] == [mode != "drop"] * 3
    for body in sent:
        assert body["rit"] == direct.make_rit(body["messages"], body["tools"])
        if mode == "drop":
            assert "Synthetic inline environment" not in json.dumps(body["messages"])
            assert "Synthetic per-turn reminder" not in json.dumps(body["messages"])
        else:
            assert "Synthetic inline environment" in json.dumps(body["messages"])
    assert len({head_of(body) for body in sent}) == 1 and len({body["openAICacheKey"] for body in sent}) == 1
    assert signatures(sent[-1]) == ["opaque-0", "opaque-1"]
    assert len(relay.events(mark, "native_history.resume")) == 2 and not relay.events(mark, "native_history.reset")
    assert not relay.events(mark, "prompt.runtime_error")


def with_late_developer_message(body, text):
    body = copy.deepcopy(body)
    body["input"].append({"type": "message", "role": "developer",
                          "content": [{"type": "input_text", "text": text}]})
    body["input"].append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go on"}]})
    return body


@pytest.mark.parametrize("mode", ["drop", "wrap", "plain"])
def test_codex_keeps_its_leading_developer_items_as_the_head_and_only_a_later_one_is_rewritten(prompt_relay, mode):
    relay = fake_relay(prompt_relay)
    publish(relay, mid_system=mode)
    late = "<permissions instructions>\nLATE_PERMISSIONS_UPDATE\n</permissions instructions>"
    body = with_late_developer_message(codex_body(), late)
    body["stream"] = False
    mark = relay.mark()
    status, _, headers = relay.post("responses", body, CODEX_HEADERS)
    assert status == 200
    system = "\n\n".join([CODEX_BASE, CODEX_PERMISSIONS, CODEX_COLLABORATION])
    assert head_of(relay.posts[0]) == RULES + "\n\n" + system
    text = json.dumps(relay.posts[0]["messages"][1:])
    assert ("LATE_PERMISSIONS_UPDATE" in text) == (mode != "drop")
    assert ("x-proxi-system-messages" in headers) == (mode != "drop")
    (plan,) = relay.events(mark, "prompt.plan")
    assert [(g["op"], g["mode"], g["count"]) for g in plan["data"]["history_changes"]] == [("mid_system", mode, 1)]
    (entry,) = relay.prompts.records
    assert entry["client"] == "codex" and entry["request"]["body"]["input"][-2]["role"] == "developer"
