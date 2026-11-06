# Coding clients

Current route availability and installed-client evidence are recorded in
[the model/client audit](model-client-audit-2026-11-04.md). A previous client
setup or historical benchmark below does not establish that today's installed
profile contains the same provider or that an old model/effort is still accepted.

To change what the model sees per client, model or request, use `prompts.json`
([guide](prompt-control.md)). A client that can add request headers picks a profile
with `x-proxi-profile`: the settings for Claude Code, Codex, Claude Desktop and
OpenCode are in [recipe 6](prompt-control.md#рецепт-6-профиль-по-заголовку-x-proxi-profile);
a client that cannot add headers picks one by model name (`<model>@<profile>`,
[recipe 7](prompt-control.md#рецепт-7-виртуальные-модели)).

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
Discovery reads the whole relay config as the relay does, so root options such as
`show_unrouted_models` apply.

The script reads JSONC (comments and trailing commas, as OpenCode does). When it
writes, it keeps the file's BOM, line endings, indentation and final-newline
state. Rewriting removes comments, so a file that has them is refused until you
delete them or pass `--allow-reformat`; the backup keeps the original. Nothing is
written and no backup is made when the catalog is already current: the summary
shows `written: false`. The `--output` preview masks credentials as `***`: the
values of `apiKey`, header and environment keys and other token/secret/password
fields, a string under `key`, `auth`, `pass` or `pwd`, the user info and secret
parameters (`token`, `key`, `signature` and the like) of any URL, the argument after
a secret flag in a command (`--api-key X`, `--token=X`), every `NAME=value` item of
an `env` list and secret `NAME=value` text. `--include-secrets` writes it verbatim.
The rules go by name and shape, so they can miss a credential in an unexpected place:
read a preview before you share it.

A reference to a removed relay model is replaced instead of left dangling, in
`model`, `small_model`, `agent.*.model`, `command.*.model` and the deprecated
`mode.*.model`. `small_model` and the hidden `title` and `summary` agents get the
first published model of the small tier (Haiku, Flash, nano, mini, GLM Flash), or
lose the override when none is published so OpenCode chooses its own. Everything
else gets the default model (Opus 5.5 when published). A valid reference keeps a
`#variant` suffix only while that model still publishes it.

`agent.compaction.model` is treated differently, because compaction summarizes the
whole session and a model whose window is smaller than the session stalls without an
error (OpenCode issue #46164). A removed compaction model is replaced by the first
model of the small tier whose window is at least the main `model`'s. For the default
main model, Opus 5.5 with 1,000,000 tokens, Haiku (200K), nano, mini and GLM Flash
all fall short and only Gemini 3.5 Flash qualifies, which is published only when a
Google resource is configured. So the usual outcome is that the override is removed
and OpenCode compacts with the session's model; the same happens when the main model
is not a relay model and its window is unknown. Either way the summary's `warnings`
name the old reference and what was done. A compaction model you chose that is still
published is kept as it is, even if its window is smaller: check that yourself.

### Compaction and the effort of its agent

OpenCode 2.x (every tag from v2.0.0 to v2.2.5) compacts by resending the chat's own
request: the same system prompt, tools and history, and one more user message that asks for
the summary. That message opens with "You MUST summarize the conversation above…" (a later
compaction: "Update the existing checkpoint in the conversation above…"), and a reply that
skipped the template gets one more message that opens "The previous response did not fill in
the required summary template" (`buildPrompt` and `NUDGE` in
[`packages/core/src/session/compaction.ts`](https://github.com/anomalyco/opencode/blob/v2.2.5/packages/core/src/session/compaction.ts)).
OpenCode 1.x runs the summary on its hidden `compaction` agent instead, without tools and with
that agent's own system prompt, so it shares no cache with the chat. Its user message opens, by
tag (a sweep of every `v1.*` tag of the repository): "Provide a detailed prompt for continuing
our conversation above" up to v1.14.18, "When constructing the summary, try to stick to this
template" in v1.14.19 and v1.14.20, "Create a new anchored summary…" or "Update the anchored
summary below…" from v1.14.21 to v1.18.16, and "Here is the conversation so far:" from v1.18.17
to v1.19.5 (older tags word it differently and are not recognised by their text).

When the conversation is larger than the room OpenCode 2.x keeps for the summary
(`compaction.buffer`), it does not send the chat's own request: it cuts the kept tail off the
history, or sends the history as one text message without tools (`flattenAndDropOldest`). Such a
request shares no signed branch with the chat, so it cannot resume whatever the effort; the label
still shows why it was dear. The experimental `background_compaction` is the same: it asks on the
history up to the latest reply, without that reply, and a signed branch continues only a request
that carries the reply it was made with.

The hidden `compaction` agent decides who writes the summary: with no `agents.compaction.model`
it is the session's model and variant, with one it is that model and the variant after its `#`
([compaction → model](https://opencode.ai/v2/docs/compaction/#model),
[agents → model](https://opencode.ai/v2/docs/agents/#model)). A V1 `agent.compaction` with a
separate `variant` is read the same way: the variant joins the model after `#`
([migration](https://opencode.ai/v2/docs/migrate-v1/)). So a chat on `max` whose
`agent.compaction.variant` is `high` compacts on `high`, and that is expensive twice over: on
Anthropic models the effort is rendered into the prompt, so changing it invalidates the cached
messages ([prompt caching → what invalidates the cache](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[steering thinking → prompt caching](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)),
and the relay's signed thinking history belongs to the effort it was made under, so a request on
another effort starts that history again (`native_history.reset`) and is sent without signatures.
On 2026-11-05 one compaction of a 540,000-token chat (Sonnet 5.5, OpenCode 2.2.5, chat on
`max`, compaction agent on `high`) wrote 532,000 tokens, 28 credits, where a turn of the same chat
wrote about 1,000, 3.5 credits.

The relay now recognises the request by the start of that last message, which is what stays the
same between releases (the rest of the text is reworded), and runs it on the effort of the
dialog's signed history, when that history really continues: the same system prompt, tools,
model and account and the same messages. Only the new tail is then written. The journal keeps
both efforts: `turn.begin` has `requested_effort` (the client's), `effective_effort` (the dialog's),
`effort_source: dialog` and `effort_resolution: cache_continuity`, and every event of the request
has `client_task: "compaction"`. `compaction_keep_effort: false` turns the substitution off; the
label stays. A compaction with nothing to continue (the first request of a dialog, a restart whose
stored state was not applied, another model, a history that was edited) runs on the effort the client
sent, and the label shows why it was dear: `cache.compaction_restart` names the reason (`no_branch`,
`history`, `effort`, or `scope` with `differs`: `conversation`, `provider` for a model or account,
`head`, `tools`, `tool_choice`). A restart alone does not lose the branch: with stored conversations on
(the default) the relay reads the dialog's file before it asks, so a compaction that comes first after a
restart still continues the signed history (README, "Состояние диалогов после перезапуска"). It starts
over when that state is off, was dropped (expired, damaged, written by other code) or cannot be applied
(the account of the dialog is gone, disabled or has no route to the model).

The summary a compaction gets back is not recorded as a branch of the dialog: the client replaces
its history by it and does not continue that request. So the branch of the chat stays where it
was, and a compaction asked again (the reminder after a reply that skipped the template, a second
`/compact`) or the chat itself after a compaction that was cancelled still resumes from it.

To make OpenCode compact on the chat's own effort in the first place, delete the `compaction` entry
from `agent` (V1 syntax) or `agents` (V2): with no model of its own the summary runs on the
session's model and variant
([compaction → model](https://opencode.ai/v2/docs/compaction/#model)). Taking out only the
`variant` is not enough: an entry that names a model and no variant runs on that model's own
options, and for a catalog written by `scripts/sync_opencode_catalog.py` that is the
`options.reasoningEffort` of the model, `high`. A variant written into the entry stays right only
for chats on that variant, and a chat on another one pays for the difference unless the relay
substitutes the effort as above. A different model for the summary, a cheaper one for instance,
never shares the chat's cache whatever the effort: for a chat on Opus and a `compaction` agent on
Sonnet every compaction is written in full (the journal says `scope` and `provider`).

```jsonc
// ~/.config/opencode/opencode.jsonc: no "compaction" key under "agent" (V1) or "agents" (V2)
{
  "agent": {
    "build": { "variant": "high", "model": "retool-relay/claude-opus-5-5" }
    // "compaction": { "variant": "high", "model": "retool-relay/claude-sonnet-5-5" }  <- remove
  }
}
```

With `prompts.json`, OpenCode 2.x compaction follows the chat's profile: it carries the chat's tools, so
the protective rules do not take it (they catch the hidden tool-less agents of OpenCode 1.x, titles and
probes, and the Claude Code request classes `compaction` and `auxiliary`), and its head stays the chat's
own, which keeps the cache. The label `client_task: "compaction"` selects no profile
([protective rules](prompt-control.md#защитные-правила)).

If a later release rewords the instruction, a plugin can name the task instead: OpenCode 2.x's
`model.request` hook carries `event.kind` (`"compaction"` for the checkpoint summary) and can
set request headers ([plugins → server](https://opencode.ai/v2/docs/build/plugins/server)), and the
relay reads `X-Proxi-Task: compaction`. No such plugin ships with the relay and this path was not
run against a live OpenCode 2.x; the wordings above need none.

The installed CLI syntaxes differ: OpenCode 1.19.3 uses `--model
retool-relay/claude-opus-5-5 --variant high`; OpenCode 2.2.5 uses `--model
retool-relay/claude-opus-5-5#high`. Version 2 rejects `--variant` before sending a
model request. Only a model that publishes levels has variants. GLM-5.3, GLM-5.3
Flash and Haiku 4.5 publish none, because Retool accepts an effort level for them
and ignores it: the sync gives them no variants and drops a `#high` suffix from a
reference to them (the relay itself still accepts such a request). The audit
records real GLM `high` requests through both exact clients; they were made before
that decision, when GLM still published levels, and show the clients' syntax, not a
selector GLM has today.

`scripts/probe_client_settings.py` runs the installed clients against a local fake
server with a deliberately invalid key. It finds the newest OpenCode Desktop CLI
itself (override any client with `--executable`) and removes credential-like
variables from the child environment. OpenCode gets no model request until its
resolved configuration shows the fake endpoint, and the probe fails unless the fake
server received requests, so an ignored endpoint override cannot pass silently; a
client that ignored it would still carry only the invalid key, which the real relay
rejects.

## DeepSeek Harness

The current installed CLI (0.2.2-alpha.3) and Desktop (0.2.1-rc.5) use separate
plugin patch lists at `~/.dsh/profiles/web/cordis.patch.yml` and
`~/.dsh/profiles/desktop/cordis.patch.yml`. The `llm-pi-ai` node stores providers
under `config.providers`. The existing Proxi route uses OpenAI Chat Completions.

Both profiles have been reconciled to the same 27 verified IDs. After a sync the
main `retool` group holds 23 models and `retool-text` holds 4. `retool` takes the
models that publish selectable reasoning levels and keeps its medium default and
every supported selector. `retool-text` takes the four that publish none: GPT-4.1
(not a reasoning model), Haiku 4.5, GLM-5.3 and GLM-5.3 Flash (Retool accepts an
effort level for the last three and ignores it). Both use the same existing
connection and credential reference. This split avoids an installed SDK
limitation: a provider-wide medium default is rejected for a model without
selectors before HTTP, while simply deleting that default can silently disable
thinking on a model with an off selector.

The name `retool-text` and its display-name suffix "(без thinking)" mean "no
reasoning-level selector", not "does not think". GLM-5.3 and GLM-5.3 Flash reason
on every request, whatever level is sent, and the group keeps the provider's
`thinkingFormat: deepseek` setting, so Harness shows their thinking like that of
any other model. The group is not renamed: the sync recognizes the managed group
by that name and display name.

To preview or update an existing profile, use `scripts/sync_harness_catalog.py
--settings PATH`. Preview writes only with `--output`; replacing the profile
requires `--write --backup-dir`. The utility splices only the Proxi blocks and
keeps other bytes intact, including date headers and plugin source. The active
provider/model and credentials are preserved. Both installed SDK versions
passed default, off, high and rejection checks with the exact generated files;
the SDK bundles and Desktop ASAR were not modified.

The `--output` preview masks credential values (`apiKey`, header and environment
values, tokens, passwords) as `'***'`, and also the user info and secret parameters
of URLs, the argument after a secret flag, every `NAME=value` item of an `env` list and
secret `NAME=value` text; `apiKeyEnv` references stay visible. Every comment keeps its
place but reads `# ***`, because a comment can quote an old key. Other bytes are kept,
except that a plain or quoted value with a masked URL or argument is rewritten in
double quotes. `--include-secrets` writes the exact bytes. The rules go by name and
shape, so they can miss a credential in an unexpected place: read a preview before you
share it. A profile that is already current is not rewritten and gets no backup
(`written: false`). A last provider block without a final newline is spliced correctly
and keeps that ending. A profile that is not UTF-8 or not valid YAML is reported with
its line and column, never its content. Catalog discovery uses the whole relay config
like the relay. One limit remains: a comment written directly after the Proxi block,
before the next provider, can be dropped or repeated (ruamel attaches it to the block's
last key), so compare the preview with the profile before `--write`: the preview shows
each comment as `# ***` in its place, and `--include-secrets` shows the text.

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
levels, plus `exec_command`, freeform `apply_patch`, and namespace tool support. Codex 0.166 sends
its MCP servers as namespaces (`mcp__blender` holding `look`); upstream such a tool is
`mcp__blender__look` (OpenAI accepts only `^[a-zA-Z0-9_-]{1,64}$`, a longer name is shortened
with a hash), and the reply names it to Codex by its namespace and short name again.
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
the stream with an SSE `error` event and then `response.failed` with code
`invalid_prompt` (non-stream: `502 upstream_invalid_output` with
`x-should-retry: false`), and its credits stay accounted. Codex ignores the `error`
event but handles `response.failed`: it stops at once and prints the message (the
schema path that failed). Without `response.failed` it saw the stream end before
`response.completed` and asked again up to `stream_max_retries` (5) times, each a paid
request, never showing the reason; a live run of codex-cli 0.164.0 sent 6 requests
before and sends 1 now. Temporary failures (`upstream_error`, a timeout, a rate limit)
get a code Codex retries, and `insufficient_credits` becomes `insufficient_quota`,
for which Codex prints its own fixed quota text. The README lists the mapping.
`x-proxi-output-format: validated-json-schema` marks the emulation, and a
format combined with a required tool call is rejected with 422. This is
post-validation, not constrained decoding: a live check on 2026-11-05 (`gpt-6-sol`,
`gpt-5.5`, a `project_name`/`programming_languages` schema) returned valid bare JSON,
which does not guarantee every model or schema.

Codex also sends `prompt_cache_options` and `client_metadata` with each request. The
relay does not forward them; it names both, without values, in
`x-proxi-ignored-parameters` and the `request.compatibility` journal event.

Codex 0.164.0 passed real source reading, patching and testing (two tests), and a
separate real subagent spawn/wait/result round trip. Commands, versions and
limitations were in the review of
[PR #1](https://github.com/newnout6-a11y/proxi/pull/1), `docs/pr-1-review.md`; the file
was removed from the repository, its last version is `git show 60cc80f^:docs/pr-1-review.md`.

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
`invalid_request_error` instead of `unprocessable_entity`. Every relay error carries one of
the documented Anthropic types in Messages (JSON and SSE): `upstream_error` is `api_error`,
`upstream_timeout` is `timeout_error`, `insufficient_credits` is `billing_error`; the relay's own
code stays in `error.relay_code` wherever the type replaced it; the README lists the whole table.

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
check, connection probe) are protected. With `prompts.json` the same two classes are taken by the
protected rule `protected: Claude Code compaction`, which gives them the profile `passthrough`
([protective rules](prompt-control.md#защитные-правила)).

A `compaction` request (with the `x-claude-code-compaction` header that says what
triggered it: `auto`, `manual` or `reactive`) is also labelled `client_task: "compaction"` in
every event of the request. Claude Code's compaction repeats the conversation's system prompt,
tools and history and ends with the summarization instruction
([prompt caching](https://code.claude.com/docs/en/prompt-caching#compacting-the-conversation)),
so it can resume the signed history like OpenCode 2.x's, and the relay runs it on the dialog's
effort when the effort it carries differs (`compaction_keep_effort`, see
[Compaction and the effort of its agent](#compaction-and-the-effort-of-its-agent)). With the
hint headers the request class says it; without `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` the relay
reads the start of the instruction instead: "CRITICAL: Respond with TEXT ONLY. Do NOT call any
tools." in 2.1.316 (an unofficial
[capture of the request](https://clawdipedia.cc/prompts/context-and-memory/compact); the
product is closed and Anthropic documents the instruction only in prose) and "Your task is to
create a detailed summary of the conversation so far" in earlier versions.

## Claude Desktop on Windows

The installed 4.4212.0.0 client reads its configuration from one source, chosen
in this order ([sources](https://claude.com/docs/third-party/claude-desktop/configuration-format),
[MDM](https://claude.com/docs/third-party/claude-desktop/mdm)):

1. machine policy `HKLM\SOFTWARE\Policies\Claude`; while any value exists there,
   `HKCU` is ignored entirely;
2. user policy `HKCU\SOFTWARE\Policies\Claude`;
3. the local `%LOCALAPPDATA%\Claude-3p\configLibrary\`: `_meta.json` names the
   applied `<id>.json`, whose `inferenceModels` is a native JSON array.

A policy that sets any key other than the app-behavior keys (updates,
relaunch/re-check, proxy) makes Desktop ignore the whole local configuration,
gateway URL and key included. Registry values are `REG_SZ` strings holding JSON.
On the audited machine neither policy key exists: `main.log` for the launches of
2026-11-03, 2026-11-04 and 2026-11-05 02:37 reports `type: 'local'`,
`managedKeys: []` and `picker = 3 (inferenceModels)`, so the list comes from the
applied configLibrary file.

The maintainer `scripts/sync_claude_desktop_catalog.py` detects that source the
same way and previews or updates only its `inferenceModels` list. `--source
auto|hkcu|local` states which source you expect (default `auto`); a mismatch is
an error, never a silent write to a source Desktop ignores. `--write
--backup-dir PATH` replaces a configLibrary file atomically (the backup must be
on the same volume) or updates the existing `HKCU` value in place, after saving
the old content. A configLibrary backup holds the gateway settings: keep it
private. Nothing is written and no backup is made when the generated list equals
the existing one, compared as parsed JSON so another layout does not count: the
summary then shows `written: false, changed: false` (a preview shows `changed` as
well, and `written` is `false` until `--write` has actually replaced something).
Quit Desktop first: it reads its configuration at launch. Every other
key, the endpoint, the gateway key and the authentication scheme stay untouched.
The script never writes `HKLM` and never creates a policy: a policy that sets any
other key would switch Desktop away from its working local configuration, so
with no policy the local file is the target. It also refuses to add a model list
where none exists, because a static list replaces model discovery. A missing
Desktop configuration, invalid JSON or a policy it must not touch end in a plain
error message, not a traceback.

A nonempty `inferenceModels` list is authoritative: enabling discovery alone
enriches its rows and does not add missing models. The first row is Desktop's
default model. The script keeps existing rows in order with every field it does
not derive (`labelOverride`, `supports1m`, `behavesAs` and so on), appends new
models at the end and moves the first row only for an explicit `--default-model`;
a current default that is no longer available is an error, not a silent
replacement.

The script owns exactly three row fields: `isFamilyDefault`,
`anthropicFamilyTier` and `maxEffort`. It computes them again on every run and
overwrites what the file holds, because it cannot tell its own earlier value from
one you typed. A hand-edited `maxEffort` or `isFamilyDefault` does not survive; a
row whose model has no family tier or no effort selector loses that field; and
turning `allow_effort_downgrade` on raises `maxEffort` of the five-level rows from
`high` to `max` on the next run (turning it off lowers it again). `name` identifies
the row, `labelOverride` is set only for rows the
script adds (the relay's label), and every other field is kept as it is. The
summary lists each change to an existing row in `changed_fields`, for a preview
and for `--write` alike: `model`, `field`, `was` and `now`, where `null` means the
field was absent. Read it before writing.

Which models: the relay exposes 27 routes and the script selects the verified
Claude ones (`provider` anthropic with a `claude-` prefix), currently 11. That is
the script's own selection, not a subset Desktop computes; Desktop only drops
list entries that do not name an Anthropic model. GPT and GLM are not accepted
merely because they speak the same API; do not invent Anthropic family tiers for
them. When Anthropic's catalog cache is readable (`--desktop-catalog PATH`,
default `%LOCALAPPDATA%\Claude-3p\model-catalog\published.json`) only models it
lists for Code or Cowork are included: 9 of the 11 in catalog version 4008.
`claude-opus-4-5-20251101` appears only as a disabled chat model and
`claude-sonnet-4-5-20250929` is absent, so Code has no metadata or effort
selector for them. `--include-unlisted` keeps them. Without a catalog file all 11
are listed, with a warning.

`maxEffort` hides every level above it, so a cap cannot skip a level. The relay
rejects `xhigh` unless `allow_effort_downgrade` is true in its configuration:

| Model | Desktop picker | `maxEffort`, downgrade off | downgrade on |
| --- | --- | --- | --- |
| Opus 5.5, 5, 4.8, 4.7; Sonnet 5.5, 5 | low, medium, high, xhigh, max | `high` | `max` |
| Opus 4.6, Sonnet 4.6 | low, medium, high, max | `max` | `max` |
| Haiku 4.5, Opus 4.5, Sonnet 4.5 | no effort selector | not set | not set |

With the setting off, five-level rows stop at `high`: Desktop does not offer `max`
even though the relay accepts it, because a `max` cap would also expose `xhigh`.
With it on, the script sets `max`; the relay serves `xhigh` as `high` and `max` as
`max`. Haiku 4.5, Opus 4.5 and Sonnet 4.5 have no native effort. Claude Code's
[effort table](https://code.claude.com/docs/en/model-config#adjust-effort-level)
says "Models not listed here do not support effort" and Anthropic's Haiku 4.5
page says "Default effort: Not supported". A readable catalog's effort levels
take precedence over this table.

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
`x-proxi-thinking-display: summarized` and a debug-level compatibility event naming the field; the
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
