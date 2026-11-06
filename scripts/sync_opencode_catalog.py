"""Sync only the relay catalog in OpenCode's shared legacy JSON configuration.

OpenCode 1 reads this schema directly; installed OpenCode 2 migrates it on load.
The file may be JSONC (comments, trailing commas). Its BOM, line endings and
indentation are kept, and nothing is written when the catalog is already current.
Preview is read-only and masks credentials unless --include-secrets is given.
--write requires a backup directory and preserves other providers, authentication
and unrelated settings; a file with comments is rewritten only with --allow-reformat.
"""
import argparse
import codecs
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import models
from apierr import ApiError
from scripts.probe_client_settings import catalog_policy, effort_controls, routable_entries

PROVIDER = 'retool-relay'
# Hidden OpenCode agents that run on their own (titles, summaries, compaction).
BACKGROUND_AGENTS = ('title', 'summary', 'compaction')
# Retool credits have no verified USD price here, so "cheap" means the small/fast
# tier, in the order OpenCode itself prefers for small_model (Haiku, Flash, nano).
SMALL_MODELS = ('claude-haiku-4-5-20251001', 'gemini-3.1-flash-lite-preview', 'gemini-3-flash-preview',
                'gemini-3.5-flash', 'gemini-2.5-flash', 'gpt-5-nano', 'gpt-5-mini', 'zai-org/GLM-5.3-Flash')
MASK = '***'
# Mappings whose values are credentials (a list under `env` names variables instead).
SECRET_CONTAINERS = ('headers', 'environment', 'env', 'cookies')
SECRET_KEY = re.compile(r'apikey|secret|token|password|passwd|authorization|cookie|credential|privatekey|bearer')


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


def _named_objects(section):
    return [(name, item) for name, item in section.items() if isinstance(item, dict)] if isinstance(section, dict) else []


def update_document(document, entries, default_model='claude-opus-5-5'):
    result = copy.deepcopy(document)
    providers = result.get('provider')
    provider = providers.get(PROVIDER) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise ValueError('Expected an existing retool-relay provider in the OpenCode legacy JSON schema')
    old_models = provider.get('models') or {}
    if not isinstance(old_models, dict):
        raise ValueError('Expected provider.retool-relay.models to be an object')
    new_models = {}
    for entry in entries:
        name = entry['id']
        item = copy.deepcopy(old_models.get(name))
        item = item if isinstance(item, dict) else {}
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
        options = item.get('options')
        if not isinstance(options, dict):
            options = item['options'] = {}
        previous = effort_controls(options)
        valid = [value for value in previous if isinstance(value, str) and value in levels]
        selected = valid[0] if len(set(valid)) == 1 else entry.get('default_reasoning_effort')
        _clear_effort(options)
        options['reasoningEffort'] = selected if levels else 'none'
        new_models[name] = item
    if not new_models:
        raise ValueError('Refusing to replace the client catalog with an empty set')
    provider['models'] = new_models
    provider.pop('blacklist', None)
    provider['whitelist'] = list(new_models)
    fallback = default_model if default_model in new_models else next(iter(new_models))
    small = next((name for name in SMALL_MODELS if name in new_models), None)

    def update_reference(container, key, replacement):
        """Canonicalize a relay model reference; a removed model gets `replacement`, or no override at all."""
        item = container.get(key)
        if not isinstance(item, str) or not item.startswith(PROVIDER + '/'):
            return
        raw, _, variant = item[len(PROVIDER) + 1:].partition('#')
        try:
            name = models.resolve(raw)[0]['id']
        except ApiError:
            name = None
        if name in new_models:
            suffix = '#' + variant if variant in new_models[name]['variants'] else ''
            container[key] = PROVIDER + '/' + name + suffix
        elif replacement:
            container[key] = PROVIDER + '/' + replacement
        else:
            del container[key]
    update_reference(result, 'model', fallback)
    update_reference(result, 'small_model', small)
    for section in ('agent', 'mode'):
        for name, agent in _named_objects(result.get(section)):
            update_reference(agent, 'model', small if name in BACKGROUND_AGENTS else fallback)
    for _, command in _named_objects(result.get('command')):
        update_reference(command, 'model', fallback)
    listed = '\n'.join(PROVIDER + '/' + name for name in new_models)
    if not catalog_policy(result, listed, 'opencode', allowed_entries=entries, require_complete=True)['passed']:
        raise ValueError('Generated client catalog failed validation')
    return result


def read_file(path, label, hint=''):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        raise ValueError('%s not found: %s%s' % (label, path, hint)) from None
    except OSError as error:
        raise ValueError('cannot read %s %s: %s' % (label, path, error.strerror or error)) from None


def _strip_jsonc(text):
    """Blank comments and trailing commas outside strings, keeping every offset and line break."""
    chars = list(text)
    size, index, comma, commented = len(text), 0, None, False

    def blank(start, end):
        for position in range(start, end):
            if chars[position] not in '\r\n':
                chars[position] = ' '
    while index < size:
        char = text[index]
        if char == '"':
            index += 1
            while index < size and text[index] != '"':
                index += 2 if text[index] == '\\' else 1
            index += 1
            comma = None
        elif text.startswith('//', index):
            end = index
            while end < size and text[end] not in '\r\n':
                end += 1
            blank(index, end)
            index, commented = end, True
        elif text.startswith('/*', index):
            end = text.find('*/', index + 2)
            if end < 0:
                raise ValueError('unterminated /* comment at line %d' % (text.count('\n', 0, index) + 1))
            blank(index, end + 2)
            index, commented = end + 2, True
        else:
            if char == ',':
                comma = index
            elif char in '}]':
                if comma is not None:
                    chars[comma] = ' '
                comma = None
            elif char not in ' \t\r\n':
                comma = None
            index += 1
    return ''.join(chars), commented


def parse_settings(original, label):
    """Read JSONC bytes: returns the document, its text style and whether it holds comments."""
    bom = codecs.BOM_UTF8 if original.startswith(codecs.BOM_UTF8) else b''
    try:
        text = original[len(bom):].decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('%s is not valid UTF-8 (byte offset %d); OpenCode configuration must be UTF-8'
                         % (label, error.start + len(bom))) from None
    try:
        clean, commented = _strip_jsonc(text)
    except ValueError as error:
        raise ValueError('%s is not valid JSONC: %s' % (label, error)) from None
    try:
        document = json.loads(clean)
    except json.JSONDecodeError as error:
        raise ValueError('%s is not valid JSON or JSONC: %s at line %d, column %d'
                         % (label, error.msg, error.lineno, error.colno)) from None
    if not isinstance(document, dict):
        raise ValueError('%s must contain a JSON object' % label)
    crlf = text.count('\r\n')
    indent = re.search(r'^([ \t]+)\S', clean, re.M)
    unit = indent.group(1) if indent else '  '
    style = {'bom': bom, 'newline': '\r\n' if crlf > text.count('\n') - crlf else '\n',
             'indent': '\t' if '\t' in unit else (unit if len(unit) <= 8 else '  '),
             'final_newline': text.endswith(('\n', '\r'))}
    return document, style, commented


def encode_settings(document, style):
    text = json.dumps(document, ensure_ascii=False, indent=style['indent'])
    text = text.replace('\n', style['newline']) + (style['newline'] if style['final_newline'] else '')
    return style['bom'] + text.encode('utf-8')


def is_secret_key(name, mapping=False):
    """Whether a key's value holds credentials; `mapping` says the value is a mapping."""
    key = re.sub(r'[^a-z0-9]', '', str(name).lower())
    if key in SECRET_CONTAINERS:
        return mapping
    # A name ending in "env" (apiKeyEnv) refers to an environment variable, it is not a credential.
    return bool(SECRET_KEY.search(key)) and not key.endswith('env')


def mask_document(value, secret=False):
    """Copy of a JSON document with credential strings replaced by MASK."""
    if isinstance(value, dict):
        return {key: mask_document(item, secret or is_secret_key(key, isinstance(item, dict)))
                for key, item in value.items()}
    if isinstance(value, list):
        return [mask_document(item, secret) for item in value]
    return MASK if secret and isinstance(value, str) and value else value


def write_preview(path, data):
    """Create a new owner-only file; an existing preview is never overwritten."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    except FileExistsError:
        raise ValueError('preview file already exists: %s' % path) from None
    with os.fdopen(fd, 'wb') as output:
        output.write(data)


def _replace_windows(path, replacement, backup):
    import ctypes
    from ctypes import wintypes
    replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace.restype = wintypes.BOOL
    if not replace(str(path), str(replacement), str(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_posix(path, replacement, backup):
    """Back up first, then publish with one atomic rename: the destination never disappears."""
    try:
        os.link(path, backup)
    except FileExistsError:
        raise
    except OSError:
        # No hard links on this filesystem; the exclusive open still refuses to reuse a backup name.
        with open(path, 'rb') as source, open(backup, 'xb') as target:
            shutil.copyfileobj(source, target)
    try:
        os.replace(replacement, path)
    except BaseException:
        try:
            if Path(backup).read_bytes() == Path(path).read_bytes():
                os.unlink(backup)
        except OSError:
            pass
        raise


def _replace_with_backup(path, replacement, backup):
    """Save the version displaced at commit time, not an earlier read snapshot."""
    if os.name == 'nt':
        _replace_windows(path, replacement, backup)
    else:
        _replace_posix(path, replacement, backup)


def _restore_original(path, backup):
    """After a failed replacement, ReplaceFileW (error 1177) can leave the original only as the backup."""
    if path.exists() or not backup.exists():
        return
    try:
        os.rename(backup, path)
    except OSError as error:
        raise RuntimeError('Could not restore the client configuration; the original is saved as %s (%s)'
                           % (backup, error.strerror or error)) from error


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
        try:
            _replace_with_backup(path, Path(temporary), backup)
        except BaseException:
            _restore_original(path, backup)
            raise
        if backup.read_bytes() != original:
            # A late edit was atomically displaced. Keep it in the recovery
            # backup and restore it when the destination is still our version.
            if path.read_bytes() == updated:
                recovery = Path(temporary + '.restore')
                displaced = backup.with_suffix('.displaced.bak')
                try:
                    shutil.copyfile(backup, recovery)
                    try:
                        _replace_with_backup(path, recovery, displaced)
                    except BaseException:
                        if not path.exists():
                            shutil.copyfile(backup, path)
                        raise
                finally:
                    if recovery.exists():
                        recovery.unlink()
            raise RuntimeError('Client configuration changed during replacement; recovery copies: ' + str(backup))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def run(args):
    path = Path(args.settings)
    original = read_file(path, 'OpenCode settings file', ' (use --settings; OpenCode also reads opencode.json)')
    document, style, commented = parse_settings(original, str(path))
    entries = routable_entries(args.relay_config)
    result = update_document(document, entries)
    unchanged = result == document
    encoded = original if unchanged else encode_settings(result, style)
    if args.write and commented and not unchanged and not args.allow_reformat:
        raise ValueError('%s contains comments that rewriting would remove; remove them or add --allow-reformat '
                         '(the backup keeps the original)' % path)
    if args.output:
        preview = encoded if args.include_secrets else encode_settings(mask_document(result), style)
        write_preview(args.output, preview)
    backup = write_backed_up(path, original, encoded, args.backup_dir) if args.write and not unchanged else None
    print(json.dumps({'models': len(entries), 'written': backup is not None, 'changed': not unchanged,
                      'backup': str(backup) if backup else None, 'preview': args.output,
                      'preview_redacted': bool(args.output) and not args.include_secrets,
                      'drops_comments': commented and not unchanged}, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--settings', default=str(Path.home() / '.config/opencode/opencode.jsonc'))
    parser.add_argument('--output', help='New private preview JSON file; never overwritten')
    parser.add_argument('--include-secrets', action='store_true',
                        help='Keep credentials in the --output preview; they are masked as *** by default')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--backup-dir')
    parser.add_argument('--allow-reformat', action='store_true',
                        help='Allow --write to rewrite a file with comments, which are then removed')
    args = parser.parse_args(argv)
    if args.write and not args.backup_dir:
        parser.error('--write requires --backup-dir')
    try:
        return run(args)
    except (ValueError, RuntimeError, OSError) as error:
        print('error: %s' % error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
