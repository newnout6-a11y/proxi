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


def fake_client(monkeypatch, *, honor_override=True, requests=3, marker='PROXI_SMOKE_OK', code=0):
    """Replace the installed client: it reads the override, may post to the probe server, and never starts a process."""
    calls = []

    def run(args, env=None, **kwargs):
        calls.append((list(args), dict(env or {})))
        override = json.loads(env['OPENCODE_CONFIG_CONTENT']) if 'OPENCODE_CONFIG_CONTENT' in env else None
        if 'debug' in args:
            options = override['provider']['retool-relay']['options'] if honor_override else {
                'baseURL': 'http://127.0.0.1:8787/v1', 'apiKey': 'user-configured-key'}
            return subprocess.CompletedProcess(args, 0, json.dumps({'provider': {'retool-relay': {'options': options}}}), '')
        if 'models' in args:
            return subprocess.CompletedProcess(args, 0, 'retool-relay/gpt-6-sol\n', '')
        for _ in range(requests if override else 0):
            base = override['provider']['retool-relay']['options']['baseURL']
            request = urllib.request.Request(base + '/chat/completions', method='POST',
                                             data=json.dumps({'model': 'gpt-6-sol', 'reasoning_effort': 'high'}).encode(),
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
    assert [args[1] for args, _ in calls] == ['debug', 'models', 'run']
    assert all(args[0] == sys.executable for args, _ in calls)


def test_probe_fails_when_the_client_never_reached_the_fake_server(tmp_path, monkeypatch, capsys):
    fake_client(monkeypatch, requests=0)
    code, report, error = run_probe(tmp_path, capsys)
    assert code == 1 and report['passed'] is False and report['requests'] == []
    assert 'received no model request' in report['error'] and 'real relay' in report['error']
    assert report['success_marker'] is True and 'received no model request' in error


def test_probe_sends_no_model_request_when_the_endpoint_override_was_not_applied(tmp_path, monkeypatch, capsys):
    calls = fake_client(monkeypatch, honor_override=False)
    code, report, error = run_probe(tmp_path, capsys, 'opencode2')
    assert code == 1 and report['passed'] is False and 'probe endpoint' in report['error']
    assert 'probe endpoint' in error and report['requests'] == []
    assert not [args for args, _ in calls if 'run' in args]
    assert [args for args, _ in calls if 'debug' in args][0][-1] == '--standalone'


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
