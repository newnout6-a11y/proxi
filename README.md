# Retool AI relay (OpenAI / Anthropic compatible)

Локальный HTTP-relay над внутренним WebSocket-протоколом Retool AI Agent.
Даёт доступ к моделям, доступным вашей организации Retool, из обычных OpenAI- и
Anthropic-клиентов (Codex, opencode, Aider, Continue, Claude Code, curl).

Честная рамка: это адаптер к внутреннему протоколу, а не официальный API Retool.
Совместимость ограничена списком ниже.

## Возможности

- POST /v1/chat/completions — OpenAI Chat Completions, streaming и non-streaming.
- POST /v1/responses — OpenAI Responses API (streaming и non-streaming).
- POST /v1/messages — Anthropic Messages API (streaming и non-streaming, x-api-key).
- GET /v1/models — каталог моделей с capabilities и размером контекста.
- GET /v1/balance, GET /v1/usage, GET /health (503, когда upstream недоступен).
- Обязательный локальный ключ relay: Authorization: Bearer, x-api-key или ?key=.
- Изоляция: отдельный Retool-тред и отдельный WebSocket на каждый запрос.
- Локальное выполнение команд (<exec_command>) с allowlist, чистым окружением
  и убийством дерева процессов по таймауту.
- Инжект контекста проекта с redaction секретов.
- Учёт кредитов по каждому ходу, credits_used в usage, история в /v1/usage.
- Автообновление сессии Retool через /api/refreshtoken при 401.

## Установка

    python -m pip install -r requirements.txt
    copy config.example.json config.json     # заполнить base/page_uuid/cookie/xsrf/workspace_root
    $env:RELAY_API_KEY = "<длинный случайный ключ>"
    python proxy.py

Если RELAY_API_KEY и relay_api_key не заданы, ключ генерируется в relay_key.txt
и печатается в баннере маскированно.

Секреты можно держать вне файла: RETOOL_COOKIE, RETOOL_XSRF, RETOOL_BASE,
RETOOL_PAGE_UUID, RETOOL_THREAD_ID, RELAY_API_KEY. config.json и cookies.txt в .gitignore.

## Использование

    curl.exe http://127.0.0.1:8787/v1/chat/completions -H "Authorization: Bearer $env:RELAY_API_KEY" -H "content-type: application/json" -d "{\"model\":\"claude-opus-5-5\",\"messages\":[{\"role\":\"user\",\"content\":\"привет\"}]}"

Codex (~/.codex/config.toml):

    model = "claude-opus-5-5"
    model_provider = "retool"
    [model_providers.retool]
    name = "Retool relay"
    base_url = "http://127.0.0.1:8787/v1"
    wire_api = "chat"
    env_key = "RELAY_API_KEY"

Claude Code: ANTHROPIC_BASE_URL=http://127.0.0.1:8787 и ANTHROPIC_AUTH_TOKEN=<relay key>.

## Конфигурация

Все ключи с дефолтами — в config.example.json. Важное:

| ключ | смысл |
| --- | --- |
| session_mode | per_request (изоляция, по умолчанию) или shared (один тред) |
| exec_policy | allowlist (по умолчанию), allow_all, off |
| exec_allowlist | regex-список разрешённых первых слов команд |
| strict_models | true — неизвестная модель даёт 404 вместо тихой маршрутизации |
| max_body_bytes, max_workers, rate_limit_rps | лимиты входа и параллелизма |

## Коды ответов

400 битый JSON/заголовки, 401 нет или неверный relay-ключ, 404 неизвестный маршрут или модель,
405 неверный метод, 413 слишком большое тело, 415 не JSON, 422 неверные типы полей,
429 локальный rate limit или лимит Retool, 502 отказ upstream/сессии, 503 upstream недоступен,
504 таймаут хода.

## Ограничения (важно)

- Модель отвечает одним текстовым потоком: tools/tool_choice клиента не транслируются
  в нативные tool-calls, локальные команды приходят текстом.
- temperature, top_p, stop, n, seed, response_format, logprobs принимаются, но upstream
  их не применяет; список игнорируемых полей пишется в лог.
- usage по токенам приблизительный (len/4, флаг estimated), достоверны только кредиты.
- HITL-вопрос агента отдаётся как текст; отдельного API ответа на HITL нет.
- Промпт собирается из всей истории запроса; авт-обрезки по контексту нет.
- Retool не даёт удалять треды: в режиме per_request треды копятся.
- Состав моделей статичен; доступность конкретной модели проверяет upstream.

## Тесты

    python -m pytest -q          # offline, без сети
    python probes/dbg_ws6.py     # ручной прогон против реального Retool

## Архитектура

proxy.py (HTTP/роутинг/лимиты) -> validate.py (строгая схема) -> orchestrator.py
(контекст, exec-цикл) -> session.py (тред + WS, ретраи, отмена) -> adapters.py
(три диалекта) + sse.py. Вспомогательное: auth.py, upstream_refresh.py, credits.py,
executor.py, context.py, redact.py, apierr.py, models.py.
Реверс-инжиниринг протокола — PROTOCOL.md, черновики — probes/.
