// Rule conditions (plan 2.1): every one is optional, they combine with AND, a list of values means OR,
// strings are globs (* and ?) compared without regard to case.
export const CONDITION_SPECS = [
  { name: 'client', label: 'Клиент', kind: 'list', values: ['opencode', 'claude-code', 'claude-desktop', 'codex', 'harness', 'unknown'],
    hint: 'Кто прислал запрос' },
  { name: 'agent', label: 'Агент', kind: 'list', hint: 'Имя агента OpenCode или id агента Claude Code' },
  { name: 'purpose', label: 'Назначение', kind: 'list',
    values: ['client-tools', 'client-text', 'generation', 'session-title', 'auto-mode-check', 'connection-probe', 'client-service'],
    hint: 'Зачем клиент прислал запрос' },
  { name: 'request_class', label: 'Класс запроса', kind: 'list', hint: 'Заголовок x-claude-code-request-class' },
  { name: 'model', label: 'Модель', kind: 'list', hint: 'Id модели после разрешения алиасов или имя из запроса' },
  { name: 'tools', label: 'Есть инструменты', kind: 'bool' },
  { name: 'structured', label: 'Структурированный ответ', kind: 'bool' },
  { name: 'has_client_system', label: 'Есть system клиента', kind: 'bool' },
  { name: 'dialect', label: 'Диалект', kind: 'list', values: ['chat', 'responses', 'anthropic'] },
  { name: 'header_profile', label: 'Заголовок x-proxi-profile', kind: 'list', hint: 'Значение заголовка; * — любой' },
  { name: 'virtual_profile', label: 'Профиль из имени модели', kind: 'list', hint: 'Часть после @ в <модель>@<профиль>' },
];

const isBool = (value) => ['bool', 'boolean'].includes(String(value || '').toLowerCase());

/** The conditions the relay knows: the static table, adjusted by what `builtin.conditions` says. */
export function normalizeConditions(raw) {
  const byName = new Map(CONDITION_SPECS.map((spec) => [spec.name, { ...spec }]));
  let order = CONDITION_SPECS.map((spec) => spec.name);
  const merge = (name, info) => {
    const spec = byName.get(name) || { name, label: name, kind: 'list' };
    if (info && typeof info === 'object' && !Array.isArray(info)) {
      const values = info.values || info.enum || info.options;
      if (Array.isArray(values) && values.length) spec.values = values.map(String);
      const kind = info.type || info.kind;
      if (kind) spec.kind = isBool(kind) ? 'bool' : 'list';
      if (typeof info.label === 'string' && info.label) spec.label = info.label;
      const hint = info.description || info.hint;
      if (typeof hint === 'string' && hint) spec.hint = hint;
    } else if (Array.isArray(info) && info.length && info.every((item) => typeof item === 'string')) {
      spec.values = info;
    } else if (isBool(info)) {
      spec.kind = 'bool';
    }
    byName.set(name, spec);
  };
  if (Array.isArray(raw)) {
    const named = raw.map((item) => (typeof item === 'string' ? item : item?.name)).filter(Boolean);
    raw.forEach((item) => { if (item && typeof item === 'object') merge(item.name, item); });
    if (named.length) order = [...new Set([...named, ...order.filter((name) => !named.includes(name))])];
  } else if (raw && typeof raw === 'object') {
    for (const [name, info] of Object.entries(raw)) merge(name, info);
    order = [...new Set([...Object.keys(raw), ...order])];
  }
  return order.map((name) => byName.get(name) || { name, label: name, kind: 'list' });
}

export const valuesOf = (value) => {
  if (Array.isArray(value)) return value.map(String);
  return value === undefined || value === null || value === '' ? [] : [String(value)];
};

/** One value stays a string, several become a list (OR), none removes the condition. */
export const fromValues = (values) => (values.length === 0 ? undefined : values.length === 1 ? values[0] : values);

export function setCondition(conditions, name, value) {
  const next = { ...(conditions || {}) };
  if (value === undefined || (Array.isArray(value) && !value.length)) delete next[name];
  else next[name] = value;
  return next;
}

const BOOL_TEXT = { true: 'да', false: 'нет' };

export function conditionText(spec, value) {
  if (spec?.kind === 'bool' || typeof value === 'boolean') return BOOL_TEXT[String(value)] ?? String(value);
  return valuesOf(value).join(' или ');
}

/** The rule's conditions in one line; a rule without any matches every request. */
export function summarizeConditions(conditions, specs = CONDITION_SPECS) {
  const entries = Object.entries(conditions || {}).filter(([, value]) => value !== undefined && value !== null);
  if (!entries.length) return 'любой запрос';
  const byName = new Map(specs.map((spec) => [spec.name, spec]));
  return entries.map(([name, value]) => `${byName.get(name)?.label || name}: ${conditionText(byName.get(name), value)}`).join(' · ');
}

// Rules that name a profile the draft lacks, or a special target the relay does not know.
export const SPECIAL_USES = [
  { id: '$header', label: 'Профиль из заголовка', hint: 'Берётся из x-proxi-profile; если такого профиля нет, запрос получит ошибку 400' },
  { id: '$virtual', label: 'Профиль из имени модели', hint: 'Берётся из <модель>@<профиль>' },
  { id: '$legacy', label: 'Прежний путь', hint: 'Всё как без файла промптов: relay_system_prompt* из настроек' },
];
