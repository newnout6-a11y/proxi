# Локальные клиенты и диагностика ошибок, 2026-11-03

Настроены Claude Code 2.1.310, Claude Desktop (установленная сборка 4.4212.0.0),
OpenCode CLI 1.19.3 и sidecar OpenCode Desktop 2.2.5. Настройки Codex и глобальные
переменные окружения не изменялись. Учётные данные в этот отчёт не включены.

## Применённые настройки

- Claude Code: локальный Anthropic endpoint `http://127.0.0.1:8787`,
  `effortLevel=high`, `CLAUDE_CODE_EFFORT_LEVEL=high`, значения modelSettings high.
  Opus → Opus 5.5, Sonnet/Haiku → Sonnet 5.5; для субагентов задан Sonnet 5.5.
  Haiku в текущем upstream недоступен: его успешный HTTP-статус без текста не
  считался успешной проверкой. Сохранены существующие hooks и остальные настройки.
- Шесть локальных определений агентов Claude Security с явным `effort: xhigh`
  переведены на high. Сейчас эти marketplace-агенты не включены в enabledPlugins.
- Claude Desktop: пользовательская политика `HKCU\Software\Policies\Claude`
  использует gateway, static key, x-api-key, модели Opus/Sonnet 5.5 с maxEffort high,
  defaultModelEffort high; discovery отключён. В двух сохранённых Code-сессиях
  исправлен явный xhigh. Другие сохранённые усилия не переписывались.
- OpenCode: build/plan → Opus 5.5 high; general/explore/title/summary/compaction →
  Sonnet 5.5 high; small_model → Sonnet 5.5. Варианты xhigh явно disabled,
  недоступные GPT-6 Astra/Luna и Haiku исключены из relay-каталога клиента.
  Остальные провайдеры не изменялись. Установлен локальный плагин подписей для 1.x.
  Desktop 2.x читает тот же конфиг через свой преобразователь схемы.

Оригиналы находятся в
`C:\Users\Redmi\AppData\Local\Proxi\backups\clients-20261103-92fbbd3d`:
настройки клиентов, изменённые файлы агентов, две сессии Desktop и исходный
relay-config. Резервные копии содержат секреты и остаются локально вне Git.

## Изменения relay

`client_context.py` связывает запросы с приложением, названием чата, ID сессии,
агентом и родителем. Читает локальные метаданные Claude и OpenCode без записи.
OpenCode 1.x использует `session`, 2.x — `session_v2`; настоящий ID дочернего чата
берётся из `x-opencode-session-id`, отдельно от родительской affinity.
Claude поддерживает JSON/legacy metadata.user_id и заголовки session/agent ID.
Если сохранённого названия нет, источник первой реплики помечен отдельно.
Короткий кэш, лимиты чтения и очистка управляющих символов ограничивают обработку
метаданных. Хранилища других клиентов не открываются.

Контекст распространяется в asyncio и executor; очищается после HTTP-запроса,
включая keep-alive и ошибки проверки effort до запуска модели. Журнал показывает
читаемую подпись и структурированные поля. Локальная ошибка содержит stage,
параметр, модель и effort. Одинаковые request-id/x-request-id и JSON request_id
позволяют связать SDK-ошибку с журналом.

Upstream 400 и 422 сохраняют точное тело, текст отказа и метаданные с уникальным
diagnostic_id. Заголовки авторизации не сохраняются. Параллельные отказы в одну
секунду не перезаписывают файлы. Снимки содержат переписку и остаются локально.
Ошибка записи снимка не подменяет исходную ошибку провайдера.

Для запуска текущего checkout с прежними секретами добавлены --config/--state-dir.
Ключ берётся рядом с выбранным конфигом; основной checkout не переключался.
Обновлённый relay работает на `127.0.0.1:8787`; журнал и relay.log расположены в
`C:\Users\Redmi\AppData\Local\Proxi\state`. Супервизор запускается скриптом
`scripts/run_relay.ps1` с процессным UTF-8.

## Проверки

Команды выполнены из рабочего checkout:

```text
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests -o addopts= -q --tb=short
1002 passed in 127.63s

node --test tests/test_opencode_labels.mjs
2 passed, 0 failed

python scripts/probe_client_settings.py claude
passed: true; 1 subagent spawned/completed; parent and child effort high

python scripts/probe_client_settings.py opencode
passed: true; Opus parent + Sonnet Explore child high; both chat names resolved

python scripts/probe_client_settings.py opencode2
passed: true; Opus parent + Sonnet Explore child high; distinct child/parent IDs
and both names resolved from session_v2

git diff --check
exit 0
```

CLI-пробы используют реальные установленные клиенты и их пользовательские
настройки с временной заменой endpoint/key на локальный имитатор. Родитель
запускает дочернего агента; модель не выдаёт вызовов файловых или shell-инструментов.
Claude-проба намеренно задаёт дочернему агенту xhigh: исходящий запрос всё равно
содержит high благодаря настройке окружения. Эти проверки не расходуют кредиты
провайдера; тестовые чаты OpenCode сохраняются клиентом.

Живые проверки локального relay через /v1/messages с output_config.effort=high:

| Модель | HTTP | Текст | Завершение | Request ID |
| --- | --- | --- | --- | --- |
| Opus 5.5 | 200 | PROXI_LIVE_OK | end_turn | f6a01c10a49c |
| Sonnet 5.5 | 200 | PROXI_LIVE_OK | end_turn | 1636cc97b207 |

Локальный отрицательный запрос xhigh вернул 400 до upstream; request-id,
x-request-id и request_id совпали (`b0adbcc6acd2`), журнал содержит название чата
и точное объяснение разрешённых значений.

Проверка окна Claude Desktop была остановлена пользователем клавишей Escape;
последующие UI-действия не выполнялись. При этом серверный журнал независимо
зафиксировал реальные запросы Desktop из именованного чата: Opus 5.5, effort high,
HTTP 200 (например, `11531cb63036`, `78e2022ab2ae`). Название получено из
claude-custom-title, без чтения содержимого переписки для отчёта.

## Предел ретроспективной диагностики

Старая Claude-сессия содержит явный отказ provider-scoped resource. Более поздние
старые записи relay содержат только Invalid request format; отклонённые тела для
них не сохранились. Утверждать, что у всех этих 400 была одна причина, нельзя.
Новая корреляция и снимки предназначены для точного разбора следующих отказов.
