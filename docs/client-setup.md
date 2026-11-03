# Coding clients

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
        "gpt-6.1": {
          "name": "GPT 6.1",
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
            "xhigh": { "name": "Extra High", "reasoningEffort": "xhigh", "settings": { "reasoningEffort": "xhigh" } },
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

## DeepSeek Harness

The Desktop shortcut `DeepSeek Harness.lnk` launches the installed
`@deepseek-ai/dsh` Web UI. Its multi-provider adapter is `llm-pi-ai`; use an
OpenAI Chat Completions route for this relay. The same relay key also works
with the OpenAI Responses and Anthropic Messages endpoints.

The local setup adds `llm-pi-ai.providers.retool` to
`%USERPROFILE%\.dsh\settings.yaml` and the credential reference
`RETOOL_RELAY_API_KEY` to `%USERPROFILE%\.dsh\.credentials.yaml`.
These files are outside this repository. Existing providers, comments, and the
selected provider are preserved. Both documents are watched, so the running
Harness picks up the added route without restarting existing chats.

The installed route contains all 29 currently routed models. This abbreviated
example shows the exact provider fields and two model configurations:

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
            xhigh: xhigh
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

The relay needs the client's stable dialogue identity to reuse a thread and
WebSocket. It accepts `x-session-affinity`, `x-session-id`, `x-opencode-session`,
or explicit request fields `conversation`, `conversation_id`, and `session_id`.
Parent agents and subagents must use different identities. A single static
provider-wide header would mix their conversations and must not be used.

Some in-process Claude Code subagents may share the runtime session ID:
installed 2.1.310 does not include its internal agent ID in the normal metadata
identity. Recognizing that metadata fixes the main conversation; it does not
prove separate warm channels for every in-process child. Changed client
histories still rotate their upstream context, and distinct supplied session
IDs are isolated. Stateless direct inference would avoid this conversation-ID
dependency; it is not enabled by the current agent transport fixes.

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

The relay keeps receiving control frames while a persistent socket is idle,
answers upstream ping and acknowledgement requests, and uses one ordered
receiver. Its application buffer is bounded by `max_frame_bytes` in total and
256 frames; overflow is an explicit upstream error. A dead idle socket is
reconnected before the next prompt. This does not implement the native web's
proactive sandbox-token refresh; disconnected sockets still reconnect normally.

For native client tools, a successful conversation caches the exact normalized
client history and instruction/tool fingerprint in its upstream session. A
strict append sends the new history suffix. Edited or compacted history and
changed instructions/tools start a fresh upstream thread with the complete
current context. Anonymous requests retain independent full-context threads.
Message fingerprints use the same model-visible rendering as the bridge:
moving SDK `cache_control` markers is not a content edit. Actual changes to
text, tool calls/results and identifiers still invalidate the cached prefix.

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
