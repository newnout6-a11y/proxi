"""The App Builder marker goes with requestOrigin "Browser", as every request of Retool's own frontend that carries it.

In the public frontend (4.99.0, 2026-11-18) the two requests to copilotStreamGPT that set metadata.sandpackAgent both send
{sandpackAgent: true, requestOrigin: "Browser"} (SandpackAgentOverlay @180355, ReleaseSelect @51162), and the R2 agent sends
requestOrigin "Browser" with every prompt; Retool's AI monitoring sorts the credits of an organization by that origin
(Browser, MCP, CLI, Other, else Unknown). The relay sent the marker alone, a body no browser sends (R1a R7). Neither rit nor
the cache key covers metadata, so no conversation's prefix changes."""
import direct

PROVIDER = {"model": "claude-opus-5-5", "reasoningEffort": "high"}


def test_the_marker_goes_with_the_browser_origin():
    body = direct.build_body({}, PROVIDER, "head", "hi", [], None, None)
    assert body["metadata"] == {"sandpackAgent": True, "requestOrigin": "Browser"}


def test_the_assist_route_still_gets_no_metadata_at_all():
    body = direct._unmarked(direct.build_body({}, PROVIDER, "head", "hi", [], None, None))
    assert "metadata" not in body


def test_the_origin_changes_neither_the_integrity_hint_nor_the_cache_key():
    body = direct.build_body({}, PROVIDER, "head", "hi", [], None, None)
    assert body["rit"] == direct.make_rit(body["messages"], None)
    assert body["openAICacheKey"] == direct.cache_key_for("head", "claude-opus-5-5", [])
