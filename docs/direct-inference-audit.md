# Direct model inference audit

Verified on 2026-10-29 against the currently configured Retool tenant. No
provider keys were extracted, resources created, or project tools executed.

## Direct backend exists

The current native editor calls `POST /api/gpt/copilotStreamGPT?origin=frontend`
through its normal cookie/XSRF-authenticated `callInternalApi`. This request
does not create a sandbox, thread, or WebSocket agent run. It accepts structured
system/user/assistant/tool messages, tools, model/resource selection, effort and
maximum completion tokens. Its response is newline-delimited JSON, with text,
thinking/signature and tool-call blocks, metadata usage and `end_of_stream`.

Public source evidence:

- [Endpoint constant](https://retool-edge.com/assets_vjs/loggingSpanEndpointTypes-CfQ_1ztW.js)
- [Request wrapper](https://retool-edge.com/assets_vjs/queryCopilotWrapper-T1BTu1VZ.js)
- [Message, tool and request schemas](https://retool-edge.com/assets_vjs/main-CnZ2mbHm.js)
- [Native stream parser](https://retool-edge.com/assets_vjs/StreamHandler-CfXtcWV5.js)

`rit` is computed by the native client from the JavaScript JSON lengths of
messages/tools. A minimal request without the native request fields returned
400. Including the native `rit` produced a validated request. Python character
counts are not generally equivalent to JavaScript UTF-16 JSON lengths.

## Access context matters

A normal direct request for `claude-opus-5-5` with the configured managed
resource and valid `rit` returned HTTP 422:

```text
claude-opus-5-5 is not available when using Retool's managed key for Assist.
Please configure your own API key.
```

The native Retool React editor uses a distinct app-building context for its
coding/helper requests. A request matching that context returned Opus 5.5
metadata, a real `get_goal` tool call and thinking signature at `max`, in
1.988 seconds. That demonstrates the editor-specific capability; it does not
establish a generally supported external-IDE inference entitlement. It must
not be presented as an unrestricted substitute for the rejected Assist route.

The current official CLI model catalog also distinguishes `aiQueries` from
`retoolReact`: managed Opus 5.5 requires BYOK for the former, while the latter
admits it. See the [current CLI core](https://cli.retool-edge.com/versions/0.5.106/retool.bundle.cjs)
and [Retool model documentation](https://docs.retool.com/build/apps/reference/models).

An ordinary direct request for `claude-sonnet-5`, without the editor-context
marker, succeeded with the same configured account/resource:

| Observation | Result |
| --- | --- |
| HTTP status / returned model | 200 / `claude-sonnet-5` |
| First block / completion | 1.672s / 1.943s |
| Tool call | Native `get_goal`; no execution |
| Input / output tokens | 441 / 27, from backend metadata |

This was a small request. It does not predict latency with a full IDE system
prompt or project history. No client model or key was changed by this audit.

## Compatibility work needed before migration

The gateway tool schema differs from OpenAI/Anthropic JSON Schema: it uses
`params` and per-field `required` booleans, with a limited parameter vocabulary.
Unsupported constraints must not silently disappear during conversion.
`end_of_stream` has no finish-reason field in the published schema. A transport
must preserve real usage, cancellation, errors and native tool/result cycles;
it must not silently fall back to the slower agent route.

Official product references:

- [AI resource queries](https://docs.retool.com/queries/guides/ai)
- [Streaming](https://docs.retool.com/data-sources/concepts/streaming)
- [Resources in backend functions](https://docs.retool.com/build/apps/reference/resources)

The separate CLI resource-query route uses OAuth. Current browser cookies
received 401 there; a connector authenticated to another tenant was not used
to infer access or billing for this tenant.
