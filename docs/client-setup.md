# Coding clients

Current route availability and installed-client evidence are recorded in
[the model/client audit](model-client-audit-2026-11-04.md). A previous client
setup or historical benchmark below does not establish that today's installed
profile contains the same provider or that an old model/effort is still accepted.

## OpenCode

OpenCode Desktop and CLI configure custom providers via `~/.config/opencode/opencode.jsonc` (or project-level `opencode.json`).

The relay runs at `http://127.0.0.1:8787/v1` under `@ai-sdk/openai-compatible`.

Example provider configuration with custom reasoning effort variants:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "retool-relay": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Retool relay",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "relay-..."
      },
      "models": {
        "claude-opus-5-5": {
          "name": "Claude Opus 5.5",
          "limit": {
            "context": 1000000,
            "output": 128000
          },
          "tool_call": true,
          "attachment": true,
          "reasoning": true,
          "variants": {
            "low": { "name": "Low", "reasoningEffort": "low", "settings": { "reasoningEffort": "low" } },
            "medium": { "name": "Medium", "reasoningEffort": "medium", "settings": { "reasoningEffort": "medium" } },
            "high": { "name": "High", "reasoningEffort": "high", "settings": { "reasoningEffort": "high" } },
            "max": { "name": "Max", "reasoningEffort": "max", "settings": { "reasoningEffort": "max" } }
          }
        },
        "gpt-6.1-sol": {
          "name": "GPT 6.1 Sol",
          "limit": {
            "context": 1050000,
            "output": 128000
          },
          "tool_call": true,
          "attachment": true,
          "reasoning": true,
          "variants": {
            "low": { "name": "Low", "reasoningEffort": "low", "settings": { "reasoningEffort": "low" } },
            "medium": { "name": "Medium", "reasoningEffort": "medium", "settings": { "reasoningEffort": "medium" } },
            "high": { "name": "High", "reasoningEffort": "high", "settings": { "reasoningEffort": "high" } },
            "max": { "name": "Max", "reasoningEffort": "max", "settings": { "reasoningEffort": "max" } }
          }
        }
      }
    }
  },
  "model": "retool-relay/claude-opus-5-5"
}
```

> [!NOTE]
> OpenCode requires the `variants` field to be a dictionary/object mapping variant IDs to their configuration (`{ "id": { ... } }`). Passing an array (`[ ... ]`) causes schema validation failure and skips loading the provider.

For an existing relay provider, `scripts/sync_opencode_catalog.py` builds the
canonical catalog from the enabled account resources. Preview with `--output`
first; writing requires `--write --backup-dir`. The current configured pool
publishes 27 verified routes. Its effort lists, including GPT-6's lack of xhigh,
come from the dated route checks rather than the native provider's larger list.

The installed CLI syntaxes differ: OpenCode 1.19.3 uses `--model
retool-relay/zai-org/GLM-5.3 --variant high`; OpenCode 2.2.5 uses `--model
retool-relay/zai-org/GLM-5.3#high`. Version 2 rejects `--variant` before sending a
model request. Both exact clients passed a real GLM high request through an
isolated current-source relay; their timings and evidence are in the audit.

## DeepSeek Harness

The current installed CLI (0.2.2-alpha.3) and Desktop (0.2.1-rc.5) use separate
plugin patch lists at `~/.dsh/profiles/web/cordis.patch.yml` and
`~/.dsh/profiles/desktop/cordis.patch.yml`. The `llm-pi-ai` node stores providers
under `config.providers`. The existing Proxi route uses OpenAI Chat Completions.

Both profiles have been reconciled to the same 27 verified IDs. The main
`retool` group holds 26 reasoning models, preserving its medium default and
every supported selector; `retool-text` holds GPT-4.1 without a reasoning
selector. Both use the same existing connection and credential reference. This
split avoids an installed SDK limitation: a provider-wide medium default is
rejected for a non-reasoning model before HTTP, while simply deleting that
default can silently disable thinking on a model with an off selector.

To preview or update an existing profile, use `scripts/sync_harness_catalog.py
--settings PATH`. Preview writes only with `--output`; replacing the profile
requires `--write --backup-dir`. The utility splices only the Proxi blocks and
keeps other bytes intact, including date headers and plugin source. The active
provider/model and credentials are preserved. Both installed SDK versions
passed default, off, high and rejection checks with the exact generated files;
the SDK bundles and Desktop ASAR were not modified.

### Historical Harness setup — 0.1.5-rc.2

The earlier installed `DeepSeek Harness.lnk` setup launched the
`@deepseek-ai/dsh` Web UI. Its multi-provider adapter was `llm-pi-ai`; use an
OpenAI Chat Completions route for this relay. The same relay key also works
with the OpenAI Responses and Anthropic Messages endpoints.

That local setup added `llm-pi-ai.providers.retool` to
`%USERPROFILE%\.dsh\settings.yaml` and the credential reference
`RETOOL_RELAY_API_KEY` to `%USERPROFILE%\.dsh\.credentials.yaml`.
These files are outside this repository. Existing providers, comments, and the
selected provider are preserved. Both documents are watched, so the running
Harness could pick up the added route without restarting existing chats. These
paths and the abbreviated mapping below describe that earlier configuration;
use the profile patch paths above for the current installed versions.

This abbreviated configuration shows two models. Generate the complete list
and valid efforts from the current `/v1/models` response; this example is not
a claim about today's installed Harness profile or catalog size:

```yaml
llm-pi-ai:
  providers:
    retool:
      displayName: Retool Relay
      api: openai-completions
      baseURL: http://127.0.0.1:8787/v1
      apiKeyEnv: RETOOL_RELAY_API_KEY
      reasoning: medium
      compat:
        thinkingFormat: deepseek
        supportsReasoningEffort: true
        supportsUsageInStreaming: true
        sendSessionAffinityHeaders: true
      retryPolicy:
        mode: normal
        maxRetries: 1
      models:
        - id: gpt-6-sol
          name: GPT 6 Sol
          contextWindow: 1050000
          maxTokens: 128000
          input: [text]
          reasoningEfforts:
            off: none
            low: low
            medium: medium
            high: high
            max: max
        - id: claude-opus-5-5
          name: Claude Opus 5.5
          contextWindow: 1000000
          maxTokens: 128000
          input: [text]
          reasoningEfforts:
            low: low
            medium: medium
            high: high
            max: max
```

`apiKeyEnv` names a Harness credential reference: the key can be stored in the
local credential document and does not have to be inherited by the already
running process. It is never placed in the provider settings or committed.
Advertise only levels accepted by the exact relay model; `/v1/models` exposes
them in `capabilities.reasoning_effort_levels`. The `off` option is offered
only where that list contains `none`. Installed pi-ai then sends
`thinking: {type: disabled}`, which the relay maps to the actual `none` level.

`thinkingFormat: deepseek` makes the Harness adapter consume the relay's
`reasoning_content` as a separate reasoning stream. It displays summaries
provided by Retool, when present; the relay does not invent reasoning text.
Project access and `get_goal`, `create_goal`, and `update_goal` are ordinary
client-owned tools. Their schemas, arguments, call IDs, and complete results
pass through the relay, while Harness executes the tools and persists its goal
state. A tool declaration alone does not create a goal.

Validation used the installed Harness 0.1.5-rc.2 adapter against local mock SSE:
the same relay key authenticated, a reasoning delta became a Harness reasoning
block, and `get_goal` became a normal tool-call block. These mock summaries
verify transport behavior; they are not evidence that the upstream always emits
thinking. Multimodal image inputs are supported and forwarded for vision-capable models (e.g. Claude Opus 5.5, Sonnet 5, GPT-5.5) on the direct transport.

The installed adapter also passed a real Retool request: Claude Opus 5.5 returned
`get_goal`, arguments `{}`, finish `tool-calls`, in 26.37 seconds. This verified
the tool transport without changing the user's real goal. That low-effort
upstream response supplied no visible thinking text.

### Keep the upstream conversation warm

The direct transport uses the client's stable dialogue identity to reuse its
native history and cache context. It accepts `x-session-affinity`, `x-session-id`, `x-opencode-session`,
or explicit request fields `conversation`, `conversation_id`, and `session_id`.
Parent agents and subagents must use different identities. A single static
provider-wide header would mix their conversations and must not be used.

Some in-process Claude Code subagents may share the runtime session ID:
installed 2.1.310 does not include its internal agent ID in the normal metadata
identity. Recognizing that metadata fixes the main conversation; it does not
prove separate warm channels for every in-process child. Changed client
histories still rotate their upstream context, and distinct supplied session
IDs are isolated. Direct inference is the current transport; that does not
eliminate the stable identity needed for signed tool-history continuation.

Claude Code 2.1.310 supplies its current `session_id` inside the JSON string
`metadata.user_id`. The relay recognizes that explicit session identity too;
device/account IDs, opaque user IDs and `parent_session_id` are not substituted
for the current dialogue ID. Explicit headers/top-level IDs take precedence.

Requests declaring native tools and tool-less requests have separate upstream
scopes. Tool-less scopes also include the system-instruction fingerprint. This
prevents a title-generation or side-query request with the same client ID from
entering the main agent's thread, waiting on its conversation lock, or
invalidating its cached history. Shared mode uses an isolated lease for such
explicitly identified tool-less requests.

Harness supplies its unique session ID to pi-ai, but stock 0.1.5-rc.2 withholds
the `sendSessionAffinityHeaders` compatibility setting. This setup includes a
host-adapter fix exposing that existing SDK boolean, enabled only on the Retool
route above. A YAML change alone does not enable it in the unpatched adapter.
Keep the SDK cache retention at its default or `short`: `none` also suppresses
session affinity. Restart the Harness host after updating its adapter bundle;
settings-file watching alone cannot replace already loaded JavaScript.

The direct route replays the accepted raw assistant blocks, including opaque
thinking signatures, when client tool results strictly continue the same
history. It keeps stable cache markers and a provider cache key. Changed
instructions, tools, model or resource invalidate the old signed prefix;
unchanged output-only verbosity does not. Unknown requests keep independent
context. Cache lifetime and a hit still require upstream telemetry.

### Historical agent/WebSocket measurements — 2026-10-29

The measurements below describe the former agent/WebSocket transport and an
earlier installed Harness adapter. They do not describe the current direct
request path or establish its present latency.

A bounded live Opus 5.5 `max` comparison on 2026-10-29 used two short plain
prompts on the same thread/socket. Cold setup took 0.589s to create the thread,
4.799s to start and 1.791s to connect; the first text arrived 9.100s after send.
The warm request needed no start/connect and produced its first text after
2.996s, finishing in 5.096s. Raw-to-normalized forwarding took 0.117ms/0.084ms.
An independent native-web short follow-up became visible after 2.823s including
browser automation overhead. These are short-prompt observations, not a
one-second guarantee for IDE requests with a large project context.

The final installed-Harness-adapter check used two real native-tool-enabled
requests through an isolated relay at unchanged Opus 5.5 `max`. First text took
19.329s cold and 1.406s warm; completion took 20.148s/2.182s. There was exactly
one thread creation, start and connection for both requests. The transmitted
prompt shrank from 1790 to 392 characters. This test declared a read-only tool
but explicitly requested plain answers; it did not execute project tools.

Validation on the prospective relay commit, excluding the separately paused
admin work: `python -m pytest -o addopts= -q` — 534 passed in 49.77s.
The Harness adapter change separately passed 167 scoped source tests,
typecheck/build and seven localhost-only installed-bundle wire checks.

Those small-request measurements did not exercise the full Harness title-job
lifecycle. A real project run subsequently showed 35.836s/27.952s total and
32.460s/25.466s to first text. Its identical 73064-character system prompt was
not the cache-breaking change: a concurrent tool-less title request entered the
same thread and invalidated the native cache. Separate scopes fix that case.

The saved full request sequence was replayed through the installed Harness
adapter with the title running concurrently, using an isolated relay and the
same Opus 5.5 `max` setting. First text took 28.519s cold and 4.906s on the next
native request; completion took 30.450s/7.107s. Exactly two thread/start/connect
operations served three requests (one main scope and one title scope); native
prompts were 80960 then 902 characters. The replay retained the saved assistant
message and executed no project tools. Relay-local execution/project injection
were disabled for the diagnostic; client project instructions were preserved.
These are captured-request replay observations, not a live GUI latency promise.

Direct model API findings and access limitations are recorded in
[direct-inference-audit.md](direct-inference-audit.md). This follow-up does not
silently switch the client's model, credential or inference context.

## Codex

Use the same relay key in `RELAY_API_KEY` and a Responses provider with a
Codex-native model catalog:

```toml
model = "gpt-6-sol"
model_provider = "retool"
model_reasoning_summary = "detailed"
web_search = "disabled"

[model_providers.retool]
name = "Retool relay"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "RELAY_API_KEY"
supports_websockets = false
model_catalog_url = "http://127.0.0.1:8787/codex/models"
```

The native catalog supplies each model's recommended effort and valid selector
levels, plus `exec_command`, freeform `apply_patch`, and namespace tool support.
An explicit `model_reasoning_effort` remains authoritative when set. The built-in
server-side Responses web search is not implemented; client function tools for
search use the ordinary tool bridge.

`codex exec --output-schema FILE` sends the schema as Responses `text.format`
(`json_schema`, `strict: true`). Retool cannot constrain decoding, so the relay
emulates it: the schema goes into the instructions, tool turns pass through
unchanged, and only the final reply without tool calls is checked. One outer
Markdown fence is removed, the JSON is parsed and validated against the common
subset of the schema (`type`, `properties`, `required`, `additionalProperties`,
`items`, `enum`, `anyOf`, nullable, local `$ref`; unknown keywords never reject).
Nothing of that reply reaches Codex before the check passes. An invalid reply ends
the stream with an SSE `error` event (non-stream: `502 upstream_invalid_output`
with `x-should-retry: false`) and its credits stay accounted. Codex then decides how
often to ask again within its own `stream_max_retries`; every retry is a paid
request. `x-proxi-output-format: validated-json-schema` marks the emulation, and a
format combined with a required tool call is rejected with 422. This is
post-validation, not constrained decoding: a live check on 2026-11-05 (`gpt-6-sol`,
`gpt-5.5`, a `project_name`/`programming_languages` schema) returned valid bare JSON,
which does not guarantee every model or schema.

Codex also sends `prompt_cache_options` and `client_metadata` with each request. The
relay does not forward them; it names both, without values, in
`x-proxi-ignored-parameters` and the `request.compatibility` journal event.

Codex 0.164.0 passed real source reading, patching and testing (two tests), and a
separate real subagent spawn/wait/result round trip. See `pr-1-review.md` for
commands, versions and limitations.

## Claude Code

Set `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` and
`ANTHROPIC_API_KEY` to the same relay key. `ANTHROPIC_AUTH_TOKEN` is supported by
normal Claude Code; bare mode uses `ANTHROPIC_API_KEY`.

Select an advertised model, for example `claude-opus-5-5`, and a supported effort.
Claude Code 2.1.310 passed real Read/Edit/Bash plus three fixture tests. Thinking
blocks are collapsed by default: `Ctrl+O` expands them;
`showThinkingSummaries: true` enables summaries. This cannot reveal text that
Retool has redacted or omitted.

### Structured outputs, titles and side requests

Claude Code sends `output_config.format` (`json_schema`) for session titles, memory
recall and prompt hooks. Retool cannot constrain decoding, so the relay emulates it:
the schema goes into the system instructions, the whole reply is buffered, one outer
Markdown fence is removed, and the JSON is parsed and validated against the schema's
common subset (`type`, `properties`, `required`, `additionalProperties`, `items`,
`enum`, `anyOf`, nullable, local `$ref`; unknown keywords never reject). Only a
valid reply is sent; an invalid one is a `502` with type `api_error` (a stream ends with
an SSE `error` event, with no content), and its credits stay accounted. The 502 carries
`x-should-retry: false` so that Claude Code does not repeat the paid request; a stream
cannot carry that header, and Claude Code's own retries of server errors can repeat it.
A live check on 2026-11-05 (Haiku 4.5, Sonnet 5.5) returned valid JSON for the title,
memory recall and hook schemas; Haiku wrapped both replies in a ` ```json ` fence,
which is removed.

If a model keeps failing the check, set `CLAUDE_CODE_DISABLE_STRUCTURED_OUTPUTS=1`
(Claude Code 2.1.288 or newer). Claude Code then stops sending `output_config.format`
and its beta header, and titles, memory recall and hooks rely on the prompt alone.
`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` also removes the format, together with
other pre-release fields. A format combined with a required tool call cannot be
emulated and gets a 422 that names this variable. `x-proxi-output-format` shows the
emulation (`validated-json-schema`, `validated-session-title`).

A title request is recognised by its schema, exactly `{title: string}` without tools,
whatever the prompt says (Messages only: the same schema from an OpenAI-style client,
for example ai-sdk `generateObject`, is an ordinary request). Without a schema (the
variable above) the start of the prompt block decides, after the
`x-anthropic-billing-header` and identity lines. Both wordings are known: "You are
naming a coding session…" (Claude Code 2.1.234 and newer, the Agent SDK) and "Generate
a concise, sentence-case title…" (older). A system prompt that only quotes the phrase
further down stays an ordinary request and gets the configured relay policy.
`output_config.task_budget` is accepted, not enforced, and named in
`x-proxi-ignored-parameters`. A rejected request now carries the Messages error type
`invalid_request_error` instead of `unprocessable_entity`.

#### Request classes

Behind a custom base URL Claude Code omits gateway hint headers unless
`CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` is set (Claude Code 2.1.273 or newer). It then
sends `x-claude-code-request-class` with `main`, `subagent`, `workflow`, `compaction`
or `auxiliary` (titles, classifiers, summaries). For `auxiliary` and `compaction` the
relay does not apply `relay_system_prompt_mode` `append` or `replace`: those requests
keep their own system prompt and output format. Sessions, isolation and cache scopes
stay as they are; only the policy changes. Other classes, and requests without the
header, still get the configured policy. The class appears in the journal entries of
the request. Without the header only the recognised service kinds (titles, Auto mode
check, connection probe) are protected.

## Claude Desktop on Windows

The installed 4.4212.0.0 Code-mode client reads its gateway policy from
`HKCU\Software\Policies\Claude`. Its native discovery filters the relay's
27 routes to 11 currently available Claude models. It does not accept GPT or
GLM merely because those models speak the same API; do not invent Anthropic
family tiers for them.

A nonempty `inferenceModels` policy is the authoritative list. Enabling
discovery alone enriches those rows and does not add missing models. The
maintainer `scripts/sync_claude_desktop_catalog.py` previews the compatible
Claude subset and updates only that model-list value with `--write
--backup-dir PATH`. The endpoint, key, authentication scheme, high default and
all other policies are preserved. The installed policy now contains all 11
Claude routes instead of only Opus/Sonnet 5.5.

Native picker levels vary by model: a max cap on older five-level rows would
offer the route-rejected xhigh. Those rows are capped at high; Opus 4.6 and
Sonnet 4.6 keep their valid four-level max. Unknown 5.5 rows have no built-in
effort selector in this installed version; setting their policy does not claim
the GUI gained one. Their gateway default remains high.

The installed ASAR's actual discovery/filter/merge/picker functions and bundled
Agent SDK were executed without Electron or UI input. Exact SDK wrapper
0.3.309 also returned a real answer through the current production relay using
an explicitly selected cached Desktop runner 2.1.295. The embedded target is
2.1.309; that target binary was not present, and this check does not claim the
cached version is the target. See the dated audit for hashes, commands and
scope. Opening and clicking the window is not a prerequisite for these code
and protocol checks, and no GUI interaction pass is claimed.

The gateway separates the SDK's leading `x-anthropic-billing-header` attribution
line from its actual system instructions. Anthropic consumes that attribution
block; merging instructions into it discards them upstream. This deterministic
split also applies to native tool continuations and keeps cache hints on the
real system prefix.

Haiku 4.5 accepts verified `thinking: {type: "disabled"}`. The SDK's beta
`thinking.display: "updates"` uses the route's available summaries, with
`x-proxi-thinking-display: summarized` and a named compatibility warning; the
beta's native update cadence is not implemented. Its exact session-title schema
has a bounded, buffered post-validation path. A sole JSON Markdown fence may be
removed; the object must still contain exactly one string `title`, no duplicate
keys and a complete normal finish. Invalid output becomes an error before any
title content is emitted. `x-proxi-output-format: validated-session-title`
marks this case; other schemas use the same emulation described under Claude Code.
This is validation/normalization, not native constrained decoding.

## IDE commands and subagents

Commands such as `/goal` belong to the IDE. It turns them into its own workflow
or supplied `get_goal`/`create_goal`/`update_goal` calls. The relay preserves
those schemas, IDs, complete arguments and results, including namespace tools;
it does not create a second, incompatible goal engine. Subagent lifecycle,
permissions and persistence are likewise owned by the IDE. The relay carries
independent requests and isolates their upstream sessions.

Claude Code's in-process subagents may share the parent's runtime session ID:
the installed CLI does not include its internal agent ID in ordinary metadata.
Distinct supplied session IDs are isolated; independent warm conversations for
every in-process Claude child have not been verified.
