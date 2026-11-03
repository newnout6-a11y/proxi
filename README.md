# Retool AI relay (OpenAI / Anthropic compatible)

Локальный HTTP-relay над прямым протоколом Retool AI (`POST /api/gpt/copilotStreamGPT`)
с поддержкой prompt caching и нативных клиентских инструментов.
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
- Изоляция: один тред на один диалог; OpenCode affinity (`x-session-affinity`/`X-Session-Id`)
  разделяет диалоги, а tool-result ходы остаются в том же треде.
- Локальное выполнение команд (<exec_command>) с allowlist, чистым окружением
  и убийством дерева процессов по таймауту.
- Инжект контекста проекта с redaction секретов.
- Учёт кредитов по каждому ходу, credits_used в usage, история в /v1/usage.
- Фоновое продление сессии Retool каждые 15 минут, сохранение cookies после
  ротации и общий повтор при 401/403 для моделей, истории сообщений и баланса.
- Прямой транспорт (direct): быстрый streaming через POST /api/gpt/copilotStreamGPT
  с кэшируемым system-блоком (prompt caching с чтением десятков тысяч токенов
  из KV-кэша Retool) и нативной поддержкой клиентских инструментов. Первый токен —
  около 1.5–2 секунд вместе с контекстом. Устаревший WebSocket sandbox-путь отключён.
  Замеры — [docs/direct-transport.md](docs/direct-transport.md).

## Установка

    python -m pip install -r requirements.txt
    copy config.example.json config.json     # заполнить base/page_uuid/cookie/xsrf/workspace_root
    python proxy.py                          # ключ сам появится в relay_key.txt

Фоновый запуск, переживающий закрытие терминала и перезапускающийся при падении:

    Start-Process pwsh -ArgumentList '-NoProfile','-File','scripts\run_relay.ps1' -WindowStyle Hidden -PassThru |
        ForEach-Object { $_.Id | Out-File relay.pid }
    taskkill /PID (Get-Content relay.pid) /T /F     # остановить (супервизор перезапустит — убивать с /T)

Если RELAY_API_KEY и relay_api_key не заданы, ключ генерируется в relay_key.txt
и печатается в баннере маскированно.

Секреты можно держать вне файла: RETOOL_COOKIE, RETOOL_XSRF, RETOOL_BASE,
RETOOL_PAGE_UUID, RETOOL_THREAD_ID, RELAY_API_KEY. Для OpenCode affinity приходит
в `x-session-affinity`/`X-Session-Id`; config.json и cookies.txt в .gitignore.

Сессия продлевается через `/api/refreshtoken` при запуске и затем каждые
`session_refresh_interval` секунд (по умолчанию 900). Новые cookie и xsrf
атомарно сохраняются в исходный `config.json`, остальные настройки остаются
на месте. После перезапуска используется последняя сохранённая сессия.
При ошибках продление повторяется с паузой от 20 секунд до 5 минут;
параллельные клиенты используют одно обновление. `0` отключает фоновое продление.

Для секретов из `RETOOL_COOKIE`/`RETOOL_XSRF` обновление работает в памяти:
значения окружения не записываются в конфигурацию. Если выполнен новый вход
и обновлён `config.json`, работающий relay подхватит его при следующем продлении
или ошибке авторизации. Истёкшую или отозванную сервером сессию без действующих
учётных данных `/api/refreshtoken` восстановить не может — нужен новый вход.
Состояние фонового продления и ошибки сохранения видны в `/health`;
устаревший кэш баланса означает `503`, а не успешную проверку upstream.

При первой загрузке каждому `accounts[]` назначается и сохраняется уникальный
`account_id`. Обновление ищет точный ID с проверкой tenant; одинаковые страницы
не объединяют аккаунты. Запись настроек и ротаций использует общую межпроцессную
блокировку, а продление одного ID в одном файле выполняется одним владельцем.
Ресурс другого tenant не наследуется из глобальных настроек. Новые диалоги
выбирают аккаунт с маршрутом нужной модели; параллельный запрос существующего
диалога использует тот же аккаунт.

## Использование

    curl.exe http://127.0.0.1:8787/v1/chat/completions -H "Authorization: Bearer $env:RELAY_API_KEY" -H "content-type: application/json" -d "{\"model\":\"claude-opus-5-5\",\"messages\":[{\"role\":\"user\",\"content\":\"привет\"}]}"

Codex (~/.codex/config.toml):

    model = "gpt-6-sol"
    model_provider = "retool"
    model_reasoning_effort = "high"
    model_reasoning_summary = "detailed"
    web_search = "disabled"
    [model_providers.retool]
    name = "Retool relay"
    base_url = "http://127.0.0.1:8787/v1"
    wire_api = "responses"
    env_key = "RELAY_API_KEY"
    supports_websockets = false
    model_catalog_url = "http://127.0.0.1:8787/codex/models"

`/codex/models` предоставляет Codex-совместимые описания уровней мышления и
инструментов (`exec_command`, freeform `apply_patch`, namespaces субагентов).
Встроенный server-side `web_search` Responses не реализован; клиентские
инструменты поиска, объявленные как function tools, проходят обычный bridge.

Claude Code: `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` и
`ANTHROPIC_API_KEY=<тот же relay key>` (или `ANTHROPIC_AUTH_TOKEN`).
В bare-режиме Claude Code использует `ANTHROPIC_API_KEY`.

Для DeepSeek Harness используйте провайдера `retool` через OpenAI Chat:
`baseURL: http://127.0.0.1:8787/v1`, `thinkingFormat: deepseek`.
Схемы настроек трёх клиентов и границы IDE-команд — в
[docs/client-setup.md](docs/client-setup.md).

### Thinking и работа с проектом

Relay передаёт полученный от провайдера thinking отдельно от ответа:
Chat — `reasoning_content`, Responses — reasoning summary item/events,
Anthropic — thinking content blocks. Это только данные, которые Retool
раскрывает: скрытую цепочку рассуждений relay не восстанавливает и не выдумывает.
Если провайдер вернул пустой thinking, блок текста останется пустым.
В Claude Code блоки по умолчанию свёрнуты: `Ctrl+O` раскрывает их;
`showThinkingSummaries: true` включает отображение кратких сводок.

Opus 5.5 через Retool думает молча: приходит только начало блока и подпись.
Клиенты на OpenAI-совместимом API (OpenCode, Cline и другие на AI SDK) показывают
мышление лишь при непустом `reasoning_content`, поэтому в форматах Chat и Responses
relay в начале мышления отправляет метку `thinking_placeholder` (по умолчанию
«Модель думает…»). Метка служит только для отображения и в историю не попадает.
Пустая строка её отключает. В формате Anthropic открывается пустой блок thinking.

Уровень меняется настоящим полем Retool `modelProvider.reasoningEffort`:
`reasoning_effort` в Chat, `reasoning.effort` в Responses,
`thinking.type=adaptive` + `output_config.effort` в Anthropic.
Допустимые уровни конкретной модели перечислены в
`/v1/models → capabilities.reasoning_effort_levels`. Неподдерживаемый уровень
возвращает 400 до обращения к upstream. Явный `allow_effort_downgrade: true`
разрешает приближение известных уровней; журнал хранит requested/effective effort
и причину выбора. Неизвестные `extra`/`ultra` отклоняются в обоих режимах.
На 3 ноября Retool принимает для Opus 5.5 `high`/`max`, а сырой `xhigh` даёт 422.
GPT-6-sol и GPT-6.1-sol принимают `max`, но также отвергают `xhigh` с 422;
этот уровень не предлагается их каталогом Codex.
Legacy `thinking.budget_tokens` переводится в уровень;
точный бюджет токенов Retool этим интерфейсом не предоставляет.

Если клиент передал инструменты, модель получает его system/developer instructions,
историю и полные результаты инструментов. Файлы, изменения и тесты выполняются
инструментами клиента: workspace клиента имеет приоритет над папкой relay.
Поддержаны обычные function tools и Responses custom tools (`apply_patch`).
Без инструментов клиента relay добавляет список файлов, Git-состояние и выдержки
AGENTS/README/PROTOCOL настроенного workspace; дополнительные чтения доступны
через локальный exec-протокол. Это не загрузка всего репозитория в prompt.

## Админка

`http://127.0.0.1:8787/admin/` — вход по тому же relay key (ключ передаётся только в заголовке).
Разделы: Обзор (баланс, попадание в кэш, TTFT, кредиты, графики), Журнал (живой поток
каждого действия с фильтрами, поиском, паузой, раскрытием JSON и экспортом в JSONL),
Ходы (трасса каждого хода с таймлайном), Модели, Настройки. Горячие клавиши `1`–`5`,
светлая и тёмная темы.

Журнал (`journal.py`) пишет события: HTTP-запросы, начало и конец хода, первый токен,
кредиты и кэш, вызовы инструментов, локальные команды, создание и вытеснение диалогов,
продление сессии Retool, прогрев кэша, действия в админке. Секреты маскируются.
В памяти хранится 5000 последних событий, на диск они дописываются в `journal.jsonl`
(ротация на 10 МиБ) и восстанавливаются после перезапуска.

Для локальных Claude Code/Desktop и OpenCode журнал добавляет название приложения,
имя чата, ID сессии и агента. Имена читаются из локальных метаданных только этих
клиентов; `title_source` отличает сохранённое название от первой реплики, если имя
ещё не задано. Для OpenCode 1.x можно скопировать
`scripts/integrations/proxi-session-labels.js` в `~/.config/opencode/plugins/`:
плагин передаёт актуальное название, включая переименования. OpenCode 2.x читается
из таблицы `session_v2` той же локальной базы. Подписи не меняют affinity диалога.

Ответы содержат одинаковые `request-id` и `x-request-id`; ошибка также содержит
`request_id`. Отказы upstream 400/422 сохраняются в `logs/upstream-400/` отдельными
файлами: точное тело, текст ошибки и метаданные с названием чата и ID запроса.
В журнале и ответе есть `diagnostic_id`. Эти локальные снимки содержат переписку;
заголовки авторизации в них не записываются.

Запуск проверенного checkout с существующими учётными данными и отдельными логами:
`python proxy.py --config C:\specialinay_papka\config.json --state-dir "$env:LOCALAPPDATA\Proxi\state"` (PowerShell).
`--config` использует `relay_key.txt` рядом с выбранным конфигом.
Те же параметры поддерживает `scripts/run_relay.ps1` (`-Config`, `-StateDir`).

Настройки реальных клиентов и дочерних агентов можно проверить без запросов к
провайдеру: `python scripts/probe_client_settings.py claude`, аналогично
`opencode` и `opencode2`. Проверка поднимает локальный имитатор API и проверяет
`high` у родителя и дочернего агента. Она создаёт тестовые сессии OpenCode;
внешние инструменты модель не вызывает. При необходимости укажите `--executable`.
Проверка OpenCode также сверяет каталог и определения вариантов: Desktop 2.2.5
при преобразовании старой схемы игнорирует blacklist и не применяет
`variants.xhigh.disabled`. Удаляйте неподдерживаемые варианты и модели из самих
`provider.<name>.models`; для настроенной пары CLI1/Desktop2 отсутствие xhigh
проверено в обоих фактических каталогах.

Разбор задержек: `python scripts\turn_breakdown.py journal.jsonl --last 20` раскладывает каждый
ход на обработку контекста (до начала мышления), скрытое мышление, генерацию аргументов
инструментов и текст. Аргументы инструментов стримятся клиенту по мере генерации.

Сборка фронтенда: `cd web; npm install; npm run build` (результат в `web/dist`).
E2E: `npx playwright test`.

Консоль: cache `read` выделен бирюзовым, `write` — янтарным. Запись от 50 000
токенов получает красную метку `WRITE HIGH`; рост write относительно предыдущего
запроса того же диалога/модели минимум в 4 раза и на 8192 токена — `WRITE SPIKE`.
Большой объём чтения из кэша сам по себе не вызывает предупреждения.
Метки остаются видимыми и без цвета. Это подсветка телеметрии, не изменение кэширования.

После каждого модельного HTTP-запроса консоль печатает `balance`, месячный `limit`
и `spent` за запрос, включая повторные upstream-попытки. Остаток берётся из уже
полученного баланса с учётом локальных списаний: `LOCAL ESTIMATE` обозначает такой
расчёт, `STALE` — устаревшие данные, `UNAVAILABLE` — отсутствие данных. Ради этой
строки сетевой запрос не выполняется и ответ модели не задерживается.

`python scripts/watch_relay.py` показывает цветной хвост фонового `relay.log`
из `%LOCALAPPDATA%/Proxi/state/` (или укажите путь первым аргументом).
`--demo` показывает синтетические read/write/balance-примеры без обращения к модели,
`--no-follow` печатает только последние строки. В терминале цвет включается
автоматически; `NO_COLOR` отключает, `FORCE_COLOR=1` принудительно включает его.
Супервизор сохраняет файл без ANSI-кодов; просмотрщик раскрашивает его при чтении.

## Конфигурация

Все ключи с дефолтами — в config.example.json. Важное:

| ключ | смысл |
| --- | --- |
| session_mode | per_conversation (по умолчанию), per_request (новый тред на каждый запрос) или shared (один тред) |
| max_conversations, conversation_idle_timeout | максимум 128 persistent-диалогов, неактивные вытесняются через 900 с |
| exec_policy | allowlist (по умолчанию), allow_all, off |
| exec_allowlist | regex-список разрешённых первых слов команд (см. ниже) |
| strict_models | true — неизвестная модель даёт 404 вместо тихой маршрутизации |
| max_body_bytes, max_frame_bytes, max_workers | границы памяти/параллелизма: 32 МиБ запроса, 16 МиБ WS-кадра, 32 воркера |
| rate_limit_rps | 0 — дополнительная локальная квота выключена; можно включить явно |
| command_output_limit | 64 000 символов; 0 — весь вывод команды |
| max_prompt_bytes | 0 — relay не обрезает prompt; положительный лимит проверяет весь UTF-8 prompt и возвращает 413 при переполнении |
| context_max_bytes | 40 000 — сколько контекста проекта подмешивается; больше не значит лучше (консенсус 2026: компактный структурный контекст вместо больших выгрузок) |
| reasoning_effort | auto — рекомендация выбранной модели; явный уровень из клиента имеет приоритет |
| allow_effort_downgrade | false — неподдерживаемый уровень отклоняется; true явно разрешает совместимое приближение |
| agent_mode | 2 — рабочий режим агента (1 — более «разговорный», 3 — не проверялся) |
| google_provider_resource_name, retool_fast_provider_resource_name | ресурсы для gemini-* и zai-org/*; без них эти модели дают 503 |
| provider_resource_name, openai_provider_resource_name | ресурсы для claude-* и gpt-*; без них эти модели дают 503. Значение `__managed__` = тенант сам обслуживает модели (поле `providerResourceName` в запрос не отправляется); задаётся per-account в `accounts[]` |

### Как читается политика команд

Команда режется на сегменты по переводам строк и разделителям `&& || | ; &` **вне кавычек**.
Для каждого сегмента проверяется первое слово. Для `powershell`, `pwsh` и `cmd` тело после
`-Command` / `-c` / `/c` проверяется рекурсивно по списку разрешённых cmdlet-ов
(`Get-Content`, `Get-ChildItem`, `Select-String`, `dir`, `type`, `findstr`, `ping` и т. п.).
Поэтому `powershell -Command "Get-ChildItem; Get-Date"` проходит, а `powershell -c "calc"` — нет.
`allowlist` ограничивает **список программ**, а не файловую систему: `type C:\Windows\win.ini`
или чтение другого абсолютного пути возможны. Allowlist не является песочницей
файловой системы. Подстановки cmd, PowerShell script blocks/вложенные выражения,
неявные и encoded shell-команды блокируются; Python/Node/npm/curl не разрешены
по умолчанию. Полноценные правки и тесты используйте через инструменты клиента
с его разрешениями. `allow_all` остаётся явным выбором владельца relay.

Таймауты: ход — 900 с (`turn_timeout`), чтение кадра — 120 с (`recv_timeout`), команда — 300 с.
Таймаут чтения кадра сам по себе не завершает генерацию. Эти значения ограничивают
максимальное ожидание; успешный ответ отправляется сразу после готовности.
Количество шагов исполнения не ограничено (`max_exec_turns: 0`). Ограничение шагов можно
включить вручную: тогда на исчерпании агент получает финальный ход без команд. Отдельно
стоит предохранитель от цикла: если одна и та же команда повторяется, исполнение
останавливается и агент обязан дать сводку.
`max_tokens` валидируется по модели. Контексты, output caps, рекомендуемые уровни
и границы подтверждения сведены в [аудите каталога](docs/model-catalog-audit.md).

## Каталог моделей

Каталог и доступные уровни мышления сверены с picker/schema приложения Retool:
Claude Opus 5.5 / 5 / 4.8 / 4.7 / 4.6 / 4.5 / 4.1 / 4, Claude Sonnet 5.5 / 5 / 4.6 / 4.5 / 4,
Claude Fable 5, Claude Haiku 4.5, GPT-5.6 Sol/Terra/Luna, GPT-5.5, GPT-5.4, GPT-5.3 Codex,
GPT-5.2 / 5.1 / 5 / mini / nano, GPT-6 Astra/Sol/Luna, GPT-6.1 Sol,
Gemini 3.x / 2.5 Pro / 2.5 Flash, Retool Power/Mini. Список статический: обновляется в `models.py`
после проверки схемы Retool; наличие в каталоге не гарантирует доступ в организации.

Маршрутизация в провайдера — по префиксу модели: claude-* → ресурс Anthropic, gpt-* → ресурс OpenAI.

## Коды ответов

400 битый JSON/заголовки, 401 нет или неверный relay-ключ, 404 неизвестный маршрут или модель,
405 неверный метод, 413 слишком большое тело, 415 не JSON, 422 неверные типы полей,
429 локальный rate limit, лимит Retool или исчерпанные кредиты плана
(`insufficient_credits`), 502 отказ upstream/сессии, 503 upstream недоступен, 504 таймаут хода.

## Ограничения (важно)

- Retool на прямом транспорте (`copilotStreamGPT`) принимает клиентские инструменты
  (`tools`) нативно, транслируя их в JSON-схему upstream. Модель инициирует вызовы инструментов
  через нативные события `content_block_start` (`type: "tool_call"`) и `partialInputs`,
  без необходимости текстового бриджинга `<tool_call>`.
- Изображения (Vision) поддерживаются для мультимодальных моделей (Claude Opus 5.5,
  Sonnet 5, GPT-5.5 и др.) через OpenAI `image_url` и Anthropic `image` блоки. В каталоге
  `/v1/models` для них выставлен флаг `vision: true`.
- Лимиты токенов вывода (`max_tokens`, `max_output_tokens`): если клиент запрашивает
  больше аппаратного максимума модели, relay мягко ограничивает (clamps) значение
  до допустимого максимума модели вместо отклонения с ошибкой 422.
- Prompt Caching: инструкции и контекст проекта кэшируются (`openAICacheKey`, `cache: true`).
  При повторных запросах в рамках одного диалога токены читаются напрямую из KV-кэша Retool
  без повторной оплаты и задержки. Фоновый keepwarm каждые 240 с поддерживает кэш горячим.
- Opus 5.5 thinking: возвращает блок рассуждений с аутентифицированной подписью Anthropic;
  текст рассуждений скрыт upstream по умолчанию для моделей линейки 5.x (`thinking.display: "omitted"`).
- temperature, top_p, stop, n, seed, response_format, logprobs принимаются для совместимости
  с OpenAI/Anthropic SDK, но upstream их игнорирует (список игнорируемых параметров логируется).
- usage по токенам: число токенов в usage возвращается из upstream с учётом попаданий в кэш
  (`prompt_tokens_details.cached_tokens`), достоверный биллинг ведётся по полю `credits_used`.
- Состав моделей статичен в `models.py`; доступность конкретной модели проверяет upstream.

## Что проверено вживую (Retool, свежая сессия)

- все три диалекта в streaming и non-streaming возвращают реальный ответ модели;
- маршрутизация провайдеров: `claude-opus-4-8` → Anthropic, `gpt-5.5` → OpenAI;
- два параллельных запроса создают два треда и не видят историю друг друга;
- allowlisted-команда (`dir`) исполняется локально, неразрешённая (`whoami`) даёт код 126
  и текст политики, аудит пишется в `commands.log`;
- пустые ходы диагностируются: исчерпание кредитов отдаётся как `429 insufficient_credits`,
  а не пустой `200`;
- upstream 401 при мёртвой сессии отдаётся как `502 upstream_auth_error` после попытки refresh.

## Тесты

    python -m pytest -q          # offline, без сети
    python test_client.py --models
    python test_client.py "Проверь проект" --effort high

## Архитектура

proxy.py (HTTP/роутинг/лимиты) -> validate.py (строгая схема) -> orchestrator.py
(контекст, exec-цикл) -> session.py (диалоги: direct/WS, ретраи, отмена) -> direct.py
(direct-транспорт, prompt caching) -> adapters.py (три диалекта) + sse.py.
Вспомогательное: auth.py, upstream_refresh.py, credits.py, executor.py, context.py,
redact.py, apierr.py, models.py.
Реверс-инжиниринг протокола — PROTOCOL.md. Локальные capture/probe-артефакты
исключены из Git: они могут содержать credentials. Удаление файлов из текущей
ветки не удаляет ранее опубликованные секреты из истории; их следует отозвать.
