# Model and client audit — 2026-11-04

This audit tests the actual Retool route rather than treating a public model
page, a model's self-description, or a configured resource as availability proof.
All inference uses isolated synthetic conversations with project context,
relay-side execution and keepwarm disabled. Credentials and raw account dumps
are private and are never committed.

## Scope and acceptance

- Import the five supplied existing tenants before inference; preserve working
  credentials and avoid counting the existing tenant twice. Verify auth/balance.
- Research quality requirements and compare system instructions, tools, native
  thinking, prompt caching, routing, cancellation, errors and usage with this relay.
- Ask every catalog model test questions; separately verify Fable, Astra and
  GLM-5.3 effort controls, tool continuation and protocol behavior.
- Fix confirmed GLM effort and startup balance defects; reconcile discovery,
  advertised capabilities and installed client catalogs with live evidence.
- Verify Claude Code, Claude Desktop, Codex, OpenCode and the documented related
  clients at the level their installed versions and official interfaces permit.
  Distinguish installed-client execution from HTTP contract tests and documentation.
- Remove confirmed unavailable/dead model advertisements and duplicate aliases;
  retain evidence distinguishing unavailable models from transient failures.
- Add regression tests, run relevant checks, obtain independent review for every
  stage, and publish the final changes and evidence in a pull request.

## Stage 1: baseline inspection

Independent reviewer reproduced the reported GLM 400 before any upstream request:
`retool-fast` has no selectable route levels, while installed OpenCode sends
low/medium/high. Astra is also hardcoded as forbidden by the old client probe.
The startup banner uses only the primary account's credits. Discovery checks the
root config although selection checks each actual backend's provider resources.

The pool now contains the five requested tenants plus the previous disabled
exhausted account. Four were added; the already configured tenant keeps its
working credentials, app, thread and stable account ID. Each of the five enabled
sessions returned a fresh balance. Sum before inference: 13,527.0256 credits.
Private backup: `.agents/backups/config-before-model-audit-20261104.json`.

Further stages and their exact evidence are recorded below as they complete.

## Stage 2: verified fixes and first live matrix

The first direct App Builder probe asked all 38 catalog entries the same three
tiny questions (arithmetic, reversing a word, copying Cyrillic), with an isolated
system marker. Sixteen returned all answers correctly. Reported credits:
0.260664; this is a sum of provider reports, not an assumption that missing
reports mean free inference. A separate Assist probe is necessary: older GPTs
and Haiku actually answer there, so their age alone does not justify deletion.

Both GLM routes accepted `low`, `medium`, `high`, and `max`, returned correct
answers and supplied reasoning events. A provider-default request also answered,
adding spaces around separators. The first strict verifier counted that format
variation as failure; the audit now compares the four fields after trimming.
Acceptance proves those Retool controls are accepted, not that four distinct
underlying thinking budgets were measured. The public Z.ai API's native effort
list is different; it must not replace a measured Retool contract.

Implemented:

- GLM reasoning validation/selectors follow the live four-level Retool contract;
  GLM Flash is no longer advertised as lacking reasoning.
- Both Retool GLM routes also accepted images and correctly identified all four
  quadrants in two independently randomized color layouts each. This is a
  measured Retool image-input capability; it does not identify the underlying
  vision implementation or override the public native-model documentation.
- Startup balance uses the same per-account collection/aggregation as the API.
  Unknown balances remain unknown. Parallel balance collection honors the
  configured account refresh bound, capped at eight.
- Model discovery and admin resource status use the actual enabled tenants,
  preserving tenant-specific resources and reacting to account disablement.
- Model discovery includes Claude Desktop names, input-window metadata and
  documented Opus/Sonnet/Haiku family defaults. Undocumented Fable family tiers
  are not invented.
- `scripts/audit_models.py` requires `--allow-paid`, never rotates credentials,
  enables local execution or overwrites existing evidence. Synthetic prompts,
  protocol completion, usage and redacted errors are recorded separately.

Independent stage review reproduced the GLM defect and checked tenant isolation
and Desktop discovery requirements. The first seven pool/discovery regressions
and 32 GLM assertions failed before implementation. Expanded focused validation:
211 passed in 27.55 seconds. The exact command was:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_catalog_pool_discovery.py tests/test_glm_route_effort.py tests/test_model_audit.py tests/test_models_hardening.py tests/test_model_catalog.py tests/test_effort_policy.py tests/test_review_fixes_direct.py tests/test_challenger_stress.py tests/test_turnaround_optimization.py -o addopts= -q --tb=short
```

The current public Retool bundle is
[`main-DVkrwaG2.js`](https://retool-edge.com/assets_vjs/main-DVkrwaG2.js).
Its Astra entry explicitly requires BYOK for AI queries and disallows managed
keys. Live requests agree that Astra is unavailable on these managed resources.
Fable instead reports an unmet data-retention prerequisite; changing retention
was separately approved by the user for all supplied accounts. Its supported
resource configuration and actual availability are being checked; no hidden
feature flags or provider restrictions are bypassed.

Full stage validation:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests -o addopts= -q --tb=short
```

**1579 passed in 221.27 seconds.**
`node --test tests/test_opencode_labels.mjs`: **4 passed**.
`git diff --check`: clean.

After the final GLM image metadata correction, the scoped audit/discovery/GLM/
catalog/review suite passed **129 tests in 8.89 seconds**. Independent review
passed **84 post-correction tests** and approved the stage. The repeated image
probe saves the sent synthetic bodies, PNGs and SHA-256 hashes under
`.agents/model-audit-20261104/vision-captures/` for independent verification.
