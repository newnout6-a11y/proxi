# Изоляция refresh, маршрутизация, keepwarm и effort

Дата проверки: 3 ноября 2026. Изменения продолжают PR #18, исходный commit —
`a8002c31b9077308f8f840d44bd604eccdbb492a`. Credentials не включены в результаты.

## Реализовано

- `config.load()` назначает и сохраняет стабильные уникальные `account_id`
  старым записям. Reload/persist выбирают точный ID и проверяют tenant. Старый
  runtime без ID допускает только однозначное совпадение tenant+page; fallback
  на глобальную авторизацию и выбор первой совпавшей страницы удалены.
- Read–check–merge–replace сериализуется общей блокировкой для refresh,
  миграции и admin settings. Sidecar использует OS lock на Windows/POSIX;
  файл блокировки сохраняется, чтобы не разделить писателей между inode.
- Один account_id в одном конфигурационном файле имеет одного владельца
  сетевого refresh между процессами. Следующий процесс подхватывает сохранённую
  ротацию. Общая блокировка конфигурации во время сетевого запроса не удерживается.
- Новая сессия выбирается среди аккаунтов с ресурсом нужной модели. Ресурсы
  другого tenant не наследуются. Параллельный дочерний запрос остаётся на аккаунте
  беседы. У существующей беседы без нужного маршрута возвращается ошибка;
  неявного переноса в другой tenant нет.
- Активный ход получает первый keepwarm на настроенном offset, последующие —
  через `min(cache_keepwarm_seconds, 240)` секунд от начала ping. Запросы
  последовательны, ошибки используют backoff; idle-лимит не прерывает активный
  ход. Effort сохраняется. Оборванный ping считается ошибкой, cache hit/write
  заявляется только при соответствующей телеметрии. Неизвестная стоимость
  хранится как оценка отдельно от подтверждённых списаний.
- Неподдерживаемый effort отклоняется до upstream. Явный
  `allow_effort_downgrade: true` включает прежнее приближение известных уровней;
  `extra`/`ultra` не угадываются. Журнал фиксирует requested/effective effort
  и причину. Недопустимый настроенный default проверяется при загрузке.

## Локальные проверки

Windows, CPython 3.14.3, зависимости из `requirements.txt`:

```powershell
uv run --python 3.14 --with-requirements requirements.txt --no-project python -m pytest tests -o addopts= -q --tb=short
git diff --check
```

Финальный результат: **984 passed in 133.13s**, whitespace check без ошибок.
Весь suite использует offline/localhost fixtures; live-пробы запускаются отдельно.

Регрессии включают общие tenant/page при разных аккаунтах, перестановку записей,
дубликаты/пропавшие ID/изменённый tenant, одновременное сохранение разных ротаций
потоками и процессами, один сетевой refresh одного аккаунта между процессами,
незанятую во время сети конфигурацию, освобождение слотов при отсутствии маршрута,
виртуальные 900/1800-секундные ходы, backoff, truncated ping, строгий effort во всех
трёх API и явную совместимость, а также закрытие временного HTTP-сервера и журнала
диагностического CLI на Windows. Admin settings применяют ту же явную политику
совместимости и запрещают выключать её, оставляя неподдерживаемый default.

## Live: сырой маршрут Retool

Исходный конфиг читался напрямую как JSON, без миграции или сохранения. Пробы не
вызывают refresh token. Модель — `claude-opus-5-5`, существующий managed resource,
короткий синтетический prompt, лимит ответа 1024 токена.

```powershell
python scripts/probe_effort.py --config C:\specialinay_papka\config.json --efforts high xhigh max extra --timeout 90
```

| Sent effort | HTTP | end_of_stream | Ответ | creditsUsed |
|---|---|---|---|---|
| high | 200 | да | OK | 0.00304 |
| xhigh | 422 | нет | malformed input | — |
| max | 200 | да | OK | 0.01344 |
| extra | 422 | нет | malformed input | — |

Команда завершилась с кодом 0. Это подтверждает приём/отклонение переданного
параметра этим маршрутом. Ответ не содержит независимого подтверждения внутреннего
режима вычислений провайдера; сравнение скорости/расходов не заменяет такого подтверждения.

## Live: HTTP boundary этого checkout

```powershell
python scripts/probe_effort.py --config C:\specialinay_papka\config.json --gateway-check --efforts high max xhigh extra --timeout 90
```

Для каждого из Chat Completions, Responses, Anthropic:

- `high`: HTTP 200, перехваченный исходящий `reasoningEffort` равен `high`;
- `max`: HTTP 200, исходящий параметр равен `max`;
- `xhigh` и `extra`: локальный HTTP 400, список upstream-запросов пуст.

Всего 12 HTTP-проверок, финальный запуск завершился с кодом 0. Сервер использует
случайный localhost-порт и временный ключ; основной relay не перезапускался.
Проверка `--gateway-check --compatibility --dialects chat --efforts xhigh`
вернула HTTP 200 и отправила upstream `high`, как явно разрешает настройка.
Первый запуск CLI обнаружил открытый journal handle при очистке Windows temp;
закрытие исправлено и покрыто offline-тестом и финальным live-запуском.

## Live: кэш при max без понижения effort

```powershell
python scripts/probe_effort.py --config C:\specialinay_papka\config.json --efforts max --cache-check --timeout 90
```

Синтетический system-prefix одинаков во всех трёх запросах:

| Фаза | Cache write | Cache read | creditsUsed | end_of_stream |
|---|---|---|---|---|
| Seed | 14419 | — | 1.44478 | да |
| Реальный `direct._ping`, max | — | 14419 | 0.060556 | да |
| Следующий запрос, max | — | 14419 | 0.096156 | да |

Команда завершилась с кодом 0. Проверена совместимость конкретного префикса,
маршрута и effort. Длительное удержание кэша на реальных 900/1800 секундах не
измерялось: длительность планировщика проверена виртуальным временем.

## Дополнительная live-проверка GPT-6

После проверки Opus выполнены реальные пробы GPT, обнаружившие расхождение
старого bundle-каталога с текущим backend:

```powershell
python scripts/probe_effort.py --config C:\specialinay_papka\config.json --model gpt-6-sol --efforts xhigh max --timeout 90
foreach ($effortProbeModel in @('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-luna')) {
  python scripts/probe_effort.py --config C:\specialinay_papka\config.json --model $effortProbeModel --efforts xhigh max --timeout 90
}
```

| Модель | Сырой xhigh | Сырой max | Дополнительная проверка |
|---|---|---|---|
| gpt-6-sol | 422 | 200, end_of_stream, 0.00144 кредита | Responses: max 200; xhigh локальный 400 без upstream |
| gpt-6.1-sol | 422 | 200, end_of_stream, 0.00144 кредита | Responses: max 200; xhigh локальный 400 без upstream |
| gpt-6-astra | 422 | 400, model unavailable | Gateway повторил без agent marker: 400, Invalid model for Copilot |
| gpt-6-luna | 422 | 400, model unavailable | Gateway повторил без agent marker: 400, Invalid model for Copilot |

Для Sol/6.1 фактическая проверка после исправления каталога:

```powershell
foreach ($effortFinalModel in @('gpt-6-sol', 'gpt-6.1-sol')) {
  python scripts/probe_effort.py --config C:\specialinay_papka\config.json --model $effortFinalModel --gateway-check --dialects responses --efforts xhigh max --timeout 90
}
```

Команды завершились с кодом 0. В этих Responses-пробах max передан без замены,
каждый успешный запрос сообщил 0.0028 кредита. `xhigh` удалён из объявленных
GPT-6 route capabilities и списка Codex; регрессии проверяют локальный отказ
во всех трёх API. Отказ 422 происходит до проверки доступности Astra/Luna,
поэтому исполнение их max не подтверждено. Их metadata помечена partial;
доступность на других аккаунтах и ресурсах не проверялась.

## Границы

Межпроцессная координация ограничена одним файлом и account_id. Копии одной
удалённой сессии под разными ID/файлами требуют общего владельца; серверная политика
отзыва таких сессий не измерена. Для авторизации из окружения сохранение отключено,
поэтому межпроцессное переиспользование ротаций через конфиг не обещается.

Job/replay API, восстановление скрытого вычисления после потери upstream,
долговечные snapshots контекста и раздельные transport deadlines здесь не добавлены.
Текущий маршрут не предоставляет подтверждённого resume API. Production-конфиг и
запущенный основной relay не изменялись; проверялся изолированный checkout.

## Дополнение: неявный effort после проверки 82ea7bb

Устранены локальные 400 при `auto` для provider-managed GLM-5.3 и при глобальном
`max` для другой модели, поддерживающей только low/medium/high. Общий выбор
значения для HTTP-логирования и runner сохраняет строгую проверку явного effort;
глобальная настройка применяется как модельная предпочтительная величина.
Подробности и новые регрессии описаны в
`docs/service-request-isolation-2026-11-03.md`.
