"""Sync only the relay catalog in OpenCode's shared legacy JSON configuration.

OpenCode 1 reads this schema directly; installed OpenCode 2 migrates it on load.
Preview is read-only. --write requires a backup directory and preserves other
providers, authentication and unrelated settings.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import models
from apierr import ApiError
from scripts.audit_models import load_accounts
from scripts.probe_client_settings import catalog_policy, effort_controls

PROVIDER = 'retool-relay'


def _clear_effort(value):
    if not isinstance(value, dict):
        return
    for key in ('reasoningEffort', 'reasoning_effort', 'effort'):
        value.pop(key, None)
    for key in ('settings', 'options', 'body', 'extra_body', 'reasoning', 'output_config'):
        if isinstance(value.get(key), dict):
            _clear_effort(value[key])
            if not value[key]:
                value.pop(key)
    provider_options = value.get('providerOptions')
    if isinstance(provider_options, dict) and isinstance(provider_options.get('openai'), dict):
        _clear_effort(provider_options['openai'])
        if not provider_options['openai']:
            provider_options.pop('openai')
        if not provider_options:
            value.pop('providerOptions')
    if isinstance(value.get('thinking'), dict):
        for key in ('type', 'effort', 'budget_tokens'):
            value['thinking'].pop(key, None)
        if not value['thinking']:
            value.pop('thinking')


def update_document(document, entries, default_model='claude-opus-5-5'):
    result = copy.deepcopy(document)
    provider = result.get('provider', {}).get(PROVIDER)
    if not isinstance(provider, dict):
        raise ValueError('Expected an existing retool-relay provider in the OpenCode legacy JSON schema')
    old_models = provider.get('models') or {}
    new_models = {}
    for entry in entries:
        name = entry['id']
        item = copy.deepcopy(old_models.get(name) or {})
        levels = entry.get('reasoning_effort_levels') or []
        item.update(name=entry['label'], limit={'context': entry['context'], 'output': entry['max_output']},
                    tool_call=bool(entry.get('tools')), attachment=bool(entry.get('vision')),
                    reasoning=bool(entry.get('reasoning')))
        # Retool credits have no verified USD conversion here. Do not publish
        # the old placeholder as if an actual zero price had been measured.
        if item.get('cost') == {'input': 0, 'output': 0}:
            item.pop('cost')
        item['variants'] = {level: {'name': level.title(), 'reasoningEffort': level,
                            'settings': {'reasoningEffort': level}, 'body': {'reasoningEffort': level}}
                            for level in levels}
        options = item.setdefault('options', {})
        previous = effort_controls(options)
        valid = [value for value in previous if isinstance(value, str) and value in levels]
        selected = valid[0] if len(set(valid)) == 1 else entry.get('default_reasoning_effort')
        _clear_effort(options)
        if levels:
            options['reasoningEffort'] = selected
        elif not entry.get('reasoning'):
            options['reasoningEffort'] = 'none'  # a route that ignores effort never needs a client default
        new_models[name] = item
    if not new_models:
        raise ValueError('Refusing to replace the client catalog with an empty set')
    provider['models'] = new_models
    provider.pop('blacklist', None)
    provider['whitelist'] = list(new_models)
    fallback = default_model if default_model in new_models else next(iter(new_models))

    def update_reference(container, key):
        item = container.get(key)
        if isinstance(item, str) and item.startswith(PROVIDER + '/'):
            raw = item[len(PROVIDER) + 1:]
            try:
                name = models.resolve(raw)[0]['id']
            except ApiError:
                name = fallback
            container[key] = PROVIDER + '/' + (name if name in new_models else fallback)
    for key in ('model', 'small_model'):
        update_reference(result, key)
    for agent in (result.get('agent') or {}).values():
        if isinstance(agent, dict):
            update_reference(agent, 'model')
    listed = '\n'.join(PROVIDER + '/' + name for name in new_models)
    if not catalog_policy(result, listed, 'opencode', allowed_entries=entries, require_complete=True)['passed']:
        raise ValueError('Generated client catalog failed validation')
    return result


def _replace_with_backup(path, replacement, backup):
    """Save the version displaced at commit time, not an earlier read snapshot."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
        replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
        replace.restype = wintypes.BOOL
        if not replace(str(path), str(replacement), str(backup), 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        # Conservative portable fallback: preserve the actual destination, then
        # publish only if nobody created a newer file during the short gap.
        os.replace(path, backup)
        try:
            os.link(replacement, path)
        except BaseException:
            try:
                os.link(backup, path)
            except FileExistsError:
                pass
            raise


def write_backed_up(path, original, updated, backup_dir):
    path, backup_dir = Path(path), Path(backup_dir)
    if path.is_symlink():
        raise ValueError('Refusing to replace a symbolic client configuration path')
    path, backup_dir = path.resolve(), backup_dir.resolve()
    if os.name == 'nt' and path.drive.casefold() != backup_dir.drive.casefold():
        raise ValueError('Windows replacement backup must be on the same volume')
    if path.read_bytes() != original:
        raise RuntimeError('Client configuration changed during catalog preparation')
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = backup_dir / (path.name + '-' + stamp + '.bak')
    fd, temporary = tempfile.mkstemp(prefix='.proxi-catalog-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(updated)
        if path.read_bytes() != original:
            raise RuntimeError('Client configuration changed before replacement')
        _replace_with_backup(path, Path(temporary), backup)
        if backup.read_bytes() != original:
            # A late edit was atomically displaced. Keep it in the recovery
            # backup and restore it when the destination is still our version.
            if path.read_bytes() == updated:
                recovery = Path(temporary + '.restore')
                displaced = backup.with_suffix('.displaced.bak')
                try:
                    shutil.copyfile(backup, recovery)
                    _replace_with_backup(path, recovery, displaced)
                finally:
                    if recovery.exists():
                        recovery.unlink()
            raise RuntimeError('Client configuration changed during replacement; recovery copies: ' + str(backup))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--settings', default=str(Path.home() / '.config/opencode/opencode.jsonc'))
    parser.add_argument('--output', help='New private preview JSON file; never overwritten')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--backup-dir')
    args = parser.parse_args(argv)
    if args.write and not args.backup_dir:
        parser.error('--write requires --backup-dir')
    accounts = load_accounts(args.relay_config)
    entries = [entry for entry in models.catalog() if models.catalog_routable(entry, {'accounts': accounts})]
    path = Path(args.settings)
    original = path.read_bytes()
    document = json.loads(original.decode('utf-8-sig'))
    result = update_document(document, entries)
    encoded = (json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if args.output:
        with Path(args.output).open('xb') as output:
            output.write(encoded)
    backup = write_backed_up(path, original, encoded, args.backup_dir) if args.write else None
    print(json.dumps({'models': len(entries), 'written': bool(args.write),
                      'backup': str(backup) if backup else None,
                      'preview': args.output}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
