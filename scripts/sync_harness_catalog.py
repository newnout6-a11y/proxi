"""Sync only an existing Proxi provider in a DeepSeek Harness profile patch.

Preview is read-only and masks credentials and the text of comments unless
--include-secrets is given. The masking goes by name and shape, so read a preview
before sharing it. --write requires an explicit same-volume backup directory and
does nothing when the catalog is already current. Other providers, credentials,
plugin configuration and the selected model remain unchanged. Reasoning models keep
the existing default and selectors; a separate retool-text provider avoids
inheriting that default for models without selectors. Prompt profiles that publish
themselves as models (publish_as_model) are listed as <model>@<profile> beside the
catalog models, with the base model's limits and selectors (scripts/sync_virtual_models.py);
--no-virtual-models leaves them out.
"""
import argparse
import bisect
import copy
import datetime
from io import StringIO
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import sync_virtual_models
from scripts.probe_client_settings import routable_entries
from scripts.sync_opencode_catalog import (MASK, is_environment_list, is_secret_flag, is_secret_key, mask_string,
                                           mask_text, read_file, write_backed_up, write_preview)

STR = 'tag:yaml.org,2002:str'


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

    # The entries of both groups as they were, by id: a model that lands in, or moves between, retool and retool-text keeps
    # the fields of its own (a per-model compat, cost); read from one group, it lost them at the move, and only the backup
    # kept them.
    old = {}
    for group in (parent.get('retool-text'), parent['retool']):
        for item in (group.get('models') or []) if isinstance(group, dict) else []:
            if isinstance(item, dict) and isinstance(item.get('id'), str):
                old[item['id']] = item

    def catalog(selected):
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
    provider['models'] = catalog(reasoning or text)
    if not reasoning:
        provider['reasoning'] = 'off'
    if reasoning and text:
        second = _copy(secondary or provider)
        second['displayName'] = str(provider.get('displayName') or 'Retool Relay') + ' (без thinking)'
        second['models'] = catalog(text)
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


def _yaml():
    from ruamel.yaml import YAML
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def decode_profile(original, label='profile'):
    try:
        return original.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('%s is not valid UTF-8 (byte offset %d); convert it to UTF-8 first'
                         % (label, error.start)) from None


def _problem(error):
    """Kind and position of a YAML error; its snippet or values could hold credentials, so they are dropped."""
    mark = getattr(error, 'problem_mark', None) or getattr(error, 'context_mark', None)
    where = ' at line %d, column %d' % (mark.line + 1, mark.column + 1) if mark else ''
    syntax = type(error).__name__ in ('ScannerError', 'ParserError')
    return ((getattr(error, 'problem', None) if syntax else None) or 'invalid YAML') + where


def load_profile(text, label):
    from ruamel.yaml.error import YAMLError
    try:
        return _yaml().load(text)
    except YAMLError as error:
        raise ValueError('%s is not valid YAML: %s' % (label, _problem(error))) from None


def encode_document(original, document, result):
    """Splice only Proxi provider blocks; never reserialize unrelated YAML."""
    from ruamel.yaml.error import YAMLError
    source = decode_profile(original)
    newline = '\r\n' if '\r\n' in source else '\n'
    final_newline = source.endswith(('\n', '\r'))
    if not final_newline:
        # A last block has no following line start to splice up to; this ending is restored below.
        source += newline
    yaml = _yaml()
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
    keys = {key.value: key for key, _ in providers.value}
    nodes = {key.value: value for key, value in providers.value}
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def block(provider, indent):
        output = StringIO()
        yaml.dump(provider, output)
        return ''.join(' ' * indent + line + newline for line in output.getvalue().splitlines())

    def shared(target):
        """Whether an alias elsewhere in the profile refers to `target`: the composer gives it the same node."""
        found, stack, seen = 0, [root], set()
        while stack:
            node = stack.pop()
            found += node is target
            if id(node) not in seen and isinstance(node.value, list):
                seen.add(id(node))
                for item in node.value:
                    stack.extend(item if isinstance(item, tuple) else (item,))
        return found > 1

    edits = []
    for name in ('retool', 'retool-text'):
        if name not in after:
            continue
        if name in nodes:
            node = nodes[name]
            if node.flow_style or node.start_mark.line == node.end_mark.line:
                raise ValueError('Expand the Proxi provider to a block mapping before synchronizing')
            if shared(node):
                raise ValueError("The Proxi provider %r shares its block with another key through a YAML anchor and "
                                 "alias; expand the alias before synchronizing" % name)
            # The body starts at its first key: an anchor or a tag on the key line (`retool: &relay`) is where the
            # node itself starts, and that line stays as it is.
            first = node.value[0][0]
            start, end = offsets[first.start_mark.line], offsets[node.end_mark.line]
            edits.append((start, end, block(after[name], first.start_mark.column)))
        else:
            # The new key goes where the retool key is and its body where retool's body is, at the profile's own
            # indentation (two spaces or four).
            main = nodes['retool']
            position = offsets[main.end_mark.line]
            edits.append((position, position, ' ' * keys['retool'].start_mark.column + name + ':' + newline
                          + block(after[name], main.value[0][0].start_mark.column)))
    # Apply an insertion at a replacement's end first so its bytes are retained.
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        source = source[:start] + replacement + source[end:]
    if not final_newline:
        source = source.removesuffix(newline)
    try:
        reloaded = yaml.load(source)
    except YAMLError as error:
        raise ValueError('The updated profile is not valid YAML (%s; a position counts in the updated text, not in '
                         'the profile); refusing to write it' % _problem(error)) from None
    if _semantic(reloaded) != _semantic(result):
        raise ValueError('YAML serialization changed a value; refusing to write the profile')
    return source.encode('utf-8')


def _comment_spans(source, scalars):
    """(start, end) of every YAML comment: a `#` outside all scalars that starts a line or follows whitespace."""
    spans = sorted((node.start_mark.index, node.end_mark.index) for node in scalars)
    starts, line_end = [start for start, _ in spans], re.compile(r'[\r\n]')
    found, position = [], source.find('#')
    while position >= 0:
        index = bisect.bisect_right(starts, position) - 1
        outside = index < 0 or position >= spans[index][1]
        if outside and (position == 0 or source[position - 1] in ' \t\r\n\ufeff'):
            stop = line_end.search(source, position)
            end = stop.start() if stop else len(source)
            found.append((position, end))
            position = end
        position = source.find('#', position + 1)
    return found


def mask_profile(encoded):
    """Profile bytes without credentials or comment text; the structure is kept.

    MASK replaces the values under credential keys, the argument after a secret flag, the value of each NAME=value
    item of an env list, the user info and secret parameters of URLs and secret NAME=value text. Every comment keeps
    its place but reads `# ***`, because a comment can quote an old key. Every other byte is kept, except that a plain
    or quoted value holding a masked URL or argument is rewritten in double quotes. The rules go by name and shape:
    review a preview before sharing it.
    """
    from ruamel.yaml.error import YAMLError
    from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
    source = decode_profile(encoded)
    yaml = _yaml()
    try:
        root = yaml.compose(source)
    except YAMLError as error:
        raise ValueError('The preview is not valid YAML: %s' % _problem(error)) from None
    secrets, partial, scalars, seen = {}, {}, [], set()

    def visit(node, secret, assignments=False):
        if (id(node), secret, assignments) in seen:
            return
        seen.add((id(node), secret, assignments))
        if isinstance(node, MappingNode):
            for key, value in node.value:
                name = key.value if isinstance(key, ScalarNode) else ''
                visit(value, secret or is_secret_key(name, isinstance(value, MappingNode)),
                      is_environment_list(name, isinstance(value, SequenceNode)))
        elif isinstance(node, SequenceNode):
            flagged = False
            for item in node.value:
                visit(item, secret or flagged, assignments)
                flagged = isinstance(item, ScalarNode) and is_secret_flag(item.value)
        elif isinstance(node, ScalarNode) and node.value:
            if secret:
                # Numbers, booleans and dates are not credentials; text and custom tags are.
                if node.tag == STR or not node.tag.startswith('tag:yaml.org,2002:'):
                    secrets[id(node)] = node
            elif node.tag == STR and node.style not in ('|', '>'):
                masked = mask_string(node.value, assignments)
                if masked != node.value:
                    partial[id(node)] = (node, masked)

    def collect(node, visited):
        """Every scalar, keys included: a `#` inside one is text, not a comment."""
        if id(node) in visited:
            return
        visited.add(id(node))
        if isinstance(node, ScalarNode):
            scalars.append(node)
        elif isinstance(node, MappingNode):
            for key, value in node.value:
                collect(key, visited)
                collect(value, visited)
        elif isinstance(node, SequenceNode):
            for item in node.value:
                collect(item, visited)

    def replacement(node, text):
        start, end = node.start_mark.index, node.end_mark.index
        old = source[start:end]
        # Keep an anchor (aliases still resolve); drop its tag, which would not fit the masked text.
        anchors = ''.join(re.findall(r'&\S+\s+', re.match(r'(?:(?:&\S+|!\S*)\s+)*', old).group()))
        tail = next((ending for ending in ('\r\n', '\n', '\r') if old.endswith(ending)), '')
        return start, end, anchors + text + tail

    def block_edit(node):
        """A block scalar is masked where it stands, so its style and indentation survive (plugin source stays readable)."""
        start, end = node.start_mark.index, node.end_mark.index
        old = source[start:end]
        head = re.match(r'((?:(?:&\S+|!\S*)\s+)*[|>][-+0-9]*)([ \t]+#[^\r\n]*)?', old)
        if not head:
            return None
        # A comment on the header line belongs to the scalar's own text, so the generic comment scan skips it.
        new = head.group(1) + (' # ' + MASK if head.group(2) else '') + mask_text(old[head.end():])
        return (start, end, new) if new != old else None

    if root is not None:
        visit(root, False)
        collect(root, set())
    edits = [replacement(node, "'" + MASK + "'") for node in secrets.values()]
    edits += [replacement(node, json.dumps(masked, ensure_ascii=False)) for node, masked in partial.values()]
    edits += [edit for edit in (block_edit(node) for node in scalars if node.style in ('|', '>') and id(node) not in secrets)
              if edit]
    edits += [(start, end, '# ' + MASK) for start, end in _comment_spans(source, scalars)]
    for start, end, text in sorted(edits, key=lambda edit: edit[0], reverse=True):
        source = source[:start] + text + source[end:]
    try:
        yaml.compose(source)
    except YAMLError as error:
        raise ValueError('Could not mask credentials in the preview safely (%s); rerun with --include-secrets '
                         'only if the preview may hold them' % _problem(error)) from None
    return source.encode('utf-8')


def run(args):
    path = Path(args.settings)
    original = read_file(path, 'Harness profile')
    document = load_profile(decode_profile(original, str(path)).removeprefix('\ufeff'), str(path))
    entries = routable_entries(args.relay_config)
    entries, virtual = sync_virtual_models.with_virtual_models(entries, args)
    result = update_document(document, entries)
    unchanged = _semantic(result) == _semantic(document)
    encoded = original if unchanged else encode_document(original, document, result)
    if args.output:
        write_preview(args.output, encoded if args.include_secrets else mask_profile(encoded))
    backup = write_backed_up(path, original, encoded, args.backup_dir) if args.write and not unchanged else None
    print(json.dumps({'settings': str(path), 'models': len(entries), 'written': backup is not None,
                      'changed': not unchanged, 'backup': str(backup) if backup else None, 'preview': args.output,
                      'preview_redacted': bool(args.output) and not args.include_secrets, **virtual},
                     ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', required=True)
    parser.add_argument('--relay-config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    parser.add_argument('--output', help='New private preview file; never overwritten')
    parser.add_argument('--include-secrets', action='store_true',
                        help='Keep credentials and comments in the --output preview; both are masked as *** by default')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--backup-dir')
    sync_virtual_models.add_arguments(parser)
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
