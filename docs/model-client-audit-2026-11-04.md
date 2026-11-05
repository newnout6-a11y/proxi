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

Superseded after the PR review: only leading system messages form the head. Later
non-empty ones stay in place as `<system-reminder>` user text with
`x-proxi-system-messages: inline-as-user-text`, so the head, the signed prefix and
the cache survive them; blank effort records add nothing. Details and live checks:
[relay-quality-and-prompts.md](relay-quality-and-prompts.md).

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

## Stage 4A: usage fidelity

Provider-style `reasoning_tokens` is no longer inferred from visible summaries
or an output-minus-character estimate. Only explicitly reported nonnegative
integer counters, bounded by reported inclusive output when present, are used;
actual zero is preserved. Current Retool captures normally do not provide this
optional counter. Output totals remain authoritative.

Token metadata without a price now reaches every adapter as usage, rather than
being dropped or converted into a free zero-credit request. API cost fields
separate `credits_used` (null when incomplete), `reported_credits_used` (known
subtotal) and `credits_complete`. Anthropic terminal streaming usage preserves
the same price fields as non-stream payloads. A possibly billed truncated/error
attempt remains unknown even after confirmed account failover succeeds.

Within one HTTP attempt, repeated cumulative cost snapshots debit only their
positive increases; different HTTP attempts remain separately billable. Partial
token snapshots merge fields that are actually present and preserve actual zero.
Attempt boundaries clear the previous token context. Journal token updates are
additive deltas with a separate full `usage_snapshot`, preventing duplicate
counts in metrics and the admin turn aggregation. Prefix sizing uses the merged
snapshot; later token-only reports do not falsely erase a previously known price.

The first focused extensions reproduced eleven failures for repeated prices,
partial fields and Anthropic streaming parity. Independent review then found
two additional gaps: journal token duplication and unknown truncated attempts
lost during failover. Six new regressions cover those cases across all dialects.
The post-correction scoped suite passed **243 tests in 15.18 seconds**:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_usage_truth.py tests/test_usage_consistency.py tests/test_console_log.py tests/test_cache_observability.py tests/test_keepwarm_accounting.py tests/test_native_history.py tests/test_review_fixes_pr19.py tests/test_account_dispatcher.py -o addopts= -q --tb=short
```

Final full Python command above: **1716 passed in 215.45 seconds**. Independent
expanded scoped review: **319 passed in 18.47 seconds**, plus nine no-charge,
partial/corrected-snapshot, retry, failure and cleanup probes. Both follow-up
findings are closed. `git diff --check`: clean. Stage 4B generation controls
remain the next substage; no design changes are included.

## Stage 4B: generation control fidelity

`verbosity` now maps from Chat's top-level control and Responses `text.verbosity`
to the current Retool request. The same top-level extension works through
Messages when selecting a supported GPT route. Capability metadata is limited
to explicit current registry entries plus the live GPT-6 Sol probe; Codex
discovery uses the same capability. Only this proven output control is excluded
from native-history provider scope. Effort/resource/model/tool changes retain
their existing invalidation behavior.

Unsupported material semantics now fail with 422: strict/JSON output formats,
nonempty stop controls, multiple choices, server-side prompt/state/background
features, automatic truncation, audio modes, non-default service tiers and
requests to prohibit parallel native tool calls. Compatible harmless defaults
remain accepted. Chat/Messages' existing string conversation affinity remains
local; Responses' server conversation is a different contract and is rejected.

Unsupported sampling and optional cache/include/metadata fields are type-checked
and exposed by names only through `x-proxi-ignored-parameters` and a compatibility
journal event. This avoids silently pretending those controls reached Retool or
logging their values. The exact audited field behavior and limits are documented
in [relay-quality-and-prompts.md](relay-quality-and-prompts.md).

Initial regressions reproduced 76 failures out of 77; three test assertions were
then corrected to expect tool_calls at intermediate tool turns. Independent
review found missing parallel-control enforcement, an ignored server prompt
reference and stale Codex verbosity discovery; these gained additional failing
regressions before correction. An initial full run exposed six local-affinity
compatibility failures, which were fixed by distinguishing API dialects.

Six isolated live GPT-6 Sol HTTP requests passed: verbosity low/medium/high via
the three APIs, followed by a low → high → omitted sequence with two tool result
continuations. Current values reached Retool; the previous raw prefix and cache
key remained unchanged. The synthetic task returned the expected marker.
Reported price: **0.03388 credits**; config hash unchanged. Three additional
unsupported-control requests returned 422 with **zero additional upstream POSTs**.
Private evidence: `.agents/model-audit-20261104/generation-live/manifest.json`
and wire captures. This establishes protocol acceptance and prefix continuity,
not a causal response-length guarantee or a measured cache hit.

Final stage 4B full validation: **1802 passed in 216.93 seconds** using the full
Python command above. Independent expanded review: **517 passed in 40.99
seconds**, plus 18 HTTP/SSE probes checking 422 before runner invocation and
names-only compatibility headers/journal for all three API dialects.
`git diff --check`: clean. No unresolved findings remain in stages 3 or 4.
Availability/catalog reconciliation and installed-client acceptance remain
separate later stages; design work remains deferred.

## Stage 5: verified routes and client catalog

The current configured pool exposes 27 canonical models, each with a successful
synthetic answer recorded in the final route audit. The registry retains 40
metadata entries. Unsupported endpoints and unmet resource prerequisites are
filtered separately from model capabilities and account balance. GPT-6.1 Terra
and GPT-4.1 are added with exact IDs; legacy GPTs and Haiku use the verified
Assist route. Temporary or thinking-only failures do not remove a working route.
The dated matrix and access conditions are in
[model-route-availability-2026-11-05.md](model-route-availability-2026-11-05.md).

Resource access descriptors belong to a specific account and resource UUID;
they are not inherited by another tenant. They describe configured BYOK or
retention and cannot enable a server-side permission. Fable remains blocked
by the organization prerequisite, Astra requires OpenAI BYOK, and Google
requires a real Google BYOK resource.

`scripts/sync_opencode_catalog.py` preserves other providers, authentication,
connection settings and agent permissions, replaces only the relay catalog
and its stale references/filters, and normalizes supported effort aliases.
It removes the old blacklist that hid working Haiku and placeholder zero USD
prices. Preview never changes the settings; writing requires a backup directory.
Windows ReplaceFileW saves the actual displaced version. Late concurrent edits
abort with recovery copies rather than being reported as a successful update;
the documented recovery behavior does not promise a filesystem CAS lock.

Independent final scoped review: **240 passed in 29.00 seconds**, plus eight
normalization/replacement-window probes, with no unresolved findings. Exact
review command:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_sync_opencode_catalog.py tests/test_client_policy_probe.py tests/test_route_availability.py tests/test_model_audit.py tests/test_effort_policy.py tests/test_catalog_pool_discovery.py tests/test_model_catalog.py tests/test_models_hardening.py tests/test_account_dispatcher.py tests/test_review_fixes_direct.py -o addopts= -q --tb=short
```

Both installed OpenCode versions accepted the final isolated preview with
27 configured and 27 listed models, with no unavailable models, invalid effort
variants or catalog mismatch. The shared installed settings were then updated:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python scripts/sync_opencode_catalog.py --write --backup-dir .agents/backups/opencode-catalog-20261105
```

The exact displaced configuration is backed up privately under that directory.
A post-write comparison confirmed only the expected generated changes; other
providers, relay connection/authentication and agent permissions are preserved.
The running relay on port 8787 was not restarted; source changes take effect
when the user next starts it.

## Stage 6: installed CLI acceptance and final validation

After synchronization, these commands ran the actual installed clients against
a local deterministic model server, using separate synthetic conversations:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python scripts/probe_client_settings.py opencode --require-complete-catalog
uv run --python 3.14 --with-requirements requirements.txt --no-project python scripts/probe_client_settings.py opencode2 --require-complete-catalog
uv run --python 3.14 --with-requirements requirements.txt --no-project python scripts/probe_client_settings.py claude
```

OpenCode 1.19.3 and OpenCode 2.2.5 each completed three model requests,
including a child/tool continuation. Each confirmed the full 27-model catalog
with no unsupported choices. Claude Code 2.1.310 completed four model requests
and confirmed one child agent completed successfully. All model requests kept
`high` effort. Every probe returned its success marker and exit code 0.
Private results: `.agents/model-audit-20261104/stage6-{opencode,opencode2,claude}.json`.
These probes use no provider inference or credits and emit no host file/shell
tool calls. They verify installed CLI settings and protocol continuation, not
upstream model quality or cache billing. Codex and Claude Desktop discovery are
covered by the HTTP contract/regression suite; their GUI flows were not run.
Other related clients retain documented compatibility, without an installed
execution claim. Earlier isolated live API checks remain separately documented
in stages 3 and 4.

Final repository checks after the stage 5 code changes:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests -o addopts= -q --tb=short
node --test tests/test_opencode_labels.mjs
git diff --check
```

Results: **1850 Python tests passed in 216.13 seconds**, **4 Node tests passed**,
and no whitespace errors. Stage 5 code received the independent review above;
stage 6 adds acceptance evidence without further source changes. Design/admin
UI changes remain deferred.

### Stage 6 follow-up: actual installed Codex and parser regression

A fresh installed Codex CLI 0.164.0 run against an isolated current-source relay
exposed a regression missed by the contract suite: unknown reasoning usage was
serialized as `output_tokens_details: {}`. Codex rejected ResponseCompleted with
`missing field reasoning_tokens`, then issued five additional sampling requests.
The failure, rather than only its eventual fix, is retained in private evidence.

An offline four-shape probe using the real installed Codex parser showed that
an empty details object fails, while omission, null, and a complete known-zero
object each finish with one HTTP request. The adapter now omits the optional
details object when the count is unknown or invalid; actual zero and positive
counts retain the complete object. It does not manufacture a zero reasoning
count to satisfy the client. Nine new regression cases failed before the fix.

The same run exposed a second issue: the client sent `strict: false` for
`exec_command` with optional non-nullable booleans, but the relay sent
`strict: true`. The provider returned optional arguments as null, which Codex
could not parse. Explicit client strict mode is now preserved; the previous
default remains when the client does not specify it. Three cross-dialect
regressions failed before correction, while strict-true and default cases passed.

The reproducible installed-parser check is:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python scripts/probe_codex_responses.py
```

Unknown, zero and seven reasoning tokens each completed with exit code 0,
exactly one local request, and no parse error. The probe uses no provider
inference, emits no tool calls and does not write provider/auth configuration.

A real installed Codex run then read two synthetic Python files, patched the
implementation and ran the three existing unittest cases successfully. A
separate verifier repeated those tests successfully. It completed one turn
through five upstream POSTs in **13.094 seconds**, with **0.47460 reported
credits**, no ResponseCompleted errors and no null-boolean errors. The file
reads, patch and test result appear in the actual CLI output.
The relay account configuration was byte-identical before and after the probe.

The fixture run used an explicit isolated provider, temporary local endpoint,
user-config exclusion and a fixture-only task; it did not change the user's
active Codex provider. The initial workspace-write attempt separately hit the
local command policy; the successful synthetic execution run explicitly used
the user-authorized unsandboxed CLI mode. Plugin-catalog authentication and
PowerShell shell-snapshot warnings remain client warnings, not relay failures.
The production relay on 8787 was left running without a restart.

Independent review: **179 tests passed in 10.16 seconds**:

```powershell
python -B -m pytest -p no:cacheprovider tests/test_responses_usage_contract.py tests/test_tool_strict_mode.py tests/test_codex_probe.py tests/test_usage_truth.py tests/test_usage_consistency.py tests/test_responses_items.py tests/test_native_tools.py tests/test_direct_transport.py tests/test_tool_arg_streaming.py
```

The full Python suite command above subsequently passed **1881 tests in 210.07
seconds**. Private evidence: `stage6-codex-live` (failed reproduction),
`codex-usage-shapes`, `codex-parser-final.json`, `stage6-codex-live-fixed` and
`stage6-codex-live-strict`, all under `.agents/model-audit-20261104/`.
Claude Desktop GUI acceptance and the remaining related-client inventory are
still separate requirements; a Codex CLI pass does not establish those.

### Stage 6 follow-up: current OpenCode and Claude Code inference

Both installed OpenCode versions completed a real `zai-org/GLM-5.3` high
request through an isolated current-source relay. Each sent the exact canonical
ID and high effort, received upstream thinking-start and text, then exited 0
with its synthetic success marker. CLI 1.19.3 finished in **7.900 seconds**
(one POST, **0.91880 reported credits**); CLI 2.2.5 finished in **5.863 seconds**
(one POST, **0.67812 reported credits**). This directly rechecks the original
GLM effort 400 through the actual clients. A first CLI 2 invocation used the
CLI 1 `--variant` flag and was rejected before HTTP; the correct CLI 2 form is
`--model provider/model#high`. The failed command remains in private evidence.

Fresh installed Claude Code 2.1.310 also read, edited and tested a separate
synthetic fixture through the current relay: four POSTs, **18.188 seconds**,
**0.92351 reported credits**, success exit and marker. Three existing unittest
cases passed inside the client and again in an independent verifier. No agents
or real project commands were used. Its optional `HEAD /api/hello` capability
probe received 404; inference and model discovery still succeeded. The CLI's
USD list-price estimate is not a measured conversion of Retool credits.

Each run used a new synthetic conversation and temporary endpoint/key override.
Real account configuration stayed byte-identical, and the production process
was not restarted. Private evidence: `stage6-opencode-glm-live`,
`stage6-opencode2-glm-live-fixed`, and `stage6-claude-live-fixture` under the audit
scratch folder. A text GLM pass does not prove every tool or image flow in the GUI.

### Stage 6 follow-up: current Harness catalogs

Read-only inventory found a real remaining defect in both current profile
patches: 29 stale Proxi models, including seven currently unavailable routes,
five missing verified routes, and unsupported xhigh levels. The current paths
are `~/.dsh/profiles/{desktop,web}/cordis.patch.yml`; the old root settings file
is not the current installed profile location.

`scripts/sync_harness_catalog.py` now reconciles those profiles without changing
other providers, plugin source, credentials or the active model. All 27 unique
IDs remain available: 26 reasoning models in `retool` and GPT-4.1 in
`retool-text`. Both retain the same existing endpoint and credential reference.
The split is required by the two installed SDKs: an inherited medium default
rejects GPT-4.1 before HTTP, but deleting that default on the one-provider
catalog silently disables thinking for GPT-6 Sol's default/off selector.
The separate text group keeps every valid selector and the reasoning default.

An independent runtime probe loaded the actual CLI adapter 0.2.2-alpha.3 with
pi-ai 1.5.0 and the actual Desktop adapter 0.2.1-rc.5 with pi-ai 1.1.0 from its
ASAR. It ran **22 cases**, including default/explicit off/high, rejection of
medium on GPT-4.1, and exact selectors for Sol and Terra: **16 fake fetches,
zero upstream requests**, all assertions passed. The same probe passed again
after the profile update. Native tool schemas and text/image metadata survived.
The SDK/ASAR were read-only; no application boot or real image inference is
claimed from this check.

The writer replaces only the Proxi blocks in the YAML. Full-document
round-tripping was rejected during development because it could alter other
providers' timestamp headers; ruamel's TimeStamp deepcopy also loses timezone
without explicit handling. Byte splicing, immutable timestamp handling and a
semantic guard now protect other data. The independent preservation check
verified every byte outside Proxi, the full parsed unrelated state, active
selection, custom tags and plugin source. External aliases and namespace
collisions are refused before writing. It approved the exact two previews.

The following writes completed, preserving an exact backup for each profile:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python scripts/sync_harness_catalog.py --settings C:/Users/Redmi/.dsh/profiles/desktop/cordis.patch.yml --write --backup-dir .agents/backups/harness-catalog-20261105/desktop
uv run --python 3.14 --with-requirements requirements.txt --no-project python scripts/sync_harness_catalog.py --settings C:/Users/Redmi/.dsh/profiles/web/cordis.patch.yml --write --backup-dir .agents/backups/harness-catalog-20261105/web
```

Post-write bytes equal the reviewed previews. Restoring the original Proxi
blocks in memory reproduces the entire old parsed profile; the other active
provider/model remains unchanged. Shared writer/catalog tests passed **22 tests**:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_sync_harness_catalog.py tests/test_sync_opencode_catalog.py -o addopts= -q --tb=short
```

Private reproducible evidence: `reviewer-harness-runtime.mjs`,
`reviewer-harness-runtime-manifest.json`, `harness-runtime-after-write.json`,
`reviewer-harness-preservation.py` and its manifest, plus both exact previews,
under `.agents/model-audit-20261104/`. Backups stay private under `.agents/backups/`.

### Remaining installed-client coverage

The related clients were inventoried separately; using another provider is not
a Proxi failure and their active routes were not replaced:

| Client | Current local state | Evidence scope |
| --- | --- | --- |
| Codex CLI 0.164.0 | Active user provider remains separate; isolated Proxi setup works | Real read/patch/test and installed strict-parser checks above |
| Claude Code 2.1.310 | Existing relay configuration retained | Fresh real Read/Edit/Bash fixture and child/default probes above |
| OpenCode CLI 1.19.3 and Desktop sidecar 2.2.5 | Shared Proxi catalog now 27 canonical IDs | Real GLM high request, full catalog and child/tool protocol probes |
| Harness CLI 0.2.2-alpha.3 and Desktop 0.2.1-rc.5 | Proxi models now 26+1; current active provider is separate | Both installed SDKs tested with exact profiles; no fresh GUI interaction |
| Cline CLI 3.0.55 | Plan/Act provider is `cline` | Inventory only; no Proxi request or provider change |
| Cline VS Code extension 4.1.26 | Installed; effective Proxi extension settings not established | Distinct from CLI globalState; no extension execution claim |
| Continue | Existing config points to a different OpenAI-compatible endpoint; installed extension not found in checked IDE indexes | Inventory only; existing provider retained |
| Aider | Not found in PATH, standard configuration or checked Python/uv/pipx locations | No installed-client runtime available |
| Claude Desktop 4.4212.0.0 | Gateway policy reconciled to 11 Claude routes; endpoint/auth/default preserved | Actual installed discovery/picker code and Agent SDK executed; real title/main protocol pass, details below |

Earlier GUI activation failures are historical. The user explicitly selected
installed-code and protocol verification without waiting for window activation.
There is no outstanding manual-action requirement. No GUI interaction pass is
claimed; inventory-only/absent clients retain their stated coverage limits.

Final source validation after the Harness maintenance utility used the full
Python command above: **1894 passed in 210.17 seconds**. The OpenCode label
command passed **4 tests** again, and `git diff --check` was clean. The real
listener still belonged to the unchanged user process on port 8787; these
current-source acceptance probes used isolated runtimes and do not claim the
production process hot-reloaded the new code.

### Final code/protocol verification — 2026-11-05

The running relay was replaced after the user stopped its old process. Discovery
returned 27 routes and startup showed the pool balance 13,296.0055/15,000.
An eight-request production smoke exposed a genuine cold tool-history bug:
an anonymous follow-up acquired another account and flattened assistant calls
and tool results into text. The first six stream/nonstream checks had passed;
that did not close the tool-continuation failure.

Complete client-visible tool batches now replay as native user/assistant/tool
roles when an opaque signed branch cannot resume. Matching result IDs,
duplicate/orphan/incomplete batches and reordered parallel results are checked.
Client thinking/signatures are never trusted. Relay-only retry instructions and
byte-identical internal retries are preserved. Three real current-source calls
across Chat, Responses and Messages returned exact tool-result markers with
native roles. Independent review also covered budget rejection before upstream.
Evidence: `cold-tool-live-current/manifest.json` and
`desktop-code-audit/cold-history-review-manifest.json` in the private audit tree.

Desktop's installed ASAR discovery/filter/merge/picker functions were extracted
and executed read-only. Its native filter accepts 11 Claude models from the
27-route API; GPT/GLM must not be disguised as Anthropic families. A nonempty
policy list is authoritative; discovery alone does not populate missing rows.
`sync_claude_desktop_catalog.py` updates only `inferenceModels` with an exact
backup and before-write/read-back checks. The applied policy preserved every
other registry value, including auth. Older effort pickers are capped at high
to avoid route-invalid xhigh; Opus/Sonnet 4.6 keep their four-level max. This
does not invent missing selectors for new 5.5 model IDs.

Exact installed SDK wrapper **0.3.309** was loaded from ASAR. Its embedded target
runner **2.1.309** is absent on disk. Live checks explicitly used cached Desktop
runner **2.1.295**, SHA-256
`a30012741c3d0b55e2a6c252dab3781134b75be95896ae89d40a93e10f83bcd6`.
It is not passed off as the missing target. Source hashes/function pointers:
`desktop-code-audit/installed-code-manifest.json`.

Actual synthetic SDK traffic found and fixed these service incompatibilities:

- Haiku `thinking: disabled` failed locally. Direct Assist probes proved `none`
  works for this exact route; only its capability was extended. OpenCode and
  both Harness profiles were synchronized again with exact backups.
- Beta `thinking.display: updates` triggered 422 and SDK retry. It now uses
  summaries with explicit compatibility headers/warnings; native beta update
  cadence is not claimed.
- The current title prefix was unrecognized. Its exact no-tools, `title:string`
  schema now gets an isolated buffered service path. A sole JSON fence may be
  normalized; duplicate keys, extra properties, wrong types and incomplete
  finishes fail before content. Limit: 256 Ki characters. General schemas still
  get 422 before inference. Usage/cost remain separate; early adapter rejection
  cancels upstream instead of leaving a paid background turn.
- Merging SDK billing attribution and system instructions caused Anthropic to
  consume the whole block. Raw A/B measured **87 input tokens and lost policy
  when merged**, versus **786 and correct title behavior when split**. The
  deterministic first-line split preserves actual instructions, one system role,
  signed continuation scope and cache budgets. Ordinary Desktop calls benefit
  as well: their input count rose from 64 to 456 when real policy reached upstream.

The final exact-SDK run made **two generation POSTs**, title/main: both 200,
no SSE errors or generation retries, main marker exact, 4.316 seconds. Runtime
was isolated; account config and ASAR stayed byte-identical. An earlier SDK main
result succeeded despite background title failures; it alone is not counted as
full service acceptance. Evidence: `desktop-code-audit/service-current-source-final/manifest.json`
and `desktop-code-audit/system-block-comparison.json`.

Independent final review: **366 passed in 10.63 seconds**, plus **29 separate
edge checks** covering signed billing replay/reset, prefix identity, cache budget,
title buffer boundary, JSON normalization/rejection and paid-tail cancellation.
No actionable findings remained. Exact command:

```powershell
python -B -m pytest -p no:cacheprovider tests/test_desktop_service_protocol.py tests/test_native_history.py tests/test_cold_tool_history.py tests/test_native_context_reuse.py tests/test_prompt_ownership.py tests/test_generation_controls.py tests/test_client_context.py tests/test_models_hardening.py tests/test_model_catalog.py
```

Root validation and live reproduction commands:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest -o addopts= -q --tb=short
node --test tests/test_opencode_labels.mjs
git diff --check
uv run --python 3.14 --with-requirements requirements.txt --no-project python .agents/desktop-current-source.py .agents/model-audit-20261104/desktop-code-audit/service-current-source-final
```

Final full Python suite: **1,962 passed in 211.82 seconds**. OpenCode label
tests: **4 passed**. Follow-up service/catalog/CORS suite: **78 passed in
4.37 seconds**. `git diff --check` passed. No design changes were included.

### Final production acceptance

Source commit `4e2b21d` was deployed after verifying that the old listener had
zero active requests and still belonged to the known relay process. New PID
2528 listens on `127.0.0.1:8787`; startup balance was 13,271.0479/15,000.
Health is good, discovery advertises 27 routes, and Haiku exposes its verified
`none` level. No unrelated client process was stopped.

`python .agents/production-smoke-20261105.py protocol-smoke-4e2b21d.json`
passed **all eight actual production requests**: stream/nonstream in each API,
plus tool call/result continuation. Every response was HTTP 200, exact markers
matched, and summed reported cost was 0.0716 credits (not an inferred USD price).
`python .agents/capture-desktop-service-call.py .agents/model-audit-20261104/desktop-code-audit/service-production-final`
also passed: exactly two generation calls, title/main, no SSE errors or retry;
SDK main completed in 3.75 seconds. The expected `/api/hello` 404 is a separate
capability probe, not a failed inference. Final stderr log was empty. Private
manifests retain both the earlier failures and the final passing evidence.
