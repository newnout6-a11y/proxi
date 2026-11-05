// Where each piece of a composed head came from (Part.source in prompt_policy) and how it is shown.
export const KINDS = {
  client: { label: 'system клиента', hint: 'как прислал клиент' },
  edited: { label: 'system клиента после правок', hint: 'с вашими правками client_system.edits' },
  text: { label: 'именованный текст', hint: '{{text:имя}}' },
  literal: { label: 'текст шаблона', hint: 'набран прямо в шаблоне head' },
  relay: { label: 'встроенный текст relay', hint: 'роль, задача, структурированный ответ' },
  billing: { label: 'служебная строка Claude Code', hint: 'x-anthropic-billing-header остаётся первой строкой' },
  other: { label: 'прочее', hint: '' },
};

const RELAY_LABELS = {
  role: 'роль relay',
  native_role: 'роль для клиента с tools',
  task: 'задача relay',
  local_exec: 'локальные команды',
  project_context: 'контекст проекта',
  exec_reminder: 'напоминание о командах',
  structured: 'контракт structured',
};

export function sourceKind(source) {
  const name = String(source ?? '');
  if (name === 'client') return 'client';
  if (name === 'client_edited') return 'edited';
  if (name.startsWith('text:')) return 'text';
  if (name.startsWith('relay:')) return 'relay';
  if (name === 'literal') return 'literal';
  if (name === 'billing') return 'billing';
  return 'other';
}

export function sourceLabel(source) {
  const name = String(source ?? '');
  const kind = sourceKind(name);
  if (kind === 'text') return `текст «${name.slice(5)}»`;
  if (kind === 'relay') return RELAY_LABELS[name.slice(6)] || `relay: ${name.slice(6)}`;
  return KINDS[kind].label;
}

const count = (value) => (Number.isFinite(value) && value >= 0 ? value : 0);

/**
 * Parts as {source, start, end, chars, sha8}: the preview sends spans, the journal-style form is [source, chars],
 * and either may omit offsets, which then follow from the order of the parts.
 */
export function normalizeParts(parts) {
  if (!Array.isArray(parts)) return [];
  const out = [];
  let offset = 0;
  for (const part of parts) {
    const source = Array.isArray(part) ? part[0] : part?.source;
    if (typeof source !== 'string') continue;
    const given = Array.isArray(part) ? part[1] : part.chars;
    const start = Number.isFinite(part?.start) ? part.start : offset;
    const end = Number.isFinite(part?.end) ? part.end : start + count(given);
    out.push({ source, start, end, chars: end - start, sha8: part?.sha8 || '' });
    offset = end;
  }
  return out;
}

/**
 * Cut `head` along its parts. Offsets count code points like Python's len(), not UTF-16 units, so text with
 * emoji is cut where the relay cut it. Text that no part covers is kept as an "other" segment, never dropped.
 */
export function segmentHead(head, parts) {
  const text = String(head ?? '');
  if (!text) return [];
  const points = Array.from(text);
  const spans = normalizeParts(parts);
  if (!spans.length) return [{ source: 'other', kind: 'other', text, start: 0, end: points.length, chars: points.length, sha8: '' }];
  const segments = [];
  let at = 0;
  const push = (source, start, end, sha8) => {
    if (end <= start) return;
    segments.push({ source, kind: sourceKind(source), text: points.slice(start, end).join(''), start, end, chars: end - start, sha8 });
  };
  for (const span of spans) {
    const start = Math.max(span.start, at);
    const end = Math.min(span.end, points.length);
    push('other', at, start, '');
    push(span.source, start, end, span.sha8);
    at = Math.max(at, end);
  }
  push('other', at, points.length, '');
  return segments;
}

/** One entry per kind, in the order the kinds first appear: what the legend shows. */
export function summarizeKinds(segments) {
  const byKind = new Map();
  for (const segment of segments) {
    const entry = byKind.get(segment.kind) || { kind: segment.kind, label: KINDS[segment.kind].label, chars: 0, parts: 0 };
    entry.chars += segment.chars;
    entry.parts += 1;
    byKind.set(segment.kind, entry);
  }
  return [...byKind.values()];
}
