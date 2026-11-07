# Доступность маршрутов моделей — 2026-11-05

Проверены реальные ответы на синтетические вопросы: арифметика, разворот слова и кириллица. Из текущего настроенного пула доступны 27 канонических моделей; каждая из них вернула правильный ответ. Это проверка маршрута, а не всех возможностей модели, максимального окна или постоянной доступности каждого аккаунта.

В реестре сохранены 40 записей с метаданными. Публичные `/v1/models` и `/codex/models` скрывают неподдерживаемые маршруты и модели с невыполненными условиями. Админский API сохраняет запись и `route_reason` для диагностики. Баланс не используется для удаления модели из каталога.

`route_checked_at` стоит только у записей, чей маршрут действительно проверялся живым запросом (34 из 40). У шести Gemini проб не было: у них `route_status: unverified` и нет даты проверки.

| Model ID | Проверка текущего маршрута | Текущий пул |
| --- | --- | --- |
| gpt-6.1-sol | Ответ подтверждён: sandpack | публикуется |
| gpt-6.1-terra | Ответ подтверждён: sandpack | публикуется |
| gpt-6-luna | Unavailable on the Retool React route; Assist rejects this model. | скрыт |
| gpt-6-sol | Ответ подтверждён: sandpack | публикуется |
| gpt-6-astra | Requires a suitable BYOK provider resource. | скрыт |
| gpt-5 | Ответ подтверждён: assist | публикуется |
| gpt-5-mini | Ответ подтверждён: assist | публикуется |
| gpt-5-nano | Ответ подтверждён: assist | публикуется |
| gpt-5.1 | Ответ подтверждён: assist | публикуется |
| gpt-5.2 | Ответ подтверждён: assist | публикуется |
| gpt-5.3-codex | Retool Assist uses an upstream Chat endpoint this model rejects; Responses is required. | скрыт |
| gpt-5.4 | Ответ подтверждён: sandpack | публикуется |
| gpt-5.5 | Ответ подтверждён: sandpack | публикуется |
| gpt-5.6-sol | Ответ подтверждён: sandpack | публикуется |
| gpt-5.6-terra | Ответ подтверждён: sandpack | публикуется |
| gpt-5.6-luna | Ответ подтверждён: sandpack | публикуется |
| gpt-4.1 | Ответ подтверждён: assist | публикуется |
| claude-opus-5-5 | Ответ подтверждён: sandpack | публикуется |
| claude-opus-5 | Ответ подтверждён: sandpack, low; другие маленькие пробы иногда thinking-only | публикуется |
| claude-opus-4-8 | Ответ подтверждён: sandpack | публикуется |
| claude-opus-4-7 | Ответ подтверждён: sandpack | публикуется |
| claude-opus-4-20250514 | Снят Anthropic 2026-06-15; Retool: 400 «not available right now» и 422 «not available when using Retool's managed key for Assist» (проверка 2026-11-05) | скрыт (unsupported) |
| claude-opus-4-1-20250805 | Снят Anthropic 2026-08-05; Retool: те же 400 и 422 (проверка 2026-11-05) | скрыт (unsupported) |
| claude-opus-4-5-20251101 | Ответ подтверждён: sandpack | публикуется |
| claude-opus-4-6 | Ответ подтверждён: sandpack | публикуется |
| claude-fable-5 | Requires BYOK or enabled Anthropic resource data retention. | скрыт |
| claude-sonnet-5-5 | Ответ подтверждён: sandpack | публикуется |
| claude-sonnet-5 | Ответ подтверждён: sandpack | публикуется |
| claude-sonnet-4-20250514 | Снят Anthropic 2026-06-15; Retool Assist: provider 404 not_found | скрыт (unsupported) |
| claude-sonnet-4-5-20250929 | Ответ подтверждён: sandpack; повторно 2026-11-05 (HTTP 200, правильный ответ). Anthropic: deprecated 2026-09-30, отключение 2026-11-30 | публикуется до 2026-11-29, с 2026-11-30 скрыт автоматически |
| claude-sonnet-4-6 | Ответ подтверждён: sandpack | публикуется |
| claude-haiku-4-5-20251001 | Ответ подтверждён: assist | публикуется |
| gemini-3.1-pro-preview | Requires Google BYOK; проба не проводилась | скрыт |
| gemini-3-flash-preview | Requires Google BYOK; проба не проводилась | скрыт |
| gemini-3.5-flash | Requires Google BYOK; проба не проводилась | скрыт |
| gemini-3.1-flash-lite-preview | Requires Google BYOK; проба не проводилась | скрыт |
| gemini-2.5-pro | Requires Google BYOK; проба не проводилась | скрыт |
| gemini-2.5-flash | Requires Google BYOK; проба не проводилась | скрыт |
| zai-org/GLM-5.3 | Ответ подтверждён: sandpack | публикуется |
| zai-org/GLM-5.3-Flash | Ответ подтверждён: sandpack | публикуется |

## Снятые модели Anthropic

Anthropic отключила `claude-opus-4-20250514` (2026-06-15), `claude-opus-4-1-20250805` (2026-08-05) и `claude-sonnet-4-20250514` (2026-06-15): «Requests to retired models fail» ([страница Anthropic](https://platform.claude.com/docs/en/about-claude/model-deprecations)). В списке BYOK-моделей Retool их тоже нет ([AI models](https://docs.retool.com/data-sources/reference/ai-models)). Раньше Opus 4 и 4.1 были записаны как «нужен BYOK» с причиной, скопированной у Astra. Это ошибка: собственный ресурс эти модели не возвращает, и запросы падали.

Живая проверка 2026-11-05: по одному крошечному запросу на модель и на поверхность через `scripts/audit_models.probe`. Метод: прямой POST в Retool без продления сессии и без повторов, аккаунты читаются только на чтение, остановка при 401/403. Токены, cookie, заголовки и UUID ресурсов не печатались и не сохранялись.

| Модель | Поверхность | HTTP | Ответ Retool (отредактировано) |
| --- | --- | --- | --- |
| claude-opus-4-20250514 | App Builder (с маркером агента) | 400 | `claude-opus-4-20250514 is not available right now. Please try again later or select a different model.` |
| claude-opus-4-1-20250805 | App Builder | 400 | `claude-opus-4-1-20250805 is not available right now. Please try again later or select a different model.` |
| claude-opus-4-20250514 | Assist (без маркера) | 422 | `claude-opus-4-20250514 is not available when using Retool's managed key for Assist. Please configure your own API key.` |
| claude-opus-4-1-20250805 | Assist | 422 | `claude-opus-4-1-20250805 is not available when using Retool's managed key for Assist. Please configure your own API key.` |
| claude-sonnet-4-5-20250929 | App Builder | 200 | правильный ответ на синтетический вопрос, 92 входных и 22 выходных токена, 0.01212 кредита |

Оба Opus отвечали одинаково на обеих поверхностях. Сообщение Assist предлагает «свой ключ», но Anthropic сообщает, что запросы к снятым моделям завершаются ошибкой, поэтому BYOK-дескриптор модель не открывает: у маршрута `route_status: unsupported` и `route_reason` с датой снятия, а `provider_access` на него не влияет. Это вывод из документации Anthropic, а не отдельная проба с собственным ключом. В ответах с ошибкой не было `usage` и `creditsUsed`.

Sonnet 4.5 пока отвечает. Anthropic объявила deprecated 2026-09-30 и отключение 2026-11-30. У записи есть `deprecated_at` и `retires_at`, а `route_block_reason` начинает возвращать «Retired by Anthropic on 2026-11-30» в тот день (UTC): модель пропадает из `/v1/models` и `/codex/models` без перезапуска relay, а запрос к ней получает 404 с причиной.

## Условия доступа

`provider_access` хранится в записи конкретного аккаунта (`accounts[]` в `config.json`) и привязывается к точному `resource_name`:

```json
{
  "accounts": [
    {
      "account_id": "…",
      "openai_provider_resource_name": "YOUR_RESOURCE_UUID",
      "provider_access": {
        "openai": {"resource_name": "YOUR_RESOURCE_UUID", "byok": true}
      }
    }
  ]
}
```

Если у вас настоящий BYOK-ресурс (свой UUID), одного `openai_provider_resource_name` недостаточно: relay не угадывает, что ресурс собственный, и держит Astra закрытой, пока рядом нет дескриптора. Сообщение об отказе называет оба ключа и запись аккаунта. Например: «Requires a suitable BYOK provider resource. Add provider_access.openai = {"resource_name": "<value of openai_provider_resource_name>", "byok": true} to this account's entry of config.json». Подсказка зависит от состояния: нет ресурса вообще, ресурс `__managed__` (managed-ключи Retool никогда не считаются BYOK), `resource_name` дескриптора не совпадает с ключом ресурса.

Для Anthropic аналогично используется ключ `anthropic`; Fable может обслуживаться подходящим BYOK или ресурсом с подтверждённым `data_retention_enabled: true` (для managed-ресурса это единственный путь: `{"resource_name": "__managed__", "data_retention_enabled": true}`). Astra требует собственного OpenAI-ресурса: разрешение Anthropic retention его не открывает. Для текущих пяти аккаунтов обычный API включения retention вернул 403; Fable 5 и кандидат 5.1 остаются заблокированы. Astra и Fable — разные модели, связи с GLM не установлены.

Это локальное описание уже настроенного ресурса. Оно не включает права/feature flags на сервере, не заменяет реальную настройку BYOK и не доказывает успешный inference. Retool продолжает проверять доступ. Смена UUID делает прежний descriptor неприменимым; descriptors не наследуются между аккаунтами. Для Google нужен настоящий BYOK resource; `__managed__` не принимается.

Валидация при старте называет провайдера и поле (`account #1: provider_access.openai.byok must be a boolean`). `byok: true` вместе с `resource_name: "__managed__"` отклоняется: такой дескриптор не работал и вводил в заблуждение. Дескриптор в корне `config.json` при наличии `accounts[]` не применяется ни к одному аккаунту: relay его проверяет, но предупреждает при старте (stderr, баннер и журнал `relay.warning`) и просит перенести в запись аккаунта. Так же предупреждает дескриптор, чей `resource_name` не совпадает с ключом ресурса аккаунта.

`show_unrouted_models` может показать модель без ресурса для настройки, но не возвращает в публичный каталог подтверждённо несовместимые маршруты и не обходит BYOK/retention conditions.

## Ошибка, когда ни один аккаунт не может обслужить модель

Если все аккаунты отказывают модели по настройке (снята, неподдерживаемый маршрут, нужен BYOK или retention, нет ресурса провайдера), ответ — `404 not_found_error` с причиной (в формах OpenAI и Anthropic; для stream это настоящий HTTP 404 до начала потока, а не событие внутри `200`). 404 SDK не повторяют. `503 service_unavailable` остаётся для «сейчас нет пригодного аккаунта»: все выключены, единственный пригодный выключен, нужен новый вход. `402` — кредиты закончились.

## Проба effort (2026-11-05)

Вопрос: меняет ли `reasoningEffort` что-нибудь наблюдаемое на маршруте Retool. Метод: `scripts/probe_effort_effect.py`, прямые POST по поверхности модели (Assist без маркера агента для Haiku, gpt-5-mini и gpt-5-nano, App Builder для GLM), без продления сессии и без повторов. Один и тот же маленький счётный вопрос (ответ 208) на каждом уровне, затем трудный (сколько целых от 1 до 100000 имеют сумму цифр 25, ответ 5631). Записывались события thinking, число выходных токенов из `usage`, время и правильность. Всего 83 запроса с ответом HTTP 200, сообщено 3.49 кредита (Assist не присылает `creditsUsed`, поэтому для Haiku и GPT сумма неполная).

| Модель | Что показала проба | Решение |
| --- | --- | --- |
| claude-haiku-4-5-20251001 | События thinking: 0 на none/low/medium/high/max и без поля. Счётный вопрос: 207/224/364/231/195/255 токенов. Трудный: медианы 1286 (без поля), 1406 (none), 956 (low), 899 (high), 1235 (max), по три запуска, none против max p = 0.70. Модель рассуждает обычным текстом на любом уровне. Anthropic: «Default effort: Not supported», только ручной `budget_tokens` | `accepted-ignored`, уровней нет |
| zai-org/GLM-5.3 | Thinking приходит на каждом уровне, включая запрос без поля. Трудный вопрос, по 8 запусков на low и max: токены 322 и 364 (p = 0.34), символы thinking 682 и 827 (p = 0.13), время 22.2 и 21.5 с (p = 0.89). Правильных ответов 4 из 8 и 7 из 8 (точный тест Фишера p = 0.28) | `accepted-ignored`, уровней нет |
| zai-org/GLM-5.3-Flash | Те же 8 запусков: токены 307 и 320 (p = 0.75), символы thinking 756 и 759 (p = 0.97), время 4.5 и 3.3 с (p = 0.15), правильных по 7 из 8 | `accepted-ignored`, уровней нет |
| gpt-5-mini | Выходные токены растут с уровнем: медианы 170 (low), 394 (medium), 586 (high); время 3.4, 6.0 и 6.9 с. Каждый запуск low короче и быстрее каждого запуска high (138–202 токена против 458–714). События thinking Assist не присылает | уровни low/medium/high остаются выбираемыми |
| gpt-5-nano | Медианы 234, 490 и 842 токена, время 3.3, 5.2 и 9.3 с; low (202–266) против high (522–1162) без пересечения | уровни low/medium/high остаются выбираемыми |

Решение для первых трёх моделей: значение принимается и игнорируется. `reasoning_effort_control: "accepted-ignored"`, опубликованные уровни пусты и поэтому не попадают в `/v1/models`, `/codex/models` и каталоги OpenCode, Harness, Desktop. Любое известное значение (`none` … `max`, включая `xhigh`) и `thinking.budget_tokens` не дают 400: Claude Code шлёт для Haiku `thinking: {type: enabled, budget_tokens}`, а клиенты копируют один уровень на все модели. Значение в Retool не уходит; имя параметра перечисляется в `x-proxi-ignored-parameters` и журнале `request.compatibility` (`reasoning_effort`, `reasoning.effort`, `output_config.effort`, `thinking.budget_tokens` и т. д.), само значение нигде не пишется. Значение, которого не определяет ни один провайдер (`extra`), по-прежнему даёт 400. Haiku в каталоге помечен `reasoning: false`: на этом маршруте он никогда не присылал thinking, поэтому `thinking: {type: disabled}` честно выполняется и в игнорируемые не попадает.

Ограничения вывода. Для GLM-5.3 доля правильных ответов на трудном вопросе выше на max (7 из 8 против 4 из 8), но при восьми запусках это не отличимо от случайности. Если нужна уверенность, нужна большая выборка; вернуть выбираемые уровни GLM можно, убрав модель из `_EFFORT_IGNORED` в `models.py`. Для gpt-5-mini и gpt-5-nano по два запуска на уровень: монотонный рост и полное разделение low и high видны, но выборка малая. Уровень `minimal` не проверялся.

## Повторяемость и цена

Четыре прогона дали 51 строку результата: 45 upstream POST и 6 пропусков без Google-ресурса. Известная сумма сообщённых цен — 0.34952 кредита; отсутствие creditsUsed в Assist не означает бесплатность. Тесты используют отдельные синтетические диалоги, без команд хоста, project context, keepwarm и изменения cookies.

Приватные evidence-файлы: `.agents/model-audit-20261104/stage5-*.jsonl`. Они не входят в PR. До изменения каталога независимый reviewer подтвердил различие между неисправным endpoint, условиями ресурса и временными/пустыми ответами.

## Обновление OpenCode

`scripts/sync_opencode_catalog.py` генерирует общий legacy JSON catalog для OpenCode 1; установленный OpenCode 2 мигрирует тот же файл при загрузке. Утилита меняет только namespace `retool-relay`, его фильтры и ссылки на relay-модели в корне/агентах. Другие провайдеры, auth и прочие настройки сохраняются; валидные effort предпочтения нормализуются из поддержанных aliases.

Предпросмотр не меняет конфигурацию:

```powershell
python scripts/sync_opencode_catalog.py --output PRIVATE_PREVIEW.json
```

Для записи нужны `--write --backup-dir PRIVATE_BACKUP_DIR`. На Windows используется ReplaceFileW с резервной копией фактически вытеснённой версии на том же томе. Обнаруженная поздняя конкурентная правка вызывает ошибку и сохраняет recovery-копии; при возможности восстанавливается пользовательский файл. Это не обещание CAS-блокировки внешнего редактора. На других ОС консервативный fallback сохраняет старую версию и публикует файл только на свободное имя; между операциями возможен короткий промежуток без файла.

Изолированные профили обеих установленных CLI показали ровно 27 настроенных и 27 перечисленных моделей. Это проверка загрузки каталога; фактические IDE/tool сценарии относятся к следующему этапу.

После этой проверки общий установленный конфиг OpenCode обновлён с резервной
копией в `.agents/backups/opencode-catalog-20261105/`. Сравнение с копией
подтвердило сохранение других провайдеров, подключения relay и прав агентов.
Установленные OpenCode 1.19.3 и 2.2.5 также прошли локальные тестовые диалоги
с продолжением после вызова агента: по три запроса с `high`, каталог 27/27.
Эти проверки используют локальный сервер без inference и расхода кредитов.
Полные команды и границы проверки описаны в
[итоговом отчёте](model-client-audit-2026-11-04.md#stage-6-installed-cli-acceptance-and-final-validation).
