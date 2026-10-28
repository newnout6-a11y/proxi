
import json, pathlib, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).parent
rows = json.loads((HERE / "models_parsed.json").read_text(encoding="utf-8"))

lines = ['"""Model catalog mirrored from the Retool copilot bundle (probes/gen_models.py).',
         "",
         "Regenerate with: python probes/gen_models.py && python probes/gen_models_py.py",
         '"""',
         "from apierr import bad_request, not_found, service_unavailable",
         "",
         "# Accepted reasoning effort values per provider (from the same bundle).",
         "EFFORTS_BY_PROVIDER = {",
         '    "openai": ("minimal", "low", "medium", "high", "xhigh", "max", "none"),',
         '    "anthropic": ("low", "medium", "high", "max"),',
         '    "google": ("low", "medium", "high"),',
         "}",
         "",
         "# Which config key holds the providerResourceName for each provider.",
         "PROVIDER_RESOURCE_KEY = {",
         '    "openai": "openai_provider_resource_name",',
         '    "anthropic": "provider_resource_name",',
         '    "google": "google_provider_resource_name",',
         '    "retool-fast": "retool_fast_provider_resource_name",',
         "}",
         "",
         'ALIASES = {"claude-opus": "claude-opus-5-5", "claude-sonnet": "claude-sonnet-5",',
         '           "claude-haiku": "claude-haiku-4-5-20251001", "gpt-5.6": "gpt-5.6-sol",',
         '           "claude-opus-5.5": "claude-opus-5-5", "claude-opus-4.8": "claude-opus-4-8",',
         '           "claude-sonnet-4.6": "claude-sonnet-4-6", "gpt-6": "gpt-6-sol"}',
         "",
         "CATALOG = ["]
DROP = ("gpt-4.1",)  # к октябрю 2026 выведены из обихода
NEW_OPENAI = [("gpt-6-astra", 400000), ("gpt-6-sol", 400000), ("gpt-6-luna", 400000),
              ("gpt-6.1-sol", 400000), ("gpt-6.1-sol-ultrafast", 400000)]


def max_out(provider, mid):
    """Completion caps: 128k у топовых, ниже у мелких; из публичных карточек моделей."""
    if provider == "anthropic":
        return 128000
    if mid.startswith(("gpt-6", "gpt-5.4", "gpt-5.5", "gpt-5.6")):
        return 128000
    if mid.startswith("gpt-5"):
        return 128000
    if provider == "google":
        return 65536
    return 32768


def context_for(provider, mid, default):
    if provider == "anthropic":
        return 1000000          # Opus 5.5: 1M вход
    if mid.startswith("gpt-6") or mid.startswith(("gpt-5.4", "gpt-5.5", "gpt-5.6")):
        return 400000
    return int(default)


rows = [r for r in rows if not r["id"].startswith(DROP)]
for mid, ctx in NEW_OPENAI:
    rows.insert(0, {"id": mid, "provider": "openai", "label": mid.replace("gpt-", "GPT "), "context": ctx})

for row in rows:
    reasoning = "True" if row["provider"] in ("anthropic", "openai", "google") else "False"
    lines.append('    {{"id": "{id}", "label": "{label}", "provider": "{provider}", "context": {ctx}, '
                 '"max_output": {mo}, "reasoning": {r}, "tools": True, "vision": True}},'.format(
                     id=row["id"], label=row["label"].replace('"', "'"), provider=row["provider"],
                     ctx=context_for(row["provider"], row["id"], row["context"]),
                     mo=max_out(row["provider"], row["id"]), r=reasoning))
lines += ["]", "", "_BY_ID = {m['id']: m for m in CATALOG}", "",
          "_BUDGET_TO_EFFORT = ((1024, 'low'), (8192, 'medium'), (24576, 'high'))", ""]
lines += [
 "def catalog():",
 "    return CATALOG",
 "",
 "",
 "def _canonical(name):",
 "    name = (name or '').strip()",
 "    return ALIASES.get(name, name)",
 "",
 "",
 "def resolve(model, effort=None, *, strict=True, default_model=None):",
 "    name = _canonical(model or default_model or '')",
 "    if not name:",
 "        raise bad_request(\"'model' is required\", param='model')",
 "    entry = _BY_ID.get(name)",
 "    if entry is None:",
 "        if strict:",
 "            raise not_found(f\"unknown model '{name}'; see GET /v1/models\")",
 "        entry = {'id': name, 'provider': 'anthropic', 'reasoning': True, 'label': name, 'context': 200000}",
 "    if effort is not None:",
 "        allowed = EFFORTS_BY_PROVIDER.get(entry['provider'], ())",
 "        if not isinstance(effort, str) or effort.lower() not in allowed:",
 "            raise bad_request(",
 "                f\"reasoning effort for {entry['provider']} must be one of {', '.join(allowed) or 'none'}\",",
 "                param='reasoning_effort')",
 "        effort = effort.lower()",
 "        if not entry.get('reasoning', True):",
 "            effort = None",
 "    return entry, effort",
 "",
 "",
 "def provider_resource(cfg, entry):",
 "    key = PROVIDER_RESOURCE_KEY.get(entry['provider'])",
 "    if not key:",
 "        raise service_unavailable(f\"provider '{entry['provider']}' is not routed\")",
 "    value = cfg.get(key)",
 "    if not value:",
 "        raise service_unavailable(",
 "            f\"model '{entry['id']}' needs config key '{key}' (provider resource is not configured)\")",
 "    return value",
 "",
 "",
 "def effort_from_budget(budget):",
 "    try:",
 "        budget = int(budget)",
 "    except (TypeError, ValueError):",
 "        raise bad_request('thinking.budget_tokens must be an integer', param='thinking.budget_tokens')",
 "    if budget < 1024:",
 "        raise bad_request('thinking.budget_tokens must be >= 1024', param='thinking.budget_tokens')",
 "    for limit, effort in _BUDGET_TO_EFFORT:",
 "        if budget <= limit:",
 "            return effort",
 "    return 'max'",
 "",
 "",
 "def budget_from_effort(effort):",
 "    return {'low': 1024, 'medium': 8192, 'high': 24576, 'max': 32768}.get(effort, 8192)",
 ""]
(HERE.parent / "models.py").write_text("\n".join(lines), encoding="utf-8")
print("models.py written, lines:", len(lines))
