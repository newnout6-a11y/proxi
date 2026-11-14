"""Preview or update only Claude Desktop's Windows model-list policy.

The native Desktop picker accepts Claude routes, not every OpenAI-compatible
model exposed by the relay. Credentials, connection policy and session state are
never changed. Writing requires an explicit backup directory.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import models
from scripts.audit_models import load_accounts

POLICY_PATH = r'Software\Policies\Claude'
VALUE_NAME = 'inferenceModels'
DESKTOP_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Verified native picker definitions in installed Desktop 4.4212.0.0. These
# models have a four-level picker, so max does not accidentally enable xhigh.
NATIVE_EFFORTS = {name: ('low', 'medium', 'high', 'max')
                  for name in ('claude-opus-4-6', 'claude-sonnet-4-6')}


def compatible_effort_cap(levels, native_levels=DESKTOP_EFFORTS):
    """Native maxEffort exposes a contiguous prefix; do not enable route gaps."""
    cap = None
    for effort in native_levels:
        if effort not in levels:
            break
        cap = effort
    return cap


def build_catalog(entries, previous):
    if not isinstance(previous, list) or any(not isinstance(item, dict) for item in previous):
        raise ValueError('Expected an existing Claude Desktop model policy list')
    old = {item.get('name'): item for item in previous if isinstance(item.get('name'), str)}
    result = []
    for entry in entries:
        if entry.get('provider') != 'anthropic' or not entry['id'].startswith('claude-'):
            continue
        metadata = models.desktop_metadata(entry, entries)
        item = copy.deepcopy(old.get(entry['id']) or {})
        item.update(name=entry['id'], labelOverride=metadata['display_name'],
                    isFamilyDefault=bool(metadata.get('is_family_default')))
        item.pop('anthropicFamilyTier', None)
        tier = metadata.get('anthropic_family_tier')
        if tier:
            item['anthropicFamilyTier'] = tier
        cap = compatible_effort_cap(entry.get('reasoning_effort_levels') or [],
                                    NATIVE_EFFORTS.get(entry['id'], DESKTOP_EFFORTS))
        if cap:
            item['maxEffort'] = cap
        else:
            item.pop('maxEffort', None)
        result.append(item)
    if not result or len({item['name'] for item in result}) != len(result):
        raise ValueError('Refusing an empty or duplicated Claude Desktop catalog')
    return result


def read_policy():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, POLICY_PATH) as policy:
        value, kind = winreg.QueryValueEx(policy, VALUE_NAME)
    if kind != winreg.REG_SZ or not isinstance(value, str):
        raise ValueError('Expected an existing string model policy')
    return value, kind


def write_policy(original, kind, encoded, backup_dir):
    import winreg
    if read_policy() != (original, kind):
        raise RuntimeError('Desktop model policy changed during catalog preparation')
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = folder / ('inferenceModels-' + stamp + '.json')
    with backup.open('x', encoding='utf-8') as output:
        json.dump({'hive': 'HKCU', 'path': POLICY_PATH, 'name': VALUE_NAME,
                   'type': kind, 'value': original}, output, ensure_ascii=False, indent=2)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, POLICY_PATH, 0,
                        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as policy:
        if winreg.QueryValueEx(policy, VALUE_NAME) != (original, kind):
            raise RuntimeError('Desktop model policy changed before replacement; backup retained')
        winreg.SetValueEx(policy, VALUE_NAME, 0, kind, encoded)
    if read_policy() != (encoded, kind):
        raise RuntimeError('Desktop model policy verification failed; backup retained at ' + str(backup))
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--output', help='New private preview; never overwritten')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--backup-dir')
    args = parser.parse_args(argv)
    if args.write and not args.backup_dir:
        parser.error('--write requires --backup-dir')
    original, kind = read_policy()
    accounts = load_accounts(args.relay_config)
    entries = [entry for entry in models.catalog() if models.catalog_routable(entry, {'accounts': accounts})]
    result = build_catalog(entries, json.loads(original))
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    if args.output:
        with Path(args.output).open('x', encoding='utf-8') as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
    backup = write_policy(original, kind, encoded, args.backup_dir) if args.write else None
    print(json.dumps({'models': len(result), 'written': bool(args.write),
                      'backup': str(backup) if backup else None, 'preview': args.output}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
