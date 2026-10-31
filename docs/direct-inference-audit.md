# Backend Retool: что удалось выяснить

> **Статус документа (обновлено 31.10.2026):** это историческое исследование протокола. Прямой transport с тех пор **реализован и является основным** (`transport: "direct"`, `POST /api/gpt/copilotStreamGPT`); актуальное описание и замеры — в [direct-transport.md](direct-transport.md). Формулировки «не реализован и не включён» ниже относятся к моменту исследования.

Дата исследования: **29 октября 2026 года**. Основание: публичные JavaScript-бандлы Retool, официальный CLI и ограниченные живые проверки текущего аккаунта. Этот документ фиксирует выполненное исследование; при его написании новые модельные запросы не делались.

**Вывод:** backend прямого inference существует. Он принимает структурированные сообщения и настоящие tools без создания чата App Builder и запуска sandbox. Доступ к Opus 5.5 зависит от контекста запроса. Прямой transport в relay пока **не реализован и не включён**. Пользователь выбрал основной моделью **Opus 5.5**; модель и ключи не изменены.

## Два пути к модели

| Путь | Устройство | Статус |
| --- | --- | --- |
| Агент App Builder | Thread, запуск agent/sandbox, WebSocket; инструкции и клиентские tools передаются через prompt bridge | Текущий путь relay |
| Прямой inference | HTTP POST со структурированными messages/tools, поток NDJSON | Найден и проверен короткими probes |

Прямой путь убирает инфраструктуру агентского запуска. Обработка полного контекста, очередь провайдера и генерация модели остаются. Фиксированная задержка для проекта не установлена. Отдельного conversation/thread ID в опубликованной схеме прямого запроса не обнаружено: необходимую историю передаёт клиент.

## Endpoint и авторизация

```text
POST /api/gpt/copilotStreamGPT?origin=frontend
```

Это внутренний endpoint Retool, а не OpenAI-совместимый публичный API. Нативный `callInternalApi` отправляет cookies с `credentials: "same-origin"`, `X-Xsrf-Token`, заголовок версии клиента, JSON body и abort signal.

В relay уже есть `auth.upstream_headers(cfg)` для upstream-авторизации. Локальный ключ relay авторизует IDE перед relay; он не заменяет cookies/XSRF Retool или провайдерский BYOK-ключ. Значения секретов и приватные resource names здесь не публикуются.

## Поля запроса

В опубликованной клиентской схеме обязательны `model` и `messages`. Остальные поля optional; присутствие в схеме не подтверждает поддержку всех комбинаций сервером.

| Поле | Что известно |
| --- | --- |
| `model` | Идентификатор, например `claude-opus-5-5` |
| `messages` | Сообщения `system`, `user`, `assistant`, `tool` |
| `providerResourceName` | Имя настроенного ресурса провайдера текущего аккаунта |
| `tools` | Объявления инструментов в формате Retool |
| `toolChoice` | В схеме opaque/custom; точные принимаемые значения не проверены |
| `reasoningEffort` | Уровень мышления; поддерживаемые уровни зависят от модели |
| `maxCompletionTokens` | Положительное число; точное соблюдение бюджета всеми моделями не проверено |
| `enableFineGrainedStreaming` | Флаг подробного стриминга |
| `rit` | Строка, вычисляемая из длин JSON сообщений и tools |
| `metadata` | Метаданные; нативный React editor использует `sandpackAgent: true` |
| `openAICacheKey` | Cache key; влияние на задержку/биллинг не измерено |
| `wireModel` | Дополнительное имя модели в нативных callsites; серверная семантика не установлена |
| `verbosity`, `parentSpanId`, `fallbackModelChain` | Поля обнаружены; поведение отдельно не проверялось |

### Сообщения и инструменты

`system` принимает строку или text blocks; `user` — строку или text/image blocks. `assistant` поддерживает text, tool call, thinking и redacted thinking. В схеме tool results есть также image/document blocks. Это не является живой проверкой мультимодальности через relay.

Настоящий вызов инструмента и его результат имеют такой вид:

```json
{
  "role": "assistant",
  "content": [
    {"type": "tool_call", "toolCallId": "call-1", "toolName": "get_goal", "toolInput": {}}
  ]
}
```

```json
{
  "role": "tool",
  "content": {"toolCallId": "call-1", "toolResult": "No active goal"},
  "isError": false
}
```

Объявление инструмента отличается от OpenAI/Anthropic JSON Schema:

```json
{
  "name": "read_file",
  "description": "Read a project file",
  "params": {
    "path": {"type": "string", "required": true, "description": "Project-relative path"}
  },
  "strict": true
}
```

Типы параметров: `literal`, `string`, `number`, `boolean`, `null`, `array`, `object`, `enum`, `anyOf`. Обязательность указана в `required` каждого поля. В опубликованной схеме не обнаружены эквиваленты произвольных `$ref`, `$defs`, `additionalProperties`, `allOf`, `oneOf`, min/max/pattern. Конвертер не должен молча терять эти ограничения.

### Формула rit

Точная формула из нативного `StreamHandler-CfXtcWV5.js`:

```javascript
function hashLength(length) {
  return ((length * 2654435761) >>> 0).toString(16).padStart(8, "0");
}
function makeRit(messages, tools) {
  return hashLength(JSON.stringify(messages).length)
    + hashLength(JSON.stringify(tools ?? []).length);
}
```

Это не API-ключ и не криптографическая подпись. Поле optional в клиентской схеме. Первый сокращённый probe без нативных полей получил 400; после добавления правильного rit запрос прошёл валидацию. Это не доказывает, что любой 400 вызван отсутствием rit.

Нужно воспроизвести JavaScript JSON serialization и длину в UTF-16 code units. Python `len`, экранирование Unicode и сериализация чисел могут дать другой результат.

## Поток ответа

Ответ — **NDJSON**, JSON-событие на строку, без SSE-префикса `data:`. Один HTTP-чанк может содержать несколько строк или часть строки.

| Событие | Данные |
| --- | --- |
| `content_block_start` | `type`: text, tool_call, thinking, redacted_thinking; для tool — `toolName`, `toolCallId` |
| `content_block_delta`, type text | `partialText` |
| `content_block_delta`, type thinking | `partialThinking` |
| `content_block_delta`, type signature | `partialSignature` |
| `content_block_delta`, type tool_call | `partialInputs`; промежуточные аргументы нельзя без проверки считать обычными append-deltas |
| `content_block_end` | Конец блока |
| `metadata` | Фактический `model` и `usage` |
| `end_of_stream` | Конец потока; finish reason в опубликованной схеме отсутствует |
| `error` | `error`, `isRetryable`, optional `statusCode`, managed-key/OAuth recovery metadata |

`usage` содержит `inputTokens`, `outputTokens`, optional `totalTokens`, `creditsUsed`, `promptTokensReadFromCache`, `promptTokensWrittenToCache`. Отсутствие creditsUsed не означает бесплатный запрос. Реальные backend-счётчики нужно сохранять отдельно от приблизительных оценок relay.

По одному end_of_stream нельзя надёжно отличить обычное завершение от обрезки по токенам. Transport должен корректно собирать фрагменты UTF-8 и строк, ограничивать буфер, обнаруживать оборванный поток и закрывать response при отмене.

### Thinking

Есть отдельные события thinking text, signature и redacted thinking. Но наличие схемы не гарантирует видимый текст: в успешном Opus probe пришло **0 символов thinking text и 988 символов signature**. Signature — непрозрачные данные, не текст рассуждений; восстановить из неё скрытое мышление нельзя.

## Результаты живых проверок

Все probes ниже короткие, без полного project prompt IDE и без исполнения возвращённого get_goal. Это наблюдения отдельных запросов, не обещание скорости рабочего проекта.

| Запрос | Результат | Первый блок / завершение |
| --- | --- | --- |
| Обычный direct Assist, Sonnet 5, managed resource, без editor marker | HTTP 200; metadata `claude-sonnet-5`; настоящий get_goal; input/output 441/27 | 1,672 / 1,943 с |
| Обычный direct Assist, Opus 5.5, managed resource, правильный rit | HTTP 422, требование своего API-ключа | Успешной генерации нет |
| Нативный React editor context, Opus 5.5, max, `metadata.sandpackAgent: true` | HTTP 200; metadata Opus; настоящий get_goal; input/output 405/66; creditsUsed 0.0588 | 1,496 / 1,988 с |

Точный отказ ordinary Assist:

```text
claude-opus-5-5 is not available when using Retool's managed key for Assist.
Please configure your own API key.
```

`sandpackAgent: true` действительно используется callsites React App Builder, включая coding/helper запросы. Успешный probe доказывает техническую доступность Opus в этом контексте, но не устанавливает поддерживаемый универсальный inference-доступ внешних IDE. Серверные правила доступа нельзя определить по клиентскому JavaScript.

Официальный CLI **0.5.106** также разделяет `aiQueries` и `retoolReact`: для первого managed Opus 5.5 требует BYOK, для второго допускается. В aiQueries Sonnet 5, Haiku 4.5 и Sol допускают managed key; Astra требует BYOK, GLM недоступен. Каталог и права могут изменяться.

Отдельный CLI resource-query путь использует OAuth; cookies браузера получили там 401. Retool MCP авторизован в другом tenant, поэтому его ресурсы/баланс не использовались как доказательства для текущего аккаунта.

## Что требуется для полноценной IDE-интеграции

Прямой backend может получить system prompt проекта, историю, tools и результаты как структурированные данные. Доступ к файлам, shell, `/goal`, субагенты, разрешения и сохранение задач остаются работой IDE. Сам по себе backend не видит локальный проект.

Оставшаяся работа:

1. Преобразование Chat Completions, Responses и Anthropic Messages с сохранением IDs, tools/results и thinking; явная обработка неподдерживаемых JSON Schema конструкций.
2. NDJSON transport: отмена, ограниченный буфер, ошибки, общий HTTP client, без повторного воспроизведения уже начатого ответа.
3. Фактические usage/credits, корректная обработка tool choice и неизвестного finish reason.
4. Проверка полного tool/result цикла реальных IDE, больших project prompts, параллельных запросов и субагентов. Маленький get_goal probe этого не заменяет.
5. Подтверждённый способ доступа именно к Opus 5.5 в выбранном контексте. Обычный managed Assist уже дал отказ; BYOK не настроен.

Эти пункты пока **не реализованы**. Автоматического fallback между direct и agent путями нет. Исправления сессий текущего agent-пути и полный Harness replay описаны в [PR review](pr-1-review.md).

## Источники

- [Endpoint constant](https://retool-edge.com/assets_vjs/loggingSpanEndpointTypes-CfQ_1ztW.js).
- [POST wrapper](https://retool-edge.com/assets_vjs/queryCopilotWrapper-T1BTu1VZ.js).
- [Схемы messages/tools/request/stream](https://retool-edge.com/assets_vjs/main-CnZ2mbHm.js).
- [NDJSON parser и rit](https://retool-edge.com/assets_vjs/StreamHandler-CfXtcWV5.js).
- [React editor callsite](https://retool-edge.com/assets_vjs/SandpackAgentOverlay-CAV0EzLS.js).
- [Второй editor callsite](https://retool-edge.com/assets_vjs/ReleaseSelect-DY_34m1T.js).
- [Официальный CLI 0.5.106](https://cli.retool-edge.com/versions/0.5.106/retool.bundle.cjs).
- [Документация моделей](https://docs.retool.com/build/apps/reference/models), [AI queries](https://docs.retool.com/queries/guides/ai), [streaming](https://docs.retool.com/data-sources/concepts/streaming), [resources](https://docs.retool.com/build/apps/reference/resources).

Локальные копии публичных бандлов и выдержки с offsets/SHA256: `logs/native-web-source/`, файл `direct-inference-evidence.txt`. Его начальная строка об отсутствии POST относится к ранней стадии анализа исходников, до последующих живых probes. Logs исключены из Git; приватные cookies/payloads публиковать не нужно.

Asset URLs содержат хеш версии и могут устареть после обновления Retool. Внутренний endpoint, формат и доступность моделей требуют повторной проверки при изменениях продукта.
