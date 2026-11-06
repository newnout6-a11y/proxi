"""The prompt control admin API, part 1: who may read it and what GET /admin/api/prompts reports (plan 4.1).

runtime.prompts is a fake that follows the PromptStore contract (admin_prompts_harness), so these tests pin what the
admin layer does with it. The test at the end uses the real store.
"""
import pytest

import admin
import context
import models
import native_history
import orchestrator
import prompt_policy
import structured
from admin_prompts_harness import DOC, api, check_needs_the_key, get, send  # noqa: F401 - api is a fixture
from test_admin import admin_relay  # noqa: F401 - the relay the api fixture builds on


# -- who may call it ------------------------------------------------------------------------------
@pytest.mark.parametrize("key", [None, "incorrect-key"])
def test_the_state_needs_the_relay_key(api, key):
    check_needs_the_key(api, "GET", "/admin/api/prompts", None, key)


def test_the_key_in_the_url_is_not_a_login(api):
    assert get(api, "?key=private-relay-key", key=None)[0] == 401


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_the_state_route_does_not_take_these_methods(api, method):
    status, body, _ = send(api, "/admin/api/prompts", {"x": 1} if method != "DELETE" else None, method=method,
                           headers={"x-admin-request": "1", "origin": api.url}, attempts=4)
    assert status == 405 and body["error"]["code"] == "method_not_allowed"
    assert api.store.calls == []


@pytest.mark.parametrize("route", ["/admin/api/prompts/", "/admin/api/prompts/other", "/admin/api/prompts/recent/1",
                                   "/admin/api/prompt", "/admin/api/promptsx"])
def test_other_paths_near_prompts_are_not_found(api, route):
    assert api.request(route)[0] == 404


def test_the_state_carries_the_admin_security_headers_and_no_cors(api):
    status, _, headers = get(api)
    assert status == 200
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY" and "access-control-allow-origin" not in headers


# -- GET /admin/api/prompts -----------------------------------------------------------------------
def test_without_a_file_the_state_is_empty_and_says_where_the_file_would_be(api):
    status, body, _ = get(api)
    assert status == 200
    assert body["document"] is None and body["rev"] is None and body["errors"] == [] and body["warnings"] == []
    assert body["path"] == str(api.store.path)
    assert set(body) == {"document", "rev", "path", "errors", "warnings", "legacy", "builtin"}
    assert api.store.called("maybe_reload")  # a file edited by hand is picked up when the screen asks


def test_the_state_carries_the_document_its_revision_and_the_errors_that_kept_it(api):
    api.store.hold(DOC, rev=7)
    api.store.problems = [{"message": "invalid JSON at line 3", "param": "profiles", "ts": 12.5}]
    status, body, _ = get(api)
    assert status == 200 and body["document"] == DOC and body["rev"] == 7
    assert body["errors"] == [{"message": "invalid JSON at line 3", "param": "profiles", "ts": 12.5}]


@pytest.mark.parametrize("role,chars", [(None, len(context.DEFAULT_RELAY_SYSTEM_PROMPT)), ("", 0), (" \n\t ", 0),
                                        ("ж" * 10, 10), ("Роль: инженер.", 14)])
@pytest.mark.parametrize("mode", ["standalone", "append", "replace"])
def test_legacy_names_the_mode_and_the_length_of_the_role_that_applies(api, mode, role, chars):
    api.cfg.update(relay_system_prompt_mode=mode, relay_system_prompt=role)
    assert get(api)[1]["legacy"] == {"mode": mode, "role_chars": chars}


def test_the_builtin_texts_are_the_texts_the_relay_really_uses(api):
    texts = get(api)[1]["builtin"]["relay_texts"]
    assert set(texts) == set(prompt_policy.RELAY_TEXT_NAMES)
    assert texts["role"] == context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert texts["native_role"] == orchestrator.DEFAULT_NATIVE_TOOLS_ROLE
    assert texts["reminder"] == "<system-reminder>\n{{text}}\n</system-reminder>"
    # The very wrapper the relay puts around a system message that arrives mid-dialog.
    assert native_history.reminder("A MID-DIALOG RULE") == texts["reminder"].replace("{{text}}", "A MID-DIALOG RULE")
    assert texts["structured"] == "{{default}}"  # null in prompts.json is this wrapper: the default contract as is


def test_the_builtin_codex_text_is_the_one_codex_models_publishes(api):
    published = api.request("/codex/models")[1]["models"]
    assert published and {item["base_instructions"] for item in published} == {item["model_messages"][
        "instructions_template"] for item in published}
    assert get(api)[1]["builtin"]["relay_texts"]["codex_base"] == published[0]["base_instructions"] != ""


def test_the_builtin_structured_example_is_the_contract_function_called_not_a_copy(api):
    builtin = get(api)[1]["builtin"]
    assert builtin["structured_example"] == structured.instructions(admin.STRUCTURED_EXAMPLE)
    assert builtin["structured_example"].startswith("[STRUCTURED OUTPUT FORMAT]\n")


def test_the_builtin_vocabularies_are_the_constants_of_the_engine(api):
    builtin = get(api)[1]["builtin"]
    assert builtin["variables"] == list(prompt_policy.VARIABLES)
    assert builtin["relay_task_variables"] == list(prompt_policy.RELAY_TASK_VARIABLES)
    assert set(builtin["relay_task_variables"]) <= set(builtin["variables"])
    assert builtin["protected_rules"] == list(prompt_policy.PROTECTED_RULES)
    assert builtin["edit_ops"] == {"client_system": list(prompt_policy.SYSTEM_EDIT_OPS),
                                   "mid_system": list(prompt_policy.MID_SYSTEM_MODES),
                                   "user_blocks": list(prompt_policy.USER_BLOCK_OPS),
                                   "tools": list(prompt_policy.TOOL_EDIT_OPS)}
    assert builtin["special_uses"] == list(prompt_policy.SPECIAL_USES)
    assert builtin["profiles"] == ["passthrough"] == list(prompt_policy.BUILTIN_PROFILES)
    assert builtin["limits"] == {"max_text_bytes": prompt_policy.MAX_TEXT_BYTES,
                                 "max_document_bytes": prompt_policy.MAX_DOCUMENT_BYTES}
    assert builtin["profile_name"] == prompt_policy.PROFILE_NAME.pattern


def test_the_builtin_conditions_name_every_condition_with_its_kind_and_known_values(api):
    conditions = get(api)[1]["builtin"]["conditions"]
    assert list(conditions) == list(prompt_policy.CONDITIONS)
    assert all(conditions[name] == {"type": "boolean"} for name in ("tools", "structured", "has_client_system"))
    assert conditions["client"] == {"type": "string", "glob": True, "values": list(prompt_policy.CLIENTS)}
    assert conditions["dialect"]["values"] == ["chat", "responses", "anthropic"]
    assert conditions["purpose"]["values"] == list(admin.PURPOSES)
    for name in ("agent", "request_class", "model", "header_profile", "virtual_profile"):
        assert conditions[name] == {"type": "string", "glob": True}


def test_every_purpose_a_rule_can_name_is_one_the_relay_can_produce():
    # request_kind() and the service kinds are the relay's own vocabulary; client-service is the prompt layer's.
    import client_context
    known = {"client-tools", "client-text", "generation", "client-service"} | set(client_context.SERVICE_KINDS)
    assert set(admin.PURPOSES) == known
    protected = {purpose for rule in prompt_policy.PROTECTED_RULES for purpose in rule["if"].get("purpose", [])}
    assert protected <= set(admin.PURPOSES)


def test_the_builtin_block_is_a_copy_nobody_can_use_to_change_the_engine():
    first = admin._builtin_prompts()
    first["protected_rules"][0]["use"] = "mine"
    first["variables"].append("extra")
    again = admin._builtin_prompts()
    assert again["protected_rules"][0]["use"] == "passthrough" and "extra" not in again["variables"]
    assert prompt_policy.PROTECTED_RULES[0]["use"] == "passthrough"


def test_a_codex_catalog_that_gives_nothing_leaves_the_text_empty_instead_of_failing_the_screen(api, monkeypatch):
    monkeypatch.setattr(models, "codex_catalog", lambda *args, **kwargs: {"models": []})
    assert get(api)[1]["builtin"]["relay_texts"]["codex_base"] == ""
    monkeypatch.setattr(models, "catalog", lambda: [])
    assert get(api)[0] == 200


def test_the_state_warns_when_the_document_lowers_the_protection_of_service_requests(api):
    api.store.hold({"version": 1, "protected": {"enabled": False}})
    warnings = get(api)[1]["warnings"]
    assert len(warnings) == 1 and "protected.enabled" in warnings[0]
    rule = {"name": "всё мне", "if": {"client": "*"}, "use": "mine", "override_protected": True}
    api.store.hold({**DOC, "rules": [rule, {"if": {}, "use": "mine", "override_protected": True}]}, rev=2)
    warnings = get(api)[1]["warnings"]
    assert len(warnings) == 2 and "«всё мне»" in warnings[0] and "№2" in warnings[1]
    assert all("override_protected" in warning for warning in warnings)
    api.store.hold(DOC, rev=3)
    assert get(api)[1]["warnings"] == []


def test_the_real_store_starts_without_a_revision_and_reports_where_its_file_goes(admin_relay):
    state = get(admin_relay)[1]
    assert state["document"] is None and state["rev"] is None and state["errors"] == []
    assert state["path"] == str(admin_relay.path.parent / "prompts.json") == str(admin_relay.runtime.prompts.path)
