import pytest

from scripts.probe_client_settings import catalog_policy


def wrap(models, mode):
    if mode == "opencode2":
        return [{"type": "document", "info": {"providers": {"retool-relay": {"models": models}}}},
                {"type": "directory", "path": "unused"}]
    return {"provider": {"retool-relay": {"models": models}}}


@pytest.mark.parametrize("mode", ["opencode", "opencode2"])
def test_probe_rejects_xhigh_even_when_migrated_disabled_is_present(mode):
    variants = [{"id": "xhigh", "settings": {"disabled": True, "reasoningEffort": "xhigh"}}] if mode == "opencode2" else {"xhigh": {"disabled": True}}
    policy = catalog_policy(wrap({"gpt-6-sol": {"variants": variants}}, mode), "retool-relay/gpt-6-sol\n", mode)
    assert policy["passed"] is False
    assert policy["forbidden_variants"] == ["gpt-6-sol"]


@pytest.mark.parametrize("mode", ["opencode", "opencode2"])
def test_probe_detects_unavailable_models_in_config_and_effective_catalog(mode):
    policy = catalog_policy(wrap({"gpt-6-astra": {}}, mode), "retool-relay/claude-haiku-4-5-20251001\n", mode)
    assert policy["passed"] is False
    assert policy["forbidden_models"] == ["gpt-6-astra"]
    assert policy["catalog_forbidden_models"] == ["claude-haiku-4-5-20251001"]


@pytest.mark.parametrize("mode", ["opencode", "opencode2"])
def test_probe_accepts_supported_choices_and_ignores_other_providers(mode):
    variants = [{"id": "high", "settings": {"reasoningEffort": "high"}}] if mode == "opencode2" else {"high": {"reasoningEffort": "high"}}
    policy = catalog_policy(wrap({"gpt-6-sol": {"variants": variants}}, mode),
                            "other-provider/gpt-6-astra\nretool-relay/gpt-6-sol\n", mode)
    assert policy["passed"] is True
    assert policy["configured_models"] == policy["catalog_models"] == 1


@pytest.mark.parametrize("mode", ["opencode", "opencode2"])
def test_missing_catalog_cannot_pass_by_vacuous_empty_checks(mode):
    assert catalog_policy(wrap({}, mode), "", mode)["passed"] is False
