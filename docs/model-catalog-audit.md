# Model catalog audit — 2026-10-29

The catalog retains all 37 Retool model IDs: 15 OpenAI, 14 Anthropic, 6 Google and
2 Retool-fast. A provider deprecation does not remove a still-advertised Retool
ID. Resource availability is evaluated separately from model capabilities.

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
| GPT-5, mini, nano, 5.1, 5.2, 5.3 Codex | 400,000 | 128,000 | Fresh Retool registry; mini/nano effort lists inherited |
| GPT-5.4, 5.5, all 5.6 variants, 6 Astra/Sol/Luna, 6.1 Sol | 1,050,000 | 128,000 | Fresh Retool registry |
| Claude Opus 5.5/5/4.8/4.7/4.6, Fable 5, Sonnet 5/4.6 | 1,000,000 | 128,000 | Official model pages |
| Claude Opus 4.5, Sonnet 4.5, Haiku 4.5 | 200,000 | 64,000 | Official model pages |
| Claude Opus 4/4.1 | 200,000 | 32,000 | Provisional legacy output; route not verified |
| Claude Sonnet 4 | 200,000 | 64,000 | Provisional legacy output; route not verified |
| Gemini 3.5 Flash | 1,000,000 input | 65,000 | Public 1M/65K conservative values; route not configured |
| Other five Gemini entries | 983,040 reservation | 65,536 estimate | Provisional; full model limits not individually audited |
| GLM-5.3 | 1,000,000 | 128,000 | Public limits; route not configured |
| GLM-5.3 Flash | 984,576 reservation | 32,768 estimate | Provisional; independently unaudited |

Normal Claude streaming caps are used. The newer Claude 300K output beta is for
Batch API and does not apply to this relay. The old Sonnet 4/4.5 1M beta was
retired; those IDs retain 200K. For Opus 4/4.1 and Sonnet 4 the individual overview
pages were unavailable during the audit. Their conservative legacy output
values remain explicitly provisional rather than inheriting 128K.

## Effort controls and recommended defaults

The selectable list describes the Retool route. A larger public-provider list
does not authorize sending levels that this route rejects.

| Model group | Retool selectable levels | Relay recommendation |
| --- | --- | --- |
| GPT-5 | minimal, low, medium, high | medium |
| GPT-5 mini/nano | low, medium, high (family fallback) | low |
| GPT-5.1/5.2 | none, low, medium, high | medium |
| GPT-5.3 Codex/5.4/5.5/5.6 variants | low, medium, high | medium |
| GPT-6 Astra | low, medium, high, xhigh, max | high |
| GPT-6.1 Sol | low, medium, high, xhigh, max | medium |
| GPT-6 Sol/Luna | none, low, medium, high, xhigh, max | medium |
| Claude except Fable/Haiku | low, medium, high, max | medium |
| Claude Fable 5 | low, medium, high, max | high |
| Claude Haiku 4.5 | low, medium, high, max | low |
| Google route (unconfigured) | low, medium, high | low for Flash; medium for Pro |
| Retool-fast route (unconfigured) | unverified; provider-managed | provider-managed |

`default_reasoning_effort` is a relay recommendation balancing latency and work
quality, not a claim that one level wins every benchmark or matches every
vendor default. GPT-6 Astra/Fable get high; most models get medium; small/fast
models get low. Explicit client effort takes precedence over relay `auto`.
Codex model discovery uses the same recommendation. Harness has a global medium
default and retains every per-model selector; its plugin does not support auto.

Public GPT-5.6 supports additional levels and public Opus 5.5 includes xhigh;
the fresh Retool registry currently restricts both as shown above. Older Claude
may implement route effort through a thinking budget rather than the direct
provider's modern effort parameter. That distinction is not exposed as fake
extra levels.

GLM-5.3 is text-only and always reasons. Its public API supports low/high/max,
but Retool-fast forwarding was not verified, so the relay does not advertise
those selectors. GLM-5.3 Flash is a separate, unverified model; no capabilities
are inferred from GLM-5.3. Provider-supplied thinking or reasoning summaries can
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

Sanitized extracted Retool facts are retained locally under
`logs/model-audit/retool-model-metadata.json`. They contain no session
credentials. Tests pin the corrected limits, narrower route levels,
recommendations, provenance status and Codex defaults.
