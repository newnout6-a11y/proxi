"""Admin settings and pool conversion: provider access, role budget, journal hygiene, honest route reasons."""
import hashlib
import json
from types import SimpleNamespace

import pytest

import admin
import config
import context
import journal
import models
import orchestrator
from session import Backend, SessionPool
from test_admin import admin_relay  # noqa: F401 - single-account fixture
from test_admin_pool import NEW, pool  # noqa: F401 - two-account relay fixture

RETENTION = {"anthropic": {"resource_name": "private-resource", "data_retention_enabled": True}}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("RETOOL_BASE", "RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_PAGE_UUID", "RETOOL_THREAD_ID",
                 "RELAY_SESSION_MODE", "RELAY_API_KEY", "RELAY_HOST", "RELAY_PORT", "RELAY_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)


def post_account(relay):
    return relay.request("/admin/api/accounts", NEW, method="POST",
                         headers={"x-admin-request": "1", "origin": relay.url})


def offline_pool(cfg):
    return SessionPool(Backend(cfg, http_post=lambda *a, **k: None, ws_connect=lambda *a, **k: None))


# -- provider_access stays with its account ------------------------------------------
def test_single_account_gate_moves_into_the_first_pool_record_and_keeps_gated_models(admin_relay):
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    saved["provider_access"] = RETENTION
    admin_relay.path.write_text(json.dumps(saved), encoding="utf-8")
    fable = models.resolve("claude-fable-5")[0]
    single = config.load(str(admin_relay.path))
    assert models.catalog_routable(fable, single, offline_pool(single).backends)
    assert single["config_warnings"] == []  # without accounts[] the root is the source of the descriptor

    assert "provider_access" in admin.ACCOUNT_RECORD_KEYS
    status, _, _ = post_account(admin_relay)
    assert status == 200
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    first, second = saved["accounts"]
    assert first["provider_access"] == RETENTION and "provider_access" not in second
    # With accounts[] a root copy is ignored and warned about at every start: the descriptor moves, it is not copied.
    assert "provider_access" not in saved
    # The pool reads the descriptor only from the account record: the gated model must survive the conversion.
    reloaded = config.load(str(admin_relay.path))
    assert reloaded["config_warnings"] == []
    backends = offline_pool(reloaded).backends
    assert [SessionPool._routable(backend, fable) for backend in backends] == [True, False]
    assert models.catalog_routable(fable, reloaded, backends)


@pytest.mark.parametrize("root_copy", [True, False])
@pytest.mark.parametrize("second_gate", [{"anthropic": {"resource_name": "resource-acc-b", "byok": True}}, None])
def test_deleting_the_first_account_leaves_no_descriptor_in_the_root(pool, second_gate, root_copy):
    first_gate = {"anthropic": {"resource_name": "resource-acc-a", "data_retention_enabled": True}}
    saved = pool.saved()
    saved["accounts"][0]["provider_access"] = first_gate
    if root_copy:
        saved["provider_access"] = first_gate  # left by a conversion made before the root copy was dropped
    if second_gate:
        saved["accounts"][1]["provider_access"] = second_gate
    pool.path.write_text(json.dumps(saved), encoding="utf-8")
    assert pool.call("/admin/api/accounts/" + pool.tags[0], {}, method="DELETE")[0] == 200
    saved = pool.saved()
    assert [item.get("provider_access") for item in saved["accounts"]] == [second_gate]
    # The new first account keeps its own descriptor; mirrored into the root it would be warned about at every start.
    assert "provider_access" not in saved and "resource-acc-a" not in json.dumps(saved)
    reloaded = config.load(str(pool.path))
    assert reloaded["accounts"][0].get("provider_access") == second_gate
    assert reloaded["config_warnings"] == []


# -- both built-in roles -------------------------------------------------------------
def test_settings_publish_every_built_in_role_with_the_path_it_serves(admin_relay):
    _, body, _ = admin_relay.request("/admin/api/settings")
    roles = {item["id"]: item for item in body["default_system_prompts"]}
    assert set(roles) == {"relay", "native_tools"}
    assert body["default_system_prompt"] == roles["relay"]["text"] == context.DEFAULT_RELAY_SYSTEM_PROMPT
    assert roles["native_tools"]["text"] == orchestrator.DEFAULT_NATIVE_TOOLS_ROLE
    assert roles["relay"]["text"] != roles["native_tools"]["text"]
    assert all(item["title"] and item["applies_to"] for item in roles.values())
    assert "multiline" not in body["schema"]["relay_system_prompt"]


def test_published_roles_are_the_texts_the_relay_really_sends(tmp_path):
    texts = {item["id"]: item["text"] for item in admin._default_roles()}
    plain = context.build_system_instructions(str(tmp_path), enable_context=False, enable_local_exec=False)
    assert plain == texts["relay"]
    runner = SimpleNamespace(cfg={"relay_system_prompt": None})
    assert orchestrator.TurnRunner._instructions(runner, native_tools=True) == texts["native_tools"]
    runner.cfg["relay_system_prompt"] = "custom role"
    assert orchestrator.TurnRunner._instructions(runner, native_tools=True) == "custom role"


# -- role budget ---------------------------------------------------------------------
def test_role_is_bounded_in_bytes_and_by_max_prompt_bytes(admin_relay):
    cap = admin.ROLE_MAX_BYTES
    schema = admin_relay.request("/admin/api/settings")[1]["schema"]["relay_system_prompt"]
    assert (schema["max_bytes"], schema["max_prompt_share"]) == (cap, admin.ROLE_PROMPT_SHARE)
    before = admin_relay.path.read_bytes()
    status, body, _ = admin_relay.patch({"relay_system_prompt": "ж" * (cap // 2 + 1)})  # two bytes per character
    assert status == 400 and body["error"]["param"] == "relay_system_prompt" and str(cap) in body["error"]["message"]
    assert admin_relay.path.read_bytes() == before
    assert admin_relay.patch({"relay_system_prompt": "ж" * (cap // 2)})[0] == 200

    # With a prompt cap the role may take half of it, judged on the pair that would be saved.
    assert admin_relay.patch({"max_prompt_bytes": 100_000})[0] == 200
    status, body, _ = admin_relay.patch({"max_prompt_bytes": 40_000})  # the saved role no longer fits
    assert status == 400 and body["error"]["param"] == "max_prompt_bytes"
    assert admin_relay.patch({"relay_system_prompt": "x" * 20_001, "max_prompt_bytes": 40_000})[0] == 400
    assert admin_relay.patch({"relay_system_prompt": "x" * 20_000, "max_prompt_bytes": 40_000})[0] == 200
    assert admin_relay.patch({"max_prompt_bytes": 0})[0] == 200
    assert json.loads(admin_relay.path.read_text(encoding="utf-8"))["max_prompt_bytes"] == 0


def test_an_old_over_long_role_does_not_block_other_settings(admin_relay):
    saved = json.loads(admin_relay.path.read_text(encoding="utf-8"))
    saved["relay_system_prompt"] = "x" * (admin.ROLE_MAX_BYTES + 1000)  # hand-edited
    admin_relay.path.write_text(json.dumps(saved), encoding="utf-8")
    assert admin_relay.patch({"show_thinking": False})[0] == 200
    assert admin_relay.patch({"max_prompt_bytes": 1_000_000})[0] == 400
    assert admin_relay.patch({"relay_system_prompt": None})[0] == 200
    assert admin_relay.patch({"max_prompt_bytes": 1_000_000})[0] == 200


@pytest.mark.parametrize("role", ["\ud800", "role \udfff end", "half a pair \ud83d"])
def test_a_role_with_an_unpaired_surrogate_is_a_400_that_names_the_cause(admin_relay, role):
    before = admin_relay.path.read_bytes()
    status, body, _ = admin_relay.patch({"relay_system_prompt": role})  # "\ud800" is valid JSON, but not UTF-8 text
    error = body["error"]
    assert status == 400 and error["code"] == "invalid_request_error" and error["param"] == "relay_system_prompt"
    assert "surrogate" in error["message"] and "codec" not in error["message"]
    assert admin_relay.path.read_bytes() == before
    # A complete pair (written as two escapes) is an ordinary character.
    assert admin_relay.patch({"relay_system_prompt": "complete pair \U0001F600"})[0] == 200


# -- journal -------------------------------------------------------------------------
def test_journal_keeps_the_role_text_out_and_records_length_and_hash(admin_relay):
    journal.JOURNAL.reset()
    role = "Приватное правило: " + "x" * 5000  # longer than the journal's own string limit
    assert admin_relay.patch({"relay_system_prompt": role})[0] == 200
    assert admin_relay.patch({"relay_system_prompt": "Второе правило", "rate_limit_rps": 2})[0] == 200
    first, second = [event for event in journal.JOURNAL.query(order="asc") if event["kind"] == "admin.settings"]

    def digest(text):
        return {"length": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]}

    assert first["data"]["changed"]["relay_system_prompt"] == {"from": None, "to": digest(role)}
    changed = second["data"]["changed"]
    assert changed["relay_system_prompt"] == {"from": digest(role), "to": digest("Второе правило")}
    assert changed["rate_limit_rps"] == {"from": 0.0, "to": 2.0}  # ordinary values stay readable
    encoded = json.dumps(journal.JOURNAL.query(), ensure_ascii=False)
    assert "Приватное правило" not in encoded and "Второе правило" not in encoded


def test_only_free_text_settings_are_summarised(admin_relay):
    adm = admin_relay.runtime.admin
    assert [key for key, field in adm.schema.items() if admin._free_text(field)] == ["relay_system_prompt"]
    adm.schema["note"] = {"type": "string"}  # a future free-text setting follows the same rule
    assert adm._loggable("note", "abc") == {"length": 3, "sha256": hashlib.sha256(b"abc").hexdigest()[:12]}
    assert adm._loggable("relay_system_prompt", None) is None
    assert adm._loggable("model", "claude-opus-5-5") == "claude-opus-5-5"
    assert adm._loggable("max_workers", 8) == 8


# -- why a model is hidden -----------------------------------------------------------
def test_models_api_names_the_real_reason_a_model_is_hidden(admin_relay):
    _, body, _ = admin_relay.request("/admin/api/models")
    by_id = {item["id"]: item for item in body["data"]}
    shown = by_id["claude-opus-5-5"]
    assert shown["routable"] is True and "route_blocker" not in shown
    assert (shown["route_status"], shown["route_reason"]) == ("verified", models.resolve("claude-opus-5-5")[0]["route_reason"])

    # A verified route, but the only account has no OpenAI resource: the catalog's own reason would mislead.
    hidden = by_id["gpt-6-sol"]
    assert hidden["routable"] is False and hidden["route_blocker"] == "no_resource"
    assert hidden["route_status"] == "unrouted" and "Synthetic" not in hidden["route_reason"]
    assert "openai_provider_resource_name" in hidden["route_reason"]
    assert hidden["catalog_route_status"] == "verified" and "Synthetic" in hidden["catalog_route_reason"]

    gated = by_id["claude-fable-5"]
    assert (gated["route_blocker"], gated["route_status"]) == ("blocked", "gated")
    assert "retention" in gated["route_reason"] and "catalog_route_status" not in gated
    unsupported = by_id["gpt-5.3-codex"]
    assert (unsupported["route_blocker"], unsupported["route_status"]) == ("blocked", "unsupported")
    assert unsupported["route_reason"] == models.resolve("gpt-5.3-codex")[0]["route_reason"]


def test_reason_is_about_the_enabled_accounts_not_a_switched_off_one(pool):
    second = pool.backends[1]
    second.cfg["openai_provider_resource_name"] = "second-openai"
    openai = lambda: {item["id"]: item for item in pool.call("/admin/api/models")[1]["data"]}["gpt-6-sol"]  # noqa: E731
    assert openai()["routable"] is True
    assert pool.call("/admin/api/accounts/" + pool.tags[1], {"enabled": False}, method="PATCH")[0] == 200
    hidden = openai()
    assert hidden["routable"] is False and hidden["route_blocker"] == "no_resource"
    assert "No enabled account" in hidden["route_reason"]
    pool.backends[0].cfg["enabled"] = False
    nobody = openai()
    assert nobody["route_blocker"] == "no_accounts" and nobody["route_status"] == "unrouted"


def test_reason_follows_the_account_list_discovery_is_judged_against(pool, monkeypatch):
    pool.backends[1].cfg["openai_provider_resource_name"] = "second-openai"
    openai = lambda: {item["id"]: item for item in pool.call("/admin/api/models")[1]["data"]}["gpt-6-sol"]  # noqa: E731
    assert openai()["routable"] is True
    # Whatever narrows the accounts _routable looks at (shared mode, say) narrows the reason with it.
    monkeypatch.setattr(admin.Admin, "_route_backends", lambda self: [pool.backends[0]])
    narrowed = openai()
    assert narrowed["routable"] is False and narrowed["route_blocker"] == "no_resource"


def test_blocked_reason_joins_every_distinct_refusal_like_discovery(pool):
    entry = models.resolve("claude-fable-5")[0]
    fable = lambda: {item["id"]: item for item in pool.call("/admin/api/models")[1]["data"]}["claude-fable-5"]  # noqa: E731
    reasons = [models.route_block_reason(entry, backend.cfg) for backend in pool.backends]
    assert all(reasons) and len(set(reasons)) == 1  # two private resources refuse with the same hint
    shown = fable()
    assert (shown["routable"], shown["route_blocker"], shown["route_reason"]) == (False, "blocked", reasons[0])

    # A managed resource is refused with a hint of its own: the operator sees both, as discovery says it.
    pool.backends[1].cfg["provider_resource_name"] = "__managed__"
    reasons = [models.route_block_reason(entry, backend.cfg) for backend in pool.backends]
    assert all(reasons) and len(set(reasons)) == 2
    shown = fable()
    published, discovery = models.catalog_visibility(entry, pool.runtime.cfg, pool.runtime.accounts.backends)
    assert (shown["routable"], shown["route_blocker"], published) == (False, "blocked", False)
    assert shown["route_reason"] == discovery == "; ".join(reasons)


# -- overview ------------------------------------------------------------------------
def test_overview_session_speaks_for_the_enabled_accounts_only(pool):
    first, second = pool.backends
    first.refresher.last_error, first.refresher.last_success_at = "session renewal failed: HTTP 401", 100.0
    second.refresher.last_error, second.refresher.last_success_at = None, 1700000100.0
    session = pool.call("/admin/api/overview")[1]["session"]
    assert session["error"] == "session renewal failed: HTTP 401" and session["last_success_at"] == 1700000100.0
    assert session["accounts"] == {"enabled": 2, "failing": 1}

    assert pool.call("/admin/api/accounts/" + pool.tags[0], {"enabled": False}, method="PATCH")[0] == 200
    session = pool.call("/admin/api/overview")[1]["session"]
    assert session["error"] is None and session["last_success_at"] == 1700000100.0
    assert session["accounts"] == {"enabled": 1, "failing": 0}
    assert {"keepalive", "persistence_error"} <= set(session)


def test_overview_publishes_the_turn_timeout_in_force_that_the_ui_judges_a_silent_turn_by(admin_relay):
    def timeouts():
        overview = admin_relay.request("/admin/api/overview")[1]
        return overview["resources"]["turn_timeout"], admin_relay.request("/admin/api/settings")[1]["settings"]["turn_timeout"]

    assert timeouts() == (900, 900)
    assert admin_relay.patch({"turn_timeout": 7200})[0] == 200
    assert timeouts() == (900, 7200)  # saved for the next start: the running relay still times a turn out after 900 s


def test_a_role_that_applies_at_once_must_fit_the_cap_in_force_not_only_a_raised_one_waiting_for_a_restart(admin_relay):
    admin_relay.cfg["max_prompt_bytes"] = 40_000  # the cap the running relay enforces
    assert admin_relay.patch({"max_prompt_bytes": 0})[0] == 200  # raised in the file: applies after a restart
    assert admin_relay.cfg["max_prompt_bytes"] == 40_000
    status, body, _ = admin_relay.patch({"relay_system_prompt": "x" * 30_000})
    assert status == 400 and body["error"]["param"] == "relay_system_prompt" and "40000" in body["error"]["message"]
    assert admin_relay.patch({"relay_system_prompt": "x" * 20_000})[0] == 200  # fits both
