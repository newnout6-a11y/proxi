"""Editing universal providers in the admin: what a PATCH keeps, what the API shows of secrets, the probe of a saved
provider, the provider named "test", a slow catalog refresh and the last provider that serves."""
from __future__ import annotations

import json

import pytest

from test_stage5_admin_api import gateway_admin, mock_upstream  # noqa: F401 - bootstrap relay and upstream fixtures

KEY = "sk-live-secret-provider-key-9999"
OPENCODE_MODELS = {"deepseek/deepseek-chat-v3": {"name": "DeepSeek Chat", "limit": {"context": 128000}},
                   "anthropic/claude-sonnet-4-5": {"name": "Claude Sonnet 4.5"}}


def create(admin, upstream, pid="hub", **fields):
    body = {"id": pid, "name": pid, "driver": "openai-chat", "base_url": upstream, "api_key": KEY,
            "discover_now": False, **fields}
    status, answer = admin.admin_call("/admin/api/providers", body, method="POST")
    assert status == 200, answer
    return answer


def saved_provider(admin, pid="hub"):
    saved = json.loads(admin.cfg_path.read_text(encoding="utf-8"))
    return next(p for p in saved["providers"] if p["id"] == pid)


def patch(admin, changes, pid="hub"):
    return admin.admin_call("/admin/api/providers/" + pid, changes, method="PATCH")


# A14-01: the edit form put an OpenCode models object into a text field and split it at its commas; the server saved the
# pieces as model ids and the provider's models answered 404 "unknown model".
def test_a_patch_without_models_keeps_an_opencode_models_object(gateway_admin, mock_upstream):
    create(gateway_admin, mock_upstream, models=OPENCODE_MODELS)
    status, answer = patch(gateway_admin, {"name": "Renamed"})
    assert status == 200, answer
    assert saved_provider(gateway_admin)["models"] == OPENCODE_MODELS
    assert saved_provider(gateway_admin)["name"] == "Renamed"


@pytest.mark.parametrize("models", [
    ['{"deepseek/deepseek-chat-v3":{"name":"DeepSeek Chat"', '"limit":{"context":128000}}'],
    ["deepseek chat"], [""], [{"name": "no id"}], {"has space": {}}, [7],
])
def test_model_ids_that_cannot_be_ids_are_refused(gateway_admin, mock_upstream, models):
    create(gateway_admin, mock_upstream, models=OPENCODE_MODELS)
    status, answer = patch(gateway_admin, {"models": models})
    assert status == 400 and answer["error"]["param"] == "models", answer
    assert saved_provider(gateway_admin)["models"] == OPENCODE_MODELS
    status, answer = gateway_admin.admin_call("/admin/api/providers", {
        "id": "other", "driver": "openai-chat", "base_url": mock_upstream, "api_key": KEY, "discover_now": False,
        "models": models}, method="POST")
    assert status == 400 and answer["error"]["param"] == "models", answer


# A14-03: the form never had the saved headers (only their names come back), and a PATCH replaced the whole object: adding
# one header dropped HTTP-Referer and X-Title. Clearing "Body overrides" sent nothing, so they could not be removed.
def test_headers_are_merged_by_name_and_a_null_removes_one(gateway_admin, mock_upstream):
    create(gateway_admin, mock_upstream, headers={"HTTP-Referer": "https://localhost", "X-Title": "Relay"})
    assert patch(gateway_admin, {"headers": {"X-New": "1"}})[0] == 200
    assert saved_provider(gateway_admin)["headers"] == {"HTTP-Referer": "https://localhost", "X-Title": "Relay",
                                                        "X-New": "1"}
    assert patch(gateway_admin, {"headers": {"X-Title": None}})[0] == 200
    assert saved_provider(gateway_admin)["headers"] == {"HTTP-Referer": "https://localhost", "X-New": "1"}
    status, answer = patch(gateway_admin, {"headers": None})
    assert status == 200 and answer["provider"]["headers_keys"] == []
    assert "headers" not in saved_provider(gateway_admin)


def test_body_overrides_can_be_cleared(gateway_admin, mock_upstream):
    create(gateway_admin, mock_upstream, body_overrides={"service_tier": "auto"})
    status, answer = patch(gateway_admin, {"body_overrides": {}})
    assert status == 200 and answer["provider"]["body_overrides"] == {}
    assert "body_overrides" not in saved_provider(gateway_admin)


# A14-03: "Проверить соединение" of a provider whose key is an environment variable sent api_key_env, and the probe then
# left the saved provider out: its base_url, headers and flags were missing from the check.
def test_the_probe_of_a_saved_provider_always_starts_from_what_is_saved(gateway_admin, mock_upstream, monkeypatch):
    monkeypatch.setenv("HUB_TEST_KEY", KEY)
    create(gateway_admin, mock_upstream)
    status, probe = gateway_admin.admin_call("/admin/api/providers/test",
                                             {"provider_id": "hub", "api_key_env": "HUB_TEST_KEY"}, method="POST")
    assert status == 200 and probe["ok"] is True, probe
    assert probe["models_count"] == 2


# A07-09 / A14-12 / A11-19: a key preview showed 10 of a 13-character key, and a proxy_url came back with its password.
def test_the_admin_api_shows_at_most_the_last_four_characters_of_a_key_and_no_proxy_password(gateway_admin,
                                                                                              mock_upstream):
    short = "sk-short-123"  # 12 characters: nothing of it is shown
    create(gateway_admin, mock_upstream, pid="short", api_key=short)
    create(gateway_admin, mock_upstream, pid="proxied", proxy_url="socks5://alice:Pr0xyPassw0rd@127.0.0.1:9",
           auto_discover=False)
    status, listing = gateway_admin.admin_call("/admin/api/providers")
    assert status == 200
    text = json.dumps(listing)
    assert "Pr0xyPassw0rd" not in text and "alice" not in text
    by_id = {item["id"]: item for item in listing["providers"]}
    assert by_id["short"]["credentials"][0]["api_key_preview"] == "••••••••"
    long_preview = by_id["proxied"]["credentials"][0]["api_key_preview"]
    assert long_preview == "…" + KEY[-4:]
    shown_proxy = by_id["proxied"]["proxy_url"]
    assert shown_proxy.startswith("socks5://") and shown_proxy.endswith("@127.0.0.1:9")
    # The form sends the shown value back unchanged: the saved proxy keeps its password.
    assert patch(gateway_admin, {"name": "Proxied", "proxy_url": shown_proxy}, pid="proxied")[0] == 200
    assert saved_provider(gateway_admin, "proxied")["proxy_url"] == "socks5://alice:Pr0xyPassw0rd@127.0.0.1:9"


def test_a_client_key_set_by_hand_is_shown_by_its_last_four_characters_at_most(gateway_admin):
    status, created = gateway_admin.admin_call("/admin/api/client-keys", {"label": "Hand", "key": "hand-made-key-1234X"},
                                               method="POST")
    assert status == 200, created
    status, listing = gateway_admin.admin_call("/admin/api/client-keys")
    preview = listing["client_keys"][0]["key_preview"]
    assert "hand-made" not in preview and len(preview.replace("…", "").replace("•", "")) <= 4
    status, generated = gateway_admin.admin_call("/admin/api/client-keys", {"label": "Generated"}, method="POST")
    raw = generated["raw_key"]
    shown = next(item for item in gateway_admin.admin_call("/admin/api/client-keys")[1]["client_keys"]
                 if item["label"] == "Generated")["key_preview"]
    assert shown == "sk-proxi-…" + raw[-4:]  # the relay's own prefix is public


# A07-06: the probe route /admin/api/providers/test was matched before the provider routes, so a provider named "test"
# could be created and then neither changed nor deleted (405).
def test_a_provider_named_test_can_be_changed_and_deleted(gateway_admin, mock_upstream):
    create(gateway_admin, mock_upstream, pid="test")
    create(gateway_admin, mock_upstream, pid="other")
    status, answer = patch(gateway_admin, {"name": "Renamed test"}, pid="test")
    assert status == 200 and answer["provider"]["name"] == "Renamed test"
    assert gateway_admin.admin_call("/admin/api/providers/test", method="DELETE")[0] == 200
    assert [p["id"] for p in gateway_admin.admin_call("/admin/api/providers")[1]["providers"]] == ["other"]
