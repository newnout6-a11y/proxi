"""xhigh on a Claude route is served as another level instead of failing: high by default, or max as the operator chooses.

Retool's Claude routes list low, medium, high and max. Claude Desktop sends xhigh to every model, and a client cannot know that
the route has none, so the request used to fail with a 400 unless allow_effort_downgrade was on. The replacement is the one
level that never needs that setting; every other level the route lacks still does."""
import json
from pathlib import Path

import pytest

import apierr
import config
import models
import validate
from test_client_contracts import CFG, _body
from test_reasoning_off import parse
from test_native_context_reuse import run, setup
from test_service_requests import capture_direct

CLAUDE = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-opus-5", "claude-fable-5"]


@pytest.mark.parametrize("dialect, extra", [("chat", {"reasoning_effort": "xhigh"}),
                                            ("responses", {"reasoning": {"effort": "xhigh"}}),
                                            ("anthropic", {"output_config": {"effort": "xhigh"}}),
                                            ("anthropic", {"thinking": {"type": "adaptive", "effort": "xhigh"}})])
@pytest.mark.parametrize("model", CLAUDE)
def test_xhigh_is_served_as_high_by_default_with_the_downgrade_setting_off(model, dialect, extra):
    parsed = parse(dialect, model, extra)
    assert (parsed.requested_effort, parsed.effort) == ("xhigh", "high")


@pytest.mark.parametrize("downgrade", [False, True])
@pytest.mark.parametrize("replacement, level", [("high", "high"), ("max", "max")])
def test_the_setting_picks_the_replacement_and_the_downgrade_setting_has_nothing_to_say_about_it(downgrade, replacement, level):
    parsed = parse("anthropic", "claude-opus-5-5", {"output_config": {"effort": "xhigh"}},
                   claude_xhigh_effort=replacement, allow_effort_downgrade=downgrade)
    assert (parsed.requested_effort, parsed.effort) == ("xhigh", level)


@pytest.mark.parametrize("suffix", ["#xhigh", ":xhigh"])
def test_the_model_name_suffix_is_served_the_same_way(suffix):
    data = {"model": "claude-opus-5-5" + suffix, "input": "OK"}
    assert validate.parse_responses(data, CFG).effort == "high"
    assert validate.parse_responses(data, dict(CFG, claude_xhigh_effort="max")).effort == "max"


def test_the_other_levels_a_claude_route_lacks_still_need_the_downgrade_setting():
    for level in ("minimal", "none"):
        with pytest.raises(apierr.ApiError) as error:
            parse("chat", "claude-opus-5-5", {"reasoning_effort": level}, claude_xhigh_effort="max")
        assert error.value.status == 400 and "allow_effort_downgrade" in str(error.value)
    assert parse("chat", "claude-opus-5-5", {"reasoning_effort": "minimal"}, allow_effort_downgrade=True,
                 claude_xhigh_effort="max").effort == "low"


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6.1-sol", "gpt-5.5", "gpt-5"])
def test_a_route_that_is_not_claude_keeps_rejecting_xhigh_until_the_downgrade_setting_is_on(model):
    with pytest.raises(apierr.ApiError) as error:
        parse("chat", model, {"reasoning_effort": "xhigh"}, claude_xhigh_effort="max")
    assert error.value.status == 400
    # With the setting on, a level the route lacks is served at the lowest level above it (max where the route has it),
    # never a lower one; a route whose top is high serves its top.
    levels = models.resolve(model)[0].get("reasoning_effort_levels") or ()
    assert parse("chat", model, {"reasoning_effort": "xhigh"}, allow_effort_downgrade=True).effort == (
        "max" if "max" in levels else "high")


@pytest.mark.parametrize("model", ["claude-haiku-4-5-20251001", "zai-org/GLM-5.3", "zai-org/GLM-5.3-Flash"])
def test_a_route_that_ignores_effort_takes_xhigh_as_before_and_sends_none(model):
    parsed = parse("chat", model, {"reasoning_effort": "xhigh"}, claude_xhigh_effort="max")
    assert (parsed.requested_effort, parsed.effort) == ("xhigh", None)


def test_the_rule_names_the_routes_it_applies_to():
    for name in CLAUDE:
        entry = models.resolve(name)[0]
        assert models.replaces_xhigh(entry) and models.xhigh_replacement(entry, "max") == "max"
    assert not models.replaces_xhigh(models.resolve("claude-haiku-4-5-20251001")[0])  # it ignores every level
    assert not models.replaces_xhigh(models.resolve("gpt-6-sol")[0])
    assert models.xhigh_replacement(models.resolve("gpt-6-sol")[0], "high") is None
    # a replacement the route does not list is no replacement
    assert models.xhigh_replacement(models.resolve("claude-opus-5-5")[0], "minimal") is None
    # a route that lists xhigh itself needs none
    own = dict(models.resolve("claude-opus-5-5")[0], reasoning_effort_levels=["low", "high", "xhigh"])
    assert not models.replaces_xhigh(own)


def test_a_configured_default_of_xhigh_follows_the_same_choice():
    opus = models.resolve("claude-opus-5-5")[0]
    assert models.effective_effort(opus, None, "xhigh", "high") == "high"
    assert models.effective_effort(opus, None, "xhigh", "max") == "max"
    assert models.effective_effort(opus, None, "XHIGH", "max") == "max"
    assert models.effective_effort(opus, "low", "xhigh", "max") == "low"  # what the client sent is not touched
    # A route that is not Claude takes the lowest level above xhigh, never a lower one.
    assert models.effective_effort(models.resolve("gpt-6-sol")[0], None, "xhigh", "max") == "max"


# --- the configuration ---------------------------------------------------------------------------------------------------

def load(tmp_path, monkeypatch, **settings):
    path = tmp_path / "config.json"
    saved = {"base": "https://tenant.invalid", "page_uuid": "p0", "ua": "ua", "cookie": "accessToken=fake-token",
             "xsrf": "fake-xsrf", "workspace_root": str(tmp_path), "provider_resource_name": "__managed__", **settings}
    path.write_text(json.dumps(saved), encoding="utf-8")
    monkeypatch.setenv("RELAY_API_KEY", "local-test-key")
    for name in ("RETOOL_COOKIE", "RETOOL_XSRF", "RETOOL_BASE", "RETOOL_PAGE_UUID"):
        monkeypatch.delenv(name, raising=False)
    return config.load(str(path))


def test_the_default_is_high_and_the_example_config_says_so(tmp_path, monkeypatch):
    assert config.DEFAULTS["claude_xhigh_effort"] == "high"
    assert load(tmp_path, monkeypatch)["claude_xhigh_effort"] == "high"
    example = json.loads((Path(config.HERE) / "config.example.json").read_text(encoding="utf-8"))
    assert example["claude_xhigh_effort"] == "high" and "xhigh" in example["claude_xhigh_effort_note"]


@pytest.mark.parametrize("value", ["high", "max"])
def test_both_replacements_load(tmp_path, monkeypatch, value):
    assert load(tmp_path, monkeypatch, claude_xhigh_effort=value)["claude_xhigh_effort"] == value


@pytest.mark.parametrize("value", ["xhigh", "medium", "", None, True, ["max"], "MAX"])
def test_any_other_value_stops_the_start_and_names_the_choices(tmp_path, monkeypatch, value):
    with pytest.raises(apierr.ApiError, match="claude_xhigh_effort must be one of: high, max"):
        load(tmp_path, monkeypatch, claude_xhigh_effort=value)


def test_a_configured_default_effort_of_xhigh_loads_for_a_claude_default_model(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, reasoning_effort="xhigh")["reasoning_effort"] == "xhigh"
    # the old rule still holds for a route without it
    with pytest.raises(apierr.ApiError, match="reasoning effort"):
        load(tmp_path, monkeypatch, reasoning_effort="xhigh", model="gpt-6-sol", openai_provider_resource_name="__managed__")


# --- over HTTP and in the journal ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("replacement", ["high", "max"])
@pytest.mark.parametrize("path, payload", [
    ("/v1/messages", lambda: _body("anthropic", output_config={"effort": "xhigh"})),
    ("/v1/chat/completions", lambda: _body("chat", reasoning_effort="xhigh")),
])
def test_a_desktop_style_request_for_xhigh_is_answered(relay, replacement, path, payload):
    relay.cfg["claude_xhigh_effort"] = replacement
    status, _ = relay.json_any(path, payload())
    assert status == 200
    assert (relay.fake.seen[-1].requested_effort, relay.fake.seen[-1].effort) == ("xhigh", replacement)


@pytest.mark.parametrize("replacement", ["high", "max"])
def test_the_upstream_gets_the_replacement_and_the_journal_discloses_it(tmp_path, monkeypatch, replacement):
    import asyncio
    import journal
    _, _, runner = setup(tmp_path, transport="direct", project_context=False, cookie_values=[],
                         provider_resource_name="test-anthropic", claude_xhigh_effort=replacement)
    sent = capture_direct(monkeypatch)
    parsed = parse("anthropic", "claude-opus-5-5", {"output_config": {"effort": "xhigh"}}, claude_xhigh_effort=replacement)
    before = journal.JOURNAL.last_seq
    events = asyncio.run(run(runner, parsed))
    assert events[-1] == {"type": "end", "reason": "stop"}
    assert sent[0][1]["reasoningEffort"] == replacement
    begun = next(e for e in journal.JOURNAL.query(since=before) if e["kind"] == "turn.begin")
    assert (begun["data"]["requested_effort"], begun["data"]["effective_effort"]) == ("xhigh", replacement)
    assert begun["data"]["effort_resolution"] == "compatibility"
