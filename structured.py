"""JSON output formats for a route without constrained decoding.

Retool cannot force a reply to follow a schema. The relay states the contract in
the instructions and validates the complete reply before the client sees any of it.
"""
import json
import re
from urllib.parse import unquote

MAX_CHARS = 256 * 1024
MAX_DEPTH = 64
# A self-referencing schema that never consumes the reply could otherwise branch
# without end; an exhausted budget accepts the rest instead of rejecting it.
MAX_VISITS = 100_000
HINT = (" Claude Code 2.1.288 and newer can stop sending output_config.format "
        "with CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1.")
# Some models wrap bare JSON in one Markdown fence even when told not to. That
# single envelope is removed; prose around the JSON is never extracted or repaired.
_FENCE = re.compile(r'\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n```\s*', re.DOTALL | re.IGNORECASE)
_TYPES = {
    'object': lambda value: isinstance(value, dict),
    'array': lambda value: isinstance(value, list),
    'string': lambda value: isinstance(value, str),
    'boolean': lambda value: isinstance(value, bool),
    'null': lambda value: value is None,
    'number': lambda value: type(value) in (int, float),
    'integer': lambda value: type(value) is int or (type(value) is float and value.is_integer()),
}


def instructions(spec, tools=False):
    """Tell the model the contract; the adapter enforces it afterwards."""
    if spec['kind'] == 'json_object':
        shape = 'exactly one JSON object'
    else:
        shape = 'exactly one JSON value that conforms to the JSON Schema below'
    lead = ("Use the supplied tools as usual. Your final answer, the reply in which you call no tool, must be "
            if tools else "Your reply must be ")
    lines = ['[STRUCTURED OUTPUT FORMAT]',
             lead + shape + '. Output only that JSON. Do not wrap it in Markdown fences and do not add '
             'any other text.']
    if spec.get('name'):
        lines.append('Schema name: ' + spec['name'])
    if spec.get('description'):
        lines.append('Schema description: ' + spec['description'])
    if spec['kind'] == 'json_schema':
        lines.append('JSON Schema: ' + json.dumps(spec['schema'], ensure_ascii=False, separators=(',', ':')))
    return '\n'.join(lines)


def unfence(text):
    fenced = _FENCE.fullmatch(text)
    return fenced.group(1) if fenced else text


def _pairs(items):
    value = dict(items)
    if len(value) != len(items):
        raise ValueError('duplicate JSON keys')
    return value


def _constant(name):
    raise ValueError('JSON does not define ' + name)


def verify(text, spec):
    """Return the JSON text to send; raise ValueError with a problem that quotes no output."""
    body = unfence(text)
    try:
        value = json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError):
        raise ValueError('the reply is not one complete JSON value')
    if spec['kind'] == 'json_object':
        if not isinstance(value, dict):
            raise ValueError('the reply is not a JSON object')
    else:
        problem = check(value, spec['schema'])
        if problem:
            raise ValueError(problem)
    return body


def check(value, schema):
    """First violation as '<path>: <problem>' or None. Unknown keywords never reject."""
    return _Walk(schema).check(value, schema, '$', 0)


def _equal(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _type_names(declared):
    names = [declared] if isinstance(declared, str) else declared if isinstance(declared, list) else []
    return [name for name in names if isinstance(name, str)]


class _Walk:
    """One validation pass: the root for local $ref and the work budget."""

    def __init__(self, root):
        self.root = root
        self.visits = MAX_VISITS

    def resolve(self, ref):
        if ref == '#':
            return self.root
        if not ref.startswith('#/'):
            return None
        node = self.root
        for token in ref[2:].split('/'):
            token = unquote(token).replace('~1', '/').replace('~0', '~')
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
                node = node[int(token)]
            else:
                return None
        return node

    def check(self, value, schema, path, depth):
        if schema is False:
            return path + ': no value is allowed here'
        self.visits -= 1
        if not isinstance(schema, dict) or depth > MAX_DEPTH or self.visits < 0:
            return None
        if value is None and schema.get('nullable') is True:
            return None
        ref = schema.get('$ref')
        target = self.resolve(ref) if isinstance(ref, str) else None
        if target is not None:
            problem = self.check(value, target, path, depth + 1)
            if problem:
                return problem
        all_of = schema.get('allOf')
        for branch in all_of if isinstance(all_of, list) else ():
            problem = self.check(value, branch, path, depth + 1)
            if problem:
                return problem
        for keyword in ('anyOf', 'oneOf'):
            branches = schema.get(keyword)
            if isinstance(branches, list) and branches and all(
                    self.check(value, branch, path, depth + 1) for branch in branches):
                return path + ': matches none of the allowed alternatives'
        names = _type_names(schema.get('type'))
        if names and not any(_TYPES.get(name, lambda _: True)(value) for name in names):
            return '%s: expected %s' % (path, ' or '.join(names))
        if 'const' in schema and not _equal(value, schema['const']):
            return path + ': value differs from the required constant'
        options = schema.get('enum')
        if isinstance(options, list) and options and not any(_equal(value, option) for option in options):
            return path + ': value is not one of the allowed options'
        if isinstance(value, dict):
            return self.check_object(value, schema, path, depth)
        if isinstance(value, list):
            return self.check_array(value, schema, path, depth)
        return None

    def check_object(self, value, schema, path, depth):
        properties = schema.get('properties') if isinstance(schema.get('properties'), dict) else {}
        required = schema.get('required')
        for name in required if isinstance(required, list) else ():
            if isinstance(name, str) and name not in value:
                return "%s: missing required property '%s'" % (path, name)
        for name, sub in properties.items():
            if name in value:
                problem = self.check(value[name], sub, '%s.%s' % (path, name), depth + 1)
                if problem:
                    return problem
        patterns = schema.get('patternProperties') if isinstance(schema.get('patternProperties'), dict) else {}
        extra = schema.get('additionalProperties')
        for key, item in value.items():
            if key in properties:
                continue
            matched = False
            for pattern, sub in patterns.items():
                try:
                    hit = bool(re.search(pattern, key))
                except (re.error, TypeError):
                    hit, sub = True, True  # an unreadable pattern cannot reject anything
                if hit:
                    matched = True
                    problem = self.check(item, sub, path + '.*', depth + 1)
                    if problem:
                        return problem
            if matched:
                continue
            if extra is False:
                return path + ': unexpected property'
            problem = self.check(item, extra, path + '.*', depth + 1)
            if problem:
                return problem
        return None

    def check_array(self, value, schema, path, depth):
        items = schema.get('items')
        if isinstance(items, list):
            positional, rest = items, schema.get('additionalItems')
        else:
            prefix = schema.get('prefixItems')
            positional, rest = (prefix if isinstance(prefix, list) else []), items
        for index, item in enumerate(value):
            sub = positional[index] if index < len(positional) else rest
            problem = self.check(item, sub, '%s[%d]' % (path, index), depth + 1)
            if problem:
                return problem
        return None
