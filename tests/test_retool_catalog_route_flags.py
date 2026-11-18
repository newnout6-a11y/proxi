"""The catalog look reaches a model on the route Retool serves it on, and its registry flag of fine-grained tool streaming
is what decides whether the relay asks for that mode.

R1a R10: retool_refresh.probe built its body with direct.build_body, which always carries the App Builder marker
(metadata.sandpackAgent), and never asked again without it, as the transport does for the models of Assist: the overlay of
2026-11-18 holds three failures "not available right now" each for gpt-4o, gpt-4o-mini, gpt-4.1-mini and o3, which Retool's
registry serves to managed keys on the Assist route (context copilot).

R1a R11: retool_catalog.chat_models dropped the registry's supportsFineGrainedStreaming, so 0d205e9 had to name the seven
Claude models that have it in direct.py. The registry is now the source; that list serves until a look has read it."""
import io
import json

import pytest

import direct
import retool_catalog
import retool_refresh
from test_retool_refresh import Account, Frontend, Stream, isolated_catalog, refresher, registry  # noqa: F401 - fixture

NOT_NOW = b'{"statusCode":400,"error":"Bad Request","message":"gpt-4o is not available right now. Please try again later or ' \
          b'select a different model.","data":null}'
TOOLS = [{"name": "Write", "description": "write a file", "params": {}}]


@pytest.fixture(autouse=True)
def registry_flags(monkeypatch):
    monkeypatch.setattr(retool_catalog, "FINE_GRAINED", retool_catalog.FINE_GRAINED)
    monkeypatch.setattr(retool_refresh.auth, "upstream_headers", lambda cfg: {"cookie": "c"})


def answering(sent, refusal=NOT_NOW):
    """urlopen that refuses a body with the marker as the App Builder route does, and answers one without it."""
    def urlopen(request, timeout):
        body = json.loads(request.data)
        sent.append(body)
        if "metadata" in body:
            raise retool_refresh.urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(refusal))
        text = json.dumps(body)
        word = text[text.index("READY-"):text.index("READY-") + 14]
        return Stream([{"event": "content_block_delta", "type": "text", "partialText": word},
                       {"event": "metadata", "usage": {"creditsUsed": 0.001}}, {"event": "end_of_stream"}])
    return urlopen


def test_a_model_of_the_assist_route_is_asked_again_without_the_marker_and_answers(monkeypatch):
    sent = []
    monkeypatch.setattr(retool_refresh.urllib.request, "urlopen", answering(sent))
    result = retool_refresh.probe(Account(), {"id": "gpt-4o", "provider": "openai", "reasoning_effort_levels": []})
    assert result["ok"] is True and result["http"] == 200 and result["error"] is None
    assert ["metadata" in body for body in sent] == [True, False]
    assert sent[1] == direct._unmarked(sent[0]), "the same request, without the marker only"


def test_another_refusal_is_not_asked_again(monkeypatch):
    sent = []
    monkeypatch.setattr(retool_refresh.urllib.request, "urlopen",
                        answering(sent, refusal=b'{"statusCode":400,"message":"Invalid request format"}'))
    result = retool_refresh.probe(Account(), {"id": "gpt-4o", "provider": "openai", "reasoning_effort_levels": []})
    assert result["ok"] is False and result["http"] == 400 and len(sent) == 1


# -- fine-grained tool streaming from the registry ------------------------------------------------------------------
def flagged(**models):
    """The test registry with supportsFineGrainedStreaming set as given (model id -> flag)."""
    document = registry()
    for provider in document.values():
        for ident, model in (provider.get("models") or {}).items():
            if ident in models:
                model.setdefault("capabilities", {})["supportsFineGrainedStreaming"] = models[ident]
    return document


def test_the_registry_flag_is_kept_with_each_chat_model():
    found = retool_catalog.chat_models(retool_catalog.parse_registry(Frontend(flagged(**{"gpt-7-mini": True})).pages[
        retool_refresh.EDGE + "index-Reg456.js"]))
    assert found["gpt-7-mini"]["fine_grained_streaming"] is True
    assert found["claude-fable-5-2"]["fine_grained_streaming"] is False


def test_before_any_look_the_seven_models_of_the_bundle_stream_finely():
    retool_catalog.FINE_GRAINED = None
    assert direct.fine_grained_streaming("claude-opus-5-5") and not direct.fine_grained_streaming("claude-sonnet-5-5")
    assert direct.build_body({}, {"model": "claude-opus-5-5"}, "h", "p", TOOLS, None, None)["enableFineGrainedStreaming"]


def test_after_a_look_the_registry_decides(tmp_path):
    # Deprecated models count too: a client may still route one, and Retool still streams its calls finely.
    document = flagged(**{"gpt-7-mini": True, "claude-3-haiku": True, "claude-opus-5-5": False})
    value, _ = refresher(tmp_path, Frontend(document), {})
    value.run_once()
    assert retool_catalog.read(tmp_path / retool_catalog.AUTO_NAME)["fine_grained_streaming"] == [
        "claude-3-haiku", "gpt-7-mini"]
    assert direct.fine_grained_streaming("gpt-7-mini") and direct.fine_grained_streaming("claude-3-haiku")
    assert not direct.fine_grained_streaming("claude-opus-5-5")
    assert "enableFineGrainedStreaming" not in direct.build_body({}, {"model": "claude-opus-5-5"}, "h", "p", TOOLS, None,
                                                                 None)


def test_the_overlay_gives_the_flags_at_load_and_an_old_overlay_keeps_the_fallback(tmp_path):
    path = tmp_path / retool_catalog.AUTO_NAME
    retool_catalog.write(path, {"fine_grained_streaming": ["gpt-7-mini"]})
    retool_catalog.apply_overlay(path)
    assert direct.fine_grained_streaming("gpt-7-mini") and not direct.fine_grained_streaming("claude-opus-5-5")
    retool_catalog.FINE_GRAINED = None
    retool_catalog.write(path, {"registry": {}})  # written before the flag was kept
    retool_catalog.apply_overlay(path)
    assert direct.fine_grained_streaming("claude-opus-5-5")
