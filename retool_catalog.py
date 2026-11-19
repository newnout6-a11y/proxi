"""Retool's own model registry, read from its frontend, and the overlay of the models the relay published from it.

models.CATALOG is audited by hand (docs/reports/model-catalog-audit.md) and was the whole Retool catalog: a model Retool
added stayed unknown to the relay until someone added it to the code (Claude Fable 5.2 was in Retool's registry and not
in the relay's on 2026-11-13). Retool's frontend carries the registry of its AI features as one JSON document, inside a
chunk of its scripts: `JSON.parse('{"anthropic":{"defaultModel":...,"models":{"claude-fable-5-2":{...}}},"openAI":...}')`,
with each model's display name, context window, output limit, effort levels and modalities. retool_refresh finds that
chunk, asks each new chat model one question through an account, and writes the ones that answer to
retool_models.auto.json beside config.json; apply_overlay puts them into models.CATALOG, at load and after each look.
"""
import json
import os
import pathlib
import re

import auth
import models

AUTO_NAME = "retool_models.auto.json"
VERSION = 1
# The registry's providers the relay routes, by its own provider names (models.PROVIDER_RESOURCE_KEY).
PROVIDERS = {"anthropic": "anthropic", "openAI": "openai", "retoolFast": "retool-fast", "google": "google"}
REGISTRY_MARK = "JSON.parse('{\"anthropic\":"
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
# The version Retool's frontend names itself by, in its entry script (main-*.js): `const RETOOL_VERSION="4.99.0"` and
# RETOOL_VERSION_STRING=`${RETOOL_VERSION}-dd22f13 (Build 424025)`, which it sends as X-Retool-Client-Version.
_VERSION = re.compile(r'\bRETOOL_VERSION="(\d+\.\d+\.\d+)"')
_VERSION_STRING = re.compile(r"\bRETOOL_VERSION_STRING=`\$\{RETOOL_VERSION\}(-[0-9a-f]{4,40} \(Build \d+\))`")


def client_version(script):
    """The X-Retool-Client-Version of the frontend whose entry script this is ("4.99.0-dd22f13 (Build 424025)"); None when
    the script names none."""
    version, suffix = _VERSION.search(script or ""), _VERSION_STRING.search(script or "")
    return version[1] + suffix[1] if version and suffix else None


def overlay_path(config_path):
    """Where the overlay of the relay whose config.json is `config_path` lives."""
    return pathlib.Path(config_path).resolve().with_name(AUTO_NAME) if config_path else None


def read(path):
    """The overlay file's state, {} when it is missing, unreadable or of another version."""
    if not path:
        return {}
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("version") == VERSION else {}


def apply_overlay(path):
    """Publish the overlay's models (retool_refresh writes them) into models.CATALOG, in place, and give every model the
    effort levels a probe saw its route take (state "levels"); the ids added or changed. An audited entry of the same id
    wins over a published one; an unreadable overlay changes nothing. The frontend version the last look read becomes
    the X-Retool-Client-Version of every request (auth.set_client_version)."""
    state = read(path)
    if state.get("client_version"):
        auth.set_client_version(state["client_version"])
    entries = [entry for entry in (state.get("models") or {}).values() if isinstance(entry, dict)]
    changed = models.add_discovered(entries)
    verified = {ident: record.get("verified") for ident, record in (state.get("levels") or {}).items()
                if isinstance(record, dict) and record.get("verified")}
    return changed + [ident for ident in models.add_levels(verified) if ident not in changed]


def _unescape(literal):
    """A JavaScript string literal's text (the part between its quotes) as the string it stands for."""
    def one(match):
        code = match[1]
        if code[0] == "u":
            return chr(int(code[1:], 16))
        if code[0] == "x":
            return chr(int(code[1:], 16))
        return _JS_ESCAPES.get(code, code)
    return re.sub(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", one, literal, flags=re.DOTALL)


def parse_registry(script):
    """The registry document of a frontend chunk ({provider: {"models": {id: model}}}), None when the chunk holds none.
    ValueError when it holds one this reader cannot read."""
    start = script.find(REGISTRY_MARK)
    if start < 0:
        return None
    start += len("JSON.parse('")
    at = start
    while True:
        at = script.find("'", at)
        if at < 0:
            raise ValueError("the registry's string never ends")
        backslashes = 0
        while script[at - 1 - backslashes] == "\\":
            backslashes += 1
        if backslashes % 2 == 0:
            break
        at += 1
    registry = json.loads(_unescape(script[start:at]))
    if not isinstance(registry, dict) or not isinstance((registry.get("anthropic") or {}).get("models"), dict):
        raise ValueError("the registry has no models")
    return registry


def chat_models(registry):
    """The registry's chat models of the providers the relay routes, compact: {id: what a catalog entry needs}.

    A deprecated model (lifecycle.deprecatedOn), one that writes no text (images, embeddings) and a fine-tuned one
    (ft:...) are left out."""
    found = {}
    for source, provider in PROVIDERS.items():
        for ident, model in ((registry.get(source) or {}).get("models") or {}).items():
            if not isinstance(model, dict) or not isinstance(ident, str) or ":" in ident:
                continue
            capabilities = model.get("capabilities") or {}
            if capabilities.get("textGeneration") is False or (model.get("lifecycle") or {}).get("deprecatedOn"):
                continue
            levels = capabilities.get("reasoningEffortLevels")
            found[ident] = {
                "id": ident, "provider": provider, "label": model.get("displayName") or ident,
                "context": capabilities.get("maxTokens"), "max_output": capabilities.get("maxCompletionTokens"),
                "reasoning_effort_levels": [level for level in levels if isinstance(level, str)]
                if isinstance(levels, list) else [],
                "reasoning": bool(levels) or bool(capabilities.get("supportsAdaptiveThinking")),
                "tools": capabilities.get("functionCalling", True) is not False,
                "vision": bool(capabilities.get("multimodal")),
            }
    return found


def new_models(found, published=()):
    """The models of `found` (chat_models) that the relay's catalog lacks and the overlay has not published yet."""
    return {ident: model for ident, model in found.items()
            if ident not in published and (ident not in models._BY_ID or models._BY_ID[ident].get("discovered"))}


def write(path, state):
    """Write the overlay atomically (the relay reads it at every start)."""
    import tempfile
    state["version"] = VERSION
    folder = os.path.dirname(os.path.abspath(path))
    handle, temporary = tempfile.mkstemp(prefix=".retool-catalog-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
