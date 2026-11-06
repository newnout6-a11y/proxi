// How a change the relay reports reads on screen: the report on the edits of the client's system (edits), the edits that
// matched nothing (edits_unmatched), and what was done to the history and the tools (history_changes, tools_changes).
// The fields the relay documents are read by name; whatever else an item carries is still shown, never dropped.
import { fmtInt, isNum, plural } from '../format.js';
import { MID_SYSTEM_MODES, SYSTEM_OPS, TOOL_OPS, USER_BLOCK_OPS } from './ops.js';
import { isObject } from './draft.js';

const SCOPE_LABELS = { client_system: 'system клиента', history: 'история', tools: 'инструменты' };
const SCOPE_OPS = { client_system: SYSTEM_OPS, history: USER_BLOCK_OPS, tools: TOOL_OPS };

// What the screen calls the fields of an item; the ones it has no word for are shown under their own name
const FIELDS = [
  ['scope', 'где'], ['tool', 'инструмент'], ['name', 'имя'], ['role', 'роль'], ['message_index', 'сообщение'], ['index', 'позиция'], ['tag', 'тег'],
  ['heading', 'заголовок'], ['find', 'найти'], ['pattern', 'выражение'], ['contains', 'содержит'], ['count', 'совпадений'], ['removed_chars', 'убрано симв.'],
  ['chars', 'симв.'], ['bytes', 'байт'], ['from', 'было'], ['to', 'стало'], ['mode', 'режим'],
];
const LABELS = new Map(FIELDS);
const SHOWN = new Set(['op', 'message', 'reason', 'note', 'text', ...LABELS.keys()]);
// The fields the relay documents for its items: each has its own place on screen, so the rest are listed without them
const OWN = new Set(['scope', 'op', 'mode', 'edit', 'index', 'target', 'tool', 'removed_chars', 'added_chars', 'chars_before', 'chars_after', 'deleted', 'kept', 'skipped', 'reason', 'matched', 'message']);

const clip = (value, size = 80) => {
  const line = String(value).replace(/\s+/g, ' ').trim();
  return line.length > size ? `${line.slice(0, size - 1)}…` : line;
};
const present = (value) => value !== undefined && value !== null && value !== '';
const show = (value) => clip(typeof value === 'object' ? JSON.stringify(value) : value);

/** Where a change happened, as the relay names it in `scope`. A scope the screen does not know is shown as it came. */
export const scopeLabel = (scope) => SCOPE_LABELS[scope] || (scope ? String(scope) : '');

/** What an operation is called, by the scope it belongs to; `mid_system` is not an edit but a mode of the history. */
export function opLabel(scope, op, mode) {
  if (op === 'mid_system') {
    const label = MID_SYSTEM_MODES.find((item) => item.id === mode)?.label;
    return label ? `Системные сообщения посреди диалога: ${label.toLowerCase()}` : 'Системные сообщения посреди диалога';
  }
  const own = SCOPE_OPS[scope]?.[op]?.label;
  if (own) return own;
  for (const specs of Object.values(SCOPE_OPS)) if (specs[op]) return specs[op].label;
  return op ? String(op) : '';
}

/** The number an edit has on screen: positions the relay counts from zero are shown from one, as the forms number them. */
function editNumber(item) {
  const position = item.scope === 'client_system' ? item.index : item.edit;
  return isNum(position) ? position + 1 : null;
}

/** Scalar fields of an item beyond the ones the relay documents, at most three of those without a word of their own. */
function extraDetails(item) {
  const named = FIELDS.filter(([key]) => !OWN.has(key) && present(item[key])).map(([key, label]) => [label, show(item[key])]);
  const rest = Object.entries(item).filter(([key, value]) => !OWN.has(key) && !SHOWN.has(key) && present(value) && typeof value !== 'object');
  return [...named, ...rest.slice(0, 3).map(([key, value]) => [key, clip(value)])];
}

/** {title, details: [[label, value]], note}: what to print for one item whose shape is not known: a string, or any object. */
export function describeChange(item) {
  if (typeof item === 'string') return { title: item, details: [], note: '' };
  if (!isObject(item)) return { title: clip(item ?? ''), details: [], note: '' };
  const title = item.op ? opLabel(item.scope, item.op, item.mode) : clip(item.message || item.reason || 'изменение');
  const details = FIELDS.filter(([key]) => present(item[key])).map(([key, label]) => [label, show(item[key])]);
  const rest = Object.entries(item).filter(([key, value]) => !SHOWN.has(key) && present(value) && typeof value !== 'object');
  for (const [key, value] of rest.slice(0, 3)) details.push([key, clip(value)]);
  const note = item.op ? clip(item.message || item.reason || item.note || '') : '';
  return { title, details, note };
}

// What an item adds to a group: a number counts as it is, a true counts as one
const amount = (value) => (isNum(value) && value > 0 ? value : value === true ? 1 : 0);

function addDistinct(list, value) {
  if (!present(value)) return;
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
  if (!list.includes(text)) list.push(text);
}

/** "a, b, c и ещё 4": a short list for a line of details. */
export function shortList(values, size = 3) {
  const shown = values.slice(0, size).join(', ');
  return values.length > size ? `${shown} и ещё ${fmtInt(values.length - size)}` : shown;
}

/**
 * history_changes and tools_changes folded by (scope, op, mode, edit): on a large body one edit leaves an entry per
 * message, and the screen shows it once, with the count. Edits the relay skipped stay apart, one group per reason.
 */
export function groupChanges(items) {
  const groups = new Map();
  for (const item of Array.isArray(items) ? items : []) {
    const data = isObject(item) ? item : { message: typeof item === 'string' ? item : String(item ?? '') };
    const edit = isNum(data.edit) ? data.edit : null;
    const skipped = data.skipped === true;
    const reason = skipped && present(data.reason) ? String(data.reason) : '';
    const key = JSON.stringify([data.scope ?? null, data.op ?? null, data.mode ?? null, edit, skipped, reason, data.op ? null : data.message ?? null]);
    let group = groups.get(key);
    if (!group) {
      group = { key, scope: data.scope ?? '', op: data.op ?? '', mode: data.mode ?? '', edit, skipped, reason, message: data.op ? '' : String(data.message ?? ''),
        count: 0, removed: 0, added: 0, deleted: 0, kept: 0, targets: [], tools: [], positions: [], extra: [] };
      groups.set(key, group);
    }
    group.count += 1;
    group.removed += amount(data.removed_chars);
    group.added += amount(data.added_chars);
    group.deleted += amount(data.deleted);
    group.kept += amount(data.kept);
    addDistinct(group.targets, data.target);
    addDistinct(group.tools, data.tool);
    if (isNum(data.index) && !group.positions.includes(data.index)) group.positions.push(data.index);
    for (const [name, value] of extraDetails(data)) {
      if (group.extra.length < 3 && !group.extra.some(([known]) => known === name)) group.extra.push([name, value]);
    }
  }
  return [...groups.values()];
}

/** {title, where, count, details, reason}: one folded group of history or tool changes in words. */
export function describeGroup(group) {
  const details = [];
  if (group.targets.length) details.push(['цель', shortList(group.targets)]);
  if (group.tools.length) details.push([group.tools.length > 1 ? 'инструменты' : 'инструмент', shortList(group.tools)]);
  if (group.positions.length) details.push(['позиции в теле', shortList([...group.positions].sort((a, b) => a - b), 6)]);
  if (group.removed) details.push(['убрано симв.', fmtInt(group.removed)]);
  if (group.added) details.push(['добавлено симв.', fmtInt(group.added)]);
  if (group.deleted) details.push(['удалено', fmtInt(group.deleted)]);
  if (group.kept) details.push(['оставлено', fmtInt(group.kept)]);
  details.push(...group.extra);
  const place = [scopeLabel(group.scope), group.edit !== null ? `правка ${group.edit + 1}` : ''].filter(Boolean);
  return {
    title: group.op ? opLabel(group.scope, group.op, group.mode) : clip(group.message || 'изменение'),
    where: place.join(' · '),
    count: group.count,
    details,
    reason: group.reason,
  };
}

/** {title, where, details, note}: an edit that found nothing in this request, and what it was looking for. */
export function describeUnmatched(item) {
  if (!isObject(item)) return { ...describeChange(item), where: '' };
  const number = editNumber(item);
  const where = [scopeLabel(item.scope), number !== null ? `правка ${number}` : ''].filter(Boolean).join(' · ');
  // an entry the screen cannot read as an edit says what it has
  if (!item.op) return { ...describeChange(item), where };
  const details = [];
  if (present(item.target)) details.push(['искали', show(item.target)]);
  if (item.tool) details.push(['инструмент', clip(item.tool)]);
  if (item.mode && item.op !== 'mid_system') details.push(['режим', clip(item.mode)]);
  details.push(...extraDetails(item));
  return { title: opLabel(item.scope, item.op, item.mode), where, details, note: clip(item.message || item.reason || '') };
}

/**
 * What an edit of the client's system looks for, from its form in the profile: the heading, the tag, the markers, the text to
 * find, the pattern, the text to add. Empty when the edit names nothing the screen knows.
 */
export function editTarget(edit) {
  if (!isObject(edit)) return '';
  if (typeof edit.start === 'string' && edit.start && typeof edit.end === 'string' && edit.end) return `${edit.start} … ${edit.end}`;
  for (const key of ['heading', 'tag', 'start', 'find', 'pattern', 'text']) {
    const value = edit[key];
    if (typeof value === 'string' && value) return value;
    if (isObject(value) && typeof value.file === 'string' && value.file) return `файл ${value.file}`;
  }
  return Array.isArray(edit.headings) ? edit.headings.filter((item) => typeof item === 'string' && item).join(', ') : '';
}

// The edits that add a text rather than look for one
const ADDS_TEXT = new Set(['prepend', 'append']);

/** "вырезано 708 символов", "добавлено 1 символ": the change of the length of the system by one edit, from the lengths before and after. */
function lengthChange(row) {
  if (!isNum(row.chars_before) || !isNum(row.chars_after)) return '';
  const delta = row.chars_before - row.chars_after;
  if (delta === 0) return row.matched === true ? 'текст не изменился' : '';
  const size = Math.abs(delta);
  return `${delta > 0 ? 'вырезано' : 'добавлено'} ${fmtInt(size)} ${plural(size, ['символ', 'символа', 'символов'])}`;
}

/**
 * {number, title, matched, details} for one entry of the report on the edits of the client's system. The relay's entry says
 * which edit it is, whether it matched and the length of the system before and after, but not what the edit looks for:
 * `unmatched` (the edits that found nothing) knows that for a miss, and `edits` (the edits of the profile that answered, in the
 * order the relay counts them) for the rest. A detail with no name is a sentence of its own.
 */
export function describeReport(row, unmatched = [], edits = []) {
  const found = unmatched.find((item) => isObject(item) && item.scope === 'client_system' && item.index === row.index);
  const spec = Array.isArray(edits) ? edits[row.index] : null;
  // an edit of the form at this place that is not the one the relay reported (the form has changed since) says nothing of it
  const own = isObject(spec) && spec.op === row.op ? editTarget(spec) : '';
  const target = [row.target, found?.target, own].find(present);
  const details = [];
  if (present(target)) details.push([row.matched === false ? 'искали' : ADDS_TEXT.has(row.op) ? 'текст' : 'цель', show(target)]);
  const change = lengthChange(row);
  if (change) details.push(['', change]);
  if (amount(row.removed_chars)) details.push(['убрано симв.', fmtInt(row.removed_chars)]);
  if (amount(row.added_chars)) details.push(['добавлено симв.', fmtInt(row.added_chars)]);
  details.push(...extraDetails(row));
  return { number: row.index + 1, title: opLabel('client_system', row.op) || 'правка', matched: row.matched, details };
}
