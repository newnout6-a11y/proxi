// How a change the relay reports (history, tools, edits that matched nothing) reads on screen. The relay's
// items are plain objects whose fields are not fixed, so known ones are shown first and nothing is hidden.
import { SCOPES } from './ops.js';
import { isObject } from './draft.js';

const FIELDS = [
  ['scope', 'где'], ['tool', 'инструмент'], ['name', 'имя'], ['role', 'роль'], ['message_index', 'сообщение'], ['index', 'правка'], ['tag', 'тег'],
  ['heading', 'заголовок'], ['find', 'найти'], ['pattern', 'выражение'], ['contains', 'содержит'], ['count', 'совпадений'], ['removed_chars', 'убрано симв.'],
  ['chars', 'симв.'], ['bytes', 'байт'], ['from', 'было'], ['to', 'стало'], ['mode', 'режим'],
];
const SHOWN = new Set(['op', 'message', 'reason', 'note', 'text', ...FIELDS.map(([key]) => key)]);

const clip = (value, size = 80) => {
  const line = String(value).replace(/\s+/g, ' ').trim();
  return line.length > size ? `${line.slice(0, size - 1)}…` : line;
};

function opLabel(op) {
  for (const scope of Object.values(SCOPES)) if (scope.ops[op]) return scope.ops[op].label;
  return String(op);
}

/** {title, details: [[label, value]], note}: what to print for one item of a change list. */
export function describeChange(item) {
  if (typeof item === 'string') return { title: item, details: [], note: '' };
  if (!isObject(item)) return { title: clip(item ?? ''), details: [], note: '' };
  const title = item.op ? opLabel(item.op) : clip(item.message || item.reason || 'изменение');
  const details = FIELDS.filter(([key]) => item[key] !== undefined && item[key] !== null && item[key] !== '')
    .map(([key, label]) => [label, clip(typeof item[key] === 'object' ? JSON.stringify(item[key]) : item[key])]);
  const rest = Object.entries(item).filter(([key, value]) => !SHOWN.has(key) && value !== undefined && value !== null && typeof value !== 'object');
  for (const [key, value] of rest.slice(0, 3)) details.push([key, clip(value)]);
  const note = item.op ? clip(item.message || item.reason || item.note || '') : '';
  return { title, details, note };
}
