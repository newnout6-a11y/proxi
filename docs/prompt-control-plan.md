# Prompt Control: план роя

Дата: 2026-11-05. Основание: отчёты S1 (сторона relay, `sysprompt-s1\report.md`) и S2 (сторона Retool, `sysprompt-s2\report.md`).
База: интеграционная ветка `fix/pr24-integration` после слияния r2a–r2d. Ветка фичи: `feat/prompt-control`.

> **Состояние на 2026-11-06.** План исторический, его не переписывают. Что делает итоговый код, описывает [руководство](prompt-control.md). Ниже пункты помечены «Устарело», где план уже расходится с кодом: регулярные выражения (I11), тексты из файлов (раздел 2), закрепления в памяти (4.3.10). Остальные расхождения собраны здесь:
>
> - отказов слоя промптов четыре, а не два (п. 4.3.1): ещё `400` для недопустимого `generation.effort` и `413` для head больше `MAX_HEAD_BYTES` ([«Как запрос проходит»](prompt-control.md#как-запрос-проходит-через-слой-промптов));
> - порядок правил: правила с `override_protected` судятся раньше защитных ([«Правила и условия»](prompt-control.md#правила-и-условия));
> - операции правок: `replace_text.all` по умолчанию `true`, ключ `with` у замен обязателен, блок тега — тот, чей открывающий тег стоит в начале строки, `hide` не скрывает tool, который диалог уже вызывал или который назван в `tool_choice` ([«Операции правок»](prompt-control.md#операции-правок));
> - `{{role}}` и `{{native_role}}` не читают `relay_system_prompt` из Настроек ([«Тексты relay»](prompt-control.md#тексты-relay)); владение запросом (I9) определяется по исходному запросу клиента, а не по телу после правок профиля;
> - сжатие OpenCode 2.x приходит с tools и в `client-service` (п. 4.3.14) не входит: оно идёт по профилю чата ([«Защитные правила»](prompt-control.md#защитные-правила));
> - удаление файла подтверждается двумя проверками с интервалом `RELOAD_INTERVAL` (п. 4.3.11);
> - полные тексты хранятся не только в памяти (I7): на диске, в `prompt-pins/`, лежат документы закреплённых ревизий; список заголовков в п. 4.3.15 шире, тела защищённых запросов не хранятся ([«Приватность»](prompt-control.md#приватность));
> - поля и события журнала (п. 4.2) и ответы админ-API (п. 4.1) дополнены ([«Админ-API»](prompt-control.md#админ-api), [«Журнал»](prompt-control.md#журнал)).

## 0. Что хочет пользователь

Полный контроль над тем, что видит модель:
1. Свой текст **вместо** системного промпта IDE так, чтобы инструменты, заголовки и сжатие продолжали работать.
2. Свои правила **поверх** промпта IDE, в любом месте: в начале, в конце или в середине.
3. Видеть **итоговый** промпт: предпросмотр до отправки и «что увидела модель» для последних запросов.
4. Разные промпты **по клиентам**: OpenCode, Claude Code, Claude Desktop, Codex, Harness, неизвестный.
5. Свой промпт, поведение и границы **для каждой модели**.
6. Всё **без перезапуска** relay.
7. **Вырезать** ненужные части промптов IDE: разделы system клиента, напоминания посреди диалога, `<system-reminder>`-блоки в сообщениях, правки описаний инструментов.
8. Редактировать **встроенные тексты relay**: роль для «задач relay», роль для клиента с tools без system, обёртку structured-контракта, обёртку напоминаний, базовые инструкции Codex.

Факты из исследований:
- Retool ничего не добавляет к промпту: что отправил relay, то модель и видит (S2).
- Сейчас у пользователя `standalone` без роли, поэтому весь трафик OpenCode получает промпт OpenCode побайтно (S1, 200 из 200 трасс).
- Неустранимо со стороны relay:
  - служебный system Anthropic при tools (286 токенов);
  - строка effort в шаблоне GLM.

## 1. Инварианты (нарушать нельзя)

- **I1. Обратная совместимость.**
  - Нет файла промптов, или ни одно правило не сработало и `default_profile` не задан → работает **существующий** код без изменений.
  - Побайтно сохраняются head, тело в Retool (`rit` пересчитывается от тех же сообщений), события журнала и заголовки ответа.
  - Страховка — golden-тест на матрице S1 (`sysprompt-s1\golden_matrix.json`, 153 ячейки; сборка — `map_run.py`, `golden_dump.py`) и на реальных телах OpenCode и Desktop runner.
- **I2. Детерминизм.**
  - Одинаковый запрос и одинаковая ревизия дают одинаковые байты head, истории и tools.
  - В шаблонах нет дат, времени, случайных чисел и счётчиков.
  - Правки применяются в фиксированном порядке.
- **I3. Закрепление ревизии.**
  - Ключ закрепления (pin) — `(conversation_key, purpose)`.
  - Новый диалог получает текущую ревизию. Активный остаётся на своей, пока пользователь не нажмёт «применить ко всем» или «открепить».
  - Причина: смена head, истории или tools посреди диалога сбрасывает подписи thinking и переписывает кэш — 10–44 кредита на большой диалог.
  - Без `conversation_key` закрепления нет: используется текущая ревизия.
- **I4. Порядок преобразований.**
  - Правки тела (история, tools) выполняются над сырым JSON **до** `parse_*`. Тогда отпечатки native history, чанки, кэш-метки и `rit` считаются от уже изменённого запроса.
  - Head собирается до `build_body`.
  - Править тело после `build_body` запрещено.
- **I5. Billing header Claude Code.** Если system клиента начинается со строки `x-anthropic-billing-header:`, эта строка остаётся первой и отдельным блоком при любом шаблоне и любых правках. Пользователь её не вырезает: это атрибуция, а не инструкция.
- **I6. Защита служебных запросов.**
  - Встроенные защитные правила идут первыми: title, compaction и summary OpenCode; title и classifier Claude Code; compaction и auxiliary с `x-claude-code-request-class`; проба Desktop.
  - Для этих запросов выбирается профиль `passthrough` (system клиента без изменений).
  - Отключить защиту можно только явно: `protected.enabled: false` или `"override_protected": true` в правиле. Админка показывает предупреждение.
- **I7. Приватность.**
  - В `journal.jsonl` пишутся только длины, sha8 и источники частей, без текстов.
  - Полные тексты хранятся только в памяти (кольцо последних планов) и видны только в админке.
  - Трассы уже содержат тело запроса — их поведение не меняется.
- **I8. Живое применение безопасно.**
  - Невалидный файл не ломает работу: остаётся предыдущая валидная ревизия, пишется событие `prompt.config_error` и показывается баннер в админке.
  - Запись через админку атомарная: временный файл + replace, как `write_backed_up`.
- **I9. Исполнение и изоляция не меняются.**
  - `client_owns_instructions` в `orchestrator._run_once` вычисляется как сейчас: от него зависят exec, project context и изоляция сессий.
  - Переменные `{{local_exec}}`, `{{project_context}}`, `{{exec_reminder}}`, `{{relay_task}}` раскрываются только для запросов, которыми владеет relay. Для клиентских они пусты, а предпросмотр предупреждает.
- **I10. Structured.**
  - Контракт присутствует всегда.
  - Если шаблон не содержит `{{structured}}`, контракт добавляется в хвост, как сейчас.
  - Текст по умолчанию берётся из `structured.instructions(...)` вызовом функции, без копирования литерала.
- **I11. Производительность.**
  - Выбор плана и рендер head — не больше 1 мс на запрос.
  - Второй разбор тела (pass 2) — только если у профиля есть правки тела.
  - Регулярные выражения компилируются при загрузке ревизии.
  - Правки истории на теле в 800K токенов — не больше 50 мс (тест производительности).
  - Длина паттерна ограничена; паттерны с вложенными квантификаторами отклоняются при сохранении (защита от катастрофического бэктрекинга).
  - *Устарело (регулярные выражения).* Их исполняет модуль `regex` с лимитом объёма на вызов (`REGEX_WORK`) и аварийным таймаутом на операцию (`REGEX_TIMEOUT`) и на вызов (`REGEX_BUDGET`): проверка при сохранении осталась первой линией защиты, но не единственной. Правка, упёршаяся в таймаут, не применяется (`skipped: timeout`, событие `prompt.edit_skipped`). Синтаксис как у `re`, но `{`, которая не начинает счётчик, нужно экранировать; вес вложенных счётчиков ограничен. См. [«Операции правок»](prompt-control.md#операции-правок) и [«Пределы движка»](prompt-control.md#пределы-движка).
- **I12. Legacy-настройки.**
  - `relay_system_prompt_mode` и `relay_system_prompt` продолжают работать как сейчас, пока не задан файл промптов. Профиль может вернуть их поведение через `"use": "$legacy"`.
  - Обе legacy-настройки становятся живыми (без перезапуска) — это отдельная задача P4.

## 2. Конфигурация: `prompts.json`

- **Расположение.** Путь — `cfg["prompts_file"]`, по умолчанию `prompts.json` рядом с `config.json`.
- **Почему отдельный файл.** В `config.json` лежат cookie. Промпты правят часто, они бывают большими, и ими удобно делиться.
- **Тексты.** Строка или `{"file": "prompts/<имя>.md"}`. Путь считается относительно папки файла промптов; выход наружу (`..`, абсолютный путь, симлинк наружу) отклоняется. Файл читается как UTF-8, BOM снимается, переводы строк нормализуются в `\n`. *Устарело: читается только папка `prompts/` рядом с файлом промптов и только `.md` и `.txt` (папка самого файла хранит `config.json`); двоеточие в пути, другое расширение и ссылка за пределы `prompts/` отклоняются. См. [«Тексты и файлы»](prompt-control.md#тексты-и-файлы).*
- **Размеры.** Один текст — не больше 256 КиБ. Весь документ — не больше 2 МиБ. Для конкретного запроса дополнительно действует `max_prompt_bytes`.

```jsonc
{
  "version": 1,
  "default_profile": null,              // null → $legacy; иначе имя профиля для запросов, которые не поймало ни одно правило
  "protected": {"enabled": true},       // встроенные защитные правила (I6) идут первыми
  "texts": {                            // именованные тексты для {{text:<имя>}}
    "my_rules": "Отвечай по-русски. Не трогай файлы без явной просьбы.",
    "opus_role": {"file": "prompts/opus.md"}
  },
  "relay_texts": {                      // null = встроенный текст relay
    "role": null,                       // context.DEFAULT_RELAY_SYSTEM_PROMPT; для «задач relay» и {{role}}
    "native_role": null,                // orchestrator.DEFAULT_NATIVE_TOOLS_ROLE; клиент с tools без system
    "structured": null,                 // обёртка контракта; переменные {{default}}, {{schema}}, {{schema_name}}
    "reminder": null,                   // обёртка system посреди диалога; {{text}}; null → "<system-reminder>\n{{text}}\n</system-reminder>"
    "codex_base": null                  // строка или {"<glob модели>": "..."}; публикуется в /codex/models
  },
  "profiles": {
    "passthrough": {"head": "{{client_system}}"},          // встроенный, переопределять нельзя
    "mine-first": {
      "description": "мои правила поверх OpenCode, в начале",
      "head": "{{text:my_rules}}\n\n{{client_system}}",
      "client_system": {"edits": [
        {"op": "remove_section", "heading": "# Code comments"},
        {"op": "remove_tag", "tag": "available_skills"}
      ]},
      "history": {
        "mid_system": "keep",                              // keep | wrap | plain | drop
        "user_blocks": [{"op": "drop_tag", "tag": "system-reminder", "contains": "Plan mode"}]
      },
      "tools": {"edits": [{"tool": "bash", "op": "append", "text": "Не запускай команды, меняющие систему, без подтверждения."}]},
      "generation": {"effort": null, "verbosity": null},   // null = не трогать
      "publish_as_model": false                            // публиковать <модель>@mine-first в каталоге
    },
    "exact-opus": {"head": "{{text:opus_role}}"}           // ровно мой текст, system клиента выброшен
  },
  "rules": [                                               // первое сработавшее включённое правило выигрывает
    {"name": "opus свой", "if": {"model": "claude-opus-*", "client": "opencode", "tools": true}, "use": "exact-opus"},
    {"name": "по заголовку", "if": {"header_profile": "*"}, "use": "$header"},
    {"name": "opencode", "if": {"client": "opencode"}, "use": "mine-first"}
  ]
}
```

### 2.1 Условия правил

Все условия необязательны и объединяются по И. Список в поле означает ИЛИ. Строки — glob (`*`, `?`), без учёта регистра.

| Поле | Значение | Источник |
| --- | --- | --- |
| `client` | `opencode`, `claude-code`, `claude-desktop`, `codex`, `harness`, `unknown` | `prompt_client(identity, headers)` — новое поле. `identity["client"]` и `request_kind` **не меняются** (I1) |
| `agent` | имя агента OpenCode или id агента Claude Code | `identity["agent"]` |
| `purpose` | `client-tools`, `client-text`, `generation`, `session-title`, `auto-mode-check`, `connection-probe`, `client-service` | `request_kind` (+ новый `client-service`, см. P2) |
| `request_class` | значение `x-claude-code-request-class` | `identity["request_class"]` |
| `model` | id модели после разрешения алиасов (`parsed.model`) или исходное имя из запроса | parsed |
| `tools`, `structured`, `has_client_system` | bool | parsed |
| `dialect` | `chat`, `responses`, `anthropic` | proxy |
| `header_profile` | значение `x-proxi-profile` | заголовок |
| `virtual_profile` | профиль из имени модели `<модель>@<профиль>` | P6 |

`use`:
- имя профиля;
- `$header` — профиль из `x-proxi-profile`; если такого профиля нет → 400 с понятной ошибкой;
- `$virtual` — профиль из имени модели;
- `$legacy` — старый путь без изменений.

Если запрос пришёл с моделью `<модель>@<профиль>`, неявное правило `{"if": {"virtual_profile": "*"}, "use": "$virtual"}` стоит сразу после защитных правил.

### 2.2 Переменные шаблона head (закрытый список)

| Переменная | Значение |
| --- | --- |
| `{{client_system}}` | system клиента после `client_system.edits`, без billing header (I5) |
| `{{client_system_raw}}` | system клиента без правок, без billing header |
| `{{text:<имя>}}` | именованный текст |
| `{{role}}` | `relay_texts.role` или встроенный |
| `{{native_role}}` | `relay_texts.native_role` или встроенный |
| `{{relay_task}}` | то, что сегодня получает «задача relay»: роль + `[LOCAL EXECUTION]` + `[PROJECT CONTEXT]` (только I9) |
| `{{local_exec}}`, `{{project_context}}`, `{{exec_reminder}}` | отдельные блоки «задачи relay» (только I9) |
| `{{structured}}` | контракт (I10); пусто, если structured нет |
| `{{model}}`, `{{client}}`, `{{agent}}`, `{{purpose}}` | значения из входа плана (стабильны в пределах диалога) |

Правила шаблона:
- Неизвестная переменная — ошибка при сохранении.
- `\{{` — экранирование.
- Подстановка однопроходная: текст, вставленный переменной, повторно не раскрывается.
- Пустые части не дают лишних пустых строк: после рендера три и больше переводов строки подряд схлопываются в два. Схлопывание делается **только** вокруг пустых подстановок, текст пользователя не трогается.

### 2.3 Операции правок

**Правки system клиента** (`client_system.edits`, только для head; применяются по порядку):

| `op` | Параметры | Смысл |
| --- | --- | --- |
| `remove_section` | `heading` (точный текст строки заголовка markdown, например `# Code comments`) | удалить заголовок и всё до следующего заголовка того же или более высокого уровня |
| `keep_sections` | `headings[]` | оставить только перечисленные разделы и преамбулу до первого заголовка |
| `remove_tag` / `keep_tag_inner` | `tag` | удалить блок `<tag ...>…</tag>` целиком / оставить содержимое без тегов |
| `remove_between` | `start`, `end`, `inclusive` | удалить текст между маркерами |
| `remove_regex` / `replace_regex` | `pattern`, `flags` (`i`, `m`, `s`), `with` | регулярное выражение (I11) |
| `replace_text` | `find`, `with`, `all` | точная замена подстроки |
| `prepend` / `append` | `text` (литерал или `{{text:<имя>}}`) | добавить к system клиента |

Операция, которая ничего не нашла, — не ошибка. Предпросмотр показывает «не сработала».

**Правки истории** (`history`, над сырым телом до разбора, I4):
- `mid_system`: что делать с непустыми `system`/`developer` после ведущих:
  - `keep` — как сейчас;
  - `wrap` — обернуть по `relay_texts.reminder`;
  - `plain` — user-текст без обёртки;
  - `drop` — удалить.
  - Ведущие system и `developer` (head) сюда не относятся.
- `user_blocks[]` — правки текста user-сообщений и текстовых блоков tool_result. Изображения и tool_use не трогаются.
  - `drop_tag` (`tag`, необязательный `contains` или `pattern`) удаляет блок `<tag>…</tag>`.
  - Также доступны `remove_regex`, `replace_text`.
  - Если сообщение стало пустым и это не tool_result, оно удаляется целиком. Удаление не должно нарушать чередование ролей и пары tool_use / tool_result: такая правка отклоняется на этом запросе, пишется событие `prompt.edit_skipped`.

**Правки инструментов** (`tools.edits`):
- `tool` — glob по имени;
- операции над описанием: `append`, `prepend`, `replace_text`, `replace_regex`, `set`;
- удаление инструмента — `hide: true`. Удалять из подписанного диалога рискованно: предпросмотр предупреждает, а пин делает это стабильным. *Устарело: tool, который диалог уже вызывал или который назван в `tool_choice`, не скрывается (правка пропускается со `skipped`); предупреждение остаётся при сохранении документа. См. [«Правки tools»](prompt-control.md#правки-tools).*

**Генерация** (`generation`): `effort` и `verbosity` подменяют запрос клиента по тем же правилам допустимости, что `reasoning_effort` (с `allow_effort_downgrade`). Значение `null` ничего не меняет.

## 3. Поток запроса после внедрения

1. `proxy._completion`:
   - тело → `describe` → `parse_*` (pass 1) → affinity (`conversation_key` от **исходного** тела) → `model_refusal` → `purpose` — всё как сейчас;
   - `plan = runtime.prompts.plan(PlanInput.from_request(...))` — выбор правила с учётом пина;
   - `plan.profile is None` (legacy) → дальше старый путь, ничего не меняется;
   - `plan.body_edits` → `data2, changes = prompt_body.rewrite(data, dialect, plan.snapshot.profile(plan.profile), plan.snapshot)` и `parse_*(data2)` (pass 2). Из pass 1 переносятся `conversation_key`, `parent_conversation_key`, `request_purpose`, `request_id`, `dialect`;
   - `plan.generation` → подмена effort и verbosity до вычисления `effective_effort`;
   - `parsed.prompt_plan = plan`.
2. `orchestrator._run_once`:
   - если `parsed.prompt_plan` есть и не legacy: `head, parts, warnings = plan.compose_head(client_system, sources)`, где `sources` — объект `HeadSources` поверх `TurnRunner` с ленивыми методами (P2);
   - иначе выполняется **существующий** блок `:497-533` без изменений;
   - после head: событие `prompt.plan` и `runtime.prompts.record(...)` — запись в кольцо «что увидела модель».
3. `/codex/models` берёт `codex_base` и переопределения из текущего снимка (P6).

## 4. Контракт модулей

Координатор коммитит скелет до запуска роя. Источник истины — сами файлы скелета `prompt_policy.py` и `prompt_body.py` в ветке `feat/prompt-control`; ниже краткая выжимка, дополнения — в разделе 4.3. Сигнатуры менять только через координатора. В скелете у `Plan` есть ещё поле `snapshot` (ревизия, по которой принят план) и свойство `legacy`.

```python
# prompt_policy.py — P1
class PromptConfigError(ValueError):
    def __init__(self, path: str, message: str): ...   # path вида "profiles.mine.head"

@dataclass(frozen=True)
class PlanInput:
    dialect: str; client: str; agent: str; purpose: str; request_class: str
    model: str; requested_model: str; virtual_profile: str | None; header_profile: str | None
    tools: bool; structured: bool; has_client_system: bool; conversation_key: str | None

@dataclass(frozen=True)
class Part:
    source: str   # client | client_edited | text:<имя> | relay:role | relay:native_role | relay:task |
                  # relay:local_exec | relay:project_context | relay:exec_reminder | relay:structured | literal | billing
    text: str

@dataclass(frozen=True)
class Plan:
    rev: int | None            # None для черновика предпросмотра
    profile: str | None        # None → legacy-путь
    rule: str | None
    pinned: bool
    protected: bool
    body_edits: bool           # есть правки истории или tools
    generation: dict           # {"effort": ..., "verbosity": ...}, только непустые
    def compose_head(self, client_system: str, sources: "HeadSources") -> tuple[str, list[Part], list[str]]: ...

class HeadSources(Protocol):   # реализует P2 поверх TurnRunner; все методы ленивые
    client_owned: bool
    def relay_role(self) -> str: ...
    def native_role(self) -> str: ...
    def relay_task(self) -> str: ...
    def local_exec(self) -> str: ...
    def project_context(self) -> str: ...
    def exec_reminder(self) -> str: ...
    def structured(self) -> str: ...

class Snapshot:                # неизменяемая скомпилированная ревизия
    rev: int; document: dict; source_path: str | None
    def profile(self, name) -> dict: ...
    def codex_base(self, model: str) -> str | None: ...

def compile_document(doc: dict, base_dir: pathlib.Path, *, rev: int) -> Snapshot: ...   # PromptConfigError
def prompt_client(identity: dict, headers: dict) -> str: ...

class PromptStore:
    def __init__(self, cfg: dict, path: str | None = None, clock=time.monotonic): ...
    def snapshot(self) -> Snapshot | None: ...
    def plan(self, inp: PlanInput) -> Plan: ...
    def maybe_reload(self) -> None: ...                 # stat не чаще раза в секунду; невалидно → старая ревизия + prompt.config_error
    def save(self, document: dict, base_rev: int | None) -> Snapshot: ...   # проверка, атомарная запись, подмена; 409 при устаревшем base_rev
    def pins(self) -> list[dict]: ...
    def unpin(self, conversation_key: str | None = None) -> int: ...       # None → все (= «применить ко всем»)
    def record(self, entry: dict) -> None: ...          # кольцо в памяти, по умолчанию 100 записей
    def recent(self, limit: int = 50) -> list[dict]: ...
    def preview(self, body: dict, dialect: str, headers: dict, document: dict | None = None) -> dict: ...

# prompt_body.py — P3
def rewrite(data: dict, dialect: str, profile: dict, snapshot: Snapshot) -> tuple[dict, list[dict]]: ...
    # возвращает (новое тело — глубокая копия, исходник не меняется; список изменений для предпросмотра и журнала)
```

### 4.1 Админ-API (P4 реализует, P5 использует)

Авторизация и CSRF — как у `PATCH /admin/api/settings`.

- `GET /admin/api/prompts` → `{"document": {...} | null, "rev": n | null, "path": "...", "errors": [...], "legacy": {"mode": "...", "role_chars": n}, "builtin": {"relay_texts": {...}, "protected_rules": [...], "variables": [...], "edit_ops": {...}, "conditions": {...}, "profiles": ["passthrough"]}}`.
- `PUT /admin/api/prompts`, тело `{"document": {...}, "base_rev": n | null}` → `200 {"rev": n, "warnings": [...]}` или `409` (ревизия устарела) или `400 {"error": {"message", "param"}}`.
- `POST /admin/api/prompts/preview`, тело `{"request": {...}, "dialect": "chat|responses|anthropic", "headers": {...}, "document": {...} | null}` → `{"rule", "profile", "rev", "protected", "purpose", "client", "model", "head", "parts": [{"source", "start", "end", "chars", "sha8"}], "history_changes": [...], "tools_changes": [...], "edits_unmatched": [...], "warnings": [...], "head_bytes", "tokens_estimate"}`.
  - Без поля `request` берётся последний реальный запрос из кольца (по `request_id`, если он передан).
- `GET /admin/api/prompts/recent?limit=50` → `{"items": [{"ts", "request_id", "client", "agent", "model", "purpose", "rule", "profile", "rev", "pinned", "head", "parts", "history_changes", "tools_changes"}]}`.
- `GET /admin/api/prompts/pins` → `{"items": [{"conversation", "purpose", "rev", "profile", "age_s"}]}`. Поле `conversation` — тег, а не ключ.
- `POST /admin/api/prompts/pins`, тело `{"action": "unpin" | "apply_all", "conversation": "<тег>"?}` → `{"unpinned": n}`.

### 4.2 Журнал

- `prompt.plan` (debug) на каждый ход не-legacy: `{rev, profile, rule, pinned, protected, head_chars, head_sha8, parts: [[source, chars]], history_changes, tools_changes, warnings}`.
- `prompt.reload` (info): `{rev, source}`.
- `prompt.config_error` (error): `{message, param}`.
- `prompt.edit_skipped` (warn): `{op, reason}`.
- `admin.prompts` (info): `{rev, changed_paths, sha8}`.
- `prompt.runtime_error` (error, P2): `{message}` — сбой слоя промптов на запросе; запрос ушёл legacy-путём.

### 4.3 Уточнения контракта (координатор, до запуска роя)

Источник истины — `prompt_policy.py` в ветке. Эти пункты дополняют разделы 2–4 и имеют приоритет над ними.

1. **Отказы `plan()`.** `PromptStore.plan()` отказывает исключением `apierr.ApiError` до любого обращения к upstream:
   - `$header` называет профиль, которого нет в ревизии → `400 invalid_request_error`, `param: "x-proxi-profile"`;
   - `<модель>@<профиль>` называет профиль, которого нет (в том числе когда файла промптов нет вовсе) → `404 not_found_error`, `param: "model"`, текст называет профиль. Для stream — настоящий HTTP-статус до начала потока.
   - P2 пропускает `ApiError` к клиенту. Любое другое исключение слоя промптов → legacy-путь и событие `prompt.runtime_error`: сбой движка не ломает запрос (как I8).
2. **`PlanInput.from_request(parsed, identity, headers, dialect)`** — единственное отображение запроса во вход плана; им пользуются и runtime (P2), и предпросмотр (P1). Реализовано координатором; P1 владеет и может уточнять, не меняя сигнатуры.
3. **`prompt_client`** — базовая реализация координатора: явный `x-proxi-client` с именем семейства, затем клиент, распознанный `client_context.describe()`, затем Codex по `originator` / user-agent. P1 сверяет признаки Codex с исходниками codex-rs, добавляет Harness и закрепляет всё тестами.
4. **Части head.** `head == "".join(p.text for p in parts)`: литералы шаблона и разделители — части `literal`. `part_spans(parts)` и `sha8(text)` реализованы в скелете и общие для предпросмотра (P1), кольца `record` и события `prompt.plan` (P2).
5. **Аксессоры `Snapshot`** (реализует P1; пользуются P2, P3, P6):
   - `profile(name)` — нормализованный профиль: все разделы с умолчаниями, тексты — литералы, `{{text:…}}` раскрыт в полях `text` / `with` правок, но не в `head` (его рендерит `compose_head`); `KeyError` для неизвестного имени; встроенный `passthrough` включён;
   - `profile_names()`; `relay_text(name)` → текст пользователя или `None` (встроенный); `regex(pattern, flags)` → паттерн, скомпилированный при компиляции ревизии (I11); `codex_base(model)`; `codex_overrides(model)`.
6. **Имя профиля** — `PROFILE_NAME` (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, fullmatch): оно же суффикс модели. `passthrough` переопределить нельзя; имена на `$` — только служебные `use`.
7. **Виртуальная модель.** Каноническая запись — `<модель>@<профиль>`, effort идёт после профиля: `claude-opus-5-5@mine#high` (или `:high`). validate (P6) кладёт в `parsed.model` разрешённую базовую модель, в `parsed.virtual_profile` — профиль, в `parsed.requested_model` — имя клиента без суффиксов, до разрешения алиасов.
8. **`publish_as_model`**: `false` (по умолчанию) | `true` (все модели каталога) | список glob моделей.
9. **Переопределения Codex** — новый верхний ключ документа (P1 проверяет, P6 применяет):
   ```jsonc
   "codex": {"models": {"gpt-6*": {"persistent_instructions": "…",          // или {"file": "prompts/…"}
                                   "include_skills_usage_instructions": true}}}
   ```
   Допустимые ключи — `CODEX_OVERRIDE_KEYS`; выигрывает первый подходящий glob в порядке документа. `relay_texts.codex_base` остаётся источником `base_instructions` и `model_messages.instructions_template`.
10. **Пины без файла.** Пока файл не загружен, `plan()` всё равно закрепляет `(conversation_key, purpose)` за ревизией `LEGACY_REV = 0` и возвращает legacy-план. Появился файл → новые диалоги получают текущую ревизию, начатые раньше остаются на legacy до «открепить» / «применить ко всем». Пины ограничены (LRU и время простоя), живут в памяти и после перезапуска пропадают. Если закреплённую ревизию вытеснили из 32 снимков, диалог получает текущую (один reset); пины в админке это показывают. *Устарело: пины сохраняются на диск, в `<conversation_state_dir>/prompt-pins/`, когда включено сохранение состояния диалогов, и переживают перезапуск; восстановленная ревизия получает новый номер и не становится текущей. Остались пределы `MAX_PINS` и `PIN_IDLE_SECONDS`; без каталога пины только в памяти. См. [«Закрепление и цена смены промпта»](prompt-control.md#закрепление-и-цена-смены-промпта).*
11. **Загрузка и перезагрузка.** Путь: явный `path` > `cfg["prompts_file"]` > `prompts.json`; относительный — от папки `cfg["_config_path"]`, если он задан, иначе от рабочей папки. Конструктор загружает существующий файл. `maybe_reload()` замечает появление, изменение и удаление файла и его текстов: удаление файла возвращает новые диалоги к legacy и пишет `prompt.reload`. P2 вызывает `maybe_reload()` перед `plan()` на каждом запросе, сам `maybe_reload` делает stat не чаще раза в `RELOAD_INTERVAL`. `PromptStore.errors()` — ошибки файла на диске для админки (`[]`, если файл валиден).
12. **Head в не-legacy пути** — ровно результат `compose_head`; транспорт добавляет вне head только то, что добавляет сейчас. `[LOCAL EXEC REMINDER]` автоматически не дописывается: он доступен как `{{exec_reminder}}`, и `{{relay_task}}{{exec_reminder}}` повторяет legacy-head для запросов relay. Контракт structured (I10) и billing header (I5) обязательны в любом шаблоне.
13. **`.gitignore`**: `/prompts.json` и `/prompts/` (P4) — личные промпты не попадают в git случайно; пример для документации — `prompts.example.json` (P7).
14. **`client-service` — цель только для слоя промптов.** `client_context.request_kind()` и `parsed.request_purpose` не меняются: от них зависят журнал (`turn.begin.request_purpose`), консоль, повторы пустых ответов и исполнение, и в legacy они должны остаться побайтно прежними (I1). P2 добавляет отдельную функцию в `client_context` и кладёт результат в `parsed.prompt_purpose` (`client-service` для title, compaction и summary OpenCode, иначе — тот же `request_purpose`). `PlanInput.from_request` берёт `prompt_purpose`, а при его отсутствии — `request_purpose`. Защитное правило I6 для `client-service` уже есть в `PROTECTED_RULES`.
15. **Тело запроса для предпросмотра из кольца.** P2 кладёт в `record()` поле `"request": {"body", "dialect", "headers"}`: исходное тело (до правок), если его JSON не больше 1 МиБ, и только заголовки из белого списка (`user-agent`, `originator`, `x-proxi-*`, `x-claude-code-*`, `x-opencode-*`, `x-session-affinity`, `x-session-id`) — никогда `authorization`, `x-api-key`, `cookie`. P1 хранит тела только у 10 новейших записей, `recent()` их не отдаёт (только `has_request`), отдельный доступ для предпросмотра — `PromptStore.request(request_id=None)`. В журнал тело не попадает (I7).
16. **Контракт structured** собирает `prompt_policy.structured_contract(snapshot, output_spec, tools=..., tool_choice=...)` (P1). Его используют `HeadSources.structured()` в runtime (P2) и предпросмотр (P1). Пока P1 не слит, P2 в своей ветке откатывается на `structured.instructions(...)` при `NotImplementedError`.

## 5. Пакеты роя

У каждого пакета свои файлы. Чужие файлы не трогать. Если без них не обойтись, сделать минимальный хук и описать его в отчёте.

| Пакет | Что делает | Свои файлы |
| --- | --- | --- |
| **P1 Движок** | `prompt_policy.py` целиком по контракту: компиляция и проверка документа, шаблоны, правила и защитные правила, правки system клиента, `compose_head` с I5 и I10, `PromptStore` (снимки по ревизиям — последние 32; пины; живая перезагрузка по mtime и размеру файла и текстов; кольцо `recent`; `preview` на собственных функциях), `prompt_client` (OpenCode, Claude Code, Desktop, Codex по `originator` и user-agent `codex_*` — сверить с исходниками codex-rs v0.164; Harness — по user-agent, если он различим, иначе только `x-proxi-client`). Тесты на всё, включая производительность и защиту от плохих регэкспов | `prompt_policy.py`, `tests/test_prompt_policy_*.py` |
| **P2 Встраивание** | В `proxy._completion` — `plan`, pass 2, `generation`. В `orchestrator._run_once` — ветка с `compose_head` и `HeadSources` поверх `TurnRunner`, событие `prompt.plan`, `record`. В `client_context` — `client-service` для title, compaction и summary OpenCode: по `X-Proxi-Agent` и по подписи встроенных промптов OpenCode (`title.txt`, `compaction.txt`, `summary.txt`; сверить с исходником OpenCode и с плагином `scripts/integrations/proxi-session-labels.js`). Важно: `client-service` меняет `request_kind`, поэтому в legacy-режиме он должен вести себя как прежний `client-text` (I1). Golden-тест legacy и сквозные тесты: многоходовые диалоги (3 диалекта), продолжение подписанной истории при закреплённом профиле, стабильность ключа кэша, смена ревизии не трогает активный диалог, «применить ко всем» даёт ровно один reset | `orchestrator.py` (только этот блок), `proxy.py` (только prompt-хунки), `client_context.py`, `tests/test_prompt_runtime_*.py`, `tests/test_prompt_golden.py` + фикстура матрицы |
| **P3 Правки тела** | `prompt_body.rewrite` для Chat, Responses и Messages: `mid_system`, `user_blocks`, `tools.edits`; защита чередования ролей и пар tool_use / tool_result; список изменений. Тесты: идемпотентность (rewrite(rewrite(x)) = rewrite(x)), стабильность отпечатков и `native_history.resume` на многоходовом сценарии, реальные захваты Desktop runner V309 (system на каждом ходу → `drop` и `plain`: head стабилен, resume на всех ходах), трассы OpenCode, производительность на теле в 800K токенов | `prompt_body.py`, `tests/test_prompt_body_*.py` |
| **P4 Админ-API и настройки** | Эндпоинты из 4.1. `cfg["prompts_file"]` в `config.py` (проверка пути). Legacy-настройки `relay_system_prompt*` → `LIVE_SETTINGS` (применение без перезапуска; тест: следующий запрос получает новый head). Событие `admin.prompts`. Проверки авторизации и CSRF. Подключение `PromptStore` к runtime (`proxy.py`: создание `runtime.prompts` — одна строка, согласовать с P2 через скелет) | `admin.py` (prompt-хунки), `config.py`, `tests/test_admin_prompts*.py` |
| **P5 Экран «Промпты»** | Новый раздел админки: профили (редактор шаблона с палитрой переменных, счётчик байт, правки с живым «что вырезано» через preview); правила (порядок перетаскиванием, вкл/выкл, условия, защищённые заблокированы с переключателем); тексты relay; предпросмотр (вставить тело запроса или выбрать из последних; цветные части по источникам; сработавшее правило; предупреждения); «что увидела модель» (последние запросы, полный head, diff двух соседних); пины. Фикстуры и Playwright, design gate без переполнений | `web/src/views/Prompts*.jsx`, `web/src/prompts/**`, навигация (1–2 строки), `web/tests/prompts.spec.js`, фикстуры |
| **P6 Codex и виртуальные модели** | `models.codex_catalog(entries, snapshot)`: `codex_base` по моделям и переопределения `model_messages` (`persistent_instructions`, флаги `include_*`). Виртуальные модели `<модель>@<профиль>`: разбор суффикса рядом с `#effort`, `virtual_profile` в parsed, публикация в `/v1/models` и `/codex/models` для профилей с `publish_as_model`, проверка «профиль существует» → иначе 404 с причиной. Скрипты синхронизации OpenCode, Desktop, Harness: включать виртуальные модели (тесты на синтетике) | `models.py`, `validate.py` (только суффикс модели), `scripts/sync_*.py`, `tests/test_prompt_models_*.py` |
| **P7 Документация** | `docs/prompt-control.md`: руководство с рецептами — заменить промпт OpenCode целиком; свои правила в начало; вырезать разделы и `<env>`; убрать напоминания; свой промпт для Opus; профиль по заголовку для Claude Code (`ANTHROPIC_CUSTOM_HEADERS`), Codex (`http_headers`), Desktop (`inferenceCustomHeaders`), OpenCode (плагин); виртуальные модели; цена смены промпта и пины; отладка через предпросмотр. Переписать раздел README и `docs/relay-quality-and-prompts.md`. Пример `prompts.example.json`. Финальная сверка с кодом — после слияния | `docs/prompt-control.md`, `README.md`, `docs/relay-quality-and-prompts.md`, `prompts.example.json` |
| **P8 Проверка** (после слияния P1–P7) | Независимое ревью и сквозные сценарии на реальных телах; red team: обход путей в `file`, регэкспы-бомбы, гонки перезагрузки, CSRF, утечка текстов в журнал; производительность; 2–3 живые платные пробы: `exact`-профиль через Retool, профиль с `drop` напоминаний на многоходовом диалоге (resume и кэш), Codex с `codex_base` против mock | только отчёт и тесты в `tests/test_prompt_e2e_*.py` |

## 6. Порядок и обязанности координатора

1. Слить r2a и r2b. Полный pytest, node, Playwright, сборка `web/dist`. Перемотать основную ветку. Попросить пользователя перезапустить relay.
2. Создать ветку `feat/prompt-control`. Закоммитить:
   - скелет `prompt_policy.py` и `prompt_body.py` (сигнатуры из раздела 4, `NotImplementedError`);
   - пустой `runtime.prompts = PromptStore(cfg)` в `proxy.py` — он не меняет поведение, пока нет файла;
   - golden-фикстуру матрицы из `sysprompt-s1\golden_matrix.json`, подготовленную под тесты;
   - этот план — как `docs/prompt-control-plan.md`.
3. Запустить P1–P7 параллельно, каждый в своём worktree от коммита скелета.
4. Сливать в порядке: P1 → P3 → P2 → P4 → P6 → P5 → P7. После каждого слияния — целевые тесты. После всех — полный набор, `npm run build`, Playwright, design gate.
5. Запустить P8. Исправить находки отдельными пакетами.
6. Перемотать основную ветку. Перезапуск relay — пользователь. Живая проверка:
   - без файла промптов поведение то же: head OpenCode побайтно, `prompt.plan` не пишется;
   - с профилем — head меняется только у новых диалогов.

## 7. Общие правила для агентов роя

- Модель агентов — `retool-relay/claude-sonnet-5-5#max`. Работать самостоятельно до конца. Отчёт по-русски.
- Запрещено:
  - менять рабочую папку живого relay (порт 8787);
  - трогать чужие worktree и `config.json`;
  - останавливать python и `proxy.py`;
  - слать запросы на `localhost:8787`;
  - `git push`, `git stash`, менять git config.
- Платные пробы — только P8, не больше 5 крошечных, по образцу `scripts/audit_models.py`, без продления сессии.
- Коммиты небольшие, на английском, с префиксами `feat:`, `fix:`, `test:`, `docs:`, `refactor:`. `web/dist` не коммитить.
- Каждое поведение закреплено тестом. Полный pytest в конце — foreground, таймаут не меньше 900000 мс, итоговая строка в отчёт. Для P5 — также node, Playwright (`CI=1`, `PW_CHROMIUM`, прокси в `vite.config.js` временно на `127.0.0.1:9`) и design gate.
- Веб-поиск: Exa — любые режимы и Deep Research. Утверждения подтверждать первоисточниками. Ключ нигде не записывать.
- Windows, PowerShell. Команды не склеивать разделителями вроде `echo "===="`.
