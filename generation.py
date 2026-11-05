"""Explicit handling of controls absent from Retool's generation schema."""
import math
import client_context

from apierr import unprocessable


def _reject(name):
    raise unprocessable(f"'{name}' is not supported by the Retool route", param=name)


def _number(value, name, low, high, *, integer=False):
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not low <= value <= high or not math.isfinite(value):
        raise unprocessable(f"'{name}' has an invalid numeric value", param=name)


def _format(value, name):
    if not isinstance(value, dict) or not isinstance(value.get('type'), str):
        raise unprocessable(f"'{name}' must be a format object", param=name)
    if value['type'] != 'text' or set(value) != {'type'}:
        _reject(name)


TITLE_FORMAT = {'type': 'json_schema', 'schema': {'type': 'object',
    'properties': {'title': {'type': 'string'}}, 'required': ['title'], 'additionalProperties': False}}


def is_structured_title(data, system, dialect):
    """The observed SDK service contract, not general constrained decoding."""
    output = data.get('output_config')
    return (dialect == 'anthropic' and not data.get('tools')
            and client_context.is_sdk_title_system(system)
            and isinstance(output, dict) and output.get('format') == TITLE_FORMAT
            and output['format']['schema']['additionalProperties'] is False)


def _effort_controls_sent(data):
    """(client-facing name, value) for every effort or thinking control, where validate._effort reads it."""
    found = []

    def look(prefix, container):
        if not isinstance(container, dict):
            return
        for key in ('reasoning_effort', 'reasoningEffort', 'effort'):
            if container.get(key) is not None:
                found.append((prefix + key, container[key]))
        reasoning = container.get('reasoning')
        if isinstance(reasoning, dict) and reasoning.get('effort') is not None:
            found.append((prefix + 'reasoning.effort', reasoning['effort']))

    look('', data)
    for key in ('settings', 'options', 'body', 'extra_body'):
        look(key + '.', data.get(key))
    provider_options = data.get('providerOptions')
    look('providerOptions.openai.', provider_options.get('openai') if isinstance(provider_options, dict) else None)
    output = data.get('output_config')
    if isinstance(output, dict) and output.get('effort') is not None:
        found.append(('output_config.effort', output['effort']))
    thinking = data.get('thinking')
    if isinstance(thinking, dict):
        for key in ('effort', 'budget_tokens'):
            if thinking.get(key) is not None:
                found.append(('thinking.' + key, thinking[key]))
    messages = data.get('messages', data.get('input'))
    for message in messages if isinstance(messages, list) else []:
        inline = message.get('output_config') if isinstance(message, dict) else None
        if (isinstance(message, dict) and message.get('role') in ('system', 'developer')
                and isinstance(inline, dict) and inline.get('effort') is not None):
            found.append(('messages.output_config.effort', inline['effort']))
            break
    return found


def ignored_effort_controls(data, entry):
    """Names of the controls a route with reasoning_effort_control "accepted-ignored" drops.

    Retool takes these and changes nothing, so they are accepted (never a 400, which would break
    clients that send one level to every model), not forwarded, and reported by name only.
    """
    reasons = bool(entry.get('reasoning', True))
    # "none" asks for no reasoning: a route that never reasons already complies.
    names = [name for name, value in _effort_controls_sent(data)
             if reasons or str(value).lower() != 'none']
    thinking = data.get('thinking')
    kind = str(thinking.get('type') or '').lower() if isinstance(thinking, dict) else ''
    if kind and (kind != 'disabled') != reasons:
        names.append('thinking.type')  # an on/off switch this route cannot honour
    return names


def controls(data, entry, dialect, *, structured_title=False):
    """Return validated verbosity and ignored names; never echo sensitive values."""
    ignored = []
    thinking = data.get('thinking')
    if isinstance(thinking, dict) and thinking.get('display') == 'updates':
        # Retool exposes summaries, not Anthropic's negotiated update cadence.
        ignored.append('thinking.display')
    text = data.get('text')
    if text is not None and not isinstance(text, dict):
        raise unprocessable("'text' must be an object", param='text')
    text = text or {}
    output = data.get('output_config') or {}
    for value, name in ((data.get('response_format'), 'response_format'),
                        (text.get('format'), 'text.format'), (output.get('format'), 'output_config.format')):
        if value is not None:
            if not (structured_title and name == 'output_config.format'):
                _format(value, name)
    messages = data.get('messages', data.get('input'))
    for i, message in enumerate(messages if isinstance(messages, list) else []):
        if isinstance(message, dict) and message.get('role') in ('system', 'developer'):
            inline = message.get('output_config') or {}
            if isinstance(inline, dict) and inline.get('format') is not None:
                _format(inline['format'], f'messages[{i}].output_config.format')

    levels = []
    for value, name in ((data.get('verbosity'), 'verbosity'), (text.get('verbosity'), 'text.verbosity')):
        if value is not None:
            if not isinstance(value, str) or value not in ('low', 'medium', 'high'):
                raise unprocessable(f"'{name}' must be low, medium or high", param=name)
            if value not in entry.get('verbosity_levels', ()):
                _reject(name)
            levels.append(value)
    if len(set(levels)) > 1:
        raise unprocessable('conflicting verbosity controls', param='verbosity')

    for name in ('stop', 'stop_sequences'):
        value = data.get(name)
        if value is None:
            continue
        if name == 'stop' and isinstance(value, str):
            if value:
                _reject(name)
        elif not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise unprocessable(f"'{name}' must contain strings", param=name)
        elif value:
            _reject(name)
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
    if data.get('include') is not None:
        if not isinstance(data['include'], list) or not all(isinstance(item, str) for item in data['include']):
            raise unprocessable("'include' must be an array of strings", param='include')
        if data['include']:
            ignored.append('include')
    if data.get('conversation') is not None:
        # Chat/Messages already use this string as LOCAL affinity, with full
        # history supplied by the client. Responses means server-managed state.
        if dialect == 'responses' or not isinstance(data['conversation'], str):
            _reject('conversation')
    if data.get('truncation') is not None and data['truncation'] != 'disabled':
        _reject('truncation')
    if data.get('modalities') is not None and data['modalities'] != ['text']:
        _reject('modalities')

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
    if entry.get('reasoning_effort_control') == 'accepted-ignored':
        ignored.extend(name for name in ignored_effort_controls(data, entry) if name not in ignored)
    return (levels[0] if levels else None), ignored
