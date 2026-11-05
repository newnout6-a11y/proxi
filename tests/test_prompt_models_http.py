"""/v1/models and /codex/models with a prompts snapshot behind runtime.prompts (the store is replaced by a fake).

Without a snapshot both answers are what they were; with one, the published profiles add <model>@<profile> after
the other models, and a failing prompt layer must not take discovery down.
"""
import json

import pytest

import config as config_module
import journal
import models
import prompt_policy
from prompt_models_support import FakeSnapshot, store  # noqa: F401  (store is a fixture)
from test_prompt_models_catalog import CODEX_TEXT, dump, legacy_codex_catalog

ROW_METADATA = ("metadata_checked_at", "metadata_source", "metadata_status", "metadata_notes")


@pytest.fixture
def served(store, relay):
    """The relay fixture, built after the fake store is in place."""
    return relay


def legacy_rows(entries):
    """The /v1/models rows exactly as proxy.py built them before virtual models."""
    return [{
        "id": model["id"], "object": "model", "owned_by": model["provider"], "label": model["label"],
        "context_window": model["context"], "max_output_tokens": model.get("max_output"),
        **models.desktop_metadata(model, entries),
        **{key: model[key] for key in ROW_METADATA if key in model},
        "capabilities": {"reasoning": model["reasoning"], "tools": model["tools"],
                         "vision": bool(model.get("vision", False)),
                         "reasoning_effort_levels": model.get("reasoning_effort_levels", []),
                         "default_reasoning_effort": model.get("default_reasoning_effort"),
                         "reasoning_effort_control": model.get("reasoning_effort_control", "selectable")},
    } for model in entries]


def listed(served, path="/v1/models"):
    status, payload = served.json(path)
    assert status == 200
    return payload["data"] if "data" in payload else payload["models"]


def raw(served, path):
    with served.request(path) as response:
        assert response.status == 200
        return response.read()


def runtime_errors(events):
    return [event for event in events.query(limit=1000) if event["kind"] == "prompt.runtime_error"]


@pytest.fixture
def events(monkeypatch):
    """A journal of its own, so that only the events of this test are read."""
    instance = journal.Journal()
    instance.configure({"relay_api_key": "test-key-123"})
    monkeypatch.setattr(journal, "JOURNAL", instance)
    return instance


# -- /v1/models -----------------------------------------------------------------------------------

def test_the_model_list_without_a_snapshot_is_what_the_relay_always_listed(served):
    rows = listed(served)
    entries = [entry for entry in models.catalog() if entry["id"] in {row["id"] for row in rows}]
    assert len(entries) == len(rows) > 20
    assert json.dumps(rows, ensure_ascii=False) == json.dumps(legacy_rows(entries), ensure_ascii=False)
    assert not [row for row in rows if "@" in row["id"] or "base_model" in row or "prompt_profile" in row]


def test_a_snapshot_without_a_publishing_profile_changes_not_one_byte(served, store):
    before = raw(served, "/v1/models")
    codex_before = raw(served, "/codex/models")
    store.current = FakeSnapshot({"mine": {}, "other": {"publish_as_model": False}})
    assert raw(served, "/v1/models") == before
    assert raw(served, "/codex/models") == codex_before


def test_published_profiles_add_virtual_rows_after_the_unchanged_ones(served, store):
    baseline = listed(served)
    store.current = FakeSnapshot({"mine": {"publish_as_model": ["claude-opus-*", "gpt-6-sol"]},
                                  "everything": {"publish_as_model": True}})
    rows = listed(served)
    assert rows[:len(baseline)] == baseline  # the base rows are untouched and come first
    virtual = rows[len(baseline):]
    base_ids = [row["id"] for row in baseline]
    assert [row["id"] for row in virtual] == (
        [name + "@mine" for name in base_ids if name == "gpt-6-sol" or name.startswith("claude-opus-")]
        + [name + "@everything" for name in base_ids])
    assert {"gpt-6-sol@mine", "claude-opus-5-5@mine", "claude-opus-4-8@mine"} <= {row["id"] for row in virtual}
    by_id = {row["id"]: row for row in rows}
    for row in virtual:
        base = by_id[row["base_model"]]
        assert row["prompt_profile"] == row["id"].partition("@")[2]
        assert row["label"] == row["display_name"] == base["label"] + " (profile %s)" % row["prompt_profile"]
        assert row["description"] == "Retool relay: " + row["label"]
        for key in ("object", "owned_by", "context_window", "max_output_tokens", "capabilities", "max_input_tokens",
                    "supports_1m", "anthropic_family_tier", *ROW_METADATA):
            assert row.get(key) == base.get(key), (row["id"], key)
        assert "is_family_default" not in row or row["is_family_default"] is False
    # Only the base model is the family default Desktop starts from.
    assert by_id["claude-opus-5-5"]["is_family_default"] is True
    assert by_id["claude-opus-5-5@mine"].get("is_family_default") is False


def test_only_models_the_relay_publishes_get_a_virtual_row(served, store):
    baseline = {row["id"] for row in listed(served)}
    hidden = {entry["id"] for entry in models.catalog()} - baseline
    assert {"gpt-6-astra", "claude-fable-5"} <= hidden  # gated routes are not listed, so they have no virtual copy
    store.current = FakeSnapshot({"mine": {"publish_as_model": True}})
    rows = {row["id"] for row in listed(served)}
    assert rows == baseline | {name + "@mine" for name in baseline}


def test_the_models_url_with_a_trailing_slash_lists_the_same(served, store):
    store.current = FakeSnapshot({"mine": {"publish_as_model": ["gpt-4.1"]}})
    status, payload = served.json("/v1/models/")
    assert status == 200 and "gpt-4.1@mine" in [row["id"] for row in payload["data"]]


# -- /codex/models --------------------------------------------------------------------------------

def test_the_codex_catalog_without_a_snapshot_is_the_previous_one(served):
    items = listed(served, "/codex/models")
    entries = [entry for entry in models.catalog() if entry["id"] in {item["slug"] for item in items}]
    assert json.dumps({"models": items}, ensure_ascii=False) == dump(legacy_codex_catalog(entries))


def test_the_codex_catalog_carries_the_documents_base_text_overrides_and_virtual_models(served, store):
    baseline = listed(served, "/codex/models")
    store.current = FakeSnapshot(
        {"mine": {"publish_as_model": ["gpt-6-sol"]}}, codex_base={"gpt-6*": "From the document"},
        codex_models={"gpt-6*": {"persistent_instructions": "Persistent", "include_apps_usage_instructions": True}})
    items = {item["slug"]: item for item in listed(served, "/codex/models")}
    assert [item["slug"] for item in listed(served, "/codex/models")] == (
        [item["slug"] for item in baseline] + ["gpt-6-sol@mine"])
    sol, virtual = items["gpt-6-sol"], items["gpt-6-sol@mine"]
    assert sol["base_instructions"] == sol["model_messages"]["instructions_template"] == "From the document"
    assert sol["model_messages"]["persistent_instructions"] == "Persistent"
    assert sol["include_apps_usage_instructions"] is True
    assert virtual["base_instructions"] == "From the document" and virtual["display_name"].endswith("(profile mine)")
    other = items["gpt-5.5"]
    assert other["base_instructions"] == CODEX_TEXT and other["include_apps_usage_instructions"] is False


# -- a failing prompt layer -----------------------------------------------------------------------

class BrokenNames(FakeSnapshot):
    def profile_names(self):
        raise RuntimeError("profile index is broken")


class BrokenText(FakeSnapshot):
    def codex_base(self, model):
        raise RuntimeError("codex text is broken")


@pytest.mark.parametrize("path,snapshot", [("/v1/models", BrokenNames), ("/codex/models", BrokenNames),
                                           ("/codex/models", BrokenText)])
def test_a_failing_snapshot_falls_back_to_the_plain_catalog_and_is_journaled(served, store, events, path, snapshot):
    clean = raw(served, path)
    assert runtime_errors(events) == []
    store.current = snapshot({"mine": {"publish_as_model": True}})
    assert raw(served, path) == clean
    errors = runtime_errors(events)
    assert len(errors) == 1 and errors[0]["level"] == "error"
    assert "is broken" in errors[0]["message"] and "RuntimeError" in errors[0]["message"]


def test_a_codex_text_that_fails_does_not_touch_the_openai_model_list(served, store, events):
    store.current = BrokenText({"mine": {"publish_as_model": ["gpt-4.1"]}})
    assert "gpt-4.1@mine" in [row["id"] for row in listed(served)]  # /v1/models never reads codex_base
    assert runtime_errors(events) == []


def test_a_failure_unrelated_to_the_prompt_layer_is_not_hidden(served, store, events, monkeypatch):
    def broken(entries=None, snapshot=None):
        raise RuntimeError("catalog bug")
    monkeypatch.setattr(models, "codex_catalog", broken)
    assert served.status_of("/codex/models") == 500  # no snapshot: nothing to fall back from
    assert runtime_errors(events) == []
    store.current = FakeSnapshot()
    assert served.status_of("/codex/models") == 500  # a snapshot, but the plain catalog fails as well


# -- completions with a virtual model name --------------------------------------------------------

CHAT = {"messages": [{"role": "user", "content": "hi"}]}
ANTHROPIC = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
RESPONSES = {"input": "hi"}


@pytest.mark.parametrize("path,payload", [("/v1/chat/completions", CHAT), ("/v1/messages", ANTHROPIC),
                                          ("/v1/responses", RESPONSES)])
def test_a_request_for_a_virtual_model_reaches_the_runner_with_the_base_model_and_the_profile(served, path, payload):
    status, _ = served.json(path, dict(payload, model="claude-opus@mine#high"))
    assert status == 200
    parsed = served.fake.seen[-1]
    assert (parsed.model, parsed.requested_model, parsed.virtual_profile, parsed.effort) == (
        "claude-opus-5-5", "claude-opus", "mine", "high")


@pytest.mark.parametrize("path,payload", [("/v1/chat/completions", CHAT), ("/v1/messages", ANTHROPIC),
                                          ("/v1/responses", RESPONSES)])
@pytest.mark.parametrize("name", ["claude-opus-5-5@bad name", "claude-opus-5-5#high@mine", "@mine",
                                  "claude-opus-5-5@mine@x"])
def test_a_malformed_virtual_name_is_a_400_before_any_work(served, path, payload, name):
    seen = len(served.fake.seen)
    status, body = served.json_any(path, dict(payload, model=name))
    assert status == 400 and len(served.fake.seen) == seen
    message = body["error"]["message"]
    assert name[:20] in message or "profile" in message
    if path == "/v1/chat/completions":
        assert body["error"]["param"] == "model"


def test_count_tokens_accepts_a_virtual_model_name(served):
    status, body = served.json("/v1/messages/count_tokens", dict(ANTHROPIC, model="claude-opus-5-5@mine"))
    assert status == 200 and body["input_tokens"] > 0


# -- the real prompt store (skipped while prompt_policy is only the contract skeleton) -------------

@pytest.fixture
def real_prompts(monkeypatch, tmp_path):
    """cfg["prompts_file"] for the relay under test: the real PromptStore loads this file in its constructor."""
    document = {"version": 1,
                "relay_texts": {"codex_base": {"gpt-6*": "Real engine text"}},
                "codex": {"models": {"gpt-6*": {"persistent_instructions": "Real persistent text"}}},
                "profiles": {"mine": {"head": "{{client_system}}", "publish_as_model": ["gpt-6-sol", "claude-opus-*"]}}}
    try:
        prompt_policy.compile_document(document, tmp_path, rev=1)
    except NotImplementedError:
        pytest.skip("prompt_policy is only the contract skeleton in this tree")
    path = tmp_path / "real-prompts.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setitem(config_module.DEFAULTS, "prompts_file", str(path))
    return path


def test_the_real_store_publishes_virtual_models_and_codex_texts_through_the_routes(real_prompts, relay):
    rows = {row["id"]: row for row in listed(relay)}
    assert {"gpt-6-sol@mine", "claude-opus-5-5@mine"} <= set(rows) and "gpt-5.5@mine" not in rows
    assert rows["gpt-6-sol@mine"]["base_model"] == "gpt-6-sol" and rows["gpt-6-sol@mine"]["prompt_profile"] == "mine"
    items = {item["slug"]: item for item in listed(relay, "/codex/models")}
    sol = items["gpt-6-sol"]
    assert sol["base_instructions"] == sol["model_messages"]["instructions_template"] == "Real engine text"
    assert sol["model_messages"]["persistent_instructions"] == "Real persistent text"
    assert items["gpt-6-sol@mine"]["base_instructions"] == "Real engine text"
    assert items["gpt-5.5"]["base_instructions"] == CODEX_TEXT
