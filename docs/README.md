# Документация

Главное руководство — [README.md](../README.md) в корне: установка, настройки, админка, коды ответов, ограничения.
Протокол Retool — [PROTOCOL.md](../PROTOCOL.md).

## Руководства и устройство

| Документ | О чём |
|---|---|
| [client-setup.md](client-setup.md) | Настройка клиентов под relay: Codex, Claude Code, Claude Desktop, OpenCode, DeepSeek Harness |
| [prompt-control.md](prompt-control.md) | Управление системным промптом через `prompts.json`: профили, правила, правки истории и tools |
| [prompt-control-plan.md](prompt-control-plan.md) | План работ по управлению промптом (5 ноября 2026) |
| [direct-transport.md](direct-transport.md) | Прямой транспорт Retool и кэш контекста: устройство и замеры |
| [reasoning-visibility.md](reasoning-visibility.md) | Почему блок thinking бывает пустым и что с этим делать |
| [relay-quality-and-prompts.md](relay-quality-and-prompts.md) | Качество relay, системные инструкции и возможности Retool |
| [conversation-state-2026-11-06.md](conversation-state-2026-11-06.md) | Как состояние диалогов переживает перезапуск relay |
| [admin-design-2026-11-05.md](admin-design-2026-11-05.md) | Направление дизайна админки |

## Отчёты, аудиты и ревью ([reports/](reports/))

Исторические документы: что проверяли, что нашли и что решили на дату в названии. Код мог уйти дальше.

| Дата | Документ | О чём |
|---|---|---|
| 2026-10-29…11-05 | [model-catalog-audit.md](reports/model-catalog-audit.md) | Аудит каталога моделей `models.py` и источники его данных |
| 2026-10-31 | [direct-inference-audit.md](reports/direct-inference-audit.md) | Исследование backend Retool, с которого начался прямой транспорт |
| 2026-11-01 | [review-and-merge-2026-11-01.md](reports/review-and-merge-2026-11-01.md) | Ревью и мерж стека PR #1–#14 |
| 2026-11-02 | [proxi-focused-design-2026-11-02.md](reports/proxi-focused-design-2026-11-02.md) | Авторизация пула аккаунтов и обрывы долгих генераций |
| 2026-11-03 | [auth-effort-fixes-2026-11-03.md](reports/auth-effort-fixes-2026-11-03.md) | Изоляция продления сессий, маршрутизация, keepwarm и effort |
| 2026-11-03 | [client-settings-and-diagnostics-2026-11-03.md](reports/client-settings-and-diagnostics-2026-11-03.md) | Настройка локальных клиентов и диагностика ошибок |
| 2026-11-03 | [console-logs-2026-11-03.md](reports/console-logs-2026-11-03.md) | Цветные консольные логи и баланс |
| 2026-11-03 | [empty-turn-investigation-2026-11-03.md](reports/empty-turn-investigation-2026-11-03.md) | Пустой ход Retool после чтения истории OpenCode |
| 2026-11-03 | [empty-turn-recovery-2026-11-03.md](reports/empty-turn-recovery-2026-11-03.md) | Восстановление пустого хода (отозванный подход) |
| 2026-11-03 | [exact-opus-wire-replay-2026-11-03.md](reports/exact-opus-wire-replay-2026-11-03.md) | Точный повтор пустого ответа Opus |
| 2026-11-03 | [prompt-integrity-regression-2026-11-03.md](reports/prompt-integrity-regression-2026-11-03.md) | Подмена задачи при восстановлении: разбор и отмена |
| 2026-11-03 | [review-pr18-pr19-2026-11-03.md](reports/review-pr18-pr19-2026-11-03.md) | Построчное ревью PR #18 и #19 |
| 2026-11-03 | [service-request-isolation-2026-11-03.md](reports/service-request-isolation-2026-11-03.md) | Изоляция служебных запросов и учёт прогрева |
| 2026-11-04 | [model-client-audit-2026-11-04.md](reports/model-client-audit-2026-11-04.md) | Аудит моделей и клиентов на реальном маршруте Retool |
| 2026-11-04 | [thinking-latency-2026-11-04.md](reports/thinking-latency-2026-11-04.md) | Долгое thinking после результатов инструментов |
| 2026-11-05 | [model-route-availability-2026-11-05.md](reports/model-route-availability-2026-11-05.md) | Доступность маршрутов моделей |
| 2026-11-05 | [thinking-only-replies-2026-11-05.md](reports/thinking-only-replies-2026-11-05.md) | Ответы «только thinking» и пустые ответы Claude 5.5 |
| 2026-11-06 | [bug-summary-2026-11-06.md](reports/bug-summary-2026-11-06.md) | Сводка ошибок relay и клиентов после живых прогонов |
| 2026-11-07 | [fixes-2026-11-07.md](reports/fixes-2026-11-07.md) | Уборка проекта, структура и исправленные ошибки: что, где и почему |
