"""Regression tests for Wave 0 Prompt Engine & Prompt Isolation backend fixes."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest

import apierr
import client_context
import prompt_body
import prompt_policy as pp
from prompt_stabilizer import SkillLocations, Stabilizer
from prompt_support import KEY, client_request, head_of, prompt_relay  # noqa: F401
from test_client_task import CLAUDE_CODE as CLAUDE_CODE_INSTR, FIRST as OPENCODE2_COMPACT, WORK
from test_compaction_cache import CLAUDE_CODE as CLAUDE_HEADERS
from test_conversation_infer import HARNESS
from test_harness_compaction import ENGINE as DSH_COMPACT
from test_prompt_body_support import FakeSnapshot, REMINDER, drop_tag, profile as body_profile
from test_prompt_policy_common import Clock, document, inp, make_store, write_json
from test_prompt_runtime_protected import OPENCODE
from test_service_requests import SUGGESTION_PROMPT


# -- HIGH-P4: enforce publish_as_model on runtime $virtual --------------------


def test_runtime_virtual_enforces_publish_as_model_glob(tmp_path):
    doc = document(
        profiles={
            "eni": {"head": "ENI_PERSONA", "publish_as_model": ["gpt-6-sol", "claude-haiku-*"]},
            "open_bool": {"head": "BOOL", "publish_as_model": True},
            "unpublished": {"head": "UNPUB", "publish_as_model": False},
            "empty_list": {"head": "EMPTY", "publish_as_model": []},
        }
    )
    store = make_store(tmp_path, doc)

    assert store.plan(inp(model="gpt-6-sol", requested_model="gpt-6-sol", virtual_profile="eni")).profile == "eni"
    assert store.plan(
        inp(model="claude-haiku-4-5-20251001", requested_model="claude-haiku", virtual_profile="eni")
    ).profile == "eni"

    with pytest.raises(apierr.ApiError) as refused:
        store.plan(inp(model="claude-opus-5-5", requested_model="claude-opus-5-5", virtual_profile="eni"))
    assert (refused.value.status, refused.value.code, refused.value.param) == (404, "not_found_error", "model")
    assert "eni" in refused.value.message

    assert store.plan(inp(model="claude-opus-5-5", requested_model="claude-opus-5-5", virtual_profile="open_bool")).profile == "open_bool"
    assert store.plan(inp(model="claude-opus-5-5", requested_model="claude-opus-5-5", virtual_profile="unpublished")).profile == "unpublished"
    assert store.plan(inp(model="claude-opus-5-5", requested_model="claude-opus-5-5", virtual_profile="empty_list")).profile == "empty_list"


def test_http_virtual_profile_rejects_disallowed_model_before_upstream(prompt_relay, tmp_path):
    doc = {
        "version": 1,
        "profiles": {"eni": {"head": "ENI_ONLY", "publish_as_model": ["gpt-6-sol"]}},
        "rules": [],
    }
    (tmp_path / "prompts.json").write_text(json.dumps(doc), encoding="utf-8")
    relay = prompt_relay()

    status, body, _ = relay.post(
        "chat",
        client_request("chat", [{"role": "user", "content": "hi"}], model="claude-opus-5-5@eni", tools=False),
    )
    assert status == 404 and body["error"]["param"] == "model"
    assert relay.posts == []

    status, _, _ = relay.post(
        "chat",
        client_request("chat", [{"role": "user", "content": "hi"}], model="gpt-6-sol@eni", tools=False),
    )
    assert status == 200 and head_of(relay.posts[-1]) == "ENI_ONLY"


# -- HIGH-P3: stop cross-model pin revision upgrades & isolate model pins -----


def test_cross_model_virtual_or_header_profile_does_not_upgrade_legacy_pin(tmp_path):
    store = make_store(tmp_path)
    first = store.plan(inp(conversation_key="conv-1", model="claude-opus-5-5", requested_model="claude-opus-5-5"))
    assert first.legacy and not first.pinned

    doc = document(
        default_profile="broad",
        profiles={
            "broad": {"head": "BROAD_LEAK"},
            "eni": {"head": "ENI_HEAD"},
        },
        rules=[{"name": "hdr", "if": {"header_profile": "*"}, "use": "$header"}],
    )
    store.save(doc, None)

    vplan = store.plan(
        inp(conversation_key="conv-1", model="gpt-6-sol", requested_model="gpt-6-sol", virtual_profile="eni")
    )
    assert vplan.profile == "eni" and vplan.rev == 1

    hplan = store.plan(
        inp(conversation_key="conv-1", model="gpt-5.5", requested_model="gpt-5.5", header_profile="eni")
    )
    assert hplan.profile == "eni" and hplan.rev == 1

    # Original model must remain on LEGACY_REV instead of jumping to rev 1 ("broad").
    assert store.pins()[0]["rev"] == pp.LEGACY_REV
    plain_back = store.plan(inp(conversation_key="conv-1", model="claude-opus-5-5", requested_model="claude-opus-5-5"))
    assert plain_back.legacy and plain_back.pinned


def test_cross_model_virtual_profile_does_not_upgrade_pinned_old_revision(tmp_path):
    v1 = document(profiles={"a": {"head": "A"}}, rules=[{"name": "r", "use": "a"}])
    store = make_store(tmp_path, v1)
    assert store.plan(inp(conversation_key="conv-2", model="claude-opus-5-5", requested_model="claude-opus-5-5")).profile == "a"

    v2 = document(
        profiles={"a": {"head": "A2"}, "b": {"head": "B2"}, "extra": {"head": "EXTRA"}},
        rules=[{"name": "r", "use": "b"}],
    )
    store.save(v2, 1)

    extra_plan = store.plan(
        inp(conversation_key="conv-2", model="gpt-6-sol", requested_model="gpt-6-sol", virtual_profile="extra")
    )
    assert (extra_plan.profile, extra_plan.rev) == ("extra", 2)

    back = store.plan(inp(conversation_key="conv-2", model="claude-opus-5-5", requested_model="claude-opus-5-5"))
    assert (back.profile, back.rev, back.pinned) == ("a", 1, True)


def test_model_switch_in_pinned_conversation_uses_current_rev_after_rule_narrowed(tmp_path):
    v1 = document(profiles={"eni": {"head": "ENI_BROAD"}}, rules=[{"name": "broad", "if": {}, "use": "eni"}])
    store = make_store(tmp_path, v1)
    assert store.plan(inp(conversation_key="conv-3", model="gpt-6-sol", requested_model="gpt-6-sol")).profile == "eni"

    v2 = document(profiles={"eni": {"head": "ENI_ONLY"}}, rules=[])
    store.save(v2, 1)

    switched = store.plan(inp(conversation_key="conv-3", model="claude-opus-5-5", requested_model="claude-opus-5-5"))
    assert switched.legacy and switched.rev == 2 and switched.profile is None
    same_again = store.plan(inp(conversation_key="conv-3", model="gpt-6-sol", requested_model="gpt-6-sol"))
    assert same_again.profile == "eni" and same_again.rev == 1


# -- HIGH-P5: protect unhinted compaction requests ----------------------------


@pytest.mark.parametrize(
    "headers,instruction,expected_system",
    [
        (OPENCODE, OPENCODE2_COMPACT + "\n\nRules.", "OPENCODE_CHAT_SYS"),
        (
            {k: v for k, v in CLAUDE_HEADERS.items() if "request-class" not in k},
            CLAUDE_CODE_INSTR["before the no-tools line"],
            "CLAUDE_UNHINTED_SYS",
        ),
        (HARNESS, DSH_COMPACT, "HARNESS_CHAT_SYS"),
    ],
)
def test_unhinted_compactions_are_protected_from_profiles_and_body_edits(
    prompt_relay, tmp_path, headers, instruction, expected_system
):
    doc = {
        "version": 1,
        "profiles": {
            "persona": {
                "head": "HIJACKED_PERSONA",
                "history": {"user_blocks": [{"op": "replace_text", "find": "summary", "with": "HIJACK"}]},
            }
        },
        "rules": [{"name": "all", "if": {}, "use": "persona"}],
    }
    (tmp_path / "prompts.json").write_text(json.dumps(doc), encoding="utf-8")
    relay = prompt_relay()

    messages = [*WORK, {"role": "assistant", "content": "working"}, {"role": "user", "content": instruction}]
    body = client_request("chat", messages, system=expected_system, tools=True)
    status, _, _ = relay.post("chat", body, headers)
    assert status == 200
    assert head_of(relay.posts[-1]) == expected_system
    sent_json = json.dumps(relay.posts[-1]["messages"], ensure_ascii=False)
    assert "HIJACKED_PERSONA" not in sent_json and "HIJACK" not in sent_json
    assert relay.runtime.prompts.preview(copy.deepcopy(body), "chat", headers)["protected"] is True


def test_unhinted_compactions_keep_client_system_in_legacy_replace_mode(prompt_relay):
    relay = prompt_relay(relay_system_prompt="RELAY_POLICY_TEXT", relay_system_prompt_mode="replace")
    messages = [*WORK, {"role": "assistant", "content": "ok"}, {"role": "user", "content": OPENCODE2_COMPACT}]
    compaction_body = client_request("chat", messages, system="ORIGINAL_OPENCODE_SYS", tools=True)
    assert relay.post("chat", compaction_body, OPENCODE)[0] == 200
    assert head_of(relay.posts[-1]) == "ORIGINAL_OPENCODE_SYS"

    ordinary = client_request("chat", WORK, system="ORIGINAL_OPENCODE_SYS", tools=True)
    assert relay.post("chat", ordinary, OPENCODE)[0] == 200
    assert head_of(relay.posts[-1]) == "RELAY_POLICY_TEXT"

    assert client_context.keeps_client_prompt("client-tools", {"client": "opencode", "client_task": "compaction"}) is True


def test_override_protected_still_overrides_compaction_protection(prompt_relay, tmp_path):
    doc = {
        "version": 1,
        "profiles": {"custom": {"head": "CUSTOM_OVERRIDE\n\n{{client_system}}"}},
        "rules": [{"name": "force", "if": {}, "use": "custom", "override_protected": True}],
    }
    (tmp_path / "prompts.json").write_text(json.dumps(doc), encoding="utf-8")
    relay = prompt_relay()
    messages = [*WORK, {"role": "assistant", "content": "ok"}, {"role": "user", "content": OPENCODE2_COMPACT}]
    body = client_request("chat", messages, system="ORIG_SYS", tools=True)
    assert relay.post("chat", body, OPENCODE)[0] == 200
    assert head_of(relay.posts[-1]) == "CUSTOM_OVERRIDE\n\nORIG_SYS"


def test_restored_legacy_pin_keeps_legacy_across_restart_explicit_virtual_turn(tmp_path):
    state_dir = tmp_path / "state"
    prompts_path = tmp_path / "prompts.json"
    clock = Clock(1000.0)
    store1 = pp.PromptStore({"prompts_file": str(prompts_path), "conversation_state_dir": str(state_dir)}, clock=clock)
    assert store1.plan(inp(conversation_key="early-conv", model="claude-opus-5-5", requested_model="claude-opus-5-5")).legacy
    store1.close()

    write_json(prompts_path, document(default_profile="mine", profiles={"mine": {"head": "MINE"}, "eni": {"head": "ENI"}}))
    store2 = pp.PromptStore({"prompts_file": str(prompts_path), "conversation_state_dir": str(state_dir)}, clock=clock)
    vplan = store2.plan(inp(conversation_key="early-conv", model="gpt-6-sol", requested_model="gpt-6-sol", virtual_profile="eni"))
    assert vplan.profile == "eni"
    assert store2.pins()[0]["rev"] == pp.LEGACY_REV
    store2.close()


# -- HIGH-17: plan() / _note() / _write_pins() locking race -------------------


def test_concurrent_plan_and_write_pins_never_raise_typeerror_or_write_null_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "PINS_WRITE_INTERVAL", 0.0)
    state_dir = tmp_path / "state"
    prompts_path = tmp_path / "prompts.json"
    write_json(prompts_path, document(default_profile="mine", profiles={"mine": {"head": "{{client_system}}"}}))
    clock = Clock(1000.0)
    store = pp.PromptStore({"prompts_file": str(prompts_path), "conversation_state_dir": str(state_dir)}, clock=clock)

    errors = []
    stop = threading.Event()

    def planner(idx):
        try:
            for step in range(40):
                if stop.is_set():
                    break
                clock.advance(0.1)
                plan = store.plan(inp(conversation_key=f"conv-{idx}-{step % 3}", model="claude-opus-5-5"))
                assert plan.profile == "mine" and not plan.legacy
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def flusher():
        try:
            while not stop.is_set():
                store.maybe_reload()
                pins_file = state_dir / pp.PINS_DIR / pp.PINS_FILE
                if pins_file.exists():
                    raw = pins_file.read_text(encoding="utf-8")
                    if raw:
                        data = json.loads(raw)
                        for row in data["pins"].values():
                            if row["rev"] != pp.LEGACY_REV:
                                assert row["profile"] == "mine", row
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=planner, args=(i,)) for i in range(4)]
    writer = threading.Thread(target=flusher)
    writer.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    stop.set()
    writer.join(timeout=15)
    store.close()
    assert errors == []


# -- HIGH-19: budget timeout does not poison _EDIT_MEMO -----------------------


def test_shared_budget_timeout_raises_regex_timeout_and_does_not_poison_edit_memo(monkeypatch):
    deadline = pp.Deadline(budget=0.02, per_operation=1.0)
    with pytest.raises(pp.RegexTimeout) as raised:
        deadline.run(lambda _t: (_ for _ in ()).throw(TimeoutError("budget exhausted")), size=10)
    assert type(raised.value) is pp.RegexTimeout

    calls = [0]

    def simulated_remove(pattern, text, dl):
        calls[0] += 1
        return dl.run(lambda _t: (_ for _ in ()).throw(TimeoutError("budget")), len(text)) if calls[0] == 1 else ("ok", 1)

    monkeypatch.setattr(pp, "regex_remove", simulated_remove)
    token = object()
    snap = SimpleNamespace(_token=token)
    rx = pp.compile_pattern(r"bad", "")

    low_budget_ctx = SimpleNamespace(snapshot=snap, deadline=pp.Deadline(budget=0.02, per_operation=1.0))
    with pytest.raises(pp.RegexTimeout) as memo_exc:
        prompt_body._remove_regex_fn(rx, low_budget_ctx)("sample bad text")
    assert type(memo_exc.value) is pp.RegexTimeout

    healthy_ctx = SimpleNamespace(snapshot=snap, deadline=pp.Deadline(budget=8.0, per_operation=1.0))
    assert prompt_body._remove_regex_fn(rx, healthy_ctx)("sample bad text") == ("ok", len("sample bad text") - 2, 0)


# -- MEDIUM 1: SkillLocations scoping -----------------------------------------


def _skill_prompt(location, *, folder=None):
    skills = (
        "<available_skills>\n"
        "  <skill>\n"
        "    <name>stageshow</name>\n"
        "    <description>Same skill description.</description>\n"
        f"    <location>{location}</location>\n"
        "  </skill>\n"
        "</available_skills>\n"
    )
    if folder is None:
        return "You are an agent.\n\n" + skills
    return skills + f"\n<env>\n  Working directory: {folder}\n</env>\n"


def test_skill_locations_isolated_across_credential_scopes_and_folderless_contexts(tmp_path):
    path = tmp_path / "client-prompts.json"
    mem = SkillLocations(str(path), exists=lambda _p: True)

    p1 = _skill_prompt(r"C:\creds\a\SKILL.md", folder=r"C:\shared")
    p2 = _skill_prompt(r"C:\creds\b\SKILL.md", folder=r"C:\shared")
    assert mem.stabilize(p1, scope="cred-a", conversation="c1") == (p1, [])
    assert mem.stabilize(p2, scope="cred-b", conversation="c2") == (p2, [])

    # Folderless requests are isolated by conversation and skipped when completely anonymous.
    f1 = _skill_prompt(r"C:\one\SKILL.md", folder=None)
    f2 = _skill_prompt(r"C:\two\SKILL.md", folder=None)
    assert mem.stabilize(f1, scope="cred-a", conversation="conv-1") == (f1, [])
    assert mem.stabilize(f2, scope="cred-a", conversation="conv-2") == (f2, [])
    assert mem.stabilize(f2, scope="cred-a", conversation="conv-1")[1] == ["stageshow"]
    assert mem.stabilize(f2, scope="", conversation=None) == (f2, [])

    # Persisted reload preserves scoped entries and ignores legacy unscoped folderless rows.
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["skills"].append(["", "stageshow", "deadbeef00000000", r"C:\stale\SKILL.md", mem._clock()])
    path.write_text(json.dumps(raw), encoding="utf-8")
    reloaded = SkillLocations(str(path), exists=lambda _p: True)
    assert reloaded.stabilize(p2, scope="cred-b", conversation="c2") == (p2, [])
    assert all(k[0] or k[3] for k in reloaded._memory)


def test_http_stabilizer_scopes_by_credential_key(prompt_relay):
    relay = prompt_relay(
        client_keys=[
            {"id": "k1", "key": "sk-proxi-client-one-secret", "enabled": True},
            {"id": "k2", "key": "sk-proxi-client-two-secret", "enabled": True},
        ]
    )
    stabilizer = Stabilizer({})
    stabilizer.skills._exists = lambda _p: True
    relay.runtime.stabilizer = stabilizer

    s1 = _skill_prompt(r"C:\a\SKILL.md", folder=r"C:\shared")
    s2 = _skill_prompt(r"C:\b\SKILL.md", folder=r"C:\shared")
    assert relay.post(
        "chat",
        client_request("chat", [{"role": "user", "content": "hi"}], system=s1),
        {"authorization": "Bearer sk-proxi-client-one-secret", "x-session-affinity": "ses-1"},
    )[0] == 200
    assert r"C:\a\SKILL.md" in head_of(relay.posts[-1])

    assert relay.post(
        "chat",
        client_request("chat", [{"role": "user", "content": "hi"}], system=s2),
        {"authorization": "Bearer sk-proxi-client-two-secret", "x-session-affinity": "ses-1"},
    )[0] == 200
    assert r"C:\b\SKILL.md" in head_of(relay.posts[-1]) and r"C:\a\SKILL.md" not in head_of(relay.posts[-1])


# -- MEDIUM 2: prompt-suggestion inference & body_edits=True preservation -----


def test_headerless_prompt_suggestion_with_tools_never_enters_conversation_index(prompt_relay):
    relay = prompt_relay(prompt_suggestion_model="same")
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 512,
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}],
        "messages": [
            {"role": "user", "content": "Check tests"},
            {"role": "assistant", "content": "All green."},
            {"role": "user", "content": SUGGESTION_PROMPT},
        ],
    }
    mark = relay.mark()
    assert relay.post("chat", body)[0] == 200
    assert not relay.events(mark, "conversation.inferred")
    assert len(relay.runtime.conversations._chains) == 0


def test_prompt_suggestion_preserves_optimization_when_profile_runs_body_edits(prompt_relay, tmp_path):
    doc = {
        "version": 1,
        "profiles": {
            "edit_all": {
                "head": "{{client_system}}",
                "history": {"user_blocks": [drop_tag()]},
            }
        },
        "rules": [{"name": "override", "if": {}, "use": "edit_all", "override_protected": True}],
    }
    (tmp_path / "prompts.json").write_text(json.dumps(doc), encoding="utf-8")
    relay = prompt_relay()

    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 4096,
        "output_config": {"effort": "high"},
        "system": "MAIN_SYSTEM",
        "tools": [{"name": "read", "description": "d", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "keep question\n\n" + REMINDER}]},
            {"role": "assistant", "content": "working"},
            {"role": "user", "content": [{"type": "text", "text": SUGGESTION_PROMPT}]},
        ],
    }
    mark = relay.mark()
    status, _, headers = relay.post("anthropic", body, {"x-session-affinity": "ses-sugg"})
    assert status == 200
    relay.settle(headers)
    sent = relay.posts[-1]
    assert not sent.get("tools")
    assert sent["model"] == "claude-haiku-4-5-20251001"
    assert "Reply with ONLY a natural 2-12 word" in (head_of(sent) or "")
    turn_begin = relay.events(mark, "turn.begin")[-1]["data"]
    assert turn_begin["model"] == "claude-haiku-4-5-20251001"
    joined = "\n".join(
        block.get("text", "")
        for msg in sent["messages"] if msg.get("role") != "system"
        for block in (msg.get("content") if isinstance(msg.get("content"), list) else [{"text": msg.get("content", "")}])
        if isinstance(block, dict)
    )
    assert "<system-reminder>" not in joined and "keep question" in joined and SUGGESTION_PROMPT in joined


# -- MEDIUM 3: Responses reasoning deletion & cache_control preservation ------


@pytest.mark.parametrize(
    "history_items,expect_deleted",
    [
        ([{"role": "user", "content": REMINDER}, {"type": "reasoning", "summary": []}, {"role": "user", "content": "keep"}], True),
        ([{"role": "user", "content": "keep"}, {"type": "reasoning", "summary": []}, {"role": "user", "content": REMINDER}], True),
        ([{"role": "assistant", "content": "a"}, {"role": "user", "content": REMINDER}, {"type": "reasoning", "summary": []}, {"role": "user", "content": "keep"}], True),
        ([{"role": "assistant", "content": "a1"}, {"type": "reasoning", "summary": []}, {"role": "user", "content": REMINDER}, {"role": "assistant", "content": "a2"}], False),
        ([{"role": "user", "content": REMINDER}, {"type": "reasoning", "summary": []}], False),
    ],
)
def test_responses_standalone_reasoning_deletion_rules(history_items, expect_deleted):
    prof = body_profile(user_blocks=[drop_tag()])
    body = {"model": "gpt-6-sol", "input": history_items}
    rewritten, changes = prompt_body.rewrite(body, "responses", prof, FakeSnapshot())
    assert len(changes) == 1
    assert bool(changes[0].get("deleted")) is expect_deleted
    if expect_deleted:
        assert REMINDER not in json.dumps(rewritten)
    else:
        assert changes[0].get("skipped")


def test_user_blocks_emptying_text_block_preserves_cache_control_on_prior_block():
    prof = body_profile(user_blocks=[drop_tag()])
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "actual user prompt"},
                    {"type": "text", "text": REMINDER, "cache_control": {"type": "ephemeral"}},
                ],
            }
        ],
    }
    original = copy.deepcopy(body)
    rewritten, changes = prompt_body.rewrite(body, "anthropic", prof, FakeSnapshot())
    assert body == original
    assert len(changes) == 1
    assert rewritten["messages"][0]["content"] == [
        {"type": "text", "text": "actual user prompt", "cache_control": {"type": "ephemeral"}}
    ]
