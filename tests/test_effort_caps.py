"""A cap on the reasoning effort of a client and model, so that a slow combination can be lowered without an effort change
inside a conversation (which resets its signed history and rewrites the cache).

Codex test run of 2026-11-06 (a2): the wall time of a task is the number of agent steps, not the effort (gpt-6.1-sol, same
task: max 45.7 s, high 52.7 s, medium 39.3 s, 5.1/5.3/4.7 s per request), but Claude at the user's default `max` thinks
silently for 15 to 66 s on a trivial step (opus-5-5: ttft 15.0 s with 1 822 tokens, 38.0 s with 4 881, 66.4 s with 8 493;
at low 7.4 s), where gpt-6.1-sol stays at 3 s. The catalog cannot cap it (Codex sends an explicit effort whatever the
catalog lists; the catalog's default applies only when the user has set none), so the relay can."""
import copy

import pytest

import apierr
import config
import models
from prompt_support import after_tool_call, client_request, prompt_relay  # noqa: F401 - prompt_relay is a fixture
from test_conversation_state import stored
from test_native_history import tool_reply
from test_review_fixes_pr19 import END, text

CLAUDE_LEVELS = ["low", "medium", "high", "max"]
CODEX = {"originator": "codex_exec", "user-agent": "codex_exec/0.167.0 (Windows 10.0.26300; x86_64)", "x-codex-window-id": "t1:0",
         "session-id": "t1"}
CAP = [{"client": "codex", "model": "claude-*", "max": "high"}]
USER = [{"role": "user", "content": "inspect ORIGINAL_TASK"}]


def ask(relay, effort, *, model="claude-opus-5-5", headers=None, messages=None):
    body = client_request("responses", copy.deepcopy(messages or USER), model=model)
    if effort:
        body["reasoning"] = {"effort": effort}
    return relay.post("responses", body, headers or CODEX)


def efforts(relay):
    return [post.get("reasoningEffort") for post in relay.posts]


# -- the arithmetic --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("effort,ceiling,levels,expected", [
    ("max", "high", CLAUDE_LEVELS, "high"),
    ("high", "high", CLAUDE_LEVELS, "high"),
    ("low", "high", CLAUDE_LEVELS, "low"),                       # a cap lowers, it never raises
    ("max", "medium", CLAUDE_LEVELS, "medium"),
    ("max", "high", ["none", "low", "medium"], "medium"),         # the highest level the route has below the ceiling
    ("max", "minimal", ["low", "medium", "high"], "max"),         # no level that low: the effort is left alone
    ("max", "bogus", CLAUDE_LEVELS, "max"),
    (None, "high", CLAUDE_LEVELS, None),
    ("xhigh", "high", ["low", "medium", "high", "xhigh"], "high"),
])
def test_a_capped_effort_is_the_highest_level_of_the_route_not_above_the_ceiling(effort, ceiling, levels, expected):
    assert models.capped_effort(levels, effort, ceiling) == expected


# -- the request path --------------------------------------------------------------------------------------------------

def test_without_a_rule_the_effort_is_the_clients(relay):
    status, _ = relay.json_any("/v1/responses", dict(client_request("responses", copy.deepcopy(USER), model="claude-opus-5-5"),
                                                      reasoning={"effort": "max"}), headers=CODEX)
    assert status == 200 and relay.fake.seen[-1].effort == "max"


def test_a_rule_lowers_the_effort_of_its_client_and_model(relay):
    relay.cfg["effort_caps"] = CAP
    body = dict(client_request("responses", copy.deepcopy(USER), model="claude-opus-5-5"), reasoning={"effort": "max"})
    status, _ = relay.json_any("/v1/responses", body, headers=CODEX)
    seen = relay.fake.seen[-1]
    assert status == 200 and seen.effort == "high" and seen.requested_effort == "max" and seen.effort_capped == "max"


@pytest.mark.parametrize("who", [
    {"model": "gpt-6.1-sol", "headers": CODEX},                                          # another model
    {"model": "claude-opus-5-5", "headers": {"user-agent": "claude-cli/2.1.310 (external, sdk-cli)"}},   # another client
])
def test_another_client_or_model_keeps_its_effort(relay, who):
    relay.cfg["effort_caps"] = CAP
    body = dict(client_request("responses", copy.deepcopy(USER), model=who["model"]), reasoning={"effort": "max"})
    path = "/v1/responses"
    status, _ = relay.json_any(path, body, headers=who["headers"])
    assert status == 200 and relay.fake.seen[-1].effort == "max" and not getattr(relay.fake.seen[-1], "effort_capped", None)


def test_a_lower_effort_than_the_cap_is_left_as_it_is(relay):
    relay.cfg["effort_caps"] = CAP
    body = dict(client_request("responses", copy.deepcopy(USER), model="claude-opus-5-5"), reasoning={"effort": "low"})
    assert relay.json_any("/v1/responses", body, headers=CODEX)[0] == 200
    assert relay.fake.seen[-1].effort == "low" and not getattr(relay.fake.seen[-1], "effort_capped", None)


def test_the_default_a_client_would_get_is_capped_too(relay):
    relay.cfg["effort_caps"] = CAP
    relay.cfg["reasoning_effort"] = "max"
    assert relay.json_any("/v1/responses", client_request("responses", copy.deepcopy(USER), model="claude-opus-5-5"),
                          headers=CODEX)[0] == 200
    assert relay.fake.seen[-1].effort == "high"


def test_the_first_matching_rule_applies_and_the_globs_ignore_case(relay):
    relay.cfg["effort_caps"] = [{"client": "CODEX", "model": "CLAUDE-OPUS*", "max": "medium"}, *CAP]
    body = dict(client_request("responses", copy.deepcopy(USER), model="claude-opus-5-5"), reasoning={"effort": "max"})
    assert relay.json_any("/v1/responses", body, headers=CODEX)[0] == 200
    assert relay.fake.seen[-1].effort == "medium"


def test_the_journal_names_the_cap_and_every_request_of_a_conversation_goes_up_at_one_effort(prompt_relay):
    relay = prompt_relay(effort_caps=CAP)
    mark, messages = relay.mark(), copy.deepcopy(USER)
    relay.scripts.append(tool_reply("c1", "opaque-one"))
    status, payload, _ = ask(relay, "max", messages=messages)
    assert status == 200, payload
    # the user switches the effort of the conversation in the client: the relay's effort, and so the signed branch, stays
    relay.scripts.append([text("done"), *END])
    messages = after_tool_call("responses", messages, payload)
    assert ask(relay, "high", messages=messages)[0] == 200
    assert efforts(relay) == ["high", "high"]
    assert len(relay.events(mark, "native_history.resume")) == 1 and not relay.events(mark, "native_history.reset")
    first = stored(mark, "turn.begin")[0]["data"]
    assert (first["requested_effort"], first["effective_effort"], first["effort_resolution"]) == ("max", "high", "capped")
    assert stored(mark, "turn.begin")[1]["data"]["effort_resolution"] == "exact"


def test_without_the_cap_the_same_switch_resets_the_history(prompt_relay):
    relay = prompt_relay()
    mark, messages = relay.mark(), copy.deepcopy(USER)
    relay.scripts.append(tool_reply("c1", "opaque-one"))
    status, payload, _ = ask(relay, "max", messages=messages)
    relay.scripts.append([text("done"), *END])
    assert ask(relay, "high", messages=after_tool_call("responses", messages, payload))[0] == 200
    assert efforts(relay) == ["max", "high"] and relay.events(mark, "native_history.reset")


def test_an_effort_a_prompt_profile_sets_is_the_profiles_own(prompt_relay, tmp_path):
    import json
    (tmp_path / "prompts.json").write_text(json.dumps({"version": 1, "profiles": {"loud": {
        "head": "{{client_system}}", "generation": {"effort": "max"}}},
        "rules": [{"name": "codex", "if": {"client": "codex"}, "use": "loud"}]}), encoding="utf-8")
    relay = prompt_relay(effort_caps=CAP)
    assert ask(relay, "low")[0] == 200
    assert efforts(relay) == ["max"]


# -- the setting -----------------------------------------------------------------------------------------------------

def load_with(tmp_path, monkeypatch, **settings):
    import json
    path = tmp_path / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(tmp_path), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


def test_the_default_is_no_cap_and_a_rule_may_leave_client_and_model_out(tmp_path, monkeypatch):
    assert load_with(tmp_path, monkeypatch)["effort_caps"] == []
    loaded = load_with(tmp_path, monkeypatch, effort_caps=[{"max": "high"}, {"client": "codex", "max": "medium"}])
    assert loaded["effort_caps"] == [{"max": "high", "client": "*", "model": "*"},
                                     {"client": "codex", "max": "medium", "model": "*"}]


@pytest.mark.parametrize("rules", ["high", [7], [{"max": "extreme"}], [{"client": "codex"}], [{"max": "high", "extra": 1}],
                                   [{"max": "high", "model": ""}], [{"max": "high", "client": 3}]])
def test_a_wrong_rule_stops_the_relay_at_start(tmp_path, monkeypatch, rules):
    with pytest.raises(apierr.ApiError, match="effort_caps"):
        load_with(tmp_path, monkeypatch, effort_caps=rules)
