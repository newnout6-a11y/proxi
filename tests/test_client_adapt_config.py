"""The options that make the relay adapt to its clients are validated at start and default to the adaptation (a decision of the
user is the one exception: stable_prompt_date, which tells the model an older date, is off)."""
import json

import pytest

import apierr
import config


def load_with(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(tmp_path), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


def test_the_defaults_adapt_to_the_clients_except_for_the_date(tmp_path, monkeypatch):
    cfg = load_with(tmp_path, monkeypatch)
    assert cfg["infer_conversations"] is True and cfg["stabilize_client_prompts"] is True
    assert cfg["stable_prompt_date"] is False
    assert cfg["tool_union_clients"] == ["codex"] and cfg["tool_union_ttl"] == 6 * 3600
    assert cfg["claude_tool_search"] == "eager"


def test_every_option_can_be_set_back_to_what_the_relay_did_before(tmp_path, monkeypatch):
    cfg = load_with(tmp_path, monkeypatch, infer_conversations=False, stabilize_client_prompts=False, stable_prompt_date=True,
                    tool_union_clients=[], tool_union_ttl=60, claude_tool_search="native")
    assert (cfg["infer_conversations"], cfg["stabilize_client_prompts"], cfg["stable_prompt_date"]) == (False, False, True)
    assert cfg["tool_union_clients"] == [] and cfg["tool_union_ttl"] == 60 and cfg["claude_tool_search"] == "native"


@pytest.mark.parametrize("setting,message", [
    ({"infer_conversations": "yes"}, "infer_conversations must be a boolean"),
    ({"stabilize_client_prompts": 1}, "stabilize_client_prompts must be a boolean"),
    ({"stable_prompt_date": "true"}, "stable_prompt_date must be a boolean"),
    ({"tool_union_clients": "codex"}, "tool_union_clients must be a list"),
    ({"tool_union_clients": ["codex", 7]}, "tool_union_clients must be a list"),
    ({"tool_union_ttl": 0}, "tool_union_ttl must be a positive"),
    ({"tool_union_ttl": 1.5}, "tool_union_ttl must be a positive"),
    ({"tool_union_ttl": True}, "tool_union_ttl must be a positive"),
    ({"claude_tool_search": "maybe"}, "claude_tool_search must be"),
])
def test_a_wrong_value_stops_the_relay_at_start_and_says_which(tmp_path, monkeypatch, setting, message):
    with pytest.raises(apierr.ApiError, match=message):
        load_with(tmp_path, monkeypatch, **setting)
