# Retool agent API (reverse-engineered)

Host: https://<tenant>.retool.com — auth by cookies + x-xsrf-token (header must equal xsrfToken cookie).

## 1. Start run
POST /api/ai/retool-next-agent/start
body: {"threadId":"<uuid>","pageUuid":"<uuid>","environmentName":"production"}
-> {"type":"websocket_url","url":"wss://sandbox-<id>.retool.app/sandbox/sandbox-<id>/agent-ws",
    "sandboxId":"...","sandboxToken":"<jwt exp +10800s>","sandboxReused":bool,"uploadToken":"..."}

ВАЖНО: в поле url параметра sandboxToken НЕТ - токен из соседнего поля приложение
дописывает само. Клиент должен клеить "?sandboxToken=<jwt>" и не дублировать параметр,
если он уже есть. Пустой или просроченный токен даёт HTTP 401 на рукопожатии.

## 2. Stream
WS connect, Origin: https://<tenant>.retool.com (token already in URL query).
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

## Сообщения треда (REST)

- `GET .../threads/{id}/messages` отдаёт массив **от новых к старым** (index 0 — самое свежее).
- Ошибки прогона приходят не кадром WS, а сохранённым сообщением:
  `{"senderRole":"sandpack","content":{"type":"error","error":"...","statusCode":...}}`.
  Пример: `You've used all AI credits included in your plan.` Текст хода при этом пустой.
- Прочие типы контента: `text`, `thinking`, `tool_call`, `turn_commit`.
- Кадр `clear_streaming` приходит перед tool-call и обнуляет накопитель текста;
  считать текст хода надо по последнему полному `content`, а не по склейке дельт.

## Режим агента

`agentMode: 2` — рабочий режим (правит файлы приложения в песочнице, событие `files_changed`).
При длинном системном контексте модель склонна «строить приложение» и не использовать
внешние команды, поэтому инструкцию про `<exec_command>` нужно держать в начале промпта,
а контекст проекта — после неё и в хвосте промпта.

## Сессия и её срок жизни

- accessToken - JWT без exp, но сессия всё равно инвалидируется на практике через часы.
- При 401 на любом /api/* имеет смысл POST /api/refreshtoken с пустым телом.
  Успех возвращает Set-Cookie с новыми accessToken/xsrfToken; при мёртвой сессии -
  {"success":false,"message":"Authentication failure. Session has expired."} и Set-Cookie
  с Expires в 1970 (удаление).
- cf_clearance привязан к IP и User-Agent: менять UA между запросами нельзя.

## Rate limit
Исторически наблюдался `x-ratelimit-limit: 120 / 60s`. Это не подтверждение
текущей общей квоты: в проверке `GET /api/credits/balance` 2026-10-29 эти
заголовки отсутствовали. Relay обрабатывает реальные 429/Retry-After, не
добавляя фиксированный интервал между успешными запросами.
