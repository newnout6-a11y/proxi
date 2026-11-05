"""The runtime against the real prompt engine (prompt_policy) and the real body rewrite (prompt_body).

Those are other work packages: in a tree where they are not merged yet every test here is skipped. After the merge
they run for real and pin that the three agree on the contract: the plan, the pins, the protected rules, the refusals.
"""
import copy
import json
import pathlib
import time

import pytest

import direct
import prompt_policy
from prompt_bodies import (CLAUDE_SDK_HEADERS, CODEX_HEADERS, OPENCODE_HEADERS, OPENCODE_TITLE_V2, claude_sdk_body,
                           codex_body, opencode_body, service_body)
from prompt_support import (after_tool_call, client_request, head_of, prompt_relay, rewrite_ready,  # noqa: F401
                            runner_turns, strip_cache)
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, text


def engine_ready():
    try:
        prompt_policy.compile_document({"version": 1, "profiles": {}, "rules": []}, pathlib.Path("."), rev=1)
    except NotImplementedError:
        return False
    except Exception:  # an engine that refuses this minimal document is still an engine
        return True
    return True


engine = pytest.mark.skipif(not engine_ready(), reason="prompt_policy is not merged yet (work package P1)")
rewriting = pytest.mark.skipif(not (engine_ready() and rewrite_ready()),
                               reason="prompt_body.rewrite is not merged yet (work package P3)")
SYSTEM = "CLIENT_SYSTEM"
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]
OPENCODE = {"user-agent": "opencode/2.2.5"}


def document(rules_text="RULES_ONE", **extra):
    return {"version": 1, "texts": {"my_rules": rules_text},
            "profiles": {"mine": {"head": "{{text:my_rules}}\n\n{{client_system}}"}},
            "rules": [{"name": "opencode", "if": {"client": "opencode"}, "use": "mine"}], **extra}


def write(tmp_path, doc):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@engine
def test_a_rule_gives_matching_requests_the_profile_and_leaves_the_others_alone(prompt_relay, tmp_path):
    write(tmp_path, document())
    relay = prompt_relay()
    mark = relay.mark()
    chat = client_request("chat", USER, system=SYSTEM, tools=False)
    assert relay.post("chat", chat, OPENCODE)[0] == 200
    assert relay.post("chat", chat, {"user-agent": "some-other-client/1"})[0] == 200
    assert head_of(relay.posts[0]) == "RULES_ONE\n\n" + SYSTEM
    assert head_of(relay.posts[1]) == SYSTEM  # no rule, no default profile: the legacy head
    plans = relay.events(mark, "prompt.plan")
    assert len(plans) == 1 and plans[0]["data"]["profile"] == "mine" and plans[0]["data"]["rule"] == "opencode"


@engine
def test_a_service_job_of_opencode_is_untouched_even_by_a_profile_for_everything(prompt_relay, tmp_path):
    write(tmp_path, document(default_profile="mine"))
    relay = prompt_relay()
    assert relay.post("chat", service_body(OPENCODE_TITLE_V2), OPENCODE)[0] == 200
    assert head_of(relay.posts[0]) == OPENCODE_TITLE_V2
    other = client_request("chat", USER, system=SYSTEM, tools=False)
    assert relay.post("chat", other, {"user-agent": "some-other-client/1"})[0] == 200
    assert head_of(relay.posts[1]) == "RULES_ONE\n\n" + SYSTEM


@engine
def test_a_header_that_names_no_profile_is_a_400_and_a_stream_gets_the_real_status(prompt_relay, tmp_path):
    write(tmp_path, document(rules=[{"name": "by header", "if": {"header_profile": "*"}, "use": "$header"}]))
    relay = prompt_relay()
    for stream in (False, True):
        status, body, _ = relay.post("chat", client_request("chat", USER, tools=False, stream=stream),
                                     {"x-proxi-profile": "ghost"})
        assert status == 400 and body["error"]["param"] == "x-proxi-profile"
    assert relay.posts == []
    assert relay.post("chat", client_request("chat", USER, tools=False), {"x-proxi-profile": "mine"})[0] == 200


@engine
def test_a_virtual_model_that_names_no_profile_is_a_404_even_without_a_prompts_file(prompt_relay):
    relay = prompt_relay()
    status, body, _ = relay.post("chat", client_request("chat", USER, tools=False, model="claude-opus-5-5@ghost"))
    if body["error"].get("param") != "model":
        pytest.skip("virtual models (work package P6) are not merged yet: the model name is just unknown")
    assert status == 404 and body["error"]["type"] == "not_found_error" and "ghost" in body["error"]["message"]
    assert relay.posts == []


@engine
@pytest.mark.parametrize("dialect", ["chat", "anthropic", "responses"])
def test_a_new_revision_leaves_the_active_conversation_and_apply_to_all_costs_one_reset(prompt_relay, tmp_path,
                                                                                     dialect):
    path = write(tmp_path, document("RULES_ONE"))
    relay = prompt_relay()
    mark, messages = relay.mark(), list(USER)

    def turn(script):
        nonlocal messages
        relay.scripts.append(script)
        status, payload, _ = relay.post(dialect, client_request(dialect, messages, system=SYSTEM),
                                        {**OPENCODE, "x-session-affinity": "dialog-1"})
        assert status == 200, payload
        if any(event.get("type") == "tool_call" for event in script):
            messages = after_tool_call(dialect, messages, payload)
        return relay.posts[-1]

    first = turn(tool_reply("c1", "opaque-one"))
    time.sleep(1.2)  # the store looks at the file at most once a second
    path.write_text(json.dumps(document("RULES_TWO_IS_LONGER")), encoding="utf-8")
    second = turn(tool_reply("c2", "opaque-two"))
    assert head_of(first) == head_of(second) == "RULES_ONE\n\n" + SYSTEM
    assert relay.events(mark, "native_history.reset") == [] and relay.events(mark, "prompt.reload")

    assert relay.runtime.prompts.unpin(None) >= 1
    third = turn([text("done"), *END])
    assert head_of(third) == "RULES_TWO_IS_LONGER\n\n" + SYSTEM
    assert len(relay.events(mark, "native_history.reset")) == 1
    assert strip_cache(third["messages"][0]) != strip_cache(second["messages"][0])


@engine
def test_an_invalid_file_keeps_the_previous_revision_and_the_requests_keep_working(prompt_relay, tmp_path):
    path = write(tmp_path, document("RULES_ONE"))
    relay = prompt_relay()
    mark = relay.mark()
    chat = client_request("chat", USER, system=SYSTEM, tools=False)
    assert relay.post("chat", chat, OPENCODE)[0] == 200
    time.sleep(1.2)
    path.write_text("{ this is not json", encoding="utf-8")
    assert relay.post("chat", chat, OPENCODE)[0] == 200
    assert head_of(relay.posts[0]) == head_of(relay.posts[1]) == "RULES_ONE\n\n" + SYSTEM
    assert relay.events(mark, "prompt.config_error") and not relay.events(mark, "prompt.runtime_error")


@rewriting
def test_dropping_the_inline_system_entries_of_the_desktop_runner_keeps_its_history_resuming(prompt_relay, tmp_path):
    doc = document(profiles={"mine": {"head": "{{text:my_rules}}\n\n{{client_system}}",
                                      "history": {"mid_system": "drop"}}},
                   rules=[{"name": "runner", "if": {"client": "claude-code"}, "use": "mine"}])
    write(tmp_path, doc)
    relay = prompt_relay(allow_effort_downgrade=True)
    mark = relay.mark()
    results = runner_turns(relay, CLAUDE_SDK_HEADERS)
    heads = []
    for status, _, headers, sent in results:
        assert status == 200 and "x-proxi-system-messages" not in headers
        heads.append(head_of(sent))
        assert "Synthetic per-turn reminder" not in json.dumps(sent["messages"])
        assert "Synthetic inline environment" not in json.dumps(sent["messages"])
        assert sent["rit"] == direct.make_rit(sent["messages"], sent["tools"])
    # the reminders are dropped before the parse, so the head, the fingerprints and the signed history never move
    assert len(set(heads)) == 1 and len(relay.events(mark, "native_history.resume")) == 2
    assert relay.events(mark, "native_history.reset") == []
    last = results[-1][3]
    assert [block["signature"] for message in last["messages"] if isinstance(message["content"], list)
            for block in message["content"] if block.get("type") == "thinking"] == ["opaque-0", "opaque-1"]


def previewed():
    """Requests whose head the client owns, as the three clients send them: what a preview of them can show exactly."""
    return [("opencode", "chat", {**opencode_body(), "stream": False}, OPENCODE_HEADERS),
            ("agent sdk runner", "anthropic", {**claude_sdk_body(turns=2), "stream": False}, CLAUDE_SDK_HEADERS),
            ("codex", "responses", {**codex_body(), "stream": False}, CODEX_HEADERS),
            ("opencode title", "chat", service_body(OPENCODE_TITLE_V2), {**OPENCODE_HEADERS, "x-proxi-agent": "title"})]


@engine
@pytest.mark.parametrize("label,dialect,body,headers", previewed(), ids=[case[0] for case in previewed()])
def test_the_preview_shows_the_head_the_relay_then_sends(prompt_relay, tmp_path, label, dialect, body, headers):
    write(tmp_path, document(default_profile="mine"))
    relay = prompt_relay(allow_effort_downgrade=True)
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), dialect, headers)
    assert relay.post(dialect, body, headers)[0] == 200
    sent = head_of(relay.posts[-1])
    assert preview["head"] == sent and preview["purpose"] == ("client-service" if label == "opencode title"
                                                              else "client-tools")
    # the service job is the one a profile for everything does not touch
    assert ("RULES_ONE" in sent) == (label != "opencode title")


@rewriting
def test_the_preview_lists_the_changes_the_relay_then_records(prompt_relay, tmp_path):
    doc = document(default_profile="mine", profiles={"mine": {
        "head": "{{text:my_rules}}\n\n{{client_system}}", "history": {
            "mid_system": "drop", "user_blocks": [{"op": "drop_tag", "tag": "system-reminder"}]},
        "tools": {"edits": [{"tool": "Bash", "op": "append", "text": " NOTE_ON_BASH"}]}}})
    write(tmp_path, doc)
    relay = prompt_relay(allow_effort_downgrade=True)
    body = {**claude_sdk_body(turns=2), "stream": False}
    preview = relay.runtime.prompts.preview(copy.deepcopy(body), "anthropic", CLAUDE_SDK_HEADERS)
    assert relay.post("anthropic", body, CLAUDE_SDK_HEADERS)[0] == 200
    (entry,) = relay.runtime.prompts.recent()
    assert preview["history_changes"] and preview["tools_changes"]
    assert preview["history_changes"] == entry["history_changes"] and preview["tools_changes"] == entry["tools_changes"]
    assert {change["op"] for change in entry["history_changes"]} == {"drop_tag", "mid_system"}
    assert "NOTE_ON_BASH" in json.dumps(relay.posts[-1]["tools"]) and "Synthetic inline environment" not in json.dumps(
        relay.posts[-1]["messages"])
