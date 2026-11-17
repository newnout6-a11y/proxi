"""Rule matching, the built-in protection and refusals of PromptStore.plan() (plan 2.1, I6, 4.3)."""
import pytest

import apierr
import prompt_policy as pp
from test_prompt_policy_common import document, inp, make_store

PROFILES = {"a": {"head": "A"}, "b": {"head": "B"}, "edit-mid": {"history": {"mid_system": "drop"}},
            "edit-blocks": {"history": {"user_blocks": [{"op": "drop_tag", "tag": "t"}]}},
            "edit-tools": {"tools": {"edits": [{"tool": "x", "hide": True}]}},
            "gen": {"generation": {"effort": "high", "verbosity": "low"}},
            "gen-null": {"generation": {"effort": None, "verbosity": "high"}}}


def doc(rules=(), **extra):
    return document(profiles=PROFILES, rules=list(rules), **extra)


def rule(name, use, **conditions):
    item = {"name": name, "use": use}
    if conditions:
        item["if"] = conditions
    return item


def planned(tmp_path, rules=(), request=None, **extra):
    store = make_store(tmp_path, doc(rules, **extra))
    return store.plan(request or inp())


# -- order -----------------------------------------------------------------------------------------------------------


def test_the_first_enabled_matching_rule_wins(tmp_path):
    plan = planned(tmp_path, [rule("first", "a", client="opencode"), rule("second", "b")])
    assert (plan.profile, plan.rule) == ("a", "first")
    plan = planned(tmp_path, [{**rule("off", "a"), "enabled": False}, rule("second", "b")])
    assert (plan.profile, plan.rule) == ("b", "second")
    plan = planned(tmp_path, [rule("miss", "a", client="codex"), rule("hit", "b", client="opencode")])
    assert (plan.profile, plan.rule) == ("b", "hit")


def test_a_rule_without_conditions_matches_everything(tmp_path):
    assert planned(tmp_path, [rule("all", "a")], inp(client="codex", model="x", tools=False)).profile == "a"


def test_nothing_matching_and_no_default_is_the_legacy_path(tmp_path):
    plan = planned(tmp_path, [rule("codex", "a", client="codex")])
    assert plan.legacy and plan.profile is None and plan.rule is None and not plan.protected
    assert plan.rev == 1 and plan.snapshot is not None and not plan.body_edits and plan.generation == {}


def test_the_default_profile_takes_what_no_rule_caught(tmp_path):
    plan = planned(tmp_path, [rule("codex", "a", client="codex")], default_profile="b")
    assert (plan.profile, plan.rule) == ("b", "default profile") and not plan.protected
    assert planned(tmp_path / "x", [rule("any", "a")], default_profile="b").profile == "a"


def test_the_legacy_use_keeps_the_old_path_and_names_the_rule(tmp_path):
    plan = planned(tmp_path, [rule("hands off", "$legacy", client="opencode"), rule("later", "a")])
    assert plan.legacy and plan.rule == "hands off" and plan.rev == 1


def test_the_default_profile_may_be_the_builtin_passthrough(tmp_path):
    plan = planned(tmp_path, default_profile="passthrough")
    assert plan.profile == "passthrough" and not plan.protected and not plan.body_edits


# -- the built-in protection of service requests (I6) -------------------------------------------------------------------


@pytest.mark.parametrize("fields, name", [
    ({"purpose": "session-title"}, "protected: service requests"),
    ({"purpose": "auto-mode-check"}, "protected: service requests"),
    ({"purpose": "connection-probe"}, "protected: service requests"),
    ({"purpose": "client-service"}, "protected: service requests"),
    ({"request_class": "compaction"}, "protected: Claude Code compaction"),
    ({"request_class": "auxiliary"}, "protected: Claude Code compaction"),
])
def test_service_requests_are_kept_on_passthrough(tmp_path, fields, name):
    plan = planned(tmp_path, [rule("everything", "a")], inp(**fields))
    assert (plan.profile, plan.rule, plan.protected) == ("passthrough", name, True)
    assert not plan.body_edits and plan.generation == {}


def test_other_purposes_are_not_protected(tmp_path):
    for purpose in ("client-tools", "client-text", "generation"):
        plan = planned(tmp_path / purpose, [rule("everything", "a")], inp(purpose=purpose))
        assert plan.profile == "a" and not plan.protected
    assert planned(tmp_path / "rc", [rule("everything", "a")], inp(request_class="memory")).profile == "a"


def test_protection_can_be_switched_off_for_the_whole_document(tmp_path):
    plan = planned(tmp_path, [rule("everything", "a")], inp(purpose="session-title"), protected={"enabled": False})
    assert plan.profile == "a" and not plan.protected
    plan = planned(tmp_path / "on", [rule("everything", "a")], inp(purpose="session-title"), protected={"enabled": True})
    assert plan.protected


def test_a_rule_can_override_the_protection_by_itself(tmp_path):
    rules = [{**rule("title mine", "b", purpose="session-title"), "override_protected": True},
             rule("everything", "a")]
    plan = planned(tmp_path, rules, inp(purpose="session-title"))
    assert (plan.profile, plan.rule, plan.protected) == ("b", "title mine", False)
    plan = planned(tmp_path / "other", rules, inp(purpose="auto-mode-check"))
    assert (plan.profile, plan.protected) == ("passthrough", True)
    plan = planned(tmp_path / "plain", rules, inp())
    assert plan.profile == "a"


def test_override_rules_are_judged_before_every_other_rule_and_in_their_own_order(tmp_path):
    rules = [rule("early", "a"), {**rule("late override", "b"), "override_protected": True},
             {**rule("later override", "a"), "override_protected": True}]
    plan = planned(tmp_path, rules, inp())
    assert plan.rule == "late override"
    plan = planned(tmp_path / "x", rules, inp(purpose="session-title"))
    assert plan.rule == "late override" and not plan.protected


def test_a_disabled_override_rule_does_nothing(tmp_path):
    rules = [{**rule("off", "b"), "override_protected": True, "enabled": False}]
    assert planned(tmp_path, rules, inp(purpose="session-title")).protected


# -- virtual profiles and the header ---------------------------------------------------------------------------------


def test_a_virtual_profile_is_chosen_before_the_users_rules(tmp_path):
    plan = planned(tmp_path, [rule("everything", "a")], inp(virtual_profile="b", requested_model="m"))
    assert (plan.profile, plan.rule, plan.protected) == ("b", "virtual model", False)


def test_protection_goes_before_the_virtual_profile(tmp_path):
    plan = planned(tmp_path, [], inp(virtual_profile="b", purpose="session-title"))
    assert plan.profile == "passthrough" and plan.protected


def test_an_override_rule_goes_before_the_virtual_profile(tmp_path):
    rules = [{**rule("o", "a"), "override_protected": True}]
    assert planned(tmp_path, rules, inp(virtual_profile="b")).profile == "a"


def test_a_virtual_profile_that_does_not_exist_is_a_404_naming_it(tmp_path):
    store = make_store(tmp_path, doc())
    with pytest.raises(apierr.ApiError) as raised:
        store.plan(inp(virtual_profile="ghost", requested_model="claude-opus-5-5"))
    error = raised.value
    assert (error.status, error.code, error.param) == (404, "not_found_error", "model")
    assert "ghost" in error.message and "claude-opus-5-5@ghost" in error.message


def test_a_virtual_profile_without_a_prompts_file_is_a_404_too(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(apierr.ApiError) as raised:
        store.plan(inp(virtual_profile="mine", conversation_key="k"))
    assert raised.value.status == 404 and "mine" in raised.value.message and "no prompts file" in raised.value.message
    assert raised.value.param == "model"
    assert store.pins() == [] or all(item["rev"] == pp.LEGACY_REV for item in store.pins())


def test_the_virtual_rule_can_also_be_placed_by_the_user(tmp_path):
    plan = planned(tmp_path, [rule("virtual first", "$virtual", model="gpt-*")], inp(model="gpt-6-sol", virtual_profile="b"))
    assert plan.profile == "b"
    plan = planned(tmp_path / "none", [rule("virtual first", "$virtual"), rule("rest", "a")], inp())
    assert (plan.profile, plan.rule) == ("a", "rest")


def test_the_header_profile_is_used_by_a_header_rule(tmp_path):
    rules = [rule("by header", "$header", header_profile="*"), rule("rest", "a")]
    plan = planned(tmp_path, rules, inp(header_profile="b"))
    assert (plan.profile, plan.rule) == ("b", "by header")
    assert planned(tmp_path / "a", rules, inp()).profile == "a"        # no header: the rule never matches
    plan = planned(tmp_path / "pass", rules, inp(header_profile="passthrough"))
    assert plan.profile == "passthrough" and not plan.protected


def test_a_header_rule_without_a_header_condition_is_skipped_without_one(tmp_path):
    plan = planned(tmp_path, [rule("blind", "$header", client="opencode"), rule("rest", "a")], inp())
    assert plan.rule == "rest"


def test_a_header_that_names_no_profile_is_a_400(tmp_path):
    store = make_store(tmp_path, doc([rule("by header", "$header", header_profile="*")]))
    with pytest.raises(apierr.ApiError) as raised:
        store.plan(inp(header_profile="ghost"))
    error = raised.value
    assert (error.status, error.code, error.param) == (400, "invalid_request_error", "x-proxi-profile")
    assert "ghost" in error.message
    with pytest.raises(apierr.ApiError):
        store.plan(inp(header_profile="x" * 500))
    try:
        store.plan(inp(header_profile="x" * 500))
    except apierr.ApiError as long_error:
        assert len(long_error.message) < 400


def test_a_header_without_a_header_rule_is_ignored(tmp_path):
    plan = planned(tmp_path, [rule("rest", "a")], inp(header_profile="ghost"))
    assert plan.profile == "a"


def test_no_file_means_no_refusal_for_a_header(tmp_path):
    store = make_store(tmp_path)
    assert store.plan(inp(header_profile="ghost")).legacy


# -- conditions ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("conditions, request_fields, expected", [
    ({"client": "opencode"}, {}, True), ({"client": "OpenCode"}, {}, True), ({"client": "claude-*"}, {}, False),
    ({"client": ["codex", "opencode"]}, {}, True), ({"client": ["codex", "harness"]}, {}, False),
    ({"agent": "build"}, {}, True), ({"agent": "bui?d"}, {}, True), ({"agent": "[a-c]uild"}, {}, True),
    ({"agent": "plan"}, {}, False), ({"agent": "*"}, {"agent": ""}, False), ({"agent": "*"}, {}, True),
    ({"purpose": "client-*"}, {}, True), ({"purpose": "generation"}, {}, False),
    ({"request_class": "memory*"}, {"request_class": "memory-recall"}, True),
    ({"request_class": "*"}, {}, False),
    ({"model": "claude-opus-*"}, {}, True), ({"model": "CLAUDE-OPUS-5-5"}, {}, True), ({"model": "gpt-*"}, {}, False),
    ({"model": "gpt-*"}, {"model": "claude-opus-5-5", "requested_model": "gpt-6-sol"}, True),
    ({"model": "claude-opus-5-5#high"}, {"requested_model": "claude-opus-5-5#high"}, True),
    ({"tools": True}, {}, True), ({"tools": False}, {}, False), ({"tools": False}, {"tools": False}, True),
    ({"structured": True}, {"structured": True}, True), ({"structured": True}, {}, False),
    ({"has_client_system": False}, {"has_client_system": False}, True), ({"has_client_system": False}, {}, False),
    ({"dialect": "chat"}, {}, True), ({"dialect": ["responses", "anthropic"]}, {}, False),
    ({"header_profile": "*"}, {}, False), ({"header_profile": "*"}, {"header_profile": "b"}, True),
    ({"header_profile": "b"}, {"header_profile": "B"}, True), ({"header_profile": "x"}, {"header_profile": "b"}, False),

    # all the fields of a rule must hold, each list is an OR
    ({"client": "opencode", "tools": True, "model": ["gpt-*", "claude-*"]}, {}, True),
    ({"client": "opencode", "tools": False}, {}, False),
    ({"client": ["codex", "opencode"], "agent": ["plan", "build"], "dialect": "chat"}, {}, True),
])
def test_conditions(tmp_path, conditions, request_fields, expected):
    store = make_store(tmp_path, doc([{"name": "r", "use": "a", "if": conditions}]))
    plan = store.plan(inp(**request_fields))
    assert (plan.rule == "r") is expected


def test_the_virtual_profile_condition_needs_a_virtual_profile(tmp_path):
    rules = [{"name": "o", "use": "a", "override_protected": True, "if": {"virtual_profile": "b*"}},
             {"name": "any", "use": "a", "override_protected": True, "if": {"virtual_profile": "*"}}]
    store = make_store(tmp_path, doc(rules))
    assert store.plan(inp()).legacy                                    # an absent virtual profile matches nothing
    assert store.plan(inp(virtual_profile="b")).rule == "o"            # an override rule goes before the implicit one
    assert store.plan(inp(virtual_profile="a")).rule == "any"


def test_the_literal_forms_of_a_glob(tmp_path):
    store = make_store(tmp_path, doc([rule("dots", "a", model="gpt-5.5*")]))
    assert store.plan(inp(model="gpt-5.5-mini")).profile == "a"
    assert store.plan(inp(model="gpt-5x5")).legacy        # a dot in a glob is only a dot


# -- what the plan carries --------------------------------------------------------------------------------------------


def test_body_edits_say_whether_history_or_tools_are_rewritten(tmp_path):
    for name, expected in (("a", False), ("edit-mid", True), ("edit-blocks", True), ("edit-tools", True),
                           ("gen", False), ("passthrough", False)):
        store = make_store(tmp_path / name, doc([rule("r", name)]))
        assert store.plan(inp()).body_edits is expected, name


def test_mid_system_keep_is_not_a_body_edit(tmp_path):
    store = make_store(tmp_path, document(profiles={"k": {"history": {"mid_system": "keep"}}}, default_profile="k"))
    assert not store.plan(inp()).body_edits


def test_generation_has_only_the_values_the_profile_sets(tmp_path):
    store = make_store(tmp_path, doc([rule("g", "gen", model="gpt-*"), rule("n", "gen-null", model="claude-*")]))
    assert store.plan(inp(model="gpt-6")).generation == {"effort": "high", "verbosity": "low"}
    assert store.plan(inp(model="claude-opus-5-5")).generation == {"verbosity": "high"}
    first = store.plan(inp(model="gpt-6")).generation
    first["effort"] = "none"
    assert store.plan(inp(model="gpt-6")).generation["effort"] == "high"


def test_a_plan_knows_its_revision_snapshot_and_input(tmp_path):
    store = make_store(tmp_path, doc([rule("r", "a")]))
    request = inp()
    plan = store.plan(request)
    assert plan.rev == 1 and plan.snapshot is store.snapshot() and plan.input is request
    assert not plan.pinned and not plan.legacy


def test_no_file_gives_a_legacy_plan_without_a_snapshot(tmp_path):
    plan = make_store(tmp_path).plan(inp())
    assert plan == pp.Plan() and plan.legacy and plan.rev is None and plan.snapshot is None
