"""Unsupported effort must fail before upstream spend unless explicitly enabled."""
import io
import json
import urllib.error

import pytest

from apierr import ApiError
import models
from scripts import probe_effort
import validate


def body(dialect, effort):
    if dialect == "responses":
        return {"model": "claude-opus-5-5", "input": "OK", "reasoning": {"effort": effort}}
    return {"model": "claude-opus-5-5", "messages": [{"role": "user", "content": "OK"}],
            "max_tokens": 1024, "reasoning_effort": effort}


CFG = {"strict_models": True, "model": "claude-opus-5-5", "show_thinking": True}


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("effort", ["xhigh", "none", "extra", "ultra"])
def test_strict_effort_rejects_unsupported_controls(dialect, effort):
    with pytest.raises(ApiError) as caught:
        getattr(validate, "parse_" + dialect)(body(dialect, effort), CFG)
    assert caught.value.status == 400


@pytest.mark.parametrize("dialect", ["chat", "responses", "anthropic"])
def test_compatibility_is_explicit_and_retains_requested_level(dialect):
    parsed = getattr(validate, "parse_" + dialect)(body(dialect, "xhigh"),
                                                  dict(CFG, allow_effort_downgrade=True))
    assert parsed.requested_effort == "xhigh"
    assert parsed.effort == "high"


@pytest.mark.parametrize("suffix", ["#xhigh", ":xhigh"])
def test_model_suffix_cannot_bypass_strict_policy(suffix):
    data = {"model": "claude-opus-5-5" + suffix, "input": "OK"}
    with pytest.raises(ApiError):
        validate.parse_responses(data, CFG)
    parsed = validate.parse_responses(data, dict(CFG, allow_effort_downgrade=True))
    assert (parsed.requested_effort, parsed.effort) == ("xhigh", "high")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "max"])
def test_supported_effort_is_preserved(effort):
    assert validate.parse_chat(body("chat", effort), CFG).effort == effort


@pytest.mark.parametrize("compatibility", [False, True])
def test_unknown_effort_is_never_guessed(compatibility):
    with pytest.raises(ApiError):
        models.resolve_tolerant("claude-opus-5-5", "extra", allow_downgrade=compatibility)


@pytest.mark.parametrize("dialect,path", [("chat", "/v1/chat/completions"),
                                         ("responses", "/v1/responses"),
                                         ("anthropic", "/v1/messages")])
@pytest.mark.parametrize("model", ["claude-opus-5-5", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"])
def test_gateway_rejects_before_the_runner_is_called(relay, dialect, path, model):
    data = dict(body(dialect, "xhigh"), model=model)
    status, _ = relay.json_any(path, data)
    assert status == 400
    assert relay.fake.seen == []


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"])
def test_codex_does_not_offer_live_rejected_effort(relay, model):
    status, response = relay.json("/codex/models")
    assert status == 200
    entry = next(item for item in response["models"] if item["slug"] == model)
    assert "xhigh" not in [item["effort"] for item in entry["supported_reasoning_levels"]]


def test_live_probe_tests_the_raw_level_without_refreshing_or_printing_auth(monkeypatch):
    cfg = {"base": "https://fixture.invalid", "ua": "test", "cookie": "accessToken=private-cookie",
           "xsrf": "private-xsrf", "provider_resource_name": "__managed__"}
    captured = []

    def reject(request, **_):
        captured.append(json.loads(request.data))
        raise urllib.error.HTTPError(request.full_url, 422, "invalid", {},
                                     io.BytesIO(b"private-cookie private-xsrf malformed effort"))

    monkeypatch.setattr(probe_effort.urllib.request, "urlopen", reject)
    result = probe_effort.probe(cfg, "claude-opus-5-5", "xhigh")
    assert captured[0]["reasoningEffort"] == "xhigh"
    assert len(captured) == 1
    assert result["http"] == 422
    assert "private-cookie" not in json.dumps(result)
    assert "private-xsrf" not in json.dumps(result)


def test_gateway_probe_cleans_up_its_server_and_journal_without_upstream_calls(tmp_path, monkeypatch):
    import config
    monkeypatch.chdir(tmp_path)
    cfg = dict(config.DEFAULTS, base="https://fixture.invalid", ua="test", cookie="accessToken=synthetic",
               xsrf="synthetic-xsrf", provider_resource_name="__managed__")
    results = list(probe_effort.gateway_probe(cfg, "claude-opus-5-5", ["xhigh"], ["chat"]))
    assert results[0]["http"] == 400
    assert results[0]["sent_upstream_efforts"] == []
    assert list((tmp_path / ".agents").iterdir()) == []
