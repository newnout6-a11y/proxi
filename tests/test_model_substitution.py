"""A model that clients name but no account serves is answered by the model the owner chose for it, and the journal says so.

Codex's memory jobs ask for gpt-6-luna by name. Retool refuses that model, so the relay answered 404 (request 423d2c23bbc8
of 2026-11-06) and Codex never made a memory. gpt-6.1-terra answers in its place, at the same effort."""
import pytest

import apierr
import models
from test_conversation_state import stored
import journal


def test_the_substitute_is_the_model_the_owner_chose():
    assert models.substitute_for("gpt-6-luna") == "gpt-6.1-terra"
    assert models.resolve("gpt-6-luna")[0]["id"] == "gpt-6-luna"  # the catalog still describes the model itself
    entry, effort = models.resolve_tolerant("gpt-6-luna", "low")
    assert entry["id"] == "gpt-6.1-terra" and effort == "low"
    assert models.route_block_reason(entry, {"openai_provider_resource_name": "r"}) is None


def test_a_served_model_and_a_model_without_a_substitute_are_never_replaced():
    for name in ("gpt-6-sol", "gpt-6.1-terra", "gpt-5.6-luna", "claude-opus-5-5", "gpt-5.3-codex", "claude-opus-4-20250514"):
        assert models.substitute_for(name) is None
        assert models.resolve_tolerant(name)[0]["id"] == models.resolve(name)[0]["id"]


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "max"])
def test_the_effort_the_client_asked_for_is_kept(effort):
    assert models.resolve_tolerant("gpt-6-luna", effort)[1] == effort


def test_a_level_the_asked_model_itself_rejects_is_still_a_400():
    for effort in ("xhigh", "extra", "ultra"):
        with pytest.raises(apierr.ApiError) as caught:
            models.resolve_tolerant("gpt-6-luna", effort)
        assert caught.value.status == 400


def test_the_profile_suffix_and_the_effort_suffix_of_the_name_still_work():
    assert models.resolve_tolerant("gpt-6-luna#none")[1] == "none"
    assert models.resolve_tolerant("gpt-6-luna@mine")[0]["id"] == "gpt-6.1-terra"
    assert models.resolve_tolerant("openai/gpt-6-luna")[0]["id"] == "gpt-6.1-terra"
    assert models.substitute_for("openai/gpt-6-luna") == "gpt-6.1-terra"


@pytest.mark.parametrize("path,body", [
    ("/v1/responses", {"model": "gpt-6-luna", "input": "summarize", "reasoning": {"effort": "low"}}),
    ("/v1/chat/completions", {"model": "gpt-6-luna", "messages": [{"role": "user", "content": "summarize"}],
                              "reasoning_effort": "none"}),
    ("/v1/messages", {"model": "gpt-6-luna", "max_tokens": 64, "messages": [{"role": "user", "content": "x"}]}),
])
def test_the_request_is_served_by_the_substitute_and_the_journal_and_a_header_name_both(relay, path, body):
    seq = journal.JOURNAL.last_seq
    with relay.request(path, body) as response:
        assert response.status == 200
        assert response.headers["x-proxi-model-substituted"] == "gpt-6-luna=gpt-6.1-terra"
    assert relay.fake.seen[-1].model == "gpt-6.1-terra"
    events = stored(seq, "request.model_substituted")
    assert [(e["data"]["requested"], e["data"]["served"]) for e in events] == [("gpt-6-luna", "gpt-6.1-terra")]


def test_a_request_for_a_served_model_carries_no_substitution_note(relay):
    seq = journal.JOURNAL.last_seq
    with relay.request("/v1/responses", {"model": "gpt-6.1-terra", "input": "hi"}) as response:
        assert response.status == 200 and "x-proxi-model-substituted" not in response.headers
    assert not stored(seq, "request.model_substituted")


def test_the_catalogs_still_list_only_what_is_served(relay):
    _, listed = relay.json("/v1/models")
    assert "gpt-6-luna" not in {item["id"] for item in listed["data"]}
    _, codex = relay.json("/codex/models")
    assert "gpt-6-luna" not in {item["slug"] for item in codex["models"]}
