"""Preview or update the model list Claude Desktop 3P actually reads.

Desktop takes its configuration from one source, in this priority order: machine
policy HKLM\\SOFTWARE\\Policies\\Claude, user policy HKCU\\SOFTWARE\\Policies\\Claude,
then the local configLibrary. While any HKLM value exists, HKCU is ignored
entirely. A policy that sets any key other than the app-behavior ones (updates,
relaunch/re-check, proxy) makes Desktop ignore the whole local configuration,
gateway URL and key included.

The script detects that source the same way and changes only its inferenceModels
list: a configLibrary file is replaced atomically with a backup, HKCU is updated
in place and HKLM is never written. It never creates a policy: one that sets any
other key would switch Desktop away from its working local configuration.
Credentials and all other keys stay untouched. Writing requires a backup directory
(it holds the old gateway settings: keep it private). Quit Desktop before writing:
it reads the configuration at launch.
"""
import argparse
import base64
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import models
from apierr import ApiError
from scripts.audit_models import load_accounts
from scripts.sync_opencode_catalog import write_backed_up

POLICY_PATH = r'Software\Policies\Claude'
VALUE_NAME = 'inferenceModels'
# Desktop reads only these registry types: REG_QWORD, REG_MULTI_SZ and REG_BINARY are
# invisible to it, and REG_EXPAND_SZ counts as present but cannot be read.
REG_SZ, REG_EXPAND_SZ, REG_DWORD = 1, 2, 4
# A managed source that sets only these keeps the local configuration in charge
# (https://claude.com/docs/third-party/claude-desktop/mdm).
APP_BEHAVIOR_KEYS = frozenset(name.casefold() for name in (
    'disableAutoUpdates', 'autoUpdaterEnforcementHours', 'installedUpdateEnforcementEnabled',
    'installedUpdateEnforcementHours', 'updateViaUpdatesHost', 'releaseChannel',
    'relaunchEnforcementHours', 'configRecheckIntervalMinutes', 'egressProxyUrl', 'egressProxyPacUrl'))
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
DESKTOP_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Sections of Anthropic's model catalog for Code in Desktop, the Code engine and Cowork.
DESKTOP_SURFACES = ('ccd', 'cc', 'cowork')
# Claude Code's effort table (https://code.claude.com/docs/en/model-config) is what Desktop
# runs, and "models not listed there do not support effort": Haiku 4.5, Opus 4.5, Sonnet 4.5
# and the older Claude 4 models have no selector, so they get no maxEffort. Anthropic's
# catalog (v4008) agrees. A model missing here is assumed to have the full picker, which
# is the safe side: the cap keeps Desktop from requesting a level the route rejects.
NATIVE_EFFORTS = {
    **dict.fromkeys(('claude-fable-5-1', 'claude-fable-5', 'claude-opus-5-5', 'claude-sonnet-5-5',
                     'claude-haiku-5-5', 'claude-opus-5', 'claude-sonnet-5', 'claude-opus-4-8',
                     'claude-opus-4-7'), DESKTOP_EFFORTS),
    **dict.fromkeys(('claude-opus-4-6', 'claude-sonnet-4-6'), ('low', 'medium', 'high', 'max')),
    **dict.fromkeys(('claude-haiku-4-5-20251001', 'claude-opus-4-5-20251101', 'claude-sonnet-4-5-20250929',
                     'claude-opus-4-20250514', 'claude-opus-4-1-20250805', 'claude-sonnet-4-20250514'), ()),
}


class SyncError(Exception):
    """An expected, user-correctable failure; main() reports it without a traceback."""


def compatible_effort_cap(levels, native_levels=DESKTOP_EFFORTS, allow_downgrade=False):
    """Highest picker level Desktop may offer for a route; None hides the selector.

    maxEffort hides every higher level. Unless the relay maps levels a route lacks
    (allow_effort_downgrade), the route must accept everything up to the cap, so the
    cap is the contiguous prefix of the native picker. With the mapping the relay
    turns a missing level into the nearest one and the whole picker is safe.
    """
    if not levels or not native_levels:
        return None
    if allow_downgrade:
        return native_levels[-1] if set(levels) & set(DESKTOP_EFFORTS) else None
    cap = None
    for effort in native_levels:
        if effort not in levels:
            break
        cap = effort
    return cap


def native_efforts(name, desktop=None):
    """Picker levels Desktop shows for a model: its own catalog first, else Claude Code's table."""
    if desktop and desktop.get(name) is not None:
        return desktop[name]
    return NATIVE_EFFORTS.get(name, DESKTOP_EFFORTS)


def claude_routes(entries):
    return [entry for entry in entries if entry.get('provider') == 'anthropic' and entry['id'].startswith('claude-')]


def build_catalog(entries, previous, *, default_model=None, desktop=None, include_unlisted=False,
                  allow_downgrade=False):
    """Existing rows keep their order and every field the script does not derive.

    New models go to the end. The first row is Desktop's default model, so it changes
    only for an explicit default_model. Derived fields (isFamilyDefault, anthropicFamilyTier,
    maxEffort) are recomputed; labelOverride is set for new rows only. A desktop mapping
    (see load_desktop_catalog) limits the list to models Desktop knows for Code/Cowork
    unless include_unlisted.
    """
    if not isinstance(previous, list) or any(not isinstance(item, (dict, str)) for item in previous):
        raise ValueError('Expected an existing Claude Desktop model policy list')
    old = {}
    for item in previous:
        item = {'name': item} if isinstance(item, str) else item
        if isinstance(item.get('name'), str):
            old.setdefault(item['name'], item)
    routes = claude_routes(entries)
    if desktop is not None and not include_unlisted:
        listed = [entry for entry in routes if entry['id'] in desktop]
        if routes and not listed:
            raise ValueError("Anthropic's model catalog lists none of the relay's Claude routes for "
                             'Code or Cowork; use --include-unlisted to list them anyway')
    else:
        listed = routes
    by_id = {entry['id']: entry for entry in listed}
    if len(by_id) != len(listed):
        raise ValueError('Refusing a duplicated Claude Desktop catalog')
    if not by_id:
        raise ValueError('Refusing an empty Claude Desktop catalog: the relay exposes no eligible Claude route')
    names = [name for name in old if name in by_id] + [name for name in by_id if name not in old]
    current = next(iter(old), None)
    if default_model is not None:
        try:
            wanted = models.resolve(default_model)[0]['id']
        except ApiError:
            wanted = default_model
        if wanted not in by_id:
            raise ValueError('--default-model %s is not in the generated list: %s'
                             % (default_model, ', '.join(names) or 'no eligible Claude route'))
        names.remove(wanted)
        names.insert(0, wanted)
    elif current is not None and current not in by_id:
        raise ValueError('The current default model %s is not in the generated list, and the first '
                         'entry changes only on request: pass --default-model to choose the new '
                         'default (or --include-unlisted to keep it)' % current)
    result = []
    for name in names:
        entry = by_id[name]
        metadata = models.desktop_metadata(entry, listed)
        item = copy.deepcopy(old.get(name) or {})
        item['name'] = name
        if name not in old:
            item['labelOverride'] = metadata.get('display_name', entry['label'])
        item['isFamilyDefault'] = bool(metadata.get('is_family_default'))
        tier = metadata.get('anthropic_family_tier')
        if tier:
            item['anthropicFamilyTier'] = tier
        else:
            item.pop('anthropicFamilyTier', None)
        cap = compatible_effort_cap(entry.get('reasoning_effort_levels') or [],
                                    native_efforts(name, desktop), allow_downgrade)
        if cap:
            item['maxEffort'] = cap
        else:
            item.pop('maxEffort', None)
        result.append(item)
    return result


def _picker_levels(model):
    """Effort levels a catalog model offers: () for none, None when the catalog says nothing."""
    thinking = model.get('thinking')
    if isinstance(thinking, dict):
        options = thinking.get('effort_options')
        if isinstance(options, list):
            return tuple(level for level in DESKTOP_EFFORTS
                         if any(isinstance(option, dict) and option.get('id') == level for option in options))
        if thinking.get('type') in ('none', 'mode'):
            return ()
    runtime = model.get('runtime')
    levels = runtime.get('effort_levels') if isinstance(runtime, dict) else None
    if isinstance(levels, list):
        return tuple(level for level in DESKTOP_EFFORTS if level in levels)
    return None


def load_desktop_catalog(path):
    """{model id: picker levels or None} that Anthropic's catalog lists for Code and Cowork.

    Accepts Desktop's cache file (documentBytes envelope) or a raw catalog.json. This is
    a read-only hint about what Desktop knows; the signature is not verified.
    """
    document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if isinstance(document, dict) and isinstance(document.get('documentBytes'), str):
        document = json.loads(base64.b64decode(document['documentBytes']))
    surfaces = document.get('surfaces') if isinstance(document, dict) else None
    known = {}
    for surface in DESKTOP_SURFACES:
        body = surfaces.get(surface) if isinstance(surfaces, dict) else None
        sections = body.get('model_selector_config') if isinstance(body, dict) else None
        for section in sections if isinstance(sections, list) else []:
            listed = section.get('models') if isinstance(section, dict) else None
            for model in listed if isinstance(listed, list) else []:
                if not isinstance(model, dict) or not isinstance(model.get('id'), str) or model.get('disabled'):
                    continue
                levels = _picker_levels(model)
                current = known.setdefault(model['id'], None)
                if levels is not None:
                    known[model['id']] = tuple(level for level in DESKTOP_EFFORTS
                                               if level in set(current or ()) | set(levels))
    if not known:
        raise ValueError('lists no models for the Code or Cowork surfaces')
    return known


def load_relay_config(path):
    """The relay's full configuration: root keys plus enabled accounts, without config.load() side effects."""
    try:
        saved = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise SyncError('Relay configuration not found: %s (see --relay-config)' % path) from None
    except ValueError as error:
        raise SyncError('Relay configuration %s is not valid JSON: %s' % (path, error)) from error
    if not isinstance(saved, dict):
        raise SyncError('Relay configuration %s must hold a JSON object' % path)
    root = dict(config.DEFAULTS, **saved)
    if type(root['allow_effort_downgrade']) is not bool:
        raise SyncError('allow_effort_downgrade must be a boolean in the relay configuration')
    try:
        accounts = load_accounts(path)
    except (AttributeError, TypeError) as error:
        raise SyncError('Relay configuration %s has an invalid accounts section' % path) from error
    if not accounts:
        raise SyncError('The relay configuration has no enabled account')
    return dict(root, accounts=accounts)


def read_registry_policy(hive):
    """Values directly under a Claude policy key as {name: (data, type)}, None when the key is absent."""
    try:
        import winreg
    except ImportError:
        return None
    root = winreg.HKEY_LOCAL_MACHINE if hive == 'HKLM' else winreg.HKEY_CURRENT_USER
    where = '%s\\%s' % (hive, POLICY_PATH)
    try:
        key = winreg.OpenKey(root, POLICY_PATH, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SyncError('Cannot read %s: %s' % (where, error)) from error
    values = {}
    try:
        with key:
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, data, kind = winreg.EnumValue(key, index)
                values[name] = (data, kind)
    except OSError as error:
        raise SyncError('Cannot read the values of %s: %s' % (where, error)) from error
    return values


def _readable(values):
    return {name: value for name, value in values.items() if value[1] in (REG_SZ, REG_EXPAND_SZ, REG_DWORD)}


def _configures(values):
    """The policy sets a key that makes Desktop ignore its local configuration."""
    return any(name and name.casefold() not in APP_BEHAVIOR_KEYS for name in _readable(values))


def effective_source(machine, user):
    """'hklm', 'hkcu' or 'local': where Desktop takes its model list from.

    Machine policy hides user policy entirely once any readable value exists, even one
    that sets only app-behavior keys. A policy with only those keys leaves the local
    configuration in charge.
    """
    if machine is not None and _readable(machine):
        return 'hklm' if _configures(machine) else 'local'
    if user is not None and _configures(user):
        return 'hkcu'
    return 'local'


def describe_source(effective, machine, user):
    hklm, hkcu = 'HKLM\\SOFTWARE\\Policies\\Claude', 'HKCU\\SOFTWARE\\Policies\\Claude'
    if effective == 'hklm':
        return ('machine policy %s is in effect: Desktop ignores HKCU and the local configLibrary while '
                'it is present, and this script never writes to HKLM' % hklm)
    if effective == 'hkcu':
        return ('user policy %s sets configuration keys, so Desktop ignores the local configLibrary and '
                'a change there would have no effect' % hkcu)
    if machine is not None and _readable(machine):
        return ('machine policy %s is present but sets no configuration key: Desktop ignores HKCU '
                'entirely and keeps using the local configLibrary' % hklm)
    if user is None:
        return ('%s does not exist and Desktop uses the local configLibrary. The script never creates a '
                'policy: one that sets any key other than the app-behavior ones would make Desktop ignore '
                'the whole local configuration, gateway URL and key included' % hkcu)
    return ('%s sets no configuration key (app-behavior keys only, if any), so Desktop keeps using the '
            'local configLibrary' % hkcu)


def policy_models(values, hive):
    """The existing inferenceModels string of a policy key; a list is never introduced."""
    where = '%s\\%s' % (hive, POLICY_PATH)
    if values is None:
        raise SyncError('%s does not exist; the script never creates a Desktop policy' % where)
    found = next((value for name, value in values.items() if name.casefold() == VALUE_NAME.casefold()), None)
    if found is None:
        raise SyncError('%s has no %s value. The script updates an existing model list and does not '
                        'introduce one: a static list replaces Desktop model discovery' % (where, VALUE_NAME))
    value, kind = found
    if kind != REG_SZ or not isinstance(value, str):
        raise SyncError('%s\\%s must be a REG_SZ string holding JSON; Desktop cannot read other types'
                        % (where, VALUE_NAME))
    return value, kind


def read_policy(hive='HKCU'):
    return policy_models(read_registry_policy(hive), hive)


def write_policy(original, kind, encoded, backup_dir):
    """Replace only HKCU inferenceModels of an existing key, after an exact backup."""
    import winreg
    machine = read_registry_policy('HKLM')
    if machine is not None and _readable(machine):
        raise RuntimeError('Machine policy appeared under HKLM\\%s: Desktop now ignores HKCU, '
                           'nothing was written' % POLICY_PATH)
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
                        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as policy:
        if winreg.QueryValueEx(policy, VALUE_NAME) != (original, kind):
            raise RuntimeError('Desktop model policy changed before replacement; backup retained')
        winreg.SetValueEx(policy, VALUE_NAME, 0, kind, encoded)
    if read_policy() != (encoded, kind):
        raise RuntimeError('Desktop model policy verification failed; backup retained at ' + str(backup))
    return backup


def default_config_library():
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'Claude-3p' / 'configLibrary' if base else None


def default_desktop_catalog():
    base = os.environ.get('LOCALAPPDATA')
    return Path(base) / 'Claude-3p' / 'model-catalog' / 'published.json' if base else None


def read_local(library):
    """The applied configLibrary file: (path, exact bytes, parsed object)."""
    if library is None:
        raise SyncError('Cannot locate the local configLibrary (LOCALAPPDATA is not set); pass --config-library')
    meta_path = Path(library) / '_meta.json'
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise SyncError('No Desktop local configuration: %s does not exist' % meta_path) from None
    except ValueError as error:
        raise SyncError('%s is not valid JSON: %s' % (meta_path, error)) from error
    applied = meta.get('appliedId') if isinstance(meta, dict) else None
    if not isinstance(applied, str) or not SAFE_ID.fullmatch(applied):
        raise SyncError('%s names no usable applied configuration (appliedId)' % meta_path)
    path = Path(library) / (applied + '.json')
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        raise SyncError('The applied Desktop configuration %s does not exist' % path) from None
    try:
        document = json.loads(original.decode('utf-8-sig'))
    except ValueError as error:
        raise SyncError('%s is not valid JSON: %s' % (path, error)) from error
    if not isinstance(document, dict):
        raise SyncError('%s must hold a JSON object' % path)
    return path, original, document


def encode_local(original, document):
    """Desktop's layout (two-space JSON), keeping the file's BOM, line ending and final newline."""
    text = json.dumps(document, ensure_ascii=False, indent=2) + ('\n' if original.endswith(b'\n') else '')
    if b'\r\n' in original:
        text = text.replace('\n', '\r\n')
    return (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')


def _models_json(value, where):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as error:
            raise SyncError('%s is not valid JSON: %s' % (where, error)) from error
    return value


def _names(items):
    names = [item if isinstance(item, str) else item.get('name') for item in items
             if isinstance(item, (dict, str))]
    return [name for name in names if isinstance(name, str)]


def _catalog_hint(args, warnings):
    explicit = args.desktop_catalog is not None
    path = args.desktop_catalog if explicit else default_desktop_catalog()
    if path is None or not Path(path).is_file():
        if explicit:
            raise SyncError('Desktop model catalog not found: %s' % path)
        warnings.append('Anthropic model catalog not found; every verified Claude route is included')
        return None
    try:
        return load_desktop_catalog(path)
    except (OSError, ValueError) as error:
        if explicit:
            raise SyncError('Unusable Desktop model catalog %s: %s' % (path, error)) from error
        warnings.append('Anthropic model catalog %s is unusable (%s); every verified Claude route is included'
                        % (path, error))
        return None


def run(args):
    machine, user = read_registry_policy('HKLM'), read_registry_policy('HKCU')
    effective = effective_source(machine, user)
    reason = describe_source(effective, machine, user)
    if args.source not in ('auto', effective):
        raise SyncError('--source %s does not match the source Desktop uses (%s): %s'
                        % (args.source, effective, reason))
    if args.write and effective == 'hklm':
        raise SyncError('Refusing to write: ' + reason)
    warnings = []
    if effective == 'local':
        path, original, document = read_local(args.config_library or default_config_library())
        target, keys = str(path), document
        if VALUE_NAME not in document:
            raise SyncError('%s has no %s list. The script updates an existing model list and does not '
                            'introduce one: a static list replaces Desktop model discovery' % (path, VALUE_NAME))
        raw = document[VALUE_NAME]
        previous = _models_json(raw, '%s of %s' % (VALUE_NAME, path))
    else:
        hive = 'HKLM' if effective == 'hklm' else 'HKCU'
        values = machine if effective == 'hklm' else user
        raw, kind = policy_models(values, hive)
        target, keys = '%s\\%s\\%s' % (hive, POLICY_PATH, VALUE_NAME), values
        previous = _models_json(raw, target)
        if effective == 'hklm':
            warnings.append('Machine policy is read-only for this script; deploy the preview with the '
                            'tool that manages that policy')
    if any(str(key).casefold() == 'bootstrapurl' for key in keys):
        warnings.append('bootstrapUrl is set: while a bootstrap response is available it overrides these '
                        'settings and may serve the model list itself')
    relay = load_relay_config(args.relay_config)
    entries = [entry for entry in models.catalog() if models.catalog_routable(entry, relay)]
    desktop = _catalog_hint(args, warnings)
    result = build_catalog(entries, previous, default_model=args.default_model, desktop=desktop,
                           include_unlisted=args.include_unlisted,
                           allow_downgrade=relay['allow_effort_downgrade'])
    before, after = _names(previous), [item['name'] for item in result]
    skipped = ([entry['id'] for entry in claude_routes(entries) if entry['id'] not in desktop]
               if desktop is not None and not args.include_unlisted else [])
    if any(name in skipped for name in before):
        warnings.append('Removed rows the Anthropic catalog does not list for Code/Cowork: %s; '
                        '--include-unlisted keeps them' % ', '.join(name for name in before if name in skipped))
    encoded = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    if args.output:
        try:
            with Path(args.output).open('x', encoding='utf-8') as output:
                json.dump(result, output, ensure_ascii=False, indent=2)
        except FileExistsError:
            raise SyncError('Preview file already exists and is never overwritten: %s' % args.output) from None
    backup = None
    if args.write:
        if effective == 'local':
            updated = copy.deepcopy(document)
            updated[VALUE_NAME] = encoded if isinstance(raw, str) else result
            backup = write_backed_up(path, original, encode_local(original, updated), args.backup_dir)
        else:
            backup = write_policy(raw, kind, encoded, args.backup_dir)
    print(json.dumps({'models': len(result), 'written': bool(args.write),
                      'backup': str(backup) if backup else None, 'preview': args.output,
                      'source': effective, 'target': target, 'default_model': after[0],
                      'added': [name for name in after if name not in before],
                      'removed': [name for name in before if name not in after],
                      'unlisted_skipped': skipped, 'warnings': warnings}, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--source', choices=('auto', 'hkcu', 'local'), default='auto',
                        help='Source Desktop is expected to use; a mismatch is an error. auto detects it '
                             '(HKLM is never written and no policy is ever created)')
    parser.add_argument('--config-library',
                        help=r'Desktop configLibrary (default %%LOCALAPPDATA%%\Claude-3p\configLibrary)')
    parser.add_argument('--desktop-catalog',
                        help=r"Anthropic's model catalog cache "
                             r'(default %%LOCALAPPDATA%%\Claude-3p\model-catalog\published.json)')
    parser.add_argument('--include-unlisted', action='store_true',
                        help="Also list Claude routes the catalog does not offer for Code/Cowork")
    parser.add_argument('--default-model',
                        help="Make this model first (Desktop's default); otherwise the order is kept")
    parser.add_argument('--output', help='New private preview; never overwritten')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--backup-dir',
                        help='Required for --write; for configLibrary it must be on the same volume')
    args = parser.parse_args(argv)
    if args.write and not args.backup_dir:
        parser.error('--write requires --backup-dir')
    try:
        return run(args)
    except (SyncError, ValueError, RuntimeError, OSError) as error:
        print('error: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
