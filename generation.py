"""Explicit handling of controls absent from Retool's generation schema."""
import math

import structured
from apierr import unprocessable
import models


def _reject(name):
    raise unprocessable(f"'{name}' is not supported by the Retool route", param=name)


def _number(value, name, low, high, *, integer=False):
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not low <= value <= high or not math.isfinite(value):
        raise unprocessable(f"'{name}' has an invalid numeric value", param=name)


TITLE_FORMAT = {'type': 'json_schema', 'schema': {'type': 'object',
    'properties': {'title': {'type': 'string'}}, 'required': ['title'], 'additionalProperties': False}}


def _is_title_schema(schema):
    """The session-title contract of Claude Code and the Agent SDK, whatever its prompt says."""
    properties = schema.get('properties')
    return (schema.get('type') == 'object' and isinstance(properties, dict) and list(properties) == ['title']
            and isinstance(properties['title'], dict) and properties['title'].get('type') == 'string'
            and schema.get('required') == ['title'] and schema.get('additionalProperties') is False)


def _label(value, limit):
    return value.strip()[:limit] or None if isinstance(value, str) else None


def _format_spec(value, name, hint):
    """None for plain text, otherwise the JSON contract the reply has to satisfy."""
    if not isinstance(value, dict) or not isinstance(value.get('type'), str):
        raise unprocessable(f"'{name}' must be a format object" + hint, param=name)
    kind = value['type']
    if kind == 'text' and set(value) == {'type'}:
        return None
    if kind == 'json_object':
        return {'kind': kind, 'schema': None, 'name': None, 'description': None}
    if kind != 'json_schema':
        raise unprocessable(f"'{name}' is not supported by the Retool route" + hint, param=name)
    # Chat nests the schema in json_schema; Responses and Messages keep it flat.
    body = value['json_schema'] if isinstance(value.get('json_schema'), dict) else value
    if not isinstance(body.get('schema'), dict):
        raise unprocessable(f"'{name}' needs a JSON Schema object" + hint, param=name)
    return {'kind': kind, 'schema': body['schema'], 'name': _label(body.get('name'), 80),
            'description': _label(body.get('description'), 500)}


def _forces_tool(choice):
    if isinstance(choice, dict):
        return choice.get('type') in ('any', 'tool', 'custom', 'function')
    return choice == 'required'


# A name is routine, and logged at debug, only when all three hold: mass clients send it with every
# request, it changes neither the reply nor its price, and the response headers already name it
# (`x-proxi-ignored-parameters`, `x-proxi-thinking-display`). Everything else warns. Effort and thinking
# controls of a route that ignores them and `verbosity` of a model without levels are marked routine where
# they are collected; `seed`, `prompt_cache_retention`, `output_config.task_budget`, logprobs, logit_bias
# and any other `include` value are not.
ROUTINE_IGNORED = frozenset({
    'temperature', 'top_p', 'top_k', 'presence_penalty', 'frequency_penalty', 'metadata',
    'thinking.display', 'prompt_cache_key', 'prompt_cache_options', 'client_metadata',
    'verbosity', 'text.verbosity'})
# What codex-rs sends as `include` (core/src/client.rs, "reasoning.encrypted_content"; its
# "output.encrypted_content" is an off-by-default feature of the OpenAI provider) and what the AI SDK adds
# for `store: false` on a reasoning model.
DEFAULT_INCLUDE = frozenset({'reasoning.encrypted_content'})


def compatibility_level(parsed):
    """Journal level of `request.compatibility`: warn for anything material, info for inline tool changes."""
    names, changes = set(parsed.declared_ignored), set(parsed.inline_tool_changes)
    if names - set(parsed.routine_ignored) - changes:
        return 'warn'
    return 'info' if names & changes else 'debug'


# Retool has no stop sequences: the relay ends the reply at the first one itself (adapters.StopSequenceAdapter).
MAX_STOP_SEQUENCES = 16
MAX_STOP_LENGTH = 1000


def stop_sequences(data, dialect):
    """The stop sequences of a request: Chat's `stop` (a string or a list) and Messages' `stop_sequences`.

    An empty one stops nothing. Responses has no such parameter, so a `stop` there is refused as before; the safety
    classifier of Claude Desktop sends one on Messages, and refusing it blocked every check it makes."""
    found = []
    for name in ('stop', 'stop_sequences'):
        value = data.get(name)
        if value is None:
            continue
        if name == 'stop' and isinstance(value, str):
            value = [value]
        elif not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise unprocessable(f"'{name}' must contain strings", param=name)
        value = [item for item in value if item]
        if not value:
            continue
        if dialect == 'responses':
            _reject(name)
        if len(value) > MAX_STOP_SEQUENCES or any(len(item) > MAX_STOP_LENGTH for item in value):
            raise unprocessable(f"'{name}' takes at most {MAX_STOP_SEQUENCES} sequences of at most "
                                f"{MAX_STOP_LENGTH} characters", param=name)
        found.extend(value)
    return list(dict.fromkeys(found))


# Retool's Claude route takes maxCompletionTokens and ignores it: a cap of 300 returned 5,466 tokens and end_turn (protocol
# test run of 2026-11-06), so the relay cuts the reply itself (adapters.OutputCapAdapter). It does that for the caps that
# can bind: the route's answers end on their own after a few thousand tokens, and keeping a call of a client whole under a
# cap means holding it back until it ends, which the callers with caps of 32,000 to 128,000 (Claude Code, Claude Desktop,
# OpenCode) would pay for on every tool call with arguments they could not watch grow.
OUTPUT_CAP_EMULATED_BELOW = 16384


def output_cap(entry, max_tokens):
    """The cap the relay enforces on the reply of a request, or None when the route keeps it or it cannot bind."""
    if not max_tokens or max_tokens >= OUTPUT_CAP_EMULATED_BELOW or not models.ignores_output_cap(entry['id']):
        return None
    return max_tokens


def structured_output(data, dialect):
    """The JSON contract of the reply, or None for plain text.

    Retool has no constrained decoding: the relay states the contract in the
    instructions and validates the complete reply, so the client never gets
    content that breaks it. A request the relay cannot honour is rejected.
    """
    hint = structured.HINT if dialect == 'anthropic' else ''
    text = data.get('text')
    if text is not None and not isinstance(text, dict):
        raise unprocessable("'text' must be an object", param='text')
    output = data.get('output_config')
    sources = [('response_format', data.get('response_format')),
               ('text.format', (text or {}).get('format')),
               ('output_config.format', output.get('format') if isinstance(output, dict) else None)]
    messages = data.get('messages', data.get('input'))
    for i, message in enumerate(messages if isinstance(messages, list) else []):
        if isinstance(message, dict) and message.get('role') in ('system', 'developer'):
            inline = message.get('output_config')
            if isinstance(inline, dict) and inline.get('format') is not None:
                sources.append((f'messages[{i}].output_config.format', inline['format']))
    chosen = None
    for name, value in sources:
        spec = _format_spec(value, name, hint) if value is not None else None
        if spec is None:
            continue
        if chosen is None:
            chosen = dict(spec, source=name)
        elif (spec['kind'], spec['schema']) != (chosen['kind'], chosen['schema']):
            raise unprocessable('conflicting output formats' + hint, param=name)
    if chosen is None:
        return None
    if data.get('tools') and _forces_tool(data.get('tool_choice')):
        raise unprocessable(f"'{chosen['source']}' cannot be combined with a required tool call: the relay can "
                            "only validate a reply that is not a forced tool turn" + hint, param='tool_choice')
    chosen['title'] = (dialect == 'anthropic' and chosen['kind'] == 'json_schema' and not data.get('tools')
                       and _is_title_schema(chosen['schema']))
    chosen['label'] = ('validated-session-title' if chosen['title']
                       else 'validated-' + chosen['kind'].replace('_', '-'))
    return chosen


def controls(data, entry, dialect):
    """Return validated verbosity, the ignored names and which of them are routine; never echo values."""
    ignored, routine = [], set()
    thinking = data.get('thinking')
    if isinstance(thinking, dict) and thinking.get('display') == 'updates':
        # Retool exposes summaries, not Anthropic's negotiated update cadence.
        ignored.append('thinking.display')
    text = data.get('text')
    if text is not None and not isinstance(text, dict):
        raise unprocessable("'text' must be an object", param='text')
    text = text or {}
    output = data.get('output_config')
    if isinstance(output, dict) and output.get('task_budget') is not None:
        if not isinstance(output['task_budget'], dict):
            raise unprocessable("'output_config.task_budget' must be an object", param='output_config.task_budget')
        ignored.append('output_config.task_budget')

    # Verbosity is a hint: models without catalog levels (every Claude, GPT-4.x) get
    # it named as ignored instead of failing the whole request.
    levels, unsupported = [], []
    for value, name in ((data.get('verbosity'), 'verbosity'), (text.get('verbosity'), 'text.verbosity')):
        if value is not None:
            if not isinstance(value, str) or value not in ('low', 'medium', 'high'):
                raise unprocessable(f"'{name}' must be low, medium or high", param=name)
            levels.append(value)
            if value not in entry.get('verbosity_levels', ()):
                unsupported.append(name)
    if len(set(levels)) > 1:
        raise unprocessable('conflicting verbosity controls', param='verbosity')
    ignored.extend(unsupported)

    n = data.get('n')
    if n is not None:
        if type(n) is not int or n < 1:
            raise unprocessable("'n' must be a positive integer", param='n')
        if n != 1:
            _reject('n')
    for name in ('store', 'background'):
        if data.get(name) is not None:
            if type(data[name]) is not bool:
                raise unprocessable(f"'{name}' must be a boolean", param=name)
            if data[name]:
                _reject(name)
    for name in ('previous_response_id', 'audio', 'prompt'):
        if data.get(name) is not None:
            _reject(name)
    if data.get('parallel_tool_calls') is not None:
        if type(data['parallel_tool_calls']) is not bool:
            raise unprocessable("'parallel_tool_calls' must be a boolean", param='parallel_tool_calls')
        if not data['parallel_tool_calls'] and data.get('tools'):
            _reject('parallel_tool_calls')
    choice = data.get('tool_choice')
    if isinstance(choice, dict) and choice.get('disable_parallel_tool_use') is not None:
        value = choice['disable_parallel_tool_use']
        if type(value) is not bool:
            raise unprocessable("'disable_parallel_tool_use' must be a boolean", param='tool_choice.disable_parallel_tool_use')
        if value and data.get('tools'):
            _reject('tool_choice.disable_parallel_tool_use')
    if dialect == 'chat':
        legacy = data.get('function_call')
        if data.get('functions') or legacy not in (None, 'none', 'auto'):
            # The deprecated form of tools and tool_choice: the model was offered no function, and its call would come
            # back as message.function_call, which this route does not write (ultracode audit of the relay, C11).
            raise unprocessable("'functions' and 'function_call' are the deprecated form of 'tools' and 'tool_choice', "
                                "which the Retool route does not serve: send 'tools' and 'tool_choice'",
                                param='functions' if data.get('functions') else 'function_call')
        if legacy is not None:
            ignored.append('function_call')  # "auto" or "none" with no function to call asks for nothing
            routine.add('function_call')
    if data.get('service_tier') is not None and data['service_tier'] not in ('auto', 'default'):
        _reject('service_tier')
    if data.get('prompt_cache_key') is not None:
        if not isinstance(data['prompt_cache_key'], str):
            raise unprocessable("'prompt_cache_key' must be a string", param='prompt_cache_key')
        ignored.append('prompt_cache_key')
    if data.get('prompt_cache_retention') is not None:
        if data['prompt_cache_retention'] not in ('in_memory', '24h'):
            raise unprocessable("'prompt_cache_retention' has an invalid value", param='prompt_cache_retention')
        ignored.append('prompt_cache_retention')
    for name in ('prompt_cache_options', 'client_metadata'):
        # Codex sends both with every request; neither reaches Retool.
        if data.get(name) is not None:
            if not isinstance(data[name], dict):
                raise unprocessable(f"'{name}' must be an object", param=name)
            ignored.append(name)
    if data.get('include') is not None:
        if not isinstance(data['include'], list) or not all(isinstance(item, str) for item in data['include']):
            raise unprocessable("'include' must be an array of strings", param='include')
        if data['include']:
            ignored.append('include')
            if set(data['include']) <= DEFAULT_INCLUDE:
                routine.add('include')
    if data.get('conversation') is not None:
        # Chat/Messages already use this string as LOCAL affinity, with full
        # history supplied by the client. Responses means server-managed state.
        if dialect == 'responses' or not isinstance(data['conversation'], str):
            _reject('conversation')
    if data.get('truncation') is not None and data['truncation'] != 'disabled':
        _reject('truncation')
    if data.get('modalities') is not None and data['modalities'] != ['text']:
        _reject('modalities')
    if entry.get('reasoning_effort_control') == 'accepted-ignored':
        # Retool takes these and changes nothing: accepted, never forwarded, named without values.
        # Clients send one level to every model, so this is never a warning.
        names = [name for name in models.ignored_effort_controls(data, entry) if name not in ignored]
        ignored.extend(names)
        routine.update(names)

    numeric = (('temperature', 0, 2, False), ('top_p', 0, 1, False), ('top_k', 0, 2**31-1, True),
               ('seed', -(2**63), 2**63-1, True), ('presence_penalty', -2, 2, False),
               ('frequency_penalty', -2, 2, False), ('top_logprobs', 0, 20, True))
    for name, low, high, integer in numeric:
        if data.get(name) is not None:
            _number(data[name], name, low, high, integer=integer)
            ignored.append(name)
    if data.get('logprobs') is not None:
        if type(data['logprobs']) is not bool:
            raise unprocessable("'logprobs' must be a boolean", param='logprobs')
        ignored.append('logprobs')
    if data.get('logit_bias') is not None:
        if not isinstance(data['logit_bias'], dict):
            raise unprocessable("'logit_bias' must be an object", param='logit_bias')
        for value in data['logit_bias'].values():
            _number(value, 'logit_bias', -100, 100)
        ignored.append('logit_bias')
    if data.get('metadata') is not None:
        if not isinstance(data['metadata'], dict):
            raise unprocessable("'metadata' must be an object", param='metadata')
        ignored.append('metadata')
    routine.update(name for name in ignored if name in ROUTINE_IGNORED)
    return (levels[0] if levels and not unsupported else None), ignored, [name for name in ignored if name in routine]
