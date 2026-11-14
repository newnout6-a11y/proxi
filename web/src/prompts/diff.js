// A plain line diff: trim what both sides share, then a longest-common-subsequence table for the middle.
const TABLE_LIMIT = 4_000_000;

const linesOf = (text) => (text === '' || text === null || text === undefined ? [] : String(text).split('\n'));

/**
 * Rows of {type: 'same' | 'del' | 'add', text, before, after}; before/after are 1-based line numbers on that side.
 * A middle part too large for the table is shown as wholly removed and wholly added rather than guessed at.
 */
export function diffLines(before, after) {
  const a = linesOf(before);
  const b = linesOf(after);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;
  const rows = [];
  for (let i = 0; i < head; i += 1) rows.push({ type: 'same', text: a[i], before: i + 1, after: i + 1 });
  const midA = a.slice(head, a.length - tail);
  const midB = b.slice(head, b.length - tail);
  for (const row of diffMiddle(midA, midB)) {
    rows.push({ ...row, before: row.type === 'add' ? null : head + row.a + 1, after: row.type === 'del' ? null : head + row.b + 1 });
  }
  for (let i = 0; i < tail; i += 1) {
    rows.push({ type: 'same', text: a[a.length - tail + i], before: a.length - tail + i + 1, after: b.length - tail + i + 1 });
  }
  return rows.map(({ a: _a, b: _b, ...row }) => row);
}

function diffMiddle(a, b) {
  if (!a.length && !b.length) return [];
  if (!a.length) return b.map((text, j) => ({ type: 'add', text, b: j }));
  if (!b.length) return a.map((text, i) => ({ type: 'del', text, a: i }));
  const n = a.length;
  const m = b.length;
  if (n * m > TABLE_LIMIT) {
    return [...a.map((text, i) => ({ type: 'del', text, a: i })), ...b.map((text, j) => ({ type: 'add', text, b: j }))];
  }
  const width = m + 1;
  const table = new (Math.min(n, m) < 65535 ? Uint16Array : Uint32Array)((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i * width + j] = a[i] === b[j] ? table[(i + 1) * width + j + 1] + 1 : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ type: 'same', text: a[i], a: i, b: j }); i += 1; j += 1; }
    else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) { rows.push({ type: 'del', text: a[i], a: i }); i += 1; }
    else { rows.push({ type: 'add', text: b[j], b: j }); j += 1; }
  }
  for (; i < n; i += 1) rows.push({ type: 'del', text: a[i], a: i });
  for (; j < m; j += 1) rows.push({ type: 'add', text: b[j], b: j });
  return rows;
}

export function diffStats(rows) {
  let added = 0;
  let removed = 0;
  for (const row of rows) {
    if (row.type === 'add') added += 1;
    else if (row.type === 'del') removed += 1;
  }
  return { added, removed, same: rows.length - added - removed, changed: added + removed > 0 };
}

/** Collapse long unchanged stretches into {type: 'fold', rows} keeping `context` lines around every change. */
export function foldDiff(rows, context = 2) {
  const keep = new Array(rows.length).fill(false);
  rows.forEach((row, index) => {
    if (row.type === 'same') return;
    for (let k = Math.max(0, index - context); k <= Math.min(rows.length - 1, index + context); k += 1) keep[k] = true;
  });
  const folded = [];
  let run = [];
  const flush = () => {
    if (!run.length) return;
    if (run.length > 2) folded.push({ type: 'fold', rows: run });
    else folded.push(...run);
    run = [];
  };
  rows.forEach((row, index) => {
    if (keep[index]) { flush(); folded.push(row); } else run.push(row);
  });
  flush();
  return folded;
}
