"""The Minara models the relay publishes: minara_models.json and the account entries of config.json.

minara_models.json is measured by scripts/probe_minara.py: one small paid request per check on every chat model of
Minara's own catalog (tools, image input, how the reasoning arrives, the effort levels of one model per family). A model
it marks "ok" is published as minara/<name> (Claude spelled as the relay spells it: minara/claude-opus-5-5), the others
are not. An account without minara_models serves every published model; an account with the list serves exactly those
(by upstream id, minara/<name> or the older minara/<upstream id>), and a descriptor there may override a measured field.

Minara models are never Retool models: only a Minara account serves them, and no Retool model is ever sent to Minara.
"""
import copy
import json
import pathlib

FILE = pathlib.Path(__file__).with_name('minara_models.json')
PUBLISHED = frozenset({'ok'})
# What an account descriptor may override; anything else in it is an error (config._check_upstream).
OVERRIDABLE = ('label', 'provider', 'context', 'max_output', 'tools', 'image', 'reasoning', 'reasoning_effort_levels',
               'forced_tool_choice')


def load(path=FILE):
    """(upstream id -> measured entry, credits per USD); an absent or unreadable file measures nothing."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}, 220.0
    measured = {entry['upstream_model']: entry for entry in data.get('models', [])
                if isinstance(entry, dict) and isinstance(entry.get('upstream_model'), str)}
    return measured, float(data.get('credits_per_usd') or 220)


MEASURED, CREDITS_PER_USD = load()


def public_id(upstream):
    """minara/<name>: Claude as the relay spells it (claude-opus-5-5), any other model without its vendor prefix."""
    vendor, _, name = upstream.partition('/')
    if vendor == 'anthropic':
        name = name.replace('.', '-')
    return 'minara/' + (name or upstream)


def upstream_of(ident, measured=None):
    """The upstream id that a descriptor or a client name means, or None.

    Accepts the upstream id itself (anthropic/claude-opus-5.5), the published name (minara/claude-opus-5-5) and the older
    minara/<upstream id> (minara/anthropic/claude-opus-5.5)."""
    measured = MEASURED if measured is None else measured
    if not isinstance(ident, str) or not ident:
        return None
    if ident in measured:
        return ident
    if ident.startswith('minara/'):
        rest = ident[len('minara/'):]
        if rest in measured:
            return rest
        for upstream, entry in measured.items():
            if entry.get('id') == ident:
                return upstream
        return rest if '/' in rest else None
    return ident if '/' in ident else None


def _effort_control(entry, studied=None):
    if not entry['reasoning'] or studied == 'accepted-ignored':
        # Clients send one effort to every model (OpenCode, Claude Desktop): a model that does not reason, or whose levels
        # the effort study found to change nothing, takes it and nothing is forwarded, as for Retool's accepted-ignored routes.
        return 'accepted-ignored'
    if entry.get('effort_map'):
        # Every level is accepted and sent as the study's map says: as asked, or, for a broken level, as one that works.
        # The offered list may be empty (no level does more than the default); a level sent anyway still goes upstream.
        return 'mapped'
    return 'selectable' if entry['reasoning_effort_levels'] else 'passthrough'


def route(upstream, descriptor=None, measured=None):
    """The relay's model entry for one upstream id: the measurement, then the account's overrides."""
    measured = MEASURED if measured is None else measured
    found = measured.get(upstream) or {}
    value = dict(descriptor or {})
    tools = value.get('tools', found.get('tools'))
    vision = value.get('image', found.get('vision'))
    # A model nobody measured (an excluded one an account lists, or one not in the file) may well reason: its levels go
    # upstream as asked (passthrough) instead of being dropped as for a model known not to reason.
    reasoning = value.get('reasoning', found['reasoning'] if 'reasoning' in found else True)
    levels = list(value.get('reasoning_effort_levels', found.get('reasoning_effort_levels') or []))
    # An account that lists its own levels takes them as they are: the study's map no longer describes them.
    effort_map = None if 'reasoning_effort_levels' in value else found.get('effort_map')
    entry = {
        'id': public_id(upstream) if not found else found['id'],
        'upstream': 'minara', 'upstream_model': upstream,
        'provider': value.get('provider', found.get('provider') or upstream.split('/')[0]),
        'label': value.get('label', found.get('label') or upstream),
        'context': value.get('context', found.get('context')), 'max_output': value.get('max_output', found.get('max_output')),
        # An unmeasured capability is not refused: the request goes and upstream answers it.
        'tools': True if tools is None else bool(tools), 'vision': True if vision is None else bool(vision),
        'reasoning': bool(reasoning), 'reasoning_effort_levels': levels if reasoning else [],
        'effort_map': copy.deepcopy(effort_map) if reasoning and effort_map else None,
        # Unmeasured, a Claude route asks for a forced call in words: Anthropic refuses tool_choice any/tool with thinking.
        'forced_tool_choice': bool(value.get('forced_tool_choice', found.get(
            'forced_tool_choice', not upstream.startswith('anthropic/')))),
        'thinking': found.get('thinking'), 'thinking_signature': bool(found.get('thinking_signature')),
        'cache': found.get('cache') or ('explicit' if upstream.startswith('anthropic/') else 'automatic'),
        'pricing': copy.deepcopy(found.get('pricing')), 'backend': found.get('backend'),
        # The level sent when the client asks for none, where the model's own default lands on a channel that drops the
        # cache marks (scripts/probe_minara.py cached_levels); None sends nothing, the model's own default.
        'default_reasoning_effort': found.get('default_effort') if reasoning and effort_map else None,
        # The level that reasons like the model's own default, for a client that must name one (Codex's default level).
        'natural_reasoning_effort': found.get('natural_effort') if reasoning and effort_map else None,
        'metadata_source': 'minara_models.json' if found else 'operator-configured',
        'metadata_status': ('measured ' + str(found.get('checked_at'))) if found.get('checked_at') else 'unverified',
    }
    if found.get('served_as'):
        entry['served_as'] = found['served_as']
    entry['reasoning_effort_control'] = _effort_control(
        entry, None if 'reasoning_effort_levels' in value else found.get('effort_control'))
    return entry


def descriptors(cfg):
    """(upstream id, overrides) for every model this Minara account serves."""
    listed = cfg.get('minara_models')
    if not listed:
        return [(upstream, None) for upstream, entry in MEASURED.items() if entry.get('status') in PUBLISHED]
    result = []
    for item in listed:
        value = {'id': item} if isinstance(item, str) else dict(item)
        upstream = upstream_of(value.get('id'))
        if upstream is not None:
            result.append((upstream, {key: value[key] for key in OVERRIDABLE if key in value} or None))
    return result


def served(cfg):
    """The upstream ids this Minara account serves."""
    return {upstream for upstream, _ in descriptors(cfg)}


def build(configs):
    """id -> entry over every Minara account; one model described two ways by two accounts is a configuration error."""
    routes = {}
    for cfg in configs:
        if cfg.get('upstream', 'retool') != 'minara':
            continue
        for upstream, overrides in descriptors(cfg):
            entry = route(upstream, overrides)
            previous = routes.get(entry['id'])
            if previous is not None and previous != entry:
                raise ValueError('conflicting Minara model descriptors: ' + entry['id'])
            routes[entry['id']] = entry
    return routes


def credits_of(usd):
    """Minara credits for a cost in USD (usage.cost): 220 per dollar, measured on 2026-11-09."""
    return round(usd * CREDITS_PER_USD, 4)
