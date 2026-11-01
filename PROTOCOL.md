# Retool AI Protocol (Reverse-Engineered)

Host: `https://<tenant>.retool.com` — аутентификация по cookie (`accessToken`, `xsrfToken`) + `x-xsrf-token` (равен значению `xsrfToken`).

---

## 1. Прямой транспорт (Direct HTTP Transport, copilotStreamGPT) — ОСНОВНОЙ

Эндпоинт прямого инференса моделей без создания песочниц и WebSocket.
Используется relay по умолчанию (`transport: "direct"`).

### Запрос:
`POST /api/gpt/copilotStreamGPT?origin=frontend`

**Заголовки:**
- `Cookie: accessToken=...; xsrfToken=...; ...`
- `x-xsrf-token: <xsrfToken>`
- `Origin: https://<tenant>.retool.com`
- `Referer: https://<tenant>.retool.com/`
- `User-Agent: <браузерный UA>`
- `x-retool-client-version: <version>`
- `Content-Type: application/json`
- `Accept: application/json`

**Тело запроса (JSON):**
```json
{
  "model": "claude-opus-5-5",
  "rit": "99a368f5194fc7ea",
  "providerResourceName": "ffd4d570-7c6b-414f-b95a-1a695676d0a8",
  "messages": [
    {
      "role": "system",
      "content": "Инструкции и контекст проекта...",
      "cache": true
    },
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Запрос пользователя"},
        {"type": "image", "imageData": "<base64>", "mimeType": "image/png"}
      ]
    }
  ],
  "tools": [
    {
      "name": "exec_command",
      "description": "...",
      "parameters": { ... }
    }
  ],
  "reasoningEffort": "high",
  "openAICacheKey": "<uuid-хеш от head>",
  "metadata": {
    "sandpackAgent": true,
    "environmentName": "production"
  }
}
```

### Поля и валидация upstream (Zod-схема Retool):
- `model`: идентификатор модели (например `claude-opus-5-5`, `gpt-5.5`).
- `providerResourceName`: UUID провайдера (Anthropic, OpenAI, Google и др.).
- `messages`: массив сообщений диалога. Поддерживает текстовые и мультимодальные (`type: "image"`) блоки.
- `tools`: массив схем клиентских инструментов (принимаются нативно без bridging).
- `reasoningEffort`: строгая валидация (`"none"`, `"low"`, `"medium"`, `"high"`, `"max"`). Недопустимые значения отклоняются с 422.
- `openAICacheKey`: ключ кэширования префикса промпта для KV-кэша.
- `metadata.sandpackAgent: true`: открывает доступ к моделям, управляемым через Sandpack-инфраструктуру.

### Формат ответа: NDJSON (Newline Delimited JSON stream):
Каждая строка — отдельный JSON-объект:
- `{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`
- `{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"..."}}`
- `{"type":"content_block_start","index":1,"content_block":{"type":"tool_call","id":"...","name":"exec_command"}}`
- `{"type":"content_block_delta","index":1,"delta":{"type":"partialInputs","partialInputs":"{\"cmd\":...}"}}`
- `{"type":"content_block_start","index":2,"content_block":{"type":"thinking","thinking":"","signature":"..."}}` (для моделей 5.x)
- `{"type":"usage","usage":{"input_tokens":...,"cache_read_input_tokens":52509,"cache_creation_input_tokens":0,"output_tokens":...}}`
- `{"type":"turn_credits_persisted","credits":0.015}`
- `{"type":"end_of_stream"}`

---

## 2. Устаревший транспорт WebSocket Sandbox (Архивный)

> **Статус**: Отключён в пользу прямого транспорта. Сохранён в документации для справки о протоколе песочницы Retool Agent.

### 2.1. Start run
POST /api/ai/retool-next-agent/start
body: {"threadId":"<uuid>","pageUuid":"<uuid>","environmentName":"production"}
-> {"type":"websocket_url","url":"wss://sandbox-<id>.retool.app/sandbox/sandbox-<id>/agent-ws",
    "sandboxId":"...","sandboxToken":"<jwt exp +10800s>","sandboxReused":bool,"uploadToken":"..."}

ВАЖНО: в поле url параметра sandboxToken НЕТ - токен из соседнего поля приложение
дописывает само. Клиент должен клеить "?sandboxToken=<jwt>" и не дублировать параметр,
если он уже есть. Пустой или просроченный токен даёт HTTP 401 на рукопожатии.

### 2.2. Stream
WS connect, Origin: https://<tenant>.retool.com (token already in URL query).
Send:
{"type":"user_prompt","prompt":"...","modelProvider":{"model":"claude-opus-5-5","providerResourceName":"..."},
 "attachments":[],"taggedResourceNames":[],"taggedResourceIds":[],"r2OnboardingTemplateId":null,
 "messageId":"<uuid>","expectedThreadId":"<threadId>","agentMode":2,"testingEnabled":false,"requestOrigin":"Browser"}

Receive (JSON frames):
- {"type":"ping"}            -> reply {"type":"pong"}
- {"type":"text","messageId":"...","content":"<delta>"}
- {"type":"hitl","toolCallId":"...","hitlId":"...","hitl":{"hitlType":"askUser","hitlProps":{"questions":[{"question":"..."}]}}}
- {"type":"clear_streaming",...}, {"type":"agent_loop_done","outcome":"success"}
- logs: agent_readable_log, backend_functions_analysis, run_credits_persisted, commit_history

---

## 3. Сессия и продление (Refresh)

- `accessToken` — JWT без срока действия в exp, но сессия инвалидируется сервером.
- При 401/403 или по расписанию: `POST /api/refreshtoken` с пустым телом.
  Успех возвращает `Set-Cookie` с новыми `accessToken` / `xsrfToken`.
- `cf_clearance` привязан к IP и User-Agent: менять UA между запросами нельзя.
