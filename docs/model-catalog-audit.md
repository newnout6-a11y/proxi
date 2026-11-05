# Model catalog audit — 2026-10-29, updated 2026-11-05

The tables below describe the registry in `models.py` as of 2026-11-05, and tests pin
the counts, limits, levels and defaults. Route availability and the effort probe
behind the "accepted, not published" rows are in
[the 2026-11-05 live route audit](model-route-availability-2026-11-05.md). Statements of
the original 2026-10-29 audit that later probes overturned are rewritten here rather than
left in place: GPT-6 does not accept xhigh, Haiku 4.5 and GLM accept an effort level and
ignore it, and the GLM routes answered live.

The registry retains 40 Retool model IDs: 17 OpenAI, 15 Anthropic, 6 Google and 2
Retool-fast. The configured pool advertises 27 of them; `claude-sonnet-5-5` was added
on 2026-10-31, GPT-6.1 Terra and GPT-4.1 on 2026-11-05 (rows below match the code).
A provider deprecation does not remove an entry: a model Anthropic has retired
(Opus 4, Opus 4.1, Sonnet 4) stays in the registry with its dates and is never
published. Sonnet 4.5 is deprecated and retires on 2026-11-30, when it stops being
published. Resource availability is evaluated separately from model capabilities.

`metadata_checked_at`, `metadata_source`, `metadata_status` and optional
`metadata_notes` accompany every entry. `verified` means the values match the
sources below, not that a maximum-size live request was run. `partial` records a
known model with an unverified route/control. `provisional` identifies remaining
legacy or input-reservation estimates. No maximum-window stress test was run.

## Context and output limits

Retool's `TOKEN_LIMITS`/model-registry `maxTokens` describe the whole GPT window.
`COPILOT_MODEL_INPUT_CONTEXT_SIZE_MAP` reserves room for output and is not the
whole window: 272,000 for older GPT, 922,000 for GPT-5.4/5.5/5.6, and differing
872,000/936,000/191,808 values for Claude. The earlier catalog incorrectly exposed
these reservations or copied newer Claude limits onto older models.

| Models | Catalog context | Max output | Source/status |
| --- | ---: | ---: | --- |
| GPT-5, mini, nano, 5.1, 5.2, 5.3 Codex | 400,000 | 128,000 | Fresh Retool registry; mini/nano effort measured 2026-11-05 |
| GPT-5.4, 5.5, all 5.6 variants, 6 Astra/Sol/Luna, 6.1 Sol | 1,050,000 | 128,000 | Fresh Retool registry |
| Claude Opus 5.5/5/4.8/4.7/4.6, Fable 5, Sonnet 5/4.6 | 1,000,000 | 128,000 | Official model pages |
| Claude Opus 4.5, Sonnet 4.5, Haiku 4.5 | 200,000 | 64,000 | Official model pages |
| Claude Opus 4/4.1 | 200,000 | 32,000 | Provisional legacy output; retired by Anthropic (2026-06-15, 2026-08-05) |
| Claude Sonnet 4 | 200,000 | 64,000 | Provisional legacy output; retired by Anthropic (2026-06-15) |
| Gemini 3.5 Flash | 1,000,000 input | 65,000 | Public 1M/65K conservative values; route never probed (needs Google BYOK) |
| Other five Gemini entries | 983,040 reservation | 65,536 estimate | Provisional; route never probed; full model limits not individually audited |
| GLM-5.3 | 1,000,000 | 128,000 | Public limits; Retool route answered on 2026-11-05; sizes not stress-tested |
| GLM-5.3 Flash | 984,576 reservation | 32,768 estimate | Provisional; its own route answered on 2026-11-05; sizes not stress-tested |

Normal Claude streaming caps are used. The newer Claude 300K output beta is for
Batch API and does not apply to this relay. The old Sonnet 4/4.5 1M beta was
retired; those IDs retain 200K. For Opus 4/4.1 and Sonnet 4 the individual overview
pages were unavailable during the audit. Their conservative legacy output
values remain explicitly provisional rather than inheriting 128K.

### Input limit and Claude clients

Retool reserves part of the window for output, so the input a route accepts is smaller than the
window. `/v1/models` publishes it as `max_input_tokens` only for models whose reservation is
known, and never claims 1M for them:

| Models | Retool input reservation | `max_input_tokens` | `supports_1m` |
| --- | ---: | ---: | --- |
| Opus 4.6, 4.7, 4.8, 5, 5.5, Fable 5 | 872,000 | 872,000 | not sent |
| Sonnet 4.6 | 936,000 | 936,000 | not sent |
| Every other model | not verified per model | not sent | not sent |

The public window stays in `context_window`. Claude Desktop reads `supports_1m`, or a
`max_input_tokens` of 1,000,000 or more, as "1M confirmed for this deployment" and adds a
separate "1M context window" picker entry; its documentation asks to set it only after
confirming the window against the deployment, and a window the route does not accept fails at the
provider ([gateway guide](https://claude.com/docs/third-party/claude-desktop/gateway)).
The 191,808 reservation of the 200K models is not assigned to individual models here, so no
`max_input_tokens` is sent for them either.

Claude Code needs a separate setting. Behind `ANTHROPIC_BASE_URL` it gives each model it
recognizes the window the model has on the Anthropic API: Opus 4.7 and later, Sonnet 5 and
later and the Fable models get 1M natively, while Opus 4.6 and Sonnet 4.6 run at 200K without
the `[1m]` suffix. It cannot detect a lower limit that the server behind the gateway enforces
([model configuration](https://code.claude.com/docs/en/model-config#context-window-behind-an-llm-gateway)).
Against this relay, whose Opus 4.7/4.8/5/5.5 and Fable routes take 872,000 input tokens, set
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` below the smallest limit you use, for example
`CLAUDE_CODE_AUTO_COMPACT_WINDOW=800000` (the variable takes 100000 to 1000000, `200k`/`1M`
suffixes need Claude Code 2.1.304, and one value applies to every model of the session; Claude
Code caps it at the model's own window; [environment variables](https://code.claude.com/docs/en/env-vars)).
Without it Claude Code compacts native-1M models at about 967K tokens by default, above the 872,000 the route
reserves for input. The status line's `used_percentage` still measures against the full window while the
variable is set.

## Effort controls and recommended defaults

The selectable list describes the Retool route. A larger public-provider list
does not authorize sending levels that this route rejects. A test compares this
table with the registry, so the two cannot drift apart again.

| Model group | Published selectable levels | Relay recommendation |
| --- | --- | --- |
| GPT-5 | minimal, low, medium, high | medium |
| GPT-5 mini/nano | low, medium, high | low |
| GPT-5.1/5.2 | none, low, medium, high | medium |
| GPT-5.3 Codex/5.4/5.5/5.6 variants | low, medium, high | medium |
| GPT-6 Astra | low, medium, high, max | high |
| GPT-6.1 Sol | low, medium, high, max | medium |
| GPT-6 Sol/Luna, GPT-6.1 Terra | none, low, medium, high, max | medium |
| GPT-4.1 | — (not a reasoning model) | — |
| Claude except Fable/Haiku | low, medium, high, max | medium |
| Claude Fable 5 | low, medium, high, max | high |
| Claude Haiku 4.5 | — (accepted, ignored) | — |
| Gemini Flash and Flash-Lite (route never probed) | low, medium, high | low |
| Gemini Pro (route never probed) | low, medium, high | medium |
| GLM-5.3 and GLM-5.3 Flash | — (accepted, ignored) | — |

GPT-6 rejects xhigh on the live route (HTTP 422, 2026-11-03), so no GPT-6 row
publishes it. "Accepted, ignored" is `reasoning_effort_control: "accepted-ignored"`:
Retool takes a `reasoningEffort` for these models and changes nothing observable
(2026-11-05 probe, see the route audit), so the relay publishes no levels,
forwards none, answers every known level with 200 instead of a 400 that would
break clients copying one effort to every model, and names the control in
`x-proxi-ignored-parameters`.

`default_reasoning_effort` is a relay recommendation balancing latency and work
quality, not a claim that one level wins every benchmark or matches every
vendor default. GPT-6 Astra/Fable get high; most models get medium; small/fast
models (GPT-5 mini/nano and the Gemini Flash variants) get low. GLM Flash is on
the small/fast list too, but with no published level there is no default to show.
Explicit client effort takes precedence over relay `auto`. Codex model discovery
uses the same recommendation. Harness has a global medium default and retains every
per-model selector; its plugin does not support auto.

Public GPT-5.6 supports additional levels and public Opus 5.5 includes xhigh;
the fresh Retool registry currently restricts both as shown above. Older Claude
may implement route effort through a thinking budget rather than the direct
provider's modern effort parameter. That distinction is not exposed as fake
extra levels.

Follow-up 2026-11-01 closed the last gap: no "extra" level exists anywhere.
The Retool bundle defines the full universe of levels (OpenAI minimal/none/
low/medium/high/xhigh/max, Anthropic low/medium/high/max, Google low/medium/
high) with no such literal, desktop clients cannot send it, and the live route
answers 422 "malformed" to `reasoningEffort:"extra"` on Claude models while
accepting max. The relay therefore rejects values no provider defines (400
with the allowed list) instead of silently clamping them to medium; known
levels unsupported by a given model still clamp to the nearest one, except on
the accepted-ignored routes, which take every known level.

GLM-5.3 always reasons and accepts images through the Retool route (two
randomized quadrant-color tests per route, 2026-11-04). Its public API supports
low/high/max and Retool accepts low/medium/high/max, but a 2026-11-05 probe found
no consistent change in thinking volume, output tokens or latency between levels,
so the relay publishes no selectors for it. GLM-5.3 Flash is a separate model whose
route was measured on its own, not inferred from GLM-5.3. Haiku 4.5 never returned
thinking at any level; Anthropic lists "Default effort: Not supported" for it and
extended thinking there needs a manual `budget_tokens` that this route does not
expose, so it is listed as not reasoning and Claude Code's budget is accepted and
ignored. Provider-supplied thinking or reasoning summaries can
be forwarded. Hidden raw internal reasoning cannot be reconstructed by a relay.

## Sources and repeatability

Fresh authenticated Retool HTML identified these public CDN assets. They were
downloaded with a browser User-Agent and Referer only, without sending cookies
or XSRF credentials to the CDN. Registry objects were parsed with Acorn AST;
downloaded JavaScript was not executed.

- [Retool main registry](https://retool-edge.com/assets_vjs/main-CnZ2mbHm.js)
- [Retool pages bundle](https://retool-edge.com/assets_vjs/pages-DuOP6Xwj.js)
- [OpenAI GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [OpenAI GPT-6 Astra guide](https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra)
- [OpenAI GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
- [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
- [Claude Opus 5.5](https://platform.claude.com/docs/en/models/opus-5-5/overview),
  [Opus 5](https://platform.claude.com/docs/en/models/opus-5/overview),
  [Opus 4.8](https://platform.claude.com/docs/en/models/opus-4-8/overview),
  [Opus 4.7](https://platform.claude.com/docs/en/models/opus-4-7/overview),
  [Opus 4.6](https://platform.claude.com/docs/en/models/opus-4-6/overview),
  [Opus 4.5](https://platform.claude.com/docs/en/models/opus-4-5/overview)
- [Claude Fable 5](https://platform.claude.com/docs/en/models/fable-5/overview),
  [Sonnet 5](https://platform.claude.com/docs/en/models/sonnet-5/overview),
  [Sonnet 4.6](https://platform.claude.com/docs/en/models/sonnet-4-6/overview),
  [Sonnet 4.5](https://platform.claude.com/docs/en/models/sonnet-4-5/overview),
  [Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview)
- [Claude thinking](https://platform.claude.com/docs/en/build-with-claude/thinking),
  [release notes](https://platform.claude.com/docs/en/release-notes/overview),
  [legacy Bedrock context limits](https://platform.claude.com/docs/en/build-with-claude/claude-on-amazon-bedrock-legacy)
- [Gemini 3.5 Flash](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5)
- [GLM-5.3](https://docs.z.ai/guides/llm/glm-5.3)
- [Claude model deprecations](https://platform.claude.com/docs/en/about-claude/model-deprecations)
  (retirement dates of Opus 4, Opus 4.1, Sonnet 4 and Sonnet 4.5, checked 2026-11-05),
  [Retool AI models](https://docs.retool.com/data-sources/reference/ai-models) (managed and BYOK lists)
- [Claude Desktop gateway guide](https://claude.com/docs/third-party/claude-desktop/gateway),
  [model configuration](https://code.claude.com/docs/en/model-config),
  [environment variables](https://code.claude.com/docs/en/env-vars)
- [openai/codex 86b1123](https://github.com/openai/codex/commit/86b1123ff6b5d089a146be4e603a324cf454223a)
  (2026-08-14) removed `supports_parallel_tool_calls` from the model catalog. Every earlier release checked
  (tags 0.80.0, 0.100.0, 0.120.0, 0.130.0, 0.148.0-alpha.9) declares it as a required `bool` without a serde
  default, so a catalog that omits it does not parse in those clients
  ([openai/codex#38934](https://github.com/openai/codex/issues/38934) shows the `missing field` error). Built-in
  fallback metadata for a slug sets the field to `false` and those clients then send `parallel_tool_calls: false`,
  which the relay rejects when tools are present; the fallback path itself was not exercised here. `/codex/models`
  therefore keeps sending `true`; `ModelInfo` has no `deny_unknown_fields`, so newer clients ignore it

Sanitized extracted Retool facts are retained locally under
`logs/model-audit/retool-model-metadata.json`. They contain no session
credentials. Tests pin the corrected limits, narrower route levels,
recommendations, provenance status and Codex defaults; `tests/test_catalog_docs.py` checks that
the effort table above and the route table of the 2026-11-05 audit match the registry.
