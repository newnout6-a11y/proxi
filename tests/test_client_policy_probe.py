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
    policy = catalog_policy(wrap({"gpt-6-astra": {}}, mode), "retool-relay/gpt-5.3-codex\n", mode)
    assert policy["passed"] is False
    assert policy["forbidden_models"] == ["gpt-6-astra"]
    assert policy["catalog_forbidden_models"] == ["gpt-5.3-codex"]


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


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
def test_working_haiku_is_not_forbidden_by_name(mode):
    assert catalog_policy(wrap({'claude-haiku-4-5-20251001': {}}, mode),
                          'retool-relay/claude-haiku-4-5-20251001\n', mode)['passed']


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
@pytest.mark.parametrize('model,label,effort', [('gpt-5.5', 'max', 'max'), ('gpt-6-sol', 'high', 'xhigh')])
def test_variants_check_actual_per_model_effort_in_both_schemas(mode, model, label, effort):
    choices = ([{'id': label, 'settings': {'reasoningEffort': effort}}] if mode == 'opencode2' else
               {label: {'reasoningEffort': effort}})
    result = catalog_policy(wrap({model: {'variants': choices}}, mode), 'retool-relay/' + model + '\n', mode)
    assert not result['passed'] and result['forbidden_variants'] == [model]


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
def test_alias_duplicates_and_catalog_config_mismatch_cannot_pass(mode):
    result = catalog_policy(wrap({'gpt-6-sol': {}, 'gpt-6': {}}, mode), 'retool-relay/gpt-6-sol\n', mode)
    assert not result['passed']
    assert result['forbidden_models'] == ['gpt-6']


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
def test_explicit_eligible_catalog_can_include_byok_only_model(mode):
    import models
    result = catalog_policy(wrap({'gpt-6-astra': {}}, mode), 'retool-relay/gpt-6-astra\n', mode,
                            allowed_entries=[models.resolve('gpt-6-astra')[0]])
    assert result['passed']


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
def test_default_options_and_nested_effort_controls_are_also_validated(mode):
    settings = {'settings' if mode == 'opencode2' else 'options': {'reasoningEffort': 'xhigh'}}
    result = catalog_policy(wrap({'gpt-6-sol': settings}, mode), 'retool-relay/gpt-6-sol\n', mode)
    assert not result['passed'] and result['forbidden_variants'] == ['gpt-6-sol']
    variants = ([{'id': 'high', 'settings': {'reasoningEffort': 'high', 'body': {'reasoningEffort': 'xhigh'}}}]
                if mode == 'opencode2' else {'high': {'reasoningEffort': 'high', 'body': {'reasoningEffort': 'xhigh'}}})
    result = catalog_policy(wrap({'gpt-6-sol': {'variants': variants}}, mode), 'retool-relay/gpt-6-sol\n', mode)
    assert not result['passed']
