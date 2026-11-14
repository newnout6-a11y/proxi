// Forms for the edit operations of a profile (plan section 2.3). The relay validates; these specs only say
// which fields an operation takes and how to show them. Unknown operations are kept as they are.
const heading = { key: 'heading', label: 'Заголовок', placeholder: '# Code comments', required: true,
  hint: 'Точный текст строки заголовка markdown' };
const tag = { key: 'tag', label: 'Тег', placeholder: 'available_skills', required: true };
const text = { key: 'text', label: 'Текст', kind: 'area', required: true, hint: 'Можно {{text:имя}}' };
const find = { key: 'find', label: 'Найти', kind: 'area', required: true };
const replacement = { key: 'with', label: 'Заменить на', kind: 'area' };
const pattern = { key: 'pattern', label: 'Регулярное выражение', placeholder: '(?s)<env>.*?</env>', required: true, mono: true };
const flags = { key: 'flags', label: 'Флаги', kind: 'flags' };
const all = { key: 'all', label: 'Все вхождения', kind: 'bool' };

export const SYSTEM_OPS = {
  remove_section: { label: 'Убрать раздел', hint: 'Заголовок и всё до следующего заголовка того же или более высокого уровня', fields: [heading] },
  keep_sections: { label: 'Оставить только разделы', hint: 'Остаются преамбула до первого заголовка и перечисленные разделы',
    fields: [{ key: 'headings', label: 'Заголовки', kind: 'lines', required: true, placeholder: '# Tone and style', hint: 'По одному в строке' }] },
  remove_tag: { label: 'Убрать блок тега', hint: 'Блок <тег …>…</тег> целиком', fields: [tag] },
  keep_tag_inner: { label: 'Оставить содержимое тега', hint: 'Содержимое остаётся, сами теги убираются', fields: [tag] },
  remove_between: { label: 'Убрать между маркерами', hint: 'Всё между двумя маркерами',
    fields: [{ key: 'start', label: 'Начало', required: true }, { key: 'end', label: 'Конец', required: true },
      { key: 'inclusive', label: 'Вместе с маркерами', kind: 'bool' }] },
  remove_regex: { label: 'Убрать по выражению', hint: 'Всё, что совпало с регулярным выражением', fields: [pattern, flags] },
  replace_regex: { label: 'Заменить по выражению', hint: 'Совпадения заменяются текстом', fields: [pattern, flags, replacement] },
  replace_text: { label: 'Заменить текст', hint: 'Точная замена подстроки', fields: [find, replacement, all] },
  prepend: { label: 'Добавить в начало', hint: 'К system клиента', fields: [text] },
  append: { label: 'Добавить в конец', hint: 'К system клиента', fields: [text] },
};

export const USER_BLOCK_OPS = {
  drop_tag: { label: 'Убрать блок тега', hint: 'Блок <тег>…</тег> в сообщениях пользователя и результатах инструментов',
    fields: [tag, { key: 'contains', label: 'Только если содержит', hint: 'Необязательно' },
      { key: 'pattern', label: 'Или подходит под выражение', mono: true, hint: 'Необязательно' }] },
  remove_regex: { label: 'Убрать по выражению', hint: 'Из текста сообщений', fields: [pattern, flags] },
  replace_text: { label: 'Заменить текст', hint: 'Точная замена подстроки в сообщениях', fields: [find, replacement, all] },
};

export const TOOL_OPS = {
  append: { label: 'Добавить в конец описания', fields: [text] },
  prepend: { label: 'Добавить в начало описания', fields: [text] },
  replace_text: { label: 'Заменить текст в описании', fields: [find, replacement, all] },
  replace_regex: { label: 'Заменить по выражению', fields: [pattern, flags, replacement] },
  set: { label: 'Задать описание целиком', fields: [{ ...text, label: 'Новое описание' }] },
  hide: { label: 'Скрыть инструмент', hint: 'Инструмент не попадёт к модели. Из начатого диалога убирать рискованно', fields: [] },
};

export const MID_SYSTEM_MODES = [
  { id: 'keep', label: 'Оставить как есть', hint: 'Системные сообщения посреди диалога идут к модели без изменений' },
  { id: 'wrap', label: 'Обернуть как напоминание', hint: 'Текст оборачивается шаблоном relay_texts.reminder и идёт как сообщение пользователя' },
  { id: 'plain', label: 'Обычный текст пользователя', hint: 'Текст идёт как сообщение пользователя без обёртки' },
  { id: 'drop', label: 'Удалить', hint: 'Системные сообщения посреди диалога не доходят до модели' },
];

export const SCOPES = {
  client_system: { ops: SYSTEM_OPS, noun: 'правка system клиента' },
  user_blocks: { ops: USER_BLOCK_OPS, noun: 'правка сообщений' },
  tools: { ops: TOOL_OPS, noun: 'правка инструмента' },
};

const names = (specs) => Object.keys(specs);

const opNames = (value) => {
  if (Array.isArray(value)) {
    return value.map((item) => (typeof item === 'string' ? item : item?.op || item?.name || item?.id)).filter((item) => typeof item === 'string');
  }
  return value && typeof value === 'object' ? Object.keys(value) : [];
};

/**
 * Which operations the relay offers per scope. Accepts per-scope lists of names or {op|name}, maps keyed by
 * operation, or one flat map of operations; whatever is not understood falls back to the full static list.
 */
export function normalizeEditOps(raw) {
  const result = { client_system: names(SYSTEM_OPS), user_blocks: names(USER_BLOCK_OPS), tools: names(TOOL_OPS), mid_system: MID_SYSTEM_MODES.map((m) => m.id) };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return result;
  const history = raw.history && typeof raw.history === 'object' && !Array.isArray(raw.history) ? raw.history : {};
  const scoped = {
    client_system: raw.client_system ?? raw.system,
    user_blocks: raw.user_blocks ?? history.user_blocks ?? (Array.isArray(raw.history) ? raw.history : undefined),
    tools: raw.tools,
    mid_system: raw.mid_system ?? history.mid_system,
  };
  let found = false;
  for (const [scope, value] of Object.entries(scoped)) {
    const ops = opNames(value);
    if (!ops.length) continue;
    result[scope] = ops;
    found = true;
  }
  if (!found) {
    const flat = Object.keys(raw);
    for (const scope of ['client_system', 'user_blocks', 'tools']) {
      const known = flat.filter((op) => op in SCOPES[scope].ops);
      if (known.length) result[scope] = known;
    }
  }
  return result;
}

const clip = (value, size = 48) => {
  const line = String(value ?? '').replace(/\s+/g, ' ').trim();
  return line.length > size ? `${line.slice(0, size - 1)}…` : line;
};

/** One line saying what an edit does, for lists and collapsed rows. */
export function describeEdit(scope, edit) {
  if (!edit || typeof edit !== 'object') return '';
  const op = scope === 'tools' && edit.hide === true ? 'hide' : edit.op;
  const spec = SCOPES[scope]?.ops[op];
  const label = spec?.label || (op ? String(op) : 'правка');
  const details = [];
  if (scope === 'tools' && edit.tool) details.push(clip(edit.tool));
  for (const field of spec?.fields || []) {
    const value = edit[field.key];
    if (value === undefined || value === null || value === '' || field.kind === 'bool' || field.kind === 'flags') continue;
    details.push(clip(Array.isArray(value) ? value.join(', ') : typeof value === 'object' ? JSON.stringify(value) : value));
    if (details.length >= 2) break;
  }
  return details.length ? `${label}: ${details.join(' → ')}` : label;
}

/** A fresh edit with the fields of `op` empty, ready for the form. */
export function newEdit(scope, op) {
  if (scope === 'tools') return op === 'hide' ? { tool: '', hide: true } : { tool: '', op };
  return { op };
}

export function opOf(scope, edit) {
  return scope === 'tools' && edit?.hide === true ? 'hide' : edit?.op;
}

/** Set one field of an edit; an emptied optional field is removed so the saved file stays tidy. */
export function setEditField(edit, key, value) {
  const next = { ...edit };
  const empty = value === '' || value === undefined || value === null || value === false || (Array.isArray(value) && !value.length);
  if (empty && key !== 'tool') delete next[key];
  else next[key] = value;
  return next;
}

/** Change the operation of an edit and keep only the fields both forms share. */
export function switchOp(scope, edit, op) {
  const fresh = newEdit(scope, op);
  const spec = SCOPES[scope]?.ops[op];
  for (const field of spec?.fields || []) if (edit[field.key] !== undefined) fresh[field.key] = edit[field.key];
  if (scope === 'tools' && edit.tool !== undefined) fresh.tool = edit.tool;
  return fresh;
}

export const flagsOf = (value) => new Set(String(value || '').split('').filter((flag) => 'ims'.includes(flag)));
export const joinFlags = (set) => ['i', 'm', 's'].filter((flag) => set.has(flag)).join('');
