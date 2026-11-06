"""Codex's Ultra reasoning reaches the relay's catalog: maximum reasoning plus delegation Codex runs itself.

Codex sends max for Ultra (openai/codex df1199f "the inference backend still expects its existing max effort value",
7f135e1 "When Ultra is selected ... prefer max") and grants the proactive multi-agent delegation only to a model whose
catalog entry says multi_agent_version v2. The relay listed only the efforts Retool takes, so Ultra was gone from the
picker of Codex Desktop."""
import models
import validate
from test_client_contracts import CFG, _body


def levels(slug):
    item = next(m for m in models.codex_catalog()["models"] if m["slug"] == slug)
    return [level["effort"] for level in item["supported_reasoning_levels"]], item.get("multi_agent_version")


def test_a_model_whose_route_takes_max_offers_ultra_with_delegation():
    for slug in ("gpt-6.1-sol", "gpt-6-sol", "claude-opus-5-5", "claude-sonnet-5-5"):
        efforts, version = levels(slug)
        assert efforts[-2:] == ["max", "ultra"] and version == "v2", slug


def test_a_model_without_max_offers_no_ultra():
    for slug in ("gpt-5.5", "gpt-5.6-sol", "gpt-5-nano"):
        efforts, version = levels(slug)
        assert "ultra" not in efforts and version is None, slug


def test_ultra_on_the_wire_means_max():
    for body in (_body("responses", reasoning={"effort": "ultra"}), _body("chat", reasoning_effort="ultra"),
                 _body("responses", reasoning={"effort": "ULTRA"})):
        dialect = "responses" if "input" in body else "chat"
        parsed = getattr(validate, "parse_" + dialect)(dict(body, model="gpt-6.1-sol"), CFG)
        assert parsed.requested_effort == "max"
