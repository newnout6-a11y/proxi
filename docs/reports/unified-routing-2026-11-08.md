# Единая маршрутизация Retool и Minara — 8 ноября 2026 года

## Запрос и текущая граница результата

Пользователю нужен не второй список моделей с `minara/`, а единый пул под привычными
именами: основной диалог может обслуживаться Retool, а новый субагент — Minara;
повторные запросы каждого должны возвращаться на свой маршрут, не теряя историю.

Реализованы явное отображение Minara upstream ID на общий `public_id`, выбор
совместимого backend, независимое распределение новых диалогов/субагентов,
устойчивая привязка и её дисковое восстановление. Прежние публичные имена моделей
и клиентский URL relay остаются прежними. Отдельные Minara-слоты для отображённых
моделей в discovery не публикуются.

**Рабочий аккаунт добавлен через настоящий admin API**, но наблюдённый ответ
содержит `pending: added` и `restart_required: true`. На момент подготовки отчёта
запущенный рабочий процесс ещё не перезапускался участниками этого этапа и не
подхватил новую маршрутизацию. Поэтому временная live-приёмка нового кода не
выдаётся за проверку уже активированной рабочей инсталляции. Финальную активацию,
проверку реального admin после неё и коммит выполняет основной агент отдельно.

## Что сохраняется и чего обещать нельзя

- Публичная модель отделена от фактического маршрута и credentials аккаунта.
  После выбора backend используются именно его upstream ID, effort, tool schema
  и provider-specific правила, а не объединённые публичные метаданные.
- Явные tools, vision, effort, verbosity, output limit и forced tool choice
  ограничивают подходящие маршруты. Неподдержанный параметр не должен молча
  превращаться в другой effort или запрос к иной семантической модели.
- Собственный conversation ID удерживает свой backend/маршрут. Новый subagent ID
  выбирается независимо; parent ID защищает родительский кэш от вытеснения,
  но не заставляет ребёнка работать на том же провайдере.
- Minara account binding теперь сохраняется даже без signed history/cache key.
  Binding-only state не объявляет существование кэша, не содержит токен и не
  переносит чужие непрозрачные подписи.
- При вынужденном переходе между Retool и Minara сохраняется каноническая история
  клиента, tool call/result и изображения. Signed thinking, cache marks и warm
  body между разными upstream не переносятся даже при включённом старом opt-in
  `account_move_keeps_history`.
- **Общий KV-cache между Retool и Minara не существует как доказанный контракт.**
  Стабильная affinity и prefix помогают сохранять provider-local cache; его hit
  подтверждается только upstream telemetry. В заключительной общей live-приёмке
  cache counters были нулевыми, поэтому cache HIT не заявляется.

## Этапы агентов и находки

Сначала отдельные разработчики изучили модели/валидацию, session/state и живой
каталог. Затем реализовали общий registry и binding; после этого другие участники
проверяли код и пытались воспроизвести пограничные случаи. Подтверждённые находки
передавались независимому проверяющему, затем исправителям и обратно на проверку.

| Находка | Исправление / проверенная граница |
|---|---|
| `route_bound` считался началом клиентского вывода | Внутренний binding event не запрещает существующий безопасный failover до видимого ответа |
| Minara с общим Claude ID получала Retool output-cap emulation | Эмуляция учитывает фактический upstream; RouteBoundAdapter заменяет enforcement при повторном binding |
| Оригинальный client effort перекрывал effort prompt-профиля | Bound route использует effective control; requirements обновляются после profile generation |
| Provider-prefixed имя обходило unified registry | `anthropic/claude-opus-5-5` разрешается через тот же cfg-owned общий слот |
| Конкретный Minara route получал Retool substitute/effort downgrade | Retool substitution не применяется к concrete Minara route |
| Metadata/schema `{type: image}` ошибочно включали vision gate | Изображения определяются по каноническим message/tool-result блокам |
| Minara binding исчезал без signed branches/cache key | Добавлено ограниченное binding-only состояние и реальное HTTP/disk/restart покрытие |
| Переход между upstream мог переносить signed/cache state | Явный запрет cross-upstream carry; каноническая история остаётся полной |
| Journal оценивал Minara category costs по тарифам Retool | Оценки используют фактический pricing route; неизвестные категории Minara остаются null |
| Проверка compaction peek слишком рано скрывала другой model scope | Сохранена диагностическая проверка scope без небезопасного восстановления чужого state |

Одна находка ревью о записи `_bound_route` только на SessionLease **отозвана как
ложная**: в коде уже было раскрытие lease в underlying session. Исправление на
основании этой находки не делалось. Последующая реальная HTTP-проба и отдельные
restart-тесты подтвердили фактический binding на RelaySession. Аналогично ранняя
profile-проба не дождалась reload интервала; её HTTP-вывод отозван и заменён
корректной проверкой с подтверждённой загрузкой профиля.

## Проверки кода

Полный прогон после основных исправлений:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest -n 4 -o addopts= -q --tb=short
```

Наблюдённый результат предшествующего полного лога: **9987 passed, 2 skipped,
3 warnings**, 70,31 с. Заключительный полный прогон после последних дополнений
на момент написания ещё выполнялся; его результат должен быть внесён основным
агентом перед объявлением завершения. Начальный прогон дал шесть отказов; он не
скрывается и не выдаётся за успешный.

Наблюдённые целевые команды:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_unified_models.py tests/test_unified_clients.py tests/test_model_substitution.py tests/test_validation.py tests/test_minara_integration.py -o addopts= -q --tb=short
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_unified_models.py tests/test_unified_clients.py tests/test_unified_affinity.py tests/test_compaction_cache.py tests/test_validation.py tests/test_minara_catalog_scope.py tests/test_model_substitution.py -o addopts= -q --tb=short
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests/test_unified_disk_http.py -o addopts= -q --tb=short
```

Результаты: **92 passed**, **115 passed**, **3 passed** соответственно. Root также
сообщил недавний целевой набор **97 passed**; этот результат не подменяет полный
suite и без сохранённой здесь точной команды не используется как его доказательство.
Ruff и diff-check для принадлежащих разработчикам файлов прошли.

`test_unified_clients.py` проверяет реальный loopback HTTP и реальный Minara SSE
transport с подменённым POST: один public ID во всех трёх API-диалектах, родитель
на Retool и ребёнок на Minara, последующий tool-result ребёнка на том же backend,
восемь конкурентных детей, единственный discovery slot и namespaced MCP schema.
`test_unified_disk_http.py` проверяет три диалекта через настоящий runner/store:
физический `.state`, остановку Runtime, изменение приоритетов и новый Runtime,
после которого возвращается тот же Minara account/upstream ID. Это пересоздание
Runtime/store в одном interpreter, не имитация аварийного выключения ОС.

## Рабочий аккаунт и admin

До изменения authenticated admin показал 24 аккаунта Retool и ноль Minara.
Через штатный POST `/admin/api/accounts` добавлена одна запись Minara; последующий
GET показал **25 аккаунтов**, из них 24 прежних и один Minara pending. Настройки
identity/enabled/priority/label прежних записей сохранены. Secret в serialized
ответе и events не обнаружен; намеренное credential поле находится только в
игнорируемом рабочем config. Токен в этот отчёт, исходники и Git не включён.

Добавлены только два проверенных отображения:

| Public ID | Фактический Minara ID | Проверенные controls |
|---|---|---|
| `gpt-6.1-sol` | `openai/gpt-6.1-sol` | high, tools, named forced choice, продолжение |
| `claude-opus-5-5` | `anthropic/claude-opus-5.5` | high, auto tools, продолжение; `forced_tool_choice: false` |

Context, output ceiling и vision для этих маршрутов не были получены из каталога
и не выдуманы. Локальный каталог содержит больше возможных сопоставлений, но они
не активированы только на основании похожего имени. Minara на Opus отвергла
named forced tool choice HTTP 400; auto/high/system/strict schema и continuation
позже прошли. False capability позволяет не выбирать этот backend для такого
явного запроса; это не обещание native forced support.

## Живая приёмка общих публичных имён

Отдельный временный subprocess relay на случайном loopback-порту получил
Minara-only конфигурацию с общими `public_id`; рабочий процесс не затрагивался.
Выполнены **четыре платных generation POST**, без автоматических повторов:

| Public ID | Сценарий | Результат |
|---|---|---|
| `gpt-6.1-sol` | high, strict echo schema, named forced, max 1024 | HTTP 200, полный echo tool call |
| `gpt-6.1-sol` | тот же affinity, канонический tool-result | HTTP 200, `OK`, stop |
| `claude-opus-5-5` | high, strict echo schema, auto, max 1024 | HTTP 200, полный echo tool call |
| `claude-opus-5-5` | тот же affinity, канонический tool-result | HTTP 200, `OK`, stop |

Discovery показал ровно два обычных public ID, без `minara/` слотов; response model
остался публичным. GPT cost не сообщён и остался null; Opus сообщил отдельные
стоимости с неизвестной валютой. Они не суммируются как USD и не превращаются в
Retool credits. Cache counters были нулевыми. Временный процесс завершён и каталог
очищен. Предшествующие диагностические capability-пробы описывают отдельный
этап и не входят в эти четыре приёмочных POST.

## Ограничения

- Отчёт не доказывает прозрачную взаимозаменяемость всего каталога: активированы
  лишь указанные явные mappings и проверенные возможности.
- Физический кэш разных провайдеров не переносим; при forced failover возможен
  честный cold replay, но не потеря канонической истории.
- Реальные установленные клиенты были проверены на предшествующем этапе
  [Minara-интеграции](minara-integration-2026-11-08.md). Новый same-public-ID слой
  отдельно проверен HTTP/MCP/concurrency/restart тестами и указанной live-приёмкой;
  эти доказательства не смешиваются в заявление о каждом IDE-сценарии.
- На момент отчёта рабочий admin account ещё pending до контролируемого restart.
  Активация и финальный рабочий smoke должны быть подтверждены отдельно, а не
  предполагаться из факта сохранения config.

## Итоговая проверка исходников

`uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest -n 4 -o addopts= -q --tb=short` — **9994 passed, 2 skipped, 3 warnings**, 72,15 с. Два предупреждения относятся к тестовому broken stdout, одно — curl chunk_size. Рабочий restart пока не выполнен: обнаружены активные запросы; принудительно обрывать их ради активации нельзя.


## Активация и проверка после пользовательского перезапуска

8 ноября 2026, после штатного запуска пользователем: рабочий admin API подтвердил
25 аккаунтов, Minara `pending: null`, `enabled: true`, `routable: true`.
`/v1/models` — 27 моделей, ровно один `gpt-6.1-sol` и один `claude-opus-5-5`,
ноль дополнительных `minara/` пунктов. Это configured readiness, не замена live generation.
В логе восстановлена Retool-история и сообщено чтение 425450 токенов кэша.
Два HTTP 400 отдельно проверены по журналу: задачи памяти Codex отключены
операторским `codex_memory_jobs: false`; генерация для них не запускалась.

Строки `max -> high` выявили ошибку speculative candidate checking: старая
`allow_effort_downgrade: true` применялась при проверке Minara-кандидата.
Фактический показанный ход Retool остался на max, но будущий выбор high-only
Minara мог ослабить запрос. Исправлены чистая строгая eligibility-проверка,
публичная unified resolution и post-acquire binding: явный max не понижается
ради другого upstream. Legacy standalone совместимость сохраняется отдельно.
Три HTTP-диалекта многократно подтвердили выбор Retool для max при high-only
Minara и включённой старой настройке. Независимый focused-прогон — 55 passed.

Полный прогон после effort-исправления:
`uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest -n 4 -o addopts= -q --tb=short`
— **10001 passed, 2 skipped, 3 warnings**, 66,98 с. Ruff и diff-check прошли.
Этот маленький follow-up требует очередной загрузки кода запущенным процессом;
самостоятельный принудительный restart не выполнялся.
