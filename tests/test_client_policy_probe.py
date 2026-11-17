import copy
import json
import subprocess
import sys
import urllib.request

import pytest

from scripts import probe_client_settings as probe
from scripts.probe_client_settings import catalog_policy


@pytest.fixture(autouse=True)
def fresh_probe_state():
    """The fake server's request log and child-agent flag are module globals; never let a test inherit them."""
    probe.records.clear()
    probe.task_sent = False
    yield
    probe.records.clear()
    probe.task_sent = False


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


def rejected(mode, model):
    result = catalog_policy(wrap({'gpt-6-sol': model}, mode), 'retool-relay/gpt-6-sol\n', mode)
    assert not result['passed'] and result['forbidden_variants'] == ['gpt-6-sol']
    return result['forbidden_variant_fields']


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
def test_each_rejection_names_the_model_the_field_and_the_problem(mode):
    block = 'settings' if mode == 'opencode2' else 'options'
    variant = ([{'id': 'high', 'settings': {'reasoningEffort': 'xhigh'}}] if mode == 'opencode2'
               else {'high': {'reasoningEffort': 'xhigh'}})
    refused = 'has a reasoning effort the route does not accept'
    assert rejected(mode, {block: {'reasoningEffort': 'xhigh'}}) == [{'model': 'gpt-6-sol', 'field': block,
                                                                      'problem': refused}]
    assert rejected(mode, {'variants': variant}) == [{'model': 'gpt-6-sol', 'field': 'variants.high',
                                                      'problem': refused}]
    assert rejected(mode, {block: {'reasoningEffort': 'high', 'effort': 'low'}}) == [
        {'model': 'gpt-6-sol', 'field': block, 'problem': 'sets conflicting reasoning efforts'}]
    assert rejected(mode, {'settings': 'text'}) == [{'model': 'gpt-6-sol', 'field': 'settings',
                                                     'problem': 'is not an object'}]
    assert rejected(mode, {'variants': 'text'}) == [
        {'model': 'gpt-6-sol', 'field': 'variants', 'problem': 'is neither an object nor a list of objects'}]
    assert rejected(mode, 'text') == [{'model': 'gpt-6-sol', 'field': 'entry', 'problem': 'is not an object'}]


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
def test_a_rejection_never_repeats_the_configured_value(mode):
    block = 'settings' if mode == 'opencode2' else 'options'
    assert 'private-level-value' not in json.dumps(rejected(mode, {block: {'reasoningEffort': 'private-level-value'}}))


def desktop(app_data, *versions, executable=True):
    for version in versions:
        folder = app_data / 'ai.opencode.desktop' / 'cli' / version
        folder.mkdir(parents=True)
        if executable:
            (folder / 'opencode-cli.exe').write_bytes(b'')


def test_desktop_cli_is_found_by_its_newest_version_not_a_hard_coded_one(tmp_path):
    assert probe.desktop_cli(tmp_path) is None
    desktop(tmp_path, '2.2.5-x64', '2.10.0-x64', '2.9.1-x64', 'latest')
    desktop(tmp_path, '2.11.0-x64', executable=False)
    found = probe.desktop_cli(tmp_path)
    assert found == str(tmp_path / 'ai.opencode.desktop' / 'cli' / '2.10.0-x64' / 'opencode-cli.exe')


def test_default_executables_follow_appdata_and_missing_desktop_cli_is_none(tmp_path):
    environ = {'APPDATA': str(tmp_path)}
    assert probe.default_executable('opencode2', environ) is None
    desktop(tmp_path, '2.4.0-x64')
    assert probe.default_executable('opencode2', environ) == str(
        tmp_path / 'ai.opencode.desktop' / 'cli' / '2.4.0-x64' / 'opencode-cli.exe')
    assert probe.default_executable('claude', environ).endswith('claude.exe')
    wrapper = probe.default_executable('opencode', environ)
    assert wrapper.startswith(str(tmp_path / 'npm')) and 'opencode-ai' in wrapper
    native = tmp_path / 'npm/node_modules/opencode-ai/node_modules/opencode-windows-x64/bin/opencode.exe'
    native.parent.mkdir(parents=True)
    native.write_bytes(b'')
    assert probe.default_executable('opencode', environ) == str(native)
    assert probe.default_executable('opencode', {}, home=tmp_path / 'home').startswith(str(tmp_path / 'home'))


def test_probe_stops_before_starting_anything_when_the_client_cannot_be_found(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    with pytest.raises(SystemExit) as error:
        probe.main(['opencode2'])
    assert error.value.code == 2 and '--executable' in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        probe.main(['opencode', '--executable', str(tmp_path / 'missing.exe')])
    assert error.value.code == 2
    assert 'missing.exe' in capsys.readouterr().err and not probe.records


RELAY_USER = {'provider': {'retool-relay': {'models': {'gpt-6-sol': {}}}}, 'model': 'retool-relay/gpt-6-sol'}
ELSEWHERE = 'anthropic/claude-sonnet-4-5'


def merged(base, extra):
    """OpenCode merges OPENCODE_CONFIG_CONTENT over the user's configuration, last."""
    if not (isinstance(base, dict) and isinstance(extra, dict)):
        return copy.deepcopy(extra)
    return {**base, **{key: merged(base.get(key), value) for key, value in extra.items()}}


def as_v2(config):
    return {{'provider': 'providers', 'agent': 'agents'}.get(key, key): value for key, value in config.items()}


def fake_client(monkeypatch, *, honor_override=True, requests=3, marker='PROXI_SMOKE_OK', code=0, user=RELAY_USER,
                dropped=(), recent=None, elsewhere=None):
    """Replace the installed client: it resolves its configuration like OpenCode, may post to the probe server, and
    never starts a process.

    The resolved configuration is the user's with the override merged last, less its `dropped` keys; OpenCode 2 lists
    the two sources apart. `run` sends the parent turns to --model, else the build agent's model, else `model`, else
    the model the TUI used last (`recent`), and the child turn to the explore agent's model, else the parent's. A
    turn for any other endpoint is never sent: it is added to `elsewhere`, or fails the test.
    """
    calls = []

    def run(args, env=None, **kwargs):
        calls.append((list(args), dict(env or {})))
        if 'OPENCODE_CONFIG_CONTENT' not in (env or {}):
            return subprocess.CompletedProcess(args, code, marker, 'client failed' if code else '')
        sent = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        override = {key: value for key, value in sent.items() if key not in dropped} if honor_override else {}
        config = merged(user, override)
        if 'debug' in args:
            if '--standalone' in args:
                sources = [{'type': 'document', 'info': as_v2(info)} for info in (user, override) if info]
                return subprocess.CompletedProcess(args, 0, json.dumps(sources + [{'type': 'directory', 'path': '.'}]), '')
            return subprocess.CompletedProcess(args, 0, json.dumps(config), '')
        if 'models' in args:
            return subprocess.CompletedProcess(args, 0, 'retool-relay/gpt-6-sol\n', '')
        agents = config.get('agent') or {}
        parent = ((args[args.index('--model') + 1] if '--model' in args else None)
                  or agents.get('build', {}).get('model') or config.get('model') or recent)
        child = agents.get('explore', {}).get('model') or parent
        base = config['provider']['retool-relay']['options']['baseURL']
        probe_base = sent['provider']['retool-relay']['options']['baseURL']
        for model in [parent, child, parent][:requests]:
            if not (str(model).startswith('retool-relay/') and base == probe_base):
                if elsewhere is None:
                    pytest.fail('a model request for %s left the probe' % model)
                elsewhere.append(model)
                continue
            request = urllib.request.Request(base + '/chat/completions', method='POST',
                                             data=json.dumps({'model': model.split('/', 1)[1],
                                                              'reasoning_effort': 'high'}).encode(),
                                             headers={'content-type': 'application/json'})
            urllib.request.urlopen(request, timeout=10).read()
        return subprocess.CompletedProcess(args, code, marker, 'client failed' if code else '')
    monkeypatch.setattr(probe, 'run_client', run)
    monkeypatch.setattr(probe, 'catalog_policy', lambda *args, **kwargs: {'passed': True})
    return calls


def run_probe(tmp_path, capsys, mode='opencode', *extra):
    code = probe.main([mode, '--executable', sys.executable, '--relay-config', str(tmp_path / 'absent.json'), *extra])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def test_probe_passes_only_when_the_fake_server_received_the_client_requests(tmp_path, monkeypatch, capsys):
    calls = fake_client(monkeypatch)
    code, report, error = run_probe(tmp_path, capsys)
    assert code == 0 and report['passed'] is True and 'error' not in report and not error
    assert len(report['requests']) == 3 and report['executable'] == sys.executable
    assert [args[1] for args, _ in calls] == ['debug', 'debug', 'models', 'run']
    assert all(args[0] == sys.executable for args, _ in calls)


def test_probe_fails_when_the_client_never_reached_the_fake_server(tmp_path, monkeypatch, capsys):
    fake_client(monkeypatch, requests=0)
    code, report, error = run_probe(tmp_path, capsys)
    assert code == 1 and report['passed'] is False and report['requests'] == []
    assert 'received no model request' in report['error'] and 'real relay' in report['error']
    assert report['success_marker'] is True and 'received no model request' in error


def test_probe_sends_no_model_request_when_the_endpoint_override_was_not_applied(tmp_path, monkeypatch, capsys):
    user = merged(RELAY_USER, {'provider': {'retool-relay': {'options': {
        'baseURL': 'http://127.0.0.1:8787/v1', 'apiKey': 'user-configured-key'}}}})
    calls = fake_client(monkeypatch, honor_override=False, user=user)
    code, report, error = run_probe(tmp_path, capsys, 'opencode2')
    assert code == 1 and report['passed'] is False and 'probe endpoint' in report['error']
    assert 'probe endpoint' in error and report['requests'] == []
    assert not [args for args, _ in calls if 'run' in args]
    assert [args for args, _ in calls if 'debug' in args][0][-1] == '--standalone'


@pytest.mark.parametrize('user,recent', [
    ({**RELAY_USER, 'model': ELSEWHERE}, None),
    ({'provider': RELAY_USER['provider']}, ELSEWHERE),
    ({**RELAY_USER, 'agent': {'build': {'model': ELSEWHERE}}}, None),
    ({**RELAY_USER, 'agent': {'explore': {'model': ELSEWHERE}}}, None),
], ids=['default model', 'last model of the TUI', 'build agent', 'explore child'])
def test_opencode_probe_keeps_every_model_request_on_the_fake_provider(tmp_path, monkeypatch, capsys, user, recent):
    """The probe overrides only the retool-relay endpoint, and the run used the user's own models: a default, build
    or explore model on another provider (or, with no `model` set, the one the TUI used last) got a real, paid
    request with the user's own key from auth.json, and the probe noticed only afterwards that its fake got nothing.
    The run is now pinned to the relay provider: --model, and every model of the run in the override."""
    elsewhere = []
    calls = fake_client(monkeypatch, user=user, recent=recent, elsewhere=elsewhere)
    code, report, _ = run_probe(tmp_path, capsys)
    assert elsewhere == []
    assert code == 0 and report['passed'] is True and len(report['requests']) == 3
    args, env = calls[-1]
    assert args[args.index('--model') + 1] == report['pinned_models']['--model'] == 'retool-relay/gpt-6-sol'
    pins = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert pins['model'] == pins['small_model'] == 'retool-relay/gpt-6-sol'
    assert {name: agent['model'] for name, agent in pins['agent'].items()} == dict.fromkeys(
        probe.RUN_AGENTS, 'retool-relay/gpt-6-sol')


def test_opencode_probe_still_tests_the_users_own_relay_models(tmp_path, monkeypatch, capsys):
    user = merged(RELAY_USER, {'provider': {'retool-relay': {'models': {'claude-opus-5-5': {}, 'claude-sonnet-5-5': {}}}},
                               'model': 'retool-relay/claude-opus-5-5', 'small_model': ELSEWHERE,
                               'agent': {'explore': {'model': 'retool-relay/claude-sonnet-5-5'}}})
    calls = fake_client(monkeypatch, user=user)
    code, report, _ = run_probe(tmp_path, capsys)
    assert code == 0 and [row['model'] for row in report['requests']] == [
        'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5']
    pins = json.loads(calls[-1][1]['OPENCODE_CONFIG_CONTENT'])
    assert pins['small_model'] == pins['agent']['title']['model'] == 'retool-relay/claude-opus-5-5'
    assert pins['agent']['explore']['model'] == 'retool-relay/claude-sonnet-5-5'


@pytest.mark.parametrize('user,dropped,refusal', [
    # A client that applied the endpoint but not the pinned models would still run the user's model elsewhere.
    ({**RELAY_USER, 'model': ELSEWHERE}, ('model', 'small_model', 'agent'), 'other providers'),
    # The relay under another provider name: its requests would carry the real relay key of the file.
    ({'provider': {'proxi': {'options': {'baseURL': 'http://127.0.0.1:9/v1', 'apiKey': 'real-relay-key'},
                             'models': {'gpt-6-sol': {}}}}, 'model': 'proxi/gpt-6-sol'}, (), 'defines no retool-relay'),
], ids=['pins ignored', 'relay under another name'])
def test_opencode_probe_sends_nothing_when_the_run_cannot_be_pinned(tmp_path, monkeypatch, capsys, user, dropped,
                                                                    refusal):
    elsewhere = []
    calls = fake_client(monkeypatch, user=user, dropped=dropped, elsewhere=elsewhere)
    code, report, error = run_probe(tmp_path, capsys)
    assert elsewhere == [] and not [args for args, _ in calls if 'run' in args]
    assert code == 1 and report['passed'] is False and refusal in report['error'] and refusal in error
    assert report['requests'] == []


def documents(*sources):
    return [{'type': 'document', 'info': as_v2(info)} for info in sources] + [{'type': 'directory', 'path': '.'}]


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
@pytest.mark.parametrize('field,reference', [('model', ELSEWHERE), ('small_model', ELSEWHERE),
                                             ('agent.build.model', ELSEWHERE), ('agent.explore.model', ELSEWHERE),
                                             ('agent.general.model', 'proxi/gpt-6-sol'),
                                             ('agent.title.model', ELSEWHERE), ('agent.compaction.model', ELSEWHERE)])
def test_the_run_is_refused_while_any_model_it_can_reach_is_on_another_provider(mode, field, reference):
    """The check that guarded the run looked only for the endpoint the probe had injected itself."""
    *path, key = field.split('.')
    stray = {key: reference}
    for part in reversed(path):
        stray = {part: stray}
    config = merged(RELAY_USER, stray)
    with pytest.raises(probe.ProbeError, match='other providers') as refusal:
        probe.check_pinned(documents(config) if mode == 'opencode2' else config, mode)
    named = field.replace('agent.', 'agents.', 1) if mode == 'opencode2' else field
    assert '(%s=%s)' % (named, reference) in str(refusal.value)


@pytest.mark.parametrize('mode', ['opencode', 'opencode2'])
def test_a_run_on_relay_models_only_passes_the_check(mode):
    config = merged(RELAY_USER, {'small_model': 'retool-relay/gpt-6-sol',
                                 'agent': {name: {'model': 'retool-relay/gpt-6-sol#high'} for name in probe.RUN_AGENTS},
                                 # A command, or an agent the run never starts, gets no request from the probe.
                                 'command': {'review': {'model': ELSEWHERE}}})
    config['agent']['reviewer'] = {'model': ELSEWHERE}
    probe.check_pinned(documents(config) if mode == 'opencode2' else config, mode)


def test_opencode2_sources_are_checked_apart_because_they_are_listed_unmerged():
    pinned = {'model': 'retool-relay/gpt-6-sol', 'agent': {'explore': {'model': 'retool-relay/gpt-6-sol'}}}
    with pytest.raises(probe.ProbeError, match='agents.explore.model=' + ELSEWHERE):
        probe.check_pinned(documents({'agent': {'explore': {'model': ELSEWHERE}}}, pinned), 'opencode2')


@pytest.mark.parametrize('mode,config', [('opencode', []), ('opencode2', {'model': 'retool-relay/gpt-6-sol'})])
def test_a_resolved_configuration_of_an_unexpected_shape_is_refused(mode, config):
    with pytest.raises(probe.ProbeError, match='unexpected shape'):
        probe.check_pinned(config, mode)


def test_probe_children_get_a_deliberately_invalid_key_and_no_inherited_credentials(tmp_path, monkeypatch, capsys):
    for name, value in (('RELAY_API_KEY', 'real-relay-key'), ('ANTHROPIC_API_KEY', 'real-anthropic-key'),
                        ('RETOOL_RELAY_API_KEY', 'real-harness-key'), ('KEEP_THIS_SETTING', 'kept')):
        monkeypatch.setenv(name, value)
    calls = fake_client(monkeypatch)
    code, report, _ = run_probe(tmp_path, capsys)
    assert code == 0
    for _, env in calls:
        assert not {'RELAY_API_KEY', 'ANTHROPIC_API_KEY', 'RETOOL_RELAY_API_KEY'} & set(env)
        assert env['KEEP_THIS_SETTING'] == 'kept' and env['PYTHONUTF8'] == '1'
    options = json.loads(calls[-1][1]['OPENCODE_CONFIG_CONTENT'])['provider']['retool-relay']['options']
    assert options['apiKey'] == probe.PROBE_KEY and options['baseURL'].startswith('http://127.0.0.1:')
    assert not probe.PROBE_KEY.startswith('relay-')


def test_claude_probe_overrides_endpoint_and_key_in_settings_and_in_the_process_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PROXI_PROBE_CHILD', '')
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'http://127.0.0.1:8787')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'real-anthropic-key')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'real-anthropic-token')
    calls = fake_client(monkeypatch, code=1)
    code, report, _ = run_probe(tmp_path, capsys, 'claude')
    assert code == 1 and report['passed'] is False and 'received no model request' in report['error']
    (args, env), = calls
    assert args[0] == sys.executable
    settings = json.loads(args[args.index('--settings') + 1])['env']
    assert settings['ANTHROPIC_BASE_URL'].startswith('http://127.0.0.1:') and settings['ANTHROPIC_BASE_URL'] != 'http://127.0.0.1:8787'
    assert {key: env[key] for key in settings} == settings
    assert env['ANTHROPIC_API_KEY'] == probe.PROBE_KEY and env['ANTHROPIC_AUTH_TOKEN'] == ''


def test_explicit_executable_wins_over_the_detected_desktop_cli(tmp_path, monkeypatch, capsys):
    desktop(tmp_path, '2.2.5-x64')
    monkeypatch.setenv('APPDATA', str(tmp_path))
    calls = fake_client(monkeypatch)
    code, report, _ = run_probe(tmp_path, capsys, 'opencode2')
    assert code == 0 and report['executable'] == sys.executable
    assert all(args[0] == sys.executable for args, _ in calls)
    assert calls[-1][0][:4] == [sys.executable, 'run', '--standalone', '--format']
