"""Offline regression tests for R3/R6 IDE catalog sync hardening."""
import ctypes
import os
import stat
from types import SimpleNamespace

import pytest

import client_sync
from scripts import sync_claude_desktop_catalog as sync_desktop
from scripts import sync_harness_catalog as sync_harness
from scripts import sync_opencode_catalog as sync_opencode


def _minimal_opencode_doc(extra_models=None):
    return {
        'model': 'retool-relay/claude-opus-5-5',
        'provider': {
            'retool-relay': {
                'npm': '@ai-sdk/anthropic',
                'options': {'baseURL': 'http://127.0.0.1:8787/v1', 'apiKey': 'test-key'},
                'models': extra_models or {},
            },
        },
    }


def _minimal_harness_doc():
    return [
        {
            'id': 'llm-pi-ai',
            'config': {
                'providers': {
                    'retool': {
                        'displayName': 'Retool Relay',
                        'api': 'openai-completions',
                        'baseURL': 'http://127.0.0.1:8787/v1',
                        'apiKeyEnv': 'RETOOL_RELAY_API_KEY',
                        'reasoning': 'medium',
                        'models': [],
                    },
                },
            },
        },
    ]


class _WinFn:
    def __init__(self, impl):
        self._impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._impl(*args)


def test_client_sync_runs_harness_when_opencode_disabled_and_falls_back_to_stdout(tmp_path, monkeypatch):
    relay_cfg = tmp_path / 'config.json'
    relay_cfg.write_text('{}', encoding='utf-8')
    harness_cfg = tmp_path / 'desktop' / 'cordis.patch.yml'
    harness_cfg.parent.mkdir(parents=True)
    harness_cfg.write_text('[]\n', encoding='utf-8')

    seen = []

    def fake_run(command, **_kwargs):
        seen.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"written": true, "changed": true, "models": 4, "backup": "b.bak"}\n',
            stderr='',
        )

    monkeypatch.setattr(client_sync.journal, 'emit', lambda *_a, **_kw: None)
    monkeypatch.setattr(client_sync.console_log, 'write', lambda *_a, **_kw: None)
    syncer = client_sync.OpenCodeSync(
        {'_config_path': str(relay_cfg), 'opencode_sync': False, 'harness_sync': True,
         'harness_settings': str(harness_cfg)},
        runner=fake_run,
    )
    res = syncer('minara')
    assert len(seen) == 1
    assert str(client_sync.HARNESS_SCRIPT) == seen[0][1]
    assert res == {
        'harness': [{'written': True, 'changed': True, 'models': 4, 'backup': 'b.bak',
                     'reason': 'minara', 'settings': str(harness_cfg)}],
        'written': True,
        'changed': True,
    }

    disabled = client_sync.OpenCodeSync(
        {'_config_path': str(relay_cfg), 'opencode_sync': False, 'harness_sync': False},
        runner=fake_run,
    )
    assert disabled('minara') is None

    def failing_stdout(command, **_kwargs):
        return SimpleNamespace(returncode=2, stdout='line 1\nSyncError: bad YAML\n', stderr='')

    fail_syncer = client_sync.OpenCodeSync({'_config_path': str(relay_cfg)}, runner=failing_stdout)
    err = fail_syncer._exec_script(client_sync.HARNESS_SCRIPT, relay_cfg, harness_cfg)
    assert err == {'error': 'SyncError: bad YAML'}


def test_opencode_handles_null_context_non_reasoning_invalid_effort_and_minara_small():
    entries = [
        {'id': 'or/custom/null-limits', 'provider': 'openrouter', 'provider_id': 'or',
         'context': None, 'max_output': None, 'reasoning': False,
         'reasoning_effort_levels': ['low', 'high'], 'tools': True},
        {'id': 'minara/claude-haiku-4-5', 'upstream': 'minara', 'provider': 'anthropic',
         'context': 0, 'max_output': False, 'reasoning': True,
         'reasoning_effort_levels': ['low', 'medium', 'high'], 'default_reasoning_effort': 'xhigh',
         'tools': True},
        {'id': 'claude-opus-5-5@strict', 'provider': 'anthropic', 'label': 'Opus (strict)',
         'context': 200000, 'max_output': 32000, 'reasoning': True,
         'reasoning_effort_levels': ['low', 'medium', 'high'], 'default_reasoning_effort': None,
         'tools': True, 'virtual': {'profile': 'strict', 'base': 'claude-opus-5-5'}},
    ]
    doc = _minimal_opencode_doc({'minara/claude-haiku-4-5': {'options': {'reasoningEffort': 'max'}}})
    doc['model'] = 'retool-relay/claude-opus-5-5@strict'
    doc['small_model'] = 'retool-relay/gone-small'

    warnings = []
    updated = sync_opencode.update_document(doc, entries, default_model='claude-opus-5-5@strict', warnings=warnings)
    models_out = updated['provider']['retool-relay']['models']

    assert models_out['or/custom/null-limits']['limit'] == {
        'context': sync_opencode.UNKNOWN_CONTEXT,
        'output': sync_opencode.UNKNOWN_OUTPUT,
    }
    assert models_out['or/custom/null-limits']['name'] == 'or/custom/null-limits'
    assert models_out['or/custom/null-limits']['variants'] == {}
    assert models_out['or/custom/null-limits']['options'] == {'reasoningEffort': 'none'}

    assert models_out['minara/claude-haiku-4-5']['limit'] == {
        'context': sync_opencode.UNKNOWN_CONTEXT,
        'output': sync_opencode.UNKNOWN_OUTPUT,
    }
    # Invalid default_reasoning_effort ('xhigh' not in levels) falls back cleanly to 'medium'.
    assert models_out['minara/claude-haiku-4-5']['options'] == {'reasoningEffort': 'medium'}
    # None default stays omitted when model has no valid previous selection.
    assert models_out['claude-opus-5-5@strict']['options'] == {}
    # Minara small-model fallback replaces stale small_model cleanly.
    assert updated['small_model'] == 'retool-relay/minara/claude-haiku-4-5'


def test_harness_handles_null_bool_context_and_omitted_label():
    doc = _minimal_harness_doc()
    entries = [
        {'id': 'or/deepseek/chat', 'context': None, 'max_output': True, 'reasoning': False,
         'reasoning_effort_levels': []},
        {'id': 'claude-opus-5-5@strict', 'label': 'Opus @strict', 'context': 250000, 'max_output': 64000,
         'reasoning': True, 'reasoning_effort_levels': ['low', 'medium', 'high'],
         'virtual': {'profile': 'strict', 'base': 'claude-opus-5-5'}},
        {'id': 'smart-opus', 'label': 'Smart Opus', 'context': -10, 'max_output': 0, 'reasoning': True,
         'reasoning_effort_levels': ['low', 'medium', 'high'], 'is_route_alias': True},
    ]
    updated = sync_harness.update_document(doc, entries)
    providers = updated[0]['config']['providers']
    reasoning_models = {m['id']: m for m in providers['retool']['models']}
    text_models = {m['id']: m for m in providers['retool-text']['models']}

    assert text_models['or/deepseek/chat']['name'] == 'or/deepseek/chat'
    assert text_models['or/deepseek/chat']['contextWindow'] == sync_harness.UNKNOWN_CONTEXT
    assert text_models['or/deepseek/chat']['maxTokens'] == sync_harness.UNKNOWN_OUTPUT
    assert reasoning_models['smart-opus']['contextWindow'] == sync_harness.UNKNOWN_CONTEXT
    assert reasoning_models['smart-opus']['maxTokens'] == sync_harness.UNKNOWN_OUTPUT
    assert 'claude-opus-5-5@strict' in reasoning_models


def test_desktop_sync_strips_colon_tag_and_matches_undated_legacy_claude():
    entries = [
        {'id': 'or/anthropic/claude-opus-4.6:free', 'provider': 'anthropic', 'provider_id': 'or',
         'label': 'Opus 4.6 Free', 'reasoning_effort_levels': ['low', 'medium', 'high', 'max']},
        {'id': 'claude-smart-opus-free', 'provider': 'anthropic', 'is_route_alias': True,
         'label': 'Smart Opus Free', 'reasoning_effort_levels': ['low', 'medium', 'high', 'max'],
         'route_targets': [{'id': 'or/anthropic/claude-opus-4.6:free'}]},
        {'id': 'minara/claude-opus-4-5', 'provider': 'anthropic', 'upstream': 'minara',
         'label': 'Minara Opus 4.5', 'reasoning_effort_levels': ['low', 'medium', 'high']},
        {'id': 'claude-opus-4-6@strict', 'provider': 'anthropic', 'label': 'Opus 4.6 @strict',
         'reasoning_effort_levels': ['low', 'medium', 'high', 'max'],
         'virtual': {'profile': 'strict', 'base': 'claude-opus-4-6'}},
    ]
    catalog = {'claude-opus-4-6': ('low', 'medium', 'high', 'max')}
    rows_listed = sync_desktop.build_catalog(entries, [], desktop=catalog, allow_downgrade=False)
    listed_by_name = {row['name']: row for row in rows_listed}
    assert listed_by_name['or/anthropic/claude-opus-4.6:free']['maxEffort'] == 'max'
    assert listed_by_name['claude-smart-opus-free']['maxEffort'] == 'max'
    assert listed_by_name['claude-opus-4-6@strict']['maxEffort'] == 'max'
    assert 'minara/claude-opus-4-5' not in listed_by_name

    # Without Desktop catalog file, NATIVE_EFFORTS fallback governs: undated legacy 4.5 strips stale maxEffort.
    previous = [{'name': 'minara/claude-opus-4-5', 'maxEffort': 'high'}]
    rows_fallback = sync_desktop.build_catalog(entries, previous, desktop=None, allow_downgrade=False)
    fallback_by_name = {row['name']: row for row in rows_fallback}
    assert fallback_by_name['or/anthropic/claude-opus-4.6:free']['maxEffort'] == 'max'
    assert 'maxEffort' not in fallback_by_name['minara/claude-opus-4-5']


def test_desktop_sync_auto_emits_behaves_as_and_supports1m_without_overwriting_overrides():
    entries = [
        {'id': 'claude-opus-5-5', 'provider': 'anthropic', 'label': 'Opus 5.5',
         'context': 1_000_000, 'route_input_tokens': 872_000,
         'reasoning_effort_levels': ['low', 'medium', 'high', 'max']},
        {'id': 'minara/claude-fable-5-2', 'provider': 'anthropic', 'upstream': 'minara',
         'label': 'Claude Fable 5.2 (Minara)', 'context': 1_000_000,
         'reasoning_effort_levels': ['low', 'medium', 'high', 'xhigh', 'max']},
        {'id': 'minara/claude-opus-5-5', 'provider': 'anthropic', 'upstream': 'minara',
         'label': 'Claude Opus 5.5 (Minara)', 'context': 1_000_000,
         'reasoning_effort_levels': ['low', 'medium', 'high', 'xhigh', 'max']},
        {'id': 'minara/claude-haiku-4-5', 'provider': 'anthropic', 'upstream': 'minara',
         'label': 'Claude Haiku 4.5 (Minara)', 'context': 200_000,
         'reasoning_effort_levels': []},
        {'id': 'or/anthropic/claude-opus-4.6:free', 'provider': 'anthropic', 'provider_id': 'or',
         'label': 'Opus 4.6 Free', 'context': 1_000_000,
         'reasoning_effort_levels': ['low', 'medium', 'high', 'max']},
        {'id': 'claude-smart-1m', 'provider': 'anthropic', 'is_route_alias': True,
         'label': 'Smart 1M', 'context': 1_000_000,
         'reasoning_effort_levels': ['low', 'medium', 'high', 'max'],
         'route_targets': [{'id': 'minara/claude-opus-5-5'}]},
        {'id': 'minara/claude-fable-5-2@strict', 'provider': 'anthropic', 'upstream': 'minara',
         'label': 'Fable 5.2 @strict', 'context': 1_000_000,
         'reasoning_effort_levels': ['low', 'medium', 'high', 'xhigh', 'max'],
         'virtual': {'profile': 'strict', 'base': 'minara/claude-fable-5-2'}},
    ]
    previous = [
        # Existing Minara row missing behavesAs/supports1m must get backfilled.
        {'name': 'minara/claude-opus-5-5', 'labelOverride': 'My Minara Opus'},
        # Explicit user overrides must be preserved untouched.
        {'name': 'minara/claude-fable-5-2', 'behavesAs': 'custom-fable', 'supports1m': False},
    ]
    rows = sync_desktop.build_catalog(entries, previous, desktop=None, allow_downgrade=True)
    by_name = {row['name']: row for row in rows}

    # Bare native id never emits behavesAs; Retool <1M route limit does not falsely set supports1m.
    assert 'behavesAs' not in by_name['claude-opus-5-5']
    assert 'supports1m' not in by_name['claude-opus-5-5']

    # Existing Minara row without overrides receives behavesAs + supports1m.
    assert by_name['minara/claude-opus-5-5']['behavesAs'] == 'claude-opus-5-5'
    assert by_name['minara/claude-opus-5-5']['supports1m'] is True
    assert by_name['minara/claude-opus-5-5']['maxEffort'] == 'max'

    # Explicit user overrides survive untouched.
    assert by_name['minara/claude-fable-5-2']['behavesAs'] == 'custom-fable'
    assert by_name['minara/claude-fable-5-2']['supports1m'] is False
    assert by_name['minara/claude-fable-5-2']['maxEffort'] == 'max'

    # Sub-1M Minara row still gets behavesAs (for native model lookup) without supports1m.
    assert by_name['minara/claude-haiku-4-5']['behavesAs'] == 'claude-haiku-4-5'
    assert 'supports1m' not in by_name['minara/claude-haiku-4-5']

    # Universal colon-tagged, route alias, and virtual @profile rows get behavesAs + supports1m.
    assert by_name['or/anthropic/claude-opus-4.6:free']['behavesAs'] == 'claude-opus-4-6'
    assert by_name['or/anthropic/claude-opus-4.6:free']['supports1m'] is True
    assert by_name['claude-smart-1m']['behavesAs'] == 'claude-opus-5-5'
    assert by_name['claude-smart-1m']['supports1m'] is True
    assert by_name['minara/claude-fable-5-2@strict']['behavesAs'] == 'claude-fable-5-2'
    assert by_name['minara/claude-fable-5-2@strict']['supports1m'] is True


def test_windows_replace_retries_transient_locks_and_restores_1177(tmp_path, monkeypatch):
    target = tmp_path / 'opencode.json'
    replacement = tmp_path / 'opencode.json.tmp'
    backup = tmp_path / 'backup.bak'
    target.write_bytes(b'old')
    replacement.write_bytes(b'new')

    attempts = {'count': 0, 'last_err': 0}

    def fake_replace(dest, src, bak, _flags, _r1, _r2):
        attempts['count'] += 1
        if attempts['count'] < 3:
            attempts['last_err'] = 32 if attempts['count'] == 1 else 5
            return 0
        os.replace(dest, bak)
        os.replace(src, dest)
        return 1

    monkeypatch.setattr(ctypes, 'WinDLL', lambda *_a, **_kw: SimpleNamespace(ReplaceFileW=_WinFn(fake_replace)),
                        raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: attempts['last_err'])

    def fake_winerror(code):
        err = PermissionError(f'winerror {code}')
        err.winerror = code
        return err

    monkeypatch.setattr(ctypes, 'WinError', fake_winerror)
    monkeypatch.setattr(sync_opencode.time, 'sleep', lambda *_a: None)

    sync_opencode._replace_windows(target, replacement, backup)
    assert attempts['count'] == 3
    assert target.read_bytes() == b'new'
    assert backup.read_bytes() == b'old'

    # Error 1177 leaves the original moved into `backup` and must NOT retry.
    target2 = tmp_path / 'opencode2.json'
    replacement2 = tmp_path / 'opencode2.json.tmp'
    backup2 = tmp_path / 'backup2.bak'
    target2.write_bytes(b'orig2')
    replacement2.write_bytes(b'new2')
    calls_1177 = {'n': 0}

    def fail_1177(dest, _src, bak, _flags, _r1, _r2):
        calls_1177['n'] += 1
        os.replace(dest, bak)
        attempts['last_err'] = 1177
        return 0

    monkeypatch.setattr(ctypes, 'WinDLL', lambda *_a, **_kw: SimpleNamespace(ReplaceFileW=_WinFn(fail_1177)),
                        raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda code: OSError(None, f'err {code}', None, code))
    with pytest.raises(OSError):
        sync_opencode._replace_windows(target2, replacement2, backup2)
    assert calls_1177['n'] == 1
    assert backup2.exists() and not target2.exists()
    sync_opencode._restore_original(target2, backup2)
    assert target2.read_bytes() == b'orig2'


def test_unlink_quiet_handles_readonly_and_does_not_mask_upstream_error(tmp_path, monkeypatch):
    ro = tmp_path / 'ro.tmp'
    ro.write_bytes(b'x')
    os.chmod(ro, stat.S_IREAD)
    sync_opencode._unlink_quiet(ro)
    assert not ro.exists()

    # Even if unlink always raises PermissionError, write_backed_up keeps its real error.
    cfg = tmp_path / 'opencode.json'
    cfg.write_bytes(b'v1')
    backups = tmp_path / 'backups'
    monkeypatch.setattr(sync_opencode, '_replace_with_backup', lambda *_a: (_ for _ in ()).throw(ValueError('boom')))
    real_unlink = os.unlink
    monkeypatch.setattr(os, 'unlink', lambda p: (_ for _ in ()).throw(PermissionError('held by scanner')))
    monkeypatch.setattr(sync_opencode.time, 'sleep', lambda *_a: None)
    try:
        with pytest.raises(ValueError, match='boom'):
            sync_opencode.write_backed_up(cfg, b'v1', b'v2', backups)
    finally:
        monkeypatch.setattr(os, 'unlink', real_unlink)
        for leftover in tmp_path.glob('.proxi-catalog-*.tmp'):
            real_unlink(leftover)
