"""Catalog hardening: admin/proxy routable parity, synthetic caps, vision enforcement."""
import admin
import models
import pytest
import validate
from apierr import ApiError


def test_admin_routable_mirrors_proxy_semantics():
    panel = admin.Admin.__new__(admin.Admin)
    panel.cfg = {"model": "claude-opus-5-5"}
    anthropic = {"id": "claude-opus-5-5", "provider": "anthropic"}
    openai = {"id": "gpt-6-sol", "provider": "openai"}
    # Nothing configured: show everything (previously every model was "no resource").
    assert panel._routable(anthropic) is True
    assert panel._routable(openai) is True
    # Only anthropic configured: openai hidden unless default/show_unrouted.
    panel.cfg = {"model": "claude-opus-5-5", "provider_resource_name": "r"}
    assert panel._routable(anthropic) is True
    assert panel._routable(openai) is False
    panel.cfg = {"model": "gpt-6-sol", "provider_resource_name": "r", "show_unrouted_models": True}
    assert panel._routable(openai) is True


def test_synthetic_tolerant_model_has_bounded_output():
    """An unknown model in lenient mode used to inherit MAX_TOKENS_CAP = 1M."""
    entry, _effort = models.resolve("mystery-model", "high", strict=False)
    assert entry["max_output"] == 128000


def test_vision_flag_is_enforced_for_glm():
    """GLM-5.3 (vision=False) previously accepted image blocks and failed at
    upstream with a bare 400."""
    data = {"model": "zai-org/GLM-5.3", "max_tokens": 16,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}]}]}
    with pytest.raises(ApiError) as exc:
        validate.parse_anthropic(data, {"strict_models": True})
    assert exc.value.status == 422
    assert "does not support image input" in exc.value.message


def test_vision_models_still_accept_images():
    data = {"model": "claude-opus-5-5", "max_tokens": 16,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}}]}]}
    parsed = validate.parse_anthropic(data, {"strict_models": True})
    assert parsed.entry["id"] == "claude-opus-5-5"


def test_assist_seeds_survive_flush():
    import direct
    direct._ASSIST_ONLY.update(f"self-learned-{i}" for i in range(210))
    direct._ASSIST_ONLY.intersection_update(direct._ASSIST_SEEDS)
    assert "claude-haiku-4-5-20251001" in direct._ASSIST_ONLY, \
        "flushing the self-learned set must keep the proven seeds"
    assert len(direct._ASSIST_ONLY) == len(direct._ASSIST_SEEDS)
