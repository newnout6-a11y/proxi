# Retool AI Agent OpenAI-Compatible Proxy Relay

Высокопроизводительный OpenAI-совместимый прокси-сервер (фасад) над внутренним WebSocket-протоколом **Retool Next AI Agent**.

Позволяет использовать флагманские модели (**Claude Opus 5.5**, **Claude Sonnet 4.6**, **GPT-5.2**, **GPT-5.1** и др.) через существующие квоты вашей организации в Retool в любых стандартных OpenAI-клиентах (Cursor, Continue, Aider, Open-WebUI, curl и т.д.).

---

## Ключевые возможности

- 🚀 **Полная совместимость с OpenAI API**:
  - `POST /v1/chat/completions` с поддержкой как потокового вывода (Server-Sent Events / SSE), так и обычных ответов.
  - `GET /v1/models` — динамический каталог моделей Claude и GPT.
  - `GET /v1/balance` — проверка доступных кредитов Retool и даты обновления квоты.
- ⚡ **Автономное выполнение команд на локальном ПК (`local_exec`)**:
  - Агент может выполнять shell-команды на хост-машине через тег `<exec_command>команда</exec_command>`.
  - Прокси перехватывает команду, исполняет её с `cwd=workspace_root`, получает вывод и возвращает агенту в следующем шаге (multi-turn loop).
  - В потоковом режиме процесс выполнения команд наглядно отображается пользователю.
- 📂 **Автоматический сбор контекста проекта (`project_context`)**:
  - Автоматически сканирует Git (текущую ветку, хэш последнего коммита, статус изменений) и дерево файлов проекта (до 60 файлов).
  - Подгружает выжимку документации (`README.md`, `PROTOCOL.md`, манифесты пакетов) и инжектирует в системный контекст.
- 💰 **Мониторинг кредитов и баланса**:
  - Отслеживание баланса организации (`/api/credits/balance`).
  - Логирование стоимости каждого запроса (`run_credits_persisted`) и расчет остатка в реальном времени.
  - Поле `usage` в ответах API возвращает точные данные о списанных кредитах.
- 🔀 **Интеллектуальная маршрутизация моделей и провайдеров**:
  - Модели `claude-*` автоматически маршрутизируются на ресурс Anthropic.
  - Модели `gpt-*` автоматически маршрутизируются на ресурс OpenAI.
  - Проброс уровня рассуждений (`reasoningEffort: low | medium | high | minimal | max`).
- 🛠️ **Исправленный стриминг**:
  - Корректная обработка кумулятивных текстовых фреймов Retool WS (нарезка дельт без дублирования текста).
  - Обработка UI-сигналов `clear_streaming` и HITL (Human-in-the-loop).

---

## Архитектура проекта

```text
├── proxy.py              # Основной HTTP/SSE сервер-прокси
├── credits.py            # Менеджер квот и баланса Retool
├── context.py            # Сборщик контекста проекта (Git, файлы, документация)
├── executor.py           # Изолированный движок локального выполнения команд
├── config.json           # Рабочая конфигурация (сессия, ключи, пути)
├── config.example.json   # Шаблон конфигурации
├── test_suite.py         # Полный интеграционный тестовый набор
├── test_client.py        # Интерактивный CLI-клиент для тестирования
├── PROTOCOL.md           # Реверс-инжиниринг спецификация WebSocket Retool
└── probes/               # Архив исследовательских скриптов и пробников
```

---

## Быстрый старт

### 1. Настройка конфигурации

Скопируйте `config.example.json` в `config.json` и укажите данные вашей сессии Retool:

```json
{
  "base": "https://<your-subdomain>.retool.com",
  "page_uuid": "<page-uuid>",
  "thread_id": "<thread-uuid>",
  "cookie": "<cookie-with-accessToken-and-cf_clearance>",
  "xsrf": "<x-xsrf-token>",
  "port": 8787,
  "model": "claude-opus-5-5",
  "provider_resource_name": "ffd4d570-7c6b-414f-b95a-1a695676d0a8",
  "openai_provider_resource_name": "1b7f4c97-241f-4117-a710-18c7cbfa74b1",
  "reasoning_effort": "high",
  "local_exec": true,
  "show_exec_output": true,
  "max_exec_turns": 5,
  "project_context": true,
  "workspace_root": "C:\\your\\project\\path"
}
```

### 2. Запуск сервера

```bash
python proxy.py
```

При запуске сервер выводит баннер с текущим балансом:
```text
============================================================
 Retool AI Agent OpenAI-Compatible Proxy Relay
 Proxy URL:     http://127.0.0.1:8787/v1
 Default Model: claude-opus-5-5
 Workspace:     C:\specialinay_papka
 Local Exec:    True
 Proj Context:  True
 Effort:        high
 Balance:       120.61 / 3000 credits (Reset: 2026-11-27)
============================================================
```

### 3. Проверка тестами

Запуск сквозного интеграционного теста:
```bash
python test_suite.py
```

Проверка баланса через CLI:
```bash
python test_client.py --balance
```

Просмотр доступных моделей:
```bash
python test_client.py --models
```

Отправка произвольного запроса:
```bash
python test_client.py "Прочитай первые 5 строк файла PROTOCOL.md" --model claude-opus-5-5
python test_client.py "Say hello from GPT" --model gpt-5.2
```

---

## Примеры использования API

### Проверка баланса кредитов
```bash
curl http://127.0.0.1:8787/v1/balance
```
Ответ:
```json
{
  "available": 120.6099,
  "monthly_allocation": 3000,
  "used_month": 2879.3901,
  "reset_date": "2026-11-27T08:50:28.593Z",
  "is_low": true,
  "session_used": 0.54
}
```

### Список моделей
```bash
curl http://127.0.0.1:8787/v1/models
```

### Запрос к чату (cURL)
```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-5-5",
    "messages": [{"role": "user", "content": "Покажи статус git в проекте"}],
    "stream": true
  }'
```

---

## Подключение к сторонним клиентам

В любом приложении (Cursor, Continue, Aider, Open-WebUI):
- **Base URL**: `http://127.0.0.1:8787/v1`
- **API Key**: любой произвольный текст (например, `sk-retool`)
- **Model**: `claude-opus-5-5` или `gpt-5.2`
