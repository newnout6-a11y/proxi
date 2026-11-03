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
  "providerResourceName": "<PROVIDER-RESOURCE-UUID>",
  "messages": [
    {
      "role": "system",
      "content": [
        {"type": "text", "text": "Инструкции и контекст проекта...", "cache": true}
      ]
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
      "name": "shell",
      "description": "...",
      "params": {
        "command": {"type": "string", "description": "...", "required": true}
      },
      "strict": true
    }
  ],
  "reasoningEffort": "high",
  "maxCompletionTokens": 4096,
  "toolChoice": "required",
  "openAICacheKey": "<первые 24 hex-символа sha256(model + tools + head)>",
  "metadata": {
    "sandpackAgent": true
  }
}
```

Состав тела, как его формирует `direct.build_body`:
- `content` системного и пользовательских сообщений — массив блоков; `cache: true`
  ставится на текстовый блок. Строка допустима только для одиночного prompt без истории.
- Каждый tool — `name`, `description`, `params` (типизированные параметры с `required`)
  и `strict: true`; JSON Schema клиента преобразуется в этот вид.
- `toolChoice` отправляется только если клиент задал не `auto` (`required` или имя инструмента);
  `maxCompletionTokens` — только если клиент задал лимит, не меньше 16.
- `openAICacheKey` — `sha256(model, 0x00, tools JSON, 0x00, head)[:24]`; `rit` — не криптографический
  hint целостности от длины `messages` и `tools`.
- `metadata` содержит только `sandpackAgent`; для моделей из списка Assist поле не отправляется
  (relay убирает его и при повторе после ответа «not available right now»).
- `providerResourceName` не отправляется, если ресурс не задан (`__managed__`).

### Поля и валидация upstream (Zod-схема Retool):
- `model`: идентификатор модели (например `claude-opus-5-5`, `gpt-5.5`).
- `providerResourceName`: UUID провайдера (Anthropic, OpenAI, Google и др.).
- `messages`: массив сообщений диалога. Поддерживает текстовые и мультимодальные (`type: "image"`) блоки.
- `tools`: массив схем клиентских инструментов (принимаются нативно без bridging).
- `reasoningEffort`: валидация по per-model списку уровней (`/v1/models → capabilities.reasoning_effort_levels`); набор включает `minimal` (GPT-5) и `max` (GPT-6*). Неподдерживаемый уровень даёт 400 до upstream. Только явный `allow_effort_downgrade: true` разрешает приближение известных уровней; requested/effective effort и причина попадают в журнал. Неизвестные значения всегда дают 400; конфликтующие thinking-контроли — 422. Live 2026-11-03: Opus 5.5 принимает high/max, GPT-6-sol и GPT-6.1-sol принимают max; xhigh отвергнут с 422 на всех проверенных GPT-6 маршрутах.
- `openAICacheKey`: ключ кэширования префикса промпта для KV-кэша.
- `metadata.sandpackAgent: true`: открывает доступ к моделям, управляемым через Sandpack-инфраструктуру.

### Формат ответа: NDJSON (Newline Delimited JSON stream):
Каждая строка — отдельный JSON-объект:
- `{"event":"content_block_start","type":"text"}`
- `{"event":"content_block_delta","type":"text","partialText":"..."}`
- `{"event":"content_block_start","type":"tool_call","toolCallId":"...","toolName":"shell"}`
- `{"event":"content_block_delta","type":"tool_call","partialInputs":"{\"command\":...}"}`
- `{"event":"content_block_start","type":"thinking"}`
- `{"event":"content_block_delta","type":"signature","partialSignature":"..."}`
- `{"event":"metadata","usage":{"inputTokens":4,"promptTokensReadFromCache":52509,"outputTokens":100,"creditsUsed":0.015}}`
- `{"event":"end_of_stream"}` (также распознаётся `type: end_of_stream`)

Это формат текущего direct-парсера. Ранее приведённая здесь форма
`type: content_block_delta` с вложенным `delta` не поддерживается этим парсером;
сводки `*-response.json` фиксируют её в `unsupported_event_shapes`, если она
встретится. Наличие такой формы в диагностике требует отдельного исправления
декодера, а не вывода, что модель ничего не сгенерировала.

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
