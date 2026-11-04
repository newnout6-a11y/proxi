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

## Stage 3: system instructions and gateway quality

The requested quality comparison, exact default prompt, Retool-specific
capabilities, sources and evidence limits are recorded in
[relay-quality-and-prompts.md](relay-quality-and-prompts.md).

Explicit instructions now belong to every client, including unknown tool-less
IDEs. They do not receive the relay's host context/execution instructions, and
their text cannot trigger relay shell execution. Standalone requests retain a
separate configurable `relay_system_prompt`, project context and execution
policy. The default is concise enough to preserve tested 1500-byte follow-up
budgets and does not claim commands succeeded before seeing results.

Anthropic inline system guidance previously disappeared during native tool
continuation. A live raw request proved that Retool rejects multiple system
messages despite its public schema. Guidance is therefore explicitly normalized
into one head, in order, with `x-proxi-system-messages: merged-into-head` and a
compatibility journal warning. A changed head invalidates its previous signed
prefix; the following unchanged tool round trip preserves the new prefix again.
Latest inline effort updates are retained. Conflicting thinking controls and
unsupported turn-scoped `clear_at` are rejected before inference.

Three isolated live gateway checks returned the expected markers for standalone
custom policy (Chat), unknown-client instructions (Responses), and inline system
guidance (Messages). All three HTTP 200; reported total 0.02656 credits. The real
user configuration hash stayed unchanged. Private evidence:
`.agents/model-audit-20261104/prompt-live/manifest.json` and its wire captures.

Independent review found two control-edge cases, which gained nine failing
regressions before correction. Final independent scoped review: **248 passed
in 37.37 seconds** plus **3 prompt-budget tests in 0.55 seconds**. Full validation:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests -o addopts= -q --tb=short
```

**1606 passed in 203.69 seconds.** `git diff --check`: clean.

Fable retention was explicitly approved for all five supplied tenants. The
normal resource-edit API, with each owned resource's existing options preserved,
returned HTTP 403 for every tenant: `Data retention enabled Anthropic models are
not enabled for your organization.` No feature flag or access restriction was
changed. Fable 5 and 5.1 remain blocked by this organization prerequisite; Google
and Astra separately require suitable BYOK resources.

### Stage 3 extension: full user policy — 2026-11-05

The configurable prompt now supports a complete user-defined role, rules,
style and behavioral boundaries. `relay_system_prompt_mode` explicitly selects
`standalone` (compatible default), `append` (client instructions, then user
policy), or `replace` (user policy alone). `null` selects the default policy;
an empty string is deliberate. Native-tools requests without a system now also
honor the custom policy. The direct transport sends it in an actual system
message for all three API dialects; the legacy agent transport composes text.

Recognized safety checks, title generation and connection probes preserve their
service-specific contract. General client text requests receive the configured
policy. Client execution ownership remains unchanged even after replacement:
custom instructions cannot enable relay shell execution for an IDE request.
Prompt instructions describe desired behavior; actual tool permissions enforce
command/file access.

Signed native tool continuations preserve the same head and cache key while
policy is unchanged. Changing policy or composition invalidates the old signed
prefix, and the next unchanged continuation resumes its new history.

New regressions reproduced **21 failures before implementation**. After the
implementation, the prompt/service/native-history/context/budget suite passed
**177 tests in 4.77 seconds**. Exact command:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_prompt_ownership.py tests/test_native_history.py tests/test_service_requests.py tests/test_native_context_reuse.py tests/test_prompt_bytes.py -o addopts= -q --tb=short
```

Independent scoped review: **291 passed in 20.08 seconds**, no confirmed code
defects. The design/admin UI and stage 4 changes are outside this extension.

Review also reproduced an existing direct-transport budget defect: the system
head bypassed `max_prompt_bytes`. The limit now checks the complete UTF-8 head
and retained text chunks before transmission and the initial request before
acquiring upstream. Direct execution follow-ups reserve the original history
and earlier results, trimming only the new command output to the remaining
budget. Summary follow-ups use the same rule. Boundary regressions cover
standalone/append/replace, an exact byte boundary, one byte over, and small/large
command outputs with and without an execution step cap. Related scoped suite:
**196 passed in 6.42 seconds**. The independent reviewer reproduced and helped
close two follow-up cases that the first budget correction missed.

An earlier full run, before those final follow-up corrections, passed **1658
tests in 212.33 seconds**. The final full run is recorded below.

Isolated live checks confirmed exact policy markers and real system heads for
append and replace across Chat, Responses and Messages. Tool continuations
returned the actual synthetic tool result and retained the cache key; the
replace probe also compared replayed raw assistant blocks with the saved reply.
The simple tasks emitted no thinking blocks at either low or high, so these
live runs do not prove signed-thinking replay. Signature preservation and
policy-change reset are covered by dedicated offline protocol regressions.
An initial live verifier incorrectly required a thinking block on every tool
call; its failed assertion is retained alongside the evidence, not called a
model or preservation failure. Across the three probes: **15 HTTP 200 upstream
requests**, reported total **0.47428 credits**, user configuration hash unchanged.
Private evidence: `.agents/model-audit-20261104/full-policy-live*/manifest.json`
and corresponding wire captures. No host tool was executed.

Final stage 3 extension full validation: **1664 passed in 214.04 seconds** using
the full Python command above. Independent final scoped run: **253 passed in
6.54 seconds**, plus eight direct boundary probes including three commands in
sequence. `git diff --check`: clean. No unresolved stage 3 findings remain.
