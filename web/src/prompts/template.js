// Head templates: the closed variable list is checked by the relay; here the editor only helps and warns.
const encoder = new TextEncoder();

export const MAX_TEXT_BYTES = 256 * 1024;

export const utf8Bytes = (text) => encoder.encode(String(text ?? '')).length;

// "\{{" escapes a brace pair; any other "{{ name }}" is a variable reference.
const TOKEN = /\\\{\{|\{\{\s*([^{}]*?)\s*\}\}/g;

/** Split a template into text, escaped braces and variable references with their offsets (UTF-16 units). */
export function tokenize(template) {
  const source = String(template ?? '');
  const tokens = [];
  let last = 0;
  for (const match of source.matchAll(TOKEN)) {
    if (match.index > last) tokens.push({ type: 'text', start: last, end: match.index });
    const end = match.index + match[0].length;
    tokens.push(match[0].startsWith('\\') ? { type: 'escape', start: match.index, end } : { type: 'var', name: match[1], start: match.index, end });
    last = end;
  }
  if (last < source.length) tokens.push({ type: 'text', start: last, end: source.length });
  return tokens;
}

export const variableNames = (template) => tokenize(template).filter((token) => token.type === 'var').map((token) => token.name);

/**
 * References the relay would refuse: an unknown variable, or {{text:x}} naming a text the document does not hold.
 * `variables` is the closed list; `texts` the names of the document's named texts.
 */
export function templateProblems(template, variables, texts) {
  const known = new Set(variables);
  const named = new Set(texts);
  const problems = [];
  for (const token of tokenize(template)) {
    if (token.type !== 'var') continue;
    if (token.name.startsWith('text:')) {
      const name = token.name.slice(5).trim();
      if (!name) problems.push({ ...token, kind: 'unnamed' });
      else if (!named.has(name)) problems.push({ ...token, kind: 'missing_text', text: name });
    } else if (!known.has(token.name)) {
      problems.push({ ...token, kind: 'unknown' });
    }
  }
  return problems;
}

export function problemText(problem) {
  if (problem.kind === 'missing_text') return `Нет именованного текста «${problem.text}»: создайте его на вкладке «Тексты»`;
  if (problem.kind === 'unnamed') return 'После «text:» нужно имя текста';
  return `Неизвестная переменная {{${problem.name}}}`;
}

/** Put `snippet` over the selection [start, end) and report where the caret goes. */
export function insertAt(value, start, end, snippet) {
  const text = String(value ?? '');
  const from = Math.max(0, Math.min(start ?? text.length, text.length));
  const to = Math.max(from, Math.min(end ?? from, text.length));
  return { value: text.slice(0, from) + snippet + text.slice(to), caret: from + snippet.length };
}
