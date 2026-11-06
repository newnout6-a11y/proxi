"""<model>@<profile>[#effort|:effort]: one parser for the catalog, validate and the prompt layer (plan 4.3 item 7).

Names without '@' must behave exactly as they did before profiles existed; legacy_split below is the old suffix
rule, kept here so that a change to it cannot pass unnoticed.
"""
import itertools
import re

import pytest

import apierr
import models
import prompt_policy
import validate
from prompt_models_support import FakeSnapshot

CFG = {"strict_models": True, "model": "claude-opus-5-5", "show_thinking": True}
LONGEST = "a" * 64
DIALECTS = ["chat", "responses", "anthropic"]


def legacy_split(raw):
    """The effort suffix rule models.resolve / resolve_tolerant / validate._finish applied before profiles."""
    name, effort = (raw or "").strip(), None
    for separator in ("#", ":"):
        if separator in name:
            head, tail = name.split(separator, 1)
            if tail.strip().lower() in models.EFFORT_ORDER:
                effort, name = tail.strip().lower(), head.strip()
            break
    return name, effort


def body(dialect, model="claude-opus-5-5", **extra):
    if dialect == "responses":
        data = {"model": model, "input": "hello"}
    else:
        data = {"model": model, "messages": [{"role": "user", "content": "hello"}]}
        if dialect == "anthropic":
            data["max_tokens"] = 1024
    return dict(data, **extra)


def parse(dialect, data, cfg=CFG):
    return getattr(validate, "parse_" + dialect)(data, cfg)


def refused(call, status=400, param="model"):
    with pytest.raises(apierr.ApiError) as raised:
        call()
    assert raised.value.status == status and raised.value.param == param
    return raised.value.message


@pytest.mark.parametrize("name,expected", [
    ("claude-opus-5-5", ("claude-opus-5-5", None, None)),
    ("claude-opus-5-5#high", ("claude-opus-5-5", None, "high")),
    ("claude-opus-5-5:high", ("claude-opus-5-5", None, "high")),
    ("claude-opus-5-5@mine", ("claude-opus-5-5", "mine", None)),
    ("claude-opus-5-5@mine#high", ("claude-opus-5-5", "mine", "high")),
    ("claude-opus-5-5@mine:high", ("claude-opus-5-5", "mine", "high")),
    ("claude-opus-5-5@mine#MAX", ("claude-opus-5-5", "mine", "max")),
    # Aliases are not resolved here, and a provider prefix stays part of the base the catalog reads.
    ("claude-opus@mine", ("claude-opus", "mine", None)),
    ("retool-relay/claude-opus-5-5@mine#high", ("retool-relay/claude-opus-5-5", "mine", "high")),
    ("zai-org/GLM-5.3@fast", ("zai-org/GLM-5.3", "fast", None)),
    ("gpt-6.1-sol@a.b_c-d", ("gpt-6.1-sol", "a.b_c-d", None)),
    ("gpt-6-sol@9lives", ("gpt-6-sol", "9lives", None)),
    ("gpt-6-sol@" + LONGEST, ("gpt-6-sol", LONGEST, None)),
    ("gpt-6-sol@x", ("gpt-6-sol", "x", None)),
    # Whitespace around the parts was always ignored around the effort; it is around the profile too.
    ("  claude-opus-5-5 @ mine # high ", ("claude-opus-5-5", "mine", "high")),
    # A tail that is no effort stays in the name, as before; so does an '@' after anything that is not a catalog model.
    ("claude-opus-5-5#bogus", ("claude-opus-5-5#bogus", None, None)),
    ("a:b@mine", ("a:b@mine", None, None)),
    ("a#b@mine", ("a#b@mine", None, None)),
    ("claude-sonnet-4-5@20250929", ("claude-sonnet-4-5@20250929", None, None)),
    ("claude-sonnet-4-5@20250929#high", ("claude-sonnet-4-5@20250929", None, "high")),
    ("custom-model@mine:max", ("custom-model@mine", None, "max")),
    ("vendor/claude-opus-9@v2", ("vendor/claude-opus-9@v2", None, None)),
    ("", ("", None, None)), ("   ", ("", None, None)), (None, ("", None, None)),
    ("#high", ("", None, "high")), (":max", ("", None, "max")),
])
def test_names_are_taken_apart_into_base_profile_and_effort(name, expected):
    assert tuple(models.parse_model_name(name)) == expected
    assert models.split_virtual(name) == (expected[0], expected[1])


@pytest.mark.parametrize("name,fragment", [
    ("@mine", "no model before '@'"), ("@", "no model before '@'"), ("@mine#high", "no model before '@'"),
    ("claude-opus-5-5@", "no profile after '@'"), ("claude-opus-5-5@#high", "no profile after '@'"),
    ("claude-opus-5-5@ :max", "no profile after '@'"),
    ("claude-opus-5-5@mine@other", "more than one '@'"), ("claude-opus-5-5@@", "more than one '@'"),
    ("claude-opus-5-5@mine#high@x", "more than one '@'"),
    ("claude-opus-5-5@bad name", "not a profile name"), ("claude-opus-5-5@-mine", "not a profile name"),
    ("claude-opus-5-5@.mine", "not a profile name"), ("claude-opus-5-5@_mine", "not a profile name"),
    ("claude-opus-5-5@$header", "not a profile name"), ("claude-opus-5-5@a/b", "not a profile name"),
    ("claude-opus-5-5@профиль", "not a profile name"), ("claude-opus-5-5@a*", "not a profile name"),
    ("claude-opus-5-5@" + LONGEST + "a", "not a profile name"),
    ("claude-opus-5-5@mine#bogus", "'bogus' after the profile is not a reasoning effort"),
    ("claude-opus-5-5@mine:bogus", "'bogus' after the profile is not a reasoning effort"),
    ("claude-opus-5-5@mine#high#low", "'high#low' after the profile is not a reasoning effort"),
    ("claude-opus-5-5#high@mine", "the effort goes after the profile: claude-opus-5-5@mine#high"),
    ("claude-opus-5-5:max@mine", "the effort goes after the profile: claude-opus-5-5@mine:max"),
])
def test_a_malformed_virtual_name_is_a_400_on_the_model_parameter(name, fragment):
    assert fragment in refused(lambda: models.parse_model_name(name))
    refused(lambda: models.split_virtual(name))
    refused(lambda: models.resolve(name))
    refused(lambda: models.resolve_tolerant(name, allow_downgrade=True))
    refused(lambda: models.canonical_name(name))


def test_the_error_quotes_a_bounded_part_of_a_hostile_name():
    message = refused(lambda: models.parse_model_name("claude-opus-5-5@" + "x" * 5000 + "!"))
    assert len(message) < 400


def test_profile_names_follow_prompt_policy_and_nothing_else(monkeypatch):
    assert models.parse_model_name("gpt-6-sol@" + LONGEST).profile == LONGEST
    monkeypatch.setattr(prompt_policy, "PROFILE_NAME", re.compile(r"[a-z]{1,3}"))
    assert models.parse_model_name("gpt-6-sol@abc").profile == "abc"
    refused(lambda: models.parse_model_name("gpt-6-sol@abcd"))
    refused(lambda: models.parse_model_name("gpt-6-sol@ab1"))


def test_no_catalog_id_and_no_alias_contains_the_profile_separator():
    names = [entry["id"] for entry in models.CATALOG] + list(models.ALIASES) + list(models.ALIASES.values())
    assert names and not [name for name in names if "@" in name]


NAMES = [entry["id"] for entry in models.CATALOG] + list(models.ALIASES) + [
    "", "unknown", "retool-relay/claude-opus-5-5", "x:y", "x#y", "#high", ":high", "a/b/c"]


@pytest.mark.parametrize("separator,tail", itertools.product(
    ["", "#", ":", " # ", " : ", "##", "::", "#:", ":#"],
    ["", "high", "LOW", " max ", "none", "xhigh", "bogus", "a:b", "high#low", "high:low", "medium "]))
def test_names_without_a_profile_come_out_exactly_as_before(separator, tail):
    for base in NAMES:
        name = base + separator + tail if separator else base
        base_name, effort = legacy_split(name)
        assert tuple(models.parse_model_name(name)) == (base_name, None, effort), name


@pytest.mark.parametrize("name,entry,effort", [
    ("claude-opus-5-5", "claude-opus-5-5", None), ("claude-opus-5-5#high", "claude-opus-5-5", "high"),
    ("claude-opus:max", "claude-opus-5-5", "max"), (" gpt-6 # LOW ", "gpt-6-sol", "low"),
    ("zai-org/GLM-5.3#high", "zai-org/GLM-5.3", None),
])
def test_the_catalog_reads_a_plain_name_as_it_did(name, entry, effort):
    assert models.resolve(name)[0]["id"] == entry and models.resolve(name)[1] == effort
    assert models.resolve_tolerant(name)[0]["id"] == entry and models.resolve_tolerant(name)[1] == effort


def test_an_unrecognized_tail_still_makes_an_unknown_model_with_the_whole_name():
    assert refused(lambda: models.resolve("claude-opus-5-5#bogus"), status=404, param=None) == (
        "unknown model 'claude-opus-5-5#bogus'; see GET /v1/models")
    assert refused(lambda: models.resolve("  "), param="model") == "'model' is required"


@pytest.mark.parametrize("name,entry,effort", [
    ("claude-opus-5-5@mine#high", "claude-opus-5-5", "high"),
    ("claude-opus-5-5@mine:max", "claude-opus-5-5", "max"),
    ("claude-opus@mine", "claude-opus-5-5", None),
    ("claude-opus-5.5@mine#low", "claude-opus-5-5", "low"),
    ("retool-relay/claude-opus-5-5@mine", "claude-opus-5-5", None),
    ("gpt-6@mine#none", "gpt-6-sol", "none"),
    ("zai-org/GLM-5.3@fast", "zai-org/GLM-5.3", None),
    ("zai-org/GLM-5.3@fast#high", "zai-org/GLM-5.3", None),  # Retool ignores effort there: accepted, not forwarded
])
def test_the_catalog_resolves_a_virtual_name_to_its_base_model_and_effort(name, entry, effort):
    assert models.resolve(name)[0]["id"] == entry and models.resolve(name)[1] == effort
    assert models.resolve_tolerant(name)[0]["id"] == entry and models.resolve_tolerant(name)[1] == effort
    assert models.resolve(name)[0] is models.resolve(entry)[0]  # the catalog's own entry, not a copy


def test_an_explicit_effort_beats_the_suffix_and_the_default_model_may_carry_a_profile():
    assert models.resolve("claude-opus-5-5@mine#high", "low")[1] == "low"
    assert models.resolve_tolerant("claude-opus-5-5@mine#high", "max")[1] == "max"
    assert models.resolve(None, default_model="claude-opus-5-5@mine#high") == (
        models.resolve("claude-opus-5-5")[0], "high")
    entry, effort = models.resolve_tolerant("#low", default_model="claude-opus-5-5@mine")
    assert (entry["id"], effort) == ("claude-opus-5-5", "low")


def test_effort_downgrade_still_works_behind_a_profile(capsys):
    entry, effort = models.resolve_tolerant("claude-opus-5-5@mine#xhigh", allow_downgrade=True)
    assert (entry["id"], effort) == ("claude-opus-5-5", "high")
    refused(lambda: models.resolve_tolerant("claude-opus-5-5@mine#xhigh"), param="reasoning_effort")
    capsys.readouterr()


def test_a_name_with_an_at_sign_after_an_unknown_model_is_one_unknown_model_and_strict_off_keeps_its_id():
    message = refused(lambda: models.resolve("claude-opus-9@mine"), status=404, param=None)
    assert message == "unknown model 'claude-opus-9@mine'; see GET /v1/models"
    entry, _ = models.resolve("claude-opus-9@mine", strict=False)
    assert entry["id"] == "claude-opus-9@mine"


def test_canonical_name_resolves_aliases_and_keeps_the_profile():
    assert models.canonical_name("claude-opus@mine#high") == "claude-opus-5-5@mine"
    assert models.canonical_name("retool-relay/gpt-6@x") == "gpt-6-sol@x"
    assert models.canonical_name("claude-opus-5-5") == "claude-opus-5-5"
    assert models.canonical_name("claude-opus#max") == "claude-opus-5-5"
    refused(lambda: models.canonical_name("nonsense@mine"), status=404, param=None)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_validate_keeps_the_base_model_the_profile_and_the_clients_own_name(dialect):
    data = body(dialect, "claude-opus@mine#high")
    parsed = parse(dialect, data)
    assert parsed.model == "claude-opus-5-5" and parsed.entry is models.resolve("claude-opus-5-5")[0]
    assert parsed.virtual_profile == "mine"
    assert parsed.requested_model == "claude-opus"  # before alias resolution, without @profile and #effort
    assert parsed.effort == parsed.requested_effort == "high"
    assert data["model"] == "claude-opus@mine#high"  # the request is not touched


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("base,suffix", [
    ("claude-opus-5-5", ""), ("claude-opus", ""), ("claude-opus-5-5", "#high"), ("claude-opus", ":max"),
    ("retool-relay/claude-opus-5-5", "")])
def test_a_virtual_name_changes_nothing_but_the_two_prompt_fields(dialect, base, suffix):
    plain = parse(dialect, body(dialect, base + suffix))
    virtual = parse(dialect, body(dialect, base + "@mine" + suffix))
    assert (plain.virtual_profile, plain.requested_model) == (None, base)
    assert (virtual.virtual_profile, virtual.requested_model) == ("mine", base)
    ignored = {"requested_model", "virtual_profile"}
    assert {key: value for key, value in vars(plain).items() if key not in ignored} == {
        key: value for key, value in vars(virtual).items() if key not in ignored}


@pytest.mark.parametrize("dialect", DIALECTS)
def test_parsed_fields_of_a_request_without_a_suffix_are_the_ones_it_always_had(dialect):
    parsed = parse(dialect, body(dialect, "gpt-6-sol"))
    assert (parsed.model, parsed.effort, parsed.requested_effort) == ("gpt-6-sol", None, None)
    assert (parsed.requested_model, parsed.virtual_profile) == ("gpt-6-sol", None)
    assert validate.Parsed().virtual_profile is None and validate.Parsed().requested_model == ""


@pytest.mark.parametrize("dialect", DIALECTS)
def test_validate_rejects_a_malformed_profile_before_anything_else(dialect):
    for name in ("claude-opus-5-5@bad name", "@mine", "claude-opus-5-5@mine@x", "claude-opus-5-5#high@mine",
                 "claude-opus-5-5@", "claude-opus-5-5@" + LONGEST + "a"):
        refused(lambda: parse(dialect, body(dialect, name)))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_that_the_profile_exists_is_not_validates_business(dialect):
    parsed = parse(dialect, body(dialect, "claude-opus-5-5@no-such-profile"))
    assert parsed.virtual_profile == "no-such-profile" and parsed.model == "claude-opus-5-5"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_a_missing_name_stands_for_the_default_model_and_its_profile(dialect):
    data = body(dialect)
    del data["model"]
    parsed = parse(dialect, data)
    assert (parsed.model, parsed.requested_model, parsed.virtual_profile) == (
        "claude-opus-5-5", "claude-opus-5-5", None)
    parsed = parse(dialect, data, dict(CFG, model="claude-opus@mine#low"))
    assert (parsed.model, parsed.requested_model, parsed.virtual_profile) == ("claude-opus-5-5", "claude-opus", "mine")
    parsed = parse(dialect, body(dialect, "#low"), dict(CFG, model="claude-opus@mine"))
    assert (parsed.model, parsed.requested_model, parsed.virtual_profile, parsed.effort) == (
        "claude-opus-5-5", "claude-opus", "mine", "low")


@pytest.mark.parametrize("dialect", DIALECTS)
def test_strict_models_off_passes_a_name_with_an_at_sign_after_an_unknown_model_on_as_it_is(dialect):
    parsed = parse(dialect, body(dialect, "custom-model@mine"), dict(CFG, strict_models=False))
    assert (parsed.model, parsed.requested_model, parsed.virtual_profile) == (
        "custom-model@mine", "custom-model@mine", None)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_an_explicit_effort_control_and_a_virtual_name_agree_like_a_plain_name(dialect):
    parsed = parse(dialect, body(dialect, "claude-opus-5-5@mine#low", reasoning_effort="high"))
    assert parsed.effort == parsed.requested_effort == "high"


def test_the_prompt_layer_reads_the_fields_through_plan_input():
    parsed = parse("chat", body("chat", "claude-opus@mine#high"))
    parsed.dialect = "chat"
    inp = prompt_policy.PlanInput.from_request(parsed, {"client": "opencode"}, {})
    assert (inp.model, inp.requested_model, inp.virtual_profile) == ("claude-opus-5-5", "claude-opus", "mine")
    plain = parse("chat", body("chat", "claude-opus"))
    inp = prompt_policy.PlanInput.from_request(plain, {}, {})
    assert (inp.model, inp.requested_model, inp.virtual_profile) == ("claude-opus-5-5", "claude-opus", None)


def test_every_published_virtual_id_parses_back_to_its_base_and_profile():
    entries = list(models.catalog())
    snapshot = FakeSnapshot({"mine": {"publish_as_model": True}, "a.b_c-d": {"publish_as_model": True}})
    published = models.virtual_entries(entries, snapshot)
    assert len(published) == 2 * len(entries)
    for item in published:
        base, profile = models.split_virtual(item["id"])
        assert (base, profile) == (item["virtual"]["base"], item["virtual"]["profile"])
        assert models.resolve(item["id"])[0]["id"] == base
        assert models.canonical_name(item["id"]) == item["id"]
