# Retool agent API (reverse-engineered)

Host: https://vovasahren.retool.com — auth by cookies + x-xsrf-token (header must equal xsrfToken cookie).

## 1. Start run
POST /api/ai/retool-next-agent/start
body: {"threadId":"<uuid>","pageUuid":"<uuid>","environmentName":"production"}
-> {"type":"websocket_url","url":"wss://sandbox-<id>.retool.app/sandbox/sandbox-<id>/agent-ws?sandboxToken=<jwt>",
    "sandboxId":"...","sandboxToken":"<jwt exp +10800s>","sandboxReused":bool,"uploadToken":"..."}

## 2. Stream
WS connect, Origin: https://vovasahren.retool.com (token already in URL query).
Send:
{"type":"user_prompt","prompt":"...","modelProvider":{"model":"claude-opus-5-5","providerResourceName":"ffd4d570-7c6b-414f-b95a-1a695676d0a8"},
 "attachments":[],"taggedResourceNames":[],"taggedResourceIds":[],"r2OnboardingTemplateId":null,
 "messageId":"<uuid>","expectedThreadId":"<threadId>","agentMode":2,"testingEnabled":false,"requestOrigin":"Browser"}

> **Encoding note**: Always serialize JSON with `ensure_ascii=True` (`\uXXXX`). Raw UTF-8 bytes (Cyrillic, etc.) sent over the sandbox WS can be mangled into `??????`, triggering HITL clarification questions.

Receive (JSON frames):
- {"type":"ping"}            -> reply {"type":"pong"}
- {"type":"text","messageId":"...","content":"<delta>"}
- {"type":"hitl","toolCallId":"...","hitlId":"...","hitl":{"hitlType":"askUser","hitlProps":{"questions":[{"question":"..."}]}}}
  (agent requests human clarification/input — turn should complete here as agent_loop_done won't be emitted until answered)
- {"type":"clear_streaming",...}, {"type":"agent_loop_done","outcome":"success"}
- logs: agent_readable_log, backend_functions_analysis, run_credits_persisted, commit_history

## 3. Thread / messages REST
GET  /api/pages/{pageUuid}/sandpack/threads
GET  /api/pages/{pageUuid}/sandpack/threads/{threadId}/messages
POST /api/pages/{pageUuid}/sandpack/threads/{threadId}/messages
   body {"messages":[{"role":"user","type":"text","text":"..."}]}
(POST only persists; the run is started via start+WS.)

## Rate limit
x-ratelimit-limit: 120 / 60s
