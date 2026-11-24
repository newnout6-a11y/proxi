"""The Minara models the relay publishes: minara_models.json and the account entries of config.json.

minara_models.json is measured by scripts/probe_minara.py: one small paid request per check on every chat model of
Minara's own catalog (tools, image input, how the reasoning arrives, the effort levels of one model per family). A model
it marks "ok" is published as minara/<name> (Claude spelled as the relay spells it: minara/claude-opus-5-5), the others
are not. An account without minara_models serves every published model; an account with the list serves exactly those
(by upstream id, minara/<name> or the older minara/<upstream id>), and a descriptor there may override a measured field.

Minara models are never Retool models: only a Minara account serves them, and no Retool model is ever sent to Minara.
"""
import copy
import datetime
import json
import pathlib
import threading

FILE = pathlib.Path(__file__).with_name('minara_models.json')
# What the relay measured by itself after this file was made (minara_refresh): a model Minara added, a new price, a model
# gone. It lives beside config.json (not in git); an entry there wins over this file's when it is newer than the file.
AUTO_NAME = 'minara_models.auto.json'
PUBLISHED = frozenset({'ok'})
# What an account descriptor may override; anything else in it is an error (config._check_upstream).
OVERRIDABLE = ('label', 'provider', 'context', 'max_output', 'tools', 'image', 'reasoning', 'reasoning_effort_levels',
               'forced_tool_choice')


def _read(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(path=FILE):
    """(upstream id -> measured entry, credits per USD); an absent or unreadable file measures nothing."""
    data = _read(path)
    measured = {entry['upstream_model']: entry for entry in data.get('models', [])
                if isinstance(entry, dict) and isinstance(entry.get('upstream_model'), str)}
    return measured, float(data.get('credits_per_usd') or 220)


def when(value):
    """The moment an ISO date or timestamp names (a bare date is its midnight, UTC), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


MEASURED, CREDITS_PER_USD = load()
SHIPPED = dict(MEASURED)  # this file's entries, whatever an overlay put over them
GENERATED_AT = _read(FILE).get('generated_at')
_LOCK = threading.RLock()


def snapshot():
    """Return a shallow copy of the measured catalog under lock for safe iteration."""
    with _LOCK:
        return dict(MEASURED)


def overlay_wins(entry, shipped=None, generated_at=None):
    """True when an entry the relay measured by itself replaces the shipped one: none shipped, or it is newer than the
    shipped file (a later measurement by hand, deployed with a newer file, wins again)."""
    generated_at = GENERATED_AT if generated_at is None else generated_at
    if shipped is None:
        return True
    moment, base = when(entry.get('refreshed_at')), when(generated_at)
    return moment is not None and (base is None or moment > base)


def apply_overlay(path):
    """Put the entries of an overlay file (minara_refresh writes it) over the measured catalog, in place; their ids.

    An entry wins over the shipped one only when newer than the shipped file; an unreadable overlay changes nothing."""
    with _LOCK:
        data = _read(path) if path else {}
        changed = []
        for upstream, entry in (data.get('models') or {}).items():
            if not isinstance(entry, dict) or entry.get('upstream_model') != upstream:
                continue
            if overlay_wins(entry, SHIPPED.get(upstream)):
                if MEASURED.get(upstream) != entry:
                    MEASURED[upstream] = entry
                    changed.append(upstream)
            elif upstream in SHIPPED and MEASURED.get(upstream) is not SHIPPED[upstream]:
                MEASURED[upstream] = SHIPPED[upstream]
                changed.append(upstream)
        return changed


def overlay_path(config_path):
    """Where the overlay of the relay whose config.json is `config_path` lives."""
    return pathlib.Path(config_path).resolve().with_name(AUTO_NAME) if config_path else None


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
    if measured is None or measured is MEASURED:
        measured = snapshot()
    if not isinstance(ident, str) or not ident:
        return None
    if ident in measured:
        return ident
    if ident.startswith('minara/'):
        rest = ident[len('minara/'):]
        if rest in measured:
            return rest
        for upstream, entry in list(measured.items()):
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
    if measured is None or measured is MEASURED:
        with _LOCK:
            found = MEASURED.get(upstream) or {}
    else:
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
        # The levels that go through Minara's /v1/messages: the chat path sends them to a channel without cache.
        'messages_levels': list(found.get('messages_levels') or []) or None,
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
    measured = snapshot()
    if not listed:
        return [(upstream, None) for upstream, entry in measured.items() if entry.get('status') in PUBLISHED]
    result = []
    for item in listed:
        value = {'id': item} if isinstance(item, str) else dict(item)
        upstream = upstream_of(value.get('id'), measured=measured)
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
