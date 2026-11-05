import { fmtDay, fmtShortTime, isNum } from './format.js';

const NBSP = '\u00a0';

/**
 * How much of a metrics window the relay's aggregates really cover: the `coverage` of /admin/api/metrics
 * (older relays do not send it). Null when the window is covered completely or the relay says nothing, so
 * only an explicit `complete: false` is ever reported as a gap.
 * `from` is the instant the figures are complete from (else the oldest event the relay has seen).
 */
export function coverageOf(metrics, windowSec) {
  const raw = metrics?.coverage;
  if (!raw || raw.complete !== false) return null;
  return {
    from: [raw.covered_from, raw.oldest_event_ts].find(isNum) ?? null,
    seconds: isNum(raw.covered_seconds) ? Math.min(windowSec, Math.max(0, raw.covered_seconds)) : null,
  };
}

/**
 * How many leading buckets end before the coverage begins: nothing is known about them, they are not zeros.
 * The newest bucket never counts, it takes every event up to now.
 */
export function uncoveredBuckets(buckets, windowSec, from) {
  const n = buckets.length;
  if (n < 2 || !isNum(from) || !(windowSec > 0)) return 0;
  const step = windowSec / n;
  let count = 0;
  while (count < n - 1 && isNum(buckets[count].t) && buckets[count].t + step <= from) count += 1;
  return count;
}

/** A moment as the time of day, with the date when it is not today. */
export function fmtSince(ts, now = Date.now() / 1000) {
  if (!isNum(ts)) return '—';
  const moment = new Date(ts * 1000);
  return moment.toDateString() === new Date(now * 1000).toDateString()
    ? fmtShortTime(ts) : `${fmtDay(moment.toISOString())}, ${fmtShortTime(ts)}`;
}

/** A length of time in words: "40 с", "12 мин", "1,5 ч". */
export function fmtSpan(seconds) {
  if (!isNum(seconds)) return '—';
  if (seconds < 60) return `${Math.round(seconds)}${NBSP}с`;
  if (seconds < 7200) return `${Math.round(seconds / 60)}${NBSP}мин`;
  return `${String(Math.round(seconds / 360) / 10).replace('.', ',')}${NBSP}ч`;
}
