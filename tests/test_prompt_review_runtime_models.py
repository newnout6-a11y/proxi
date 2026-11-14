"""R2-13: '@' separates a prompt profile only after a model of the catalog; any other id that contains one is one model."""
import pytest

CHAT = {"messages": [{"role": "user", "content": "hi"}]}
ANTHROPIC = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}
RESPONSES = {"input": "hi"}
ROUTES = [("/v1/chat/completions", CHAT), ("/v1/messages", ANTHROPIC), ("/v1/responses", RESPONSES)]
VERTEX = "claude-sonnet-4-5@20250929"  # Vertex AI spells a Claude version this way; the catalog's id is ...-4-5-20250929


@pytest.mark.parametrize("path,payload", ROUTES)
def test_strict_models_refuses_a_vertex_style_id_as_an_unknown_model_never_as_a_missing_profile(relay, path, payload):
    status, body = relay.json_any(path, dict(payload, model=VERTEX))
    assert status == 404
    assert body["error"]["message"] == "unknown model 'claude-sonnet-4-5@20250929'; see GET /v1/models"
    assert "profile" not in body["error"]["message"] and relay.fake.seen == []


@pytest.mark.parametrize("path,payload", ROUTES)
def test_strict_models_off_sends_a_vertex_style_id_on_as_the_model_it_always_was(relay, path, payload):
    relay.cfg["strict_models"] = False
    status, _ = relay.json(path, dict(payload, model=VERTEX))
    assert status == 200
    parsed = relay.fake.seen[-1]
    assert (parsed.model, parsed.requested_model, parsed.virtual_profile) == (VERTEX, VERTEX, None)
    assert parsed.entry["id"] == VERTEX


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("path,payload", ROUTES)
def test_a_catalog_model_with_a_profile_that_does_not_exist_is_still_a_404_that_names_the_profile(
        relay, path, payload, strict):
    relay.cfg["strict_models"] = strict
    status, body = relay.json_any(path, dict(payload, model="claude-opus-5-5@ghost"))
    assert status == 404 and "profile 'ghost'" in body["error"]["message"] and relay.fake.seen == []
    if path == "/v1/chat/completions":
        assert body["error"]["param"] == "model"
