"""Sync only an existing Proxi provider in a DeepSeek Harness profile patch.

Preview is read-only. --write requires an explicit same-volume backup directory.
Other providers, credentials, plugin configuration and the selected model remain
unchanged. Reasoning models keep the existing default and selectors; a separate
retool-text provider avoids inheriting that default for non-reasoning models.
"""
import argparse
import copy
import datetime
from io import StringIO
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import models
from scripts.audit_models import load_accounts
from scripts.sync_opencode_catalog import write_backed_up


def _copy(value):
    # ruamel TimeStamp inherits immutable datetime, but its deepcopy can lose
    # tzinfo. Preserve those immutable values rather than changing other headers.
    memo, seen = {}, set()

    def visit(item):
        if id(item) in seen:
            return
        seen.add(id(item))
        if isinstance(item, (datetime.date, datetime.datetime)):
            memo[id(item)] = item
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return copy.deepcopy(value, memo)


def _provider_parent(document):
    if not isinstance(document, list):
        raise ValueError('Expected a Harness plugin patch list')
    matches = []
    for plugin in document:
        if not isinstance(plugin, dict) or plugin.get('id') != 'llm-pi-ai':
            continue
        config = plugin.get('config')
        providers = config.get('providers') if isinstance(config, dict) else None
        if isinstance(providers, dict) and isinstance(providers.get('retool'), dict):
            matches.append(providers)
    if len(matches) != 1:
        raise ValueError('Expected exactly one existing llm-pi-ai retool provider')
    return matches[0]


def update_document(document, entries):
    result = _copy(document)
    parent = _provider_parent(result)
    index = next(i for i, plugin in enumerate(result)
                 if isinstance(plugin, dict) and plugin.get('id') == 'llm-pi-ai'
                 and isinstance(plugin.get('config'), dict) and plugin['config'].get('providers') is parent)
    result[index] = _copy(result[index])
    parent = _provider_parent(result)
    # Break a possible YAML alias to another provider before changing anything.
    provider = _copy(parent['retool'])
    if not entries:
        raise ValueError('Refusing to replace the client catalog with an empty set')
    if len({item['id'] for item in entries}) != len(entries):
        raise ValueError('Duplicate canonical model IDs')

    def catalog(previous, selected):
        old = {item['id']: item for item in previous.get('models', [])
               if isinstance(item, dict) and isinstance(item.get('id'), str)}
        updated = []
        for entry in selected:
            item = _copy(old.get(entry['id']) or {})
            item.update(id=entry['id'], name=entry['label'], contextWindow=entry['context'],
                        maxTokens=entry['max_output'], input=['text', 'image'] if entry.get('vision') else ['text'])
            levels = entry.get('reasoning_effort_levels') or []
            item['reasoningEfforts'] = ({'off' if level == 'none' else level: level for level in levels}
                                        if entry.get('reasoning') and levels else False)
            updated.append(item)
        return updated

    # The SDK rejects its inherited reasoning default for a model without selectors, so only
    # models with selectable levels can share the reasoning provider.
    reasoning = [entry for entry in entries if entry.get('reasoning') and entry.get('reasoning_effort_levels')]
    text = [entry for entry in entries if entry not in reasoning]
    secondary = parent.get('retool-text')
    if secondary is not None:
        # Never overwrite a pre-existing provider belonging to someone else.
        expected = str(provider.get('displayName') or 'Retool Relay') + ' (без thinking)'
        if (not isinstance(secondary, dict) or secondary.get('displayName') != expected
                or any(secondary.get(key) != provider.get(key) for key in ('api', 'baseURL', 'apiKeyEnv'))):
            raise ValueError('Existing retool-text does not match the managed Proxi provider')
        if not text or not reasoning:
            raise ValueError('Existing retool-text requires the complete split catalog')
    provider['models'] = catalog(provider, reasoning or text)
    if not reasoning:
        provider['reasoning'] = 'off'
    if reasoning and text:
        second = _copy(secondary or provider)
        second['displayName'] = str(provider.get('displayName') or 'Retool Relay') + ' (без thinking)'
        second['models'] = catalog(secondary or provider, text)
        second['reasoning'] = 'off'
        parent['retool-text'] = second
    parent['retool'] = provider
    return result


def _semantic(value):
    """Compare YAML values without TaggedScalar identity or timestamp metadata."""
    from ruamel.yaml.comments import TaggedScalar
    if isinstance(value, dict):
        return {_semantic(key): _semantic(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    if isinstance(value, TaggedScalar):
        return ('tagged', str(value.tag), _semantic(value.value))
    if isinstance(value, (datetime.date, datetime.datetime)):
        return ('timestamp', value.isoformat())
    return value


def encode_document(original, document, result):
    """Splice only Proxi provider blocks; never reserialize unrelated YAML."""
    from ruamel.yaml import YAML
    source = original.decode('utf-8')
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    root = yaml.compose(source)
    before, after = _provider_parent(document), _provider_parent(result)
    index = next(i for i, plugin in enumerate(document)
                 if isinstance(plugin, dict) and plugin.get('id') == 'llm-pi-ai'
                 and isinstance(plugin.get('config'), dict) and plugin['config'].get('providers') is before)

    def child(node, key):
        return next(value for name, value in node.value if name.value == key)

    providers = child(child(root.value[index], 'config'), 'providers')
    if providers.start_mark.index < root.value[index].start_mark.index:
        raise ValueError('Expand the aliased Proxi provider block before synchronizing')
    nodes = {key.value: value for key, value in providers.value}
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    newline = '\r\n' if '\r\n' in source else '\n'

    def block(provider, indent):
        output = StringIO()
        yaml.dump(provider, output)
        return ''.join(' ' * indent + line + newline for line in output.getvalue().splitlines())

    edits = []
    for name in ('retool', 'retool-text'):
        if name not in after:
            continue
        if name in nodes:
            node = nodes[name]
            if node.flow_style or node.start_mark.line == node.end_mark.line:
                raise ValueError('Expand the Proxi provider to a block mapping before synchronizing')
            start, end = offsets[node.start_mark.line], offsets[node.end_mark.line]
            edits.append((start, end, block(after[name], node.start_mark.column)))
        else:
            main = nodes['retool']
            position = offsets[main.end_mark.line]
            indent = main.start_mark.column - 2
            edits.append((position, position, ' ' * indent + name + ':' + newline
                          + block(after[name], main.start_mark.column)))
    # Apply an insertion at a replacement's end first so its bytes are retained.
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        source = source[:start] + replacement + source[end:]
    if _semantic(yaml.load(source)) != _semantic(result):
        raise ValueError('YAML serialization changed a value; refusing to write the profile')
    return source.encode('utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--output', help='New private preview file; never overwritten')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--backup-dir')
    args = parser.parse_args(argv)
    if args.write and not args.backup_dir:
        parser.error('--write requires --backup-dir')
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    path = Path(args.settings)
    original = path.read_bytes()
    document = yaml.load(original.decode('utf-8-sig'))
    accounts = load_accounts(args.relay_config)
    entries = [entry for entry in models.catalog() if models.catalog_routable(entry, {'accounts': accounts})]
    result = update_document(document, entries)
    encoded = encode_document(original, document, result)
    if args.output:
        with Path(args.output).open('xb') as preview:
            preview.write(encoded)
    backup = write_backed_up(path, original, encoded, args.backup_dir) if args.write else None
    print(json.dumps({'settings': str(path), 'models': len(entries), 'written': bool(args.write),
                      'backup': str(backup) if backup else None, 'preview': args.output}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
