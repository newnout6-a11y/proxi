"""Virtual models <model>@<profile> in the client catalogs the sync scripts write.

A prompts profile with publish_as_model is listed by the relay as <model>@<profile> in /v1/models and
/codex/models (models.virtual_entries). The sync scripts list the same ids in the client catalogs, with the
efforts and limits of the base model. The profiles come from the prompts file the relay itself would load for
the relay configuration (the same PromptStore, so the same path rules) or from --prompts; without such a file,
or with no profile that publishes, the catalogs are exactly what they were before.
"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import models
import prompt_policy
from config import DEFAULTS as RELAY_DEFAULTS


def add_arguments(parser):
    """--prompts and --no-virtual-models, shared by every sync script."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--prompts', help='Prompts file whose profiles publish virtual models <model>@<profile> '
                                         '(default: the one the relay loads for --relay-config, if any)')
    group.add_argument('--no-virtual-models', action='store_true',
                       help='List only the catalog models, even when a prompts file publishes profiles as models')


def load_snapshot(relay_config, prompts=None):
    """(snapshot or None, path of the prompts file) as the relay would load it for this relay configuration.

    A file that exists but did not load is an error: the sync replaces a client catalog, so a broken file
    must not silently remove the virtual models that were listed before. No file is not an error.
    """
    if prompts is not None and not Path(prompts).is_file():
        raise ValueError('prompts file not found: %s' % prompts)
    try:
        saved = json.loads(Path(relay_config).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise ValueError('cannot read relay configuration %s: %s' % (relay_config, error)) from None
    if not isinstance(saved, dict):
        raise ValueError('relay configuration %s must contain a JSON object' % relay_config)
    cfg = {**RELAY_DEFAULTS, **saved, '_config_path': str(relay_config)}
    try:
        store = prompt_policy.PromptStore(cfg, None if prompts is None else str(prompts))
        snapshot = store.snapshot()
    except prompt_policy.PromptConfigError as error:
        raise ValueError('prompts file is invalid: %s' % error) from None
    path = Path(store.path)
    if snapshot is None and path.is_file():
        problems = '; '.join(str(item.get('message')) for item in store.errors() if isinstance(item, dict))
        raise ValueError('prompts file %s exists but could not be loaded%s; fix it, or pass --no-virtual-models'
                         % (path, ': ' + problems if problems else ''))
    return snapshot, str(path)


def with_virtual_models(entries, args):
    """(entries plus the published virtual models, summary fields); the summary is {} when there are none."""
    if getattr(args, 'no_virtual_models', False):
        return list(entries), {}
    snapshot, path = load_snapshot(args.relay_config, getattr(args, 'prompts', None))
    virtual = models.virtual_entries(entries, snapshot)
    summary = {'virtual_models': len(virtual), 'prompts': path} if virtual else {}
    return list(entries) + virtual, summary
