# Доступность маршрутов моделей — 2026-11-05

Проверены реальные ответы на синтетические вопросы: арифметика, разворот слова и кириллица. Из текущего настроенного пула доступны 27 канонических моделей; каждая из них вернула правильный ответ. Это проверка маршрута, а не всех возможностей модели, максимального окна или постоянной доступности каждого аккаунта.

В реестре сохранены 40 записей с метаданными. Публичные `/v1/models` и `/codex/models` скрывают неподдерживаемые маршруты и модели с невыполненными условиями. Админский API сохраняет запись и `route_reason` для диагностики. Баланс не используется для удаления модели из каталога.

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
| claude-opus-4-20250514 | Requires a suitable BYOK provider resource. | скрыт |
| claude-opus-4-1-20250805 | Requires a suitable BYOK provider resource. | скрыт |
| claude-opus-4-5-20251101 | Ответ подтверждён: sandpack | публикуется |
| claude-opus-4-6 | Ответ подтверждён: sandpack | публикуется |
| claude-fable-5 | Requires BYOK or enabled Anthropic resource data retention. | скрыт |
| claude-sonnet-5-5 | Ответ подтверждён: sandpack | публикуется |
| claude-sonnet-5 | Ответ подтверждён: sandpack | публикуется |
| claude-sonnet-4-20250514 | Retool Assist returns provider 404 not_found for this model. | скрыт |
| claude-sonnet-4-5-20250929 | Ответ подтверждён: sandpack | публикуется |
| claude-sonnet-4-6 | Ответ подтверждён: sandpack | публикуется |
| claude-haiku-4-5-20251001 | Ответ подтверждён: assist | публикуется |
| gemini-3.1-pro-preview | Requires Google BYOK; no configured route was tested. | скрыт |
| gemini-3-flash-preview | Requires Google BYOK; no configured route was tested. | скрыт |
| gemini-3.5-flash | Requires Google BYOK; no configured route was tested. | скрыт |
| gemini-3.1-flash-lite-preview | Requires Google BYOK; no configured route was tested. | скрыт |
| gemini-2.5-pro | Requires Google BYOK; no configured route was tested. | скрыт |
| gemini-2.5-flash | Requires Google BYOK; no configured route was tested. | скрыт |
| zai-org/GLM-5.3 | Ответ подтверждён: sandpack | публикуется |
| zai-org/GLM-5.3-Flash | Ответ подтверждён: sandpack | публикуется |

## Условия доступа

`provider_access` хранится в записи конкретного аккаунта и привязывается к точному `resource_name`:

```json
{
  "openai_provider_resource_name": "YOUR_RESOURCE_UUID",
  "provider_access": {
    "openai": {"resource_name": "YOUR_RESOURCE_UUID", "byok": true}
  }
}
```

Для Anthropic аналогично используется ключ `anthropic`; Fable может обслуживаться подходящим BYOK или ресурсом с подтверждённым `data_retention_enabled: true`. Astra требует собственного OpenAI-ресурса: разрешение Anthropic retention его не открывает. Для текущих пяти аккаунтов обычный API включения retention вернул 403; Fable 5 и кандидат 5.1 остаются заблокированы. Astra и Fable — разные модели, связи с GLM не установлены.

Это локальное описание уже настроенного ресурса. Оно не включает права/feature flags на сервере, не заменяет реальную настройку BYOK и не доказывает успешный inference. Retool продолжает проверять доступ. Смена UUID делает прежний descriptor неприменимым; descriptors не наследуются между аккаунтами. Для Google нужен настоящий BYOK resource; `__managed__` не принимается.

`show_unrouted_models` может показать модель без ресурса для настройки, но не возвращает в публичный каталог подтверждённо несовместимые маршруты и не обходит BYOK/retention conditions.

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
