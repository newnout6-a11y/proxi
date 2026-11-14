"""The catalogs a prompts snapshot steers: virtual models <model>@<profile> and Codex's model_messages (plan 4.3 items 8, 9).

Without a snapshot every catalog is what it was before; legacy_codex_catalog below is the previous function,
kept verbatim so that "byte for byte" is tested against something other than the new code.
"""
import copy
import json

import pytest

import models
import prompt_policy
from prompt_models_support import FakeSnapshot

CODEX_TEXT = ("You are a coding assistant working in the user's project. Follow the "
              "user's instructions and project guidance. Use the tools provided by "
              "the client to inspect files, make requested changes, and run relevant "
              "checks. Base reports on observed tool results and state any limitations.")
MINIMAL = {"id": "plain-text-model", "reasoning": False, "reasoning_effort_levels": []}


def legacy_codex_catalog(entries=None):
    """models.codex_catalog as it was before snapshots existed."""
    instructions = (
        "You are a coding assistant working in the user's project. Follow the "
        "user's instructions and project guidance. Use the tools provided by "
        "the client to inspect files, make requested changes, and run relevant "
        "checks. Base reports on observed tool results and state any limitations."
    )
    result = []
    for index, entry in enumerate(models.CATALOG if entries is None else entries):
        efforts = entry.get('reasoning_effort_levels', [])
        default = entry.get('default_reasoning_effort')
        if default not in efforts:
            default = 'medium' if 'medium' in efforts else (efforts[0] if efforts else None)
        levels = [{'effort': effort, 'description': effort.title() + ' reasoning effort'} for effort in efforts]
        if 'max' in efforts:
            levels.append({'effort': 'ultra', 'description': 'Maximum reasoning with automatic task delegation'})
        result.append({
            'slug': entry['id'], 'display_name': entry.get('label', entry['id']),
            'description': 'Retool relay: ' + entry.get('label', entry['id']),
            'default_reasoning_level': default,
            'supported_reasoning_levels': levels,
            'shell_type': 'unified_exec', 'visibility': 'list', 'supported_in_api': True,
            'priority': index, 'additional_speed_tiers': [], 'service_tiers': [],
            'default_service_tier': None, 'availability_nux': None, 'upgrade': None,
            'base_instructions': instructions,
            'model_messages': {'instructions_template': instructions, 'persistent_instructions': ''},
            'include_skills_usage_instructions': False, 'include_plugin_usage_instructions': False,
            'include_apps_usage_instructions': False,
            'supports_reasoning_summary_parameter': bool(entry.get('reasoning')),
            'default_reasoning_summary': 'detailed' if entry.get('reasoning') else 'none',
            'support_verbosity': bool(entry.get('verbosity_levels')), 'default_verbosity': None,
            'apply_patch_tool_type': 'freeform',
            'supports_parallel_tool_calls': True,
            'truncation_policy': {'mode': 'tokens', 'limit': 10000},
            'context_window': entry.get('context', 128000),
            'effective_context_window_percent': 95,
            'experimental_supported_tools': [],
            'input_modalities': ['text', 'image'] if entry.get('vision') else ['text'],
            'supports_search_tool': False, 'supports_experimental_context': False,
            'use_responses_lite': False, 'supports_reasoning_effort_updates': False,
            'node_repl_disabled': True, 'tool_mode': 'direct',
        })
        if 'max' in efforts:
            result[-1]['multi_agent_version'] = 'v2'
    return {'models': result}


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def by_slug(catalog):
    return {item["slug"]: item for item in catalog["models"]}


def ids(items):
    return [item["id"] for item in items]


ENTRIES = [models.resolve(name)[0] for name in (
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "gpt-6-sol", "gpt-5.5", "gpt-4.1",
    "zai-org/GLM-5.3")]


# -- Codex without a snapshot ---------------------------------------------------------------------

@pytest.mark.parametrize("entries", [None, [], ENTRIES, ENTRIES[:1], [MINIMAL], ENTRIES + [MINIMAL]])
def test_codex_catalog_without_a_snapshot_is_the_previous_catalog_byte_for_byte(entries):
    expected = dump(legacy_codex_catalog(entries))
    assert dump(models.codex_catalog(entries)) == expected
    assert dump(models.codex_catalog(entries, None)) == expected
    assert dump(models.codex_catalog(entries, snapshot=None)) == expected


def test_a_generator_of_entries_is_read_once():
    assert dump(models.codex_catalog(entry for entry in ENTRIES)) == dump(legacy_codex_catalog(ENTRIES))


def test_a_snapshot_that_says_nothing_about_codex_or_publishing_changes_nothing():
    snapshot = FakeSnapshot({"mine": {"head": "{{client_system}}"}, "other": {"publish_as_model": False}})
    for entries in (None, ENTRIES, [MINIMAL]):
        assert dump(models.codex_catalog(entries, snapshot)) == dump(legacy_codex_catalog(entries))
    assert models.virtual_entries(ENTRIES, snapshot) == []


# -- Codex base instructions and overrides --------------------------------------------------------

def test_codex_base_text_is_both_the_legacy_field_and_the_instruction_template():
    catalog = models.codex_catalog(ENTRIES, FakeSnapshot(codex_base="Always answer in Russian."))
    for item in catalog["models"]:
        assert item["base_instructions"] == "Always answer in Russian."
        assert item["model_messages"]["instructions_template"] == "Always answer in Russian."
        assert item["model_messages"]["persistent_instructions"] == ""


def test_codex_base_by_model_glob_leaves_the_other_models_on_the_built_in_text():
    snapshot = FakeSnapshot(codex_base={"gpt-6*": "GPT six text", "claude-*": "Claude text"})
    items = by_slug(models.codex_catalog(ENTRIES, snapshot))
    assert items["gpt-6-sol"]["base_instructions"] == items["gpt-6-sol"]["model_messages"][
        "instructions_template"] == "GPT six text"
    assert items["claude-opus-5-5"]["base_instructions"] == "Claude text"
    assert items["claude-haiku-4-5-20251001"]["model_messages"]["instructions_template"] == "Claude text"
    for name in ("gpt-5.5", "gpt-4.1", "zai-org/GLM-5.3"):
        assert items[name]["base_instructions"] == items[name]["model_messages"]["instructions_template"] == CODEX_TEXT


def test_an_empty_codex_base_is_the_users_text_not_the_built_in_one():
    items = models.codex_catalog(ENTRIES[:2], FakeSnapshot(codex_base=""))["models"]
    assert [item["base_instructions"] for item in items] == ["", ""]
    assert [item["model_messages"]["instructions_template"] for item in items] == ["", ""]


def test_codex_overrides_set_the_persistent_text_and_the_usage_flags_of_the_matching_models_only():
    snapshot = FakeSnapshot(codex_models={
        "gpt-6*": {"persistent_instructions": "Keep going until the task is done.",
                   "include_skills_usage_instructions": True, "include_apps_usage_instructions": True},
        "claude-*": {"include_plugin_usage_instructions": True},
        "*": {"persistent_instructions": "catch-all"}})
    items = by_slug(models.codex_catalog(ENTRIES, snapshot))
    sol = items["gpt-6-sol"]  # the first matching glob wins, so the catch-all does not reach it
    assert sol["model_messages"]["persistent_instructions"] == "Keep going until the task is done."
    assert (sol["include_skills_usage_instructions"], sol["include_plugin_usage_instructions"],
            sol["include_apps_usage_instructions"]) == (True, False, True)
    opus = items["claude-opus-5-5"]
    assert opus["model_messages"]["persistent_instructions"] == ""
    assert (opus["include_skills_usage_instructions"], opus["include_plugin_usage_instructions"],
            opus["include_apps_usage_instructions"]) == (False, True, False)
    for name in ("gpt-5.5", "gpt-4.1", "zai-org/GLM-5.3"):
        assert items[name]["model_messages"]["persistent_instructions"] == "catch-all"
        assert items[name]["include_skills_usage_instructions"] is False


def test_codex_overrides_do_not_touch_the_instruction_text_or_any_other_field():
    snapshot = FakeSnapshot(codex_models={"*": {"persistent_instructions": "P", "include_skills_usage_instructions": True}})
    changed, original = models.codex_catalog(ENTRIES, snapshot)["models"], legacy_codex_catalog(ENTRIES)["models"]
    for new, old in zip(changed, original):
        new = copy.deepcopy(new)
        new["model_messages"]["persistent_instructions"] = ""
        new["include_skills_usage_instructions"] = False
        assert new == old


def test_codex_override_values_of_the_wrong_type_are_ignored():
    snapshot = FakeSnapshot(codex_models={"*": {
        "persistent_instructions": ["not", "text"], "include_skills_usage_instructions": "yes",
        "include_plugin_usage_instructions": 1, "include_apps_usage_instructions": None}})
    assert dump(models.codex_catalog(ENTRIES, snapshot)) == dump(legacy_codex_catalog(ENTRIES))

    class Odd(FakeSnapshot):
        def codex_overrides(self, model):
            return None
    assert dump(models.codex_catalog(ENTRIES, Odd())) == dump(legacy_codex_catalog(ENTRIES))


def test_the_override_keys_p6_applies_are_the_ones_the_document_may_carry():
    assert set(prompt_policy.CODEX_OVERRIDE_KEYS) == {"persistent_instructions", *models._CODEX_USAGE_FLAGS}


def test_every_codex_entry_keeps_the_text_in_both_places_with_any_snapshot():
    snapshot = FakeSnapshot({"mine": {"publish_as_model": True}}, codex_base={"gpt-*": "G"},
                            codex_models={"claude-*": {"persistent_instructions": "P"}})
    for item in models.codex_catalog(None, snapshot)["models"]:
        assert item["base_instructions"] == item["model_messages"]["instructions_template"]
        assert isinstance(item["base_instructions"], str)


# -- virtual models -------------------------------------------------------------------------------

def test_virtual_entries_are_copies_of_their_base_with_their_own_id_label_and_marker():
    snapshot = FakeSnapshot({"mine": {"publish_as_model": True}})
    base_before = copy.deepcopy(ENTRIES)
    virtual = models.virtual_entries(ENTRIES, snapshot)
    assert ENTRIES == base_before  # the base entries are not touched, not repeated, not shared
    assert ids(virtual) == [entry["id"] + "@mine" for entry in ENTRIES]
    for item, entry in zip(virtual, ENTRIES):
        assert item["label"] == entry["label"] + " (profile mine)"
        assert item["virtual"] == {"base": entry["id"], "profile": "mine"}
        rest = {key: value for key, value in item.items() if key not in ("id", "label", "virtual")}
        assert rest == {key: value for key, value in entry.items() if key not in ("id", "label")}
        assert item["reasoning_effort_levels"] == entry["reasoning_effort_levels"]
        assert item["reasoning_effort_levels"] is not entry["reasoning_effort_levels"]
        assert "virtual" not in entry


def test_nothing_is_published_without_a_snapshot_and_nothing_by_default():
    assert models.virtual_entries(ENTRIES, None) == []
    assert models.virtual_entries(ENTRIES, FakeSnapshot()) == []
    assert models.virtual_entries(ENTRIES, FakeSnapshot({"mine": {}})) == []
    assert models.virtual_entries(ENTRIES, FakeSnapshot({"mine": {"publish_as_model": False}})) == []
    assert models.virtual_entries([], FakeSnapshot({"mine": {"publish_as_model": True}})) == []


@pytest.mark.parametrize("setting,expected", [
    (True, ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "gpt-6-sol", "gpt-5.5", "gpt-4.1",
            "zai-org/GLM-5.3"]),
    (["claude-opus-*"], ["claude-opus-5-5"]),
    (["claude-*"], ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001"]),
    (["CLAUDE-OPUS-5-5"], ["claude-opus-5-5"]),
    (["gpt-6-sol", "gpt-4.1"], ["gpt-6-sol", "gpt-4.1"]),
    (["gpt-?.?"], ["gpt-5.5", "gpt-4.1"]),
    (["zai-org/*"], ["zai-org/GLM-5.3"]),
    (["*5*"], ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "gpt-5.5", "zai-org/GLM-5.3"]),
    (["claude-opus-*", "gpt-6-*"], ["claude-opus-5-5", "gpt-6-sol"]),
    (["no-such-model-*"], []), ([], []), ("gpt-4.1", ["gpt-4.1"]),
    (False, []), (None, []), (0, []), ("", []), ([None, 5], []),
])
def test_publish_as_model_true_lists_every_model_and_a_glob_list_the_matching_ones(setting, expected):
    virtual = models.virtual_entries(ENTRIES, FakeSnapshot({"mine": {"publish_as_model": setting}}))
    assert [item["virtual"]["base"] for item in virtual] == expected
    assert ids(virtual) == [name + "@mine" for name in expected]


def test_profiles_publish_in_document_order_each_over_the_models_in_catalog_order():
    snapshot = FakeSnapshot({"second": {"publish_as_model": ["gpt-*"]}, "first": {"publish_as_model": ["claude-opus-*"]},
                             "all-of-them": {"publish_as_model": ["gpt-4.1", "claude-opus-5-5"]}})
    assert ids(models.virtual_entries(ENTRIES, snapshot)) == [
        "gpt-6-sol@second", "gpt-5.5@second", "gpt-4.1@second", "claude-opus-5-5@first",
        "claude-opus-5-5@all-of-them", "gpt-4.1@all-of-them"]


def test_the_built_in_passthrough_profile_is_not_published_unless_it_asks_to_be():
    snapshot = FakeSnapshot({"mine": {"publish_as_model": True}})
    assert "passthrough" in snapshot.profile_names()
    assert ids(models.virtual_entries(ENTRIES[:1], snapshot)) == ["claude-opus-5-5@mine"]


def test_profile_names_that_would_not_parse_back_are_not_published():
    class Odd(FakeSnapshot):
        def profile_names(self):
            return ["$header", "has space", "-lead", "", "ok-1", "x" * 65]

        def profile(self, name):
            return {"publish_as_model": True}
    assert ids(models.virtual_entries(ENTRIES[:1], Odd())) == ["claude-opus-5-5@ok-1"]


def test_an_entry_that_is_already_virtual_is_not_published_again():
    snapshot = FakeSnapshot({"mine": {"publish_as_model": True}})
    first = models.virtual_entries(ENTRIES[:2], snapshot)
    again = models.virtual_entries(ENTRIES[:2] + first, snapshot)
    assert ids(again) == ids(first)


def test_virtual_fields_name_the_base_and_the_profile_only_on_a_virtual_entry():
    snapshot = FakeSnapshot({"mine": {"publish_as_model": ["gpt-4.1"]}})
    assert models.virtual_fields(models.virtual_entries(ENTRIES, snapshot)[0]) == {
        "base_model": "gpt-4.1", "prompt_profile": "mine"}
    assert models.virtual_fields(ENTRIES[0]) == {} and models.virtual_fields(MINIMAL) == {}


def test_the_label_of_an_entry_without_one_falls_back_to_its_id():
    virtual = models.virtual_entries([MINIMAL], FakeSnapshot({"mine": {"publish_as_model": True}}))
    assert virtual[0]["label"] == "plain-text-model (profile mine)"


def test_codex_publishes_the_virtual_models_after_the_others_as_copies_of_their_base():
    snapshot = FakeSnapshot({"mine": {"publish_as_model": ["claude-opus-*", "gpt-6-sol"]}},
                            codex_base={"gpt-6-sol": "Sol text"},
                            codex_models={"claude-opus-5-5": {"persistent_instructions": "P", "include_skills_usage_instructions": True}})
    catalog = models.codex_catalog(ENTRIES, snapshot)["models"]
    assert [item["slug"] for item in catalog] == [entry["id"] for entry in ENTRIES] + [
        "claude-opus-5-5@mine", "gpt-6-sol@mine"]
    assert [item["priority"] for item in catalog] == list(range(len(ENTRIES) + 2))
    items = {item["slug"]: item for item in catalog}
    for virtual in ("claude-opus-5-5", "gpt-6-sol"):
        base, copy_ = items[virtual], items[virtual + "@mine"]
        assert copy_["display_name"] == base["display_name"] + " (profile mine)"
        assert copy_["description"] == "Retool relay: " + copy_["display_name"]
        # Text and overrides follow the base model, not the slug: the profile's own head is the relay's job.
        rest = {key: value for key, value in copy_.items() if key not in ("slug", "display_name", "description", "priority")}
        assert rest == {key: value for key, value in base.items() if key not in ("slug", "display_name", "description", "priority")}
    assert items["gpt-6-sol@mine"]["base_instructions"] == "Sol text"
    assert items["claude-opus-5-5@mine"]["model_messages"]["persistent_instructions"] == "P"
    assert items["claude-opus-5-5@mine"]["supported_reasoning_levels"] == items["claude-opus-5-5"]["supported_reasoning_levels"]


def test_codex_virtual_models_exist_only_for_the_models_it_is_given():
    snapshot = FakeSnapshot({"mine": {"publish_as_model": True}})
    assert [item["slug"] for item in models.codex_catalog([], snapshot)["models"]] == []
    only_gpt = [entry for entry in ENTRIES if entry["id"].startswith("gpt-")]
    assert [item["slug"] for item in models.codex_catalog(only_gpt, snapshot)["models"]] == (
        [entry["id"] for entry in only_gpt] + [entry["id"] + "@mine" for entry in only_gpt])


# -- the real engine (skipped while its accessors are not implemented) -----------------------------

def compiled(tmp_path, document):
    try:
        return prompt_policy.compile_document(document, tmp_path, rev=1)
    except NotImplementedError:
        pytest.skip("prompt_policy.compile_document is not implemented in this tree")


def test_the_real_snapshot_steers_the_catalogs_like_the_fake_one(tmp_path):
    snapshot = compiled(tmp_path, {
        "version": 1,
        "relay_texts": {"codex_base": {"gpt-6*": "Real engine text"}},
        "codex": {"models": {"gpt-6*": {"persistent_instructions": "Real persistent text",
                                        "include_skills_usage_instructions": True}}},
        "profiles": {"mine": {"head": "{{client_system}}", "publish_as_model": ["gpt-6-sol", "claude-opus-*"]},
                     "quiet": {"head": "{{client_system}}"}}})
    catalog = by_slug(models.codex_catalog(ENTRIES, snapshot))
    assert catalog["gpt-6-sol"]["base_instructions"] == catalog["gpt-6-sol"]["model_messages"][
        "instructions_template"] == "Real engine text"
    assert catalog["gpt-6-sol"]["model_messages"]["persistent_instructions"] == "Real persistent text"
    assert catalog["gpt-6-sol"]["include_skills_usage_instructions"] is True
    assert catalog["gpt-5.5"]["base_instructions"] == CODEX_TEXT
    assert set(catalog) == {entry["id"] for entry in ENTRIES} | {"gpt-6-sol@mine", "claude-opus-5-5@mine"}
    assert catalog["gpt-6-sol@mine"]["base_instructions"] == "Real engine text"
    assert ids(models.virtual_entries(ENTRIES, snapshot)) == ["claude-opus-5-5@mine", "gpt-6-sol@mine"]
