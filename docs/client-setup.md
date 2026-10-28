# Coding clients

## DeepSeek Harness

The Desktop shortcut `DeepSeek Harness.lnk` launches the installed
`@deepseek-ai/dsh` Web UI. Its multi-provider adapter is `llm-pi-ai`; use an
OpenAI Chat Completions route for this relay. The same relay key also works
with the OpenAI Responses and Anthropic Messages endpoints.

The local setup adds `llm-pi-ai.providers.retool` to
`C:\Users\Redmi\.dsh\settings.yaml` and the credential reference
`RETOOL_RELAY_API_KEY` to `C:\Users\Redmi\.dsh\.credentials.yaml`.
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
thinking. Text-only input is advertised because this relay does not forward
image attachments.

The installed adapter also passed a real Retool request: Claude Opus 5.5 returned
`get_goal`, arguments `{}`, finish `tool-calls`, in 26.37 seconds. This verified
the tool transport without changing the user's real goal. That low-effort
upstream response supplied no visible thinking text.

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
