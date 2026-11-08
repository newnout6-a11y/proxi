const NBSP = '\u00a0';
const integer = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const cache = new Map();

export const isNum = (value) => typeof value === 'number' && Number.isFinite(value);

function formatter(min, max) {
  const key = `${min}:${max}`;
  if (!cache.has(key)) cache.set(key, new Intl.NumberFormat('ru-RU', { minimumFractionDigits: min, maximumFractionDigits: max }));
  return cache.get(key);
}

export function fmtInt(value) {
  return isNum(value) ? integer.format(Math.round(value)) : '—';
}

/** Fixed number of decimals, ru-RU grouping. */
export function fmtFixed(value, digits = 2) {
  return isNum(value) ? formatter(digits, digits).format(value) : '—';
}

export function fmtNum(value, digits = 2) {
  return isNum(value) ? formatter(0, digits).format(value) : '—';
}

/** Reported end-to-end output rate; missing/invalid telemetry is not a zero rate. */
export function fmtSpeed(value) {
  if (!isNum(value) || value < 0) return '—';
  if (value > 0 && value < 0.1) return `<0,1${NBSP}ток/с`;
  return `${fmtNum(value, 1)}${NBSP}ток/с`;
}

export function fmtMs(ms) {
  if (!isNum(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)}${NBSP}мс`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1).replace('.', ',')}${NBSP}с`;
}

export function fmtClock(ts) {
  if (!isNum(ts)) return '—';
  const date = new Date(ts * 1000);
  return `${date.toLocaleTimeString('ru-RU', { hour12: false })}.${String(date.getMilliseconds()).padStart(3, '0')}`;
}

export function fmtShortTime(ts) {
  if (!isNum(ts)) return '—';
  return new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function fmtDateTime(ts) {
  if (!isNum(ts)) return '—';
  return new Date(ts * 1000).toLocaleString('ru-RU', { hour12: false });
}

/** Accepts a date (2026-12-01) or a full ISO timestamp (2026-11-29T10:36:15Z). */
export function parseDay(iso) {
  if (!iso) return null;
  const text = String(iso);
  const date = new Date(text.includes('T') ? text : `${text}T00:00:00`);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function fmtDay(iso) {
  const date = parseDay(iso);
  if (!date) return iso ? String(iso) : '—';
  return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
}

/** Russian plural: plural(5, ['день', 'дня', 'дней']). */
export function plural(n, forms) {
  const a = Math.abs(Math.round(n)) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}

export function fmtAgo(ts, now = Date.now() / 1000) {
  if (!isNum(ts)) return '—';
  const delta = Math.max(0, now - ts);
  if (delta < 5) return 'только что';
  if (delta < 60) return `${Math.floor(delta)}${NBSP}с назад`;
  if (delta < 3600) return `${Math.floor(delta / 60)}${NBSP}мин назад`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}${NBSP}ч назад`;
  return `${Math.floor(delta / 86400)}${NBSP}д назад`;
}

export function fmtUptime(seconds) {
  if (!isNum(seconds)) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}${NBSP}д ${hours}${NBSP}ч`;
  if (hours) return `${hours}${NBSP}ч ${minutes}${NBSP}мин`;
  return `${minutes}${NBSP}мин ${Math.floor(seconds % 60)}${NBSP}с`;
}

export function fmtTokens(value) {
  if (!isNum(value)) return '—';
  if (value >= 1e6) return `${fmtNum(value / 1e6, 2)}${NBSP}млн`;
  if (value >= 1e3) return `${fmtNum(value / 1e3, 1)}${NBSP}тыс`;
  return fmtInt(value);
}

export function fmtPercent(value, digits = 0) {
  return isNum(value) ? `${fmtNum(value * 100, digits)}%` : '—';
}

export function fmtDuration(days) {
  if (!isNum(days)) return '—';
  if (days < 1) return `${fmtNum(Math.max(days * 24, 0.1), 1)}${NBSP}ч`;
  if (days < 10) return `${fmtNum(days, 1)}${NBSP}дн`;
  return `${fmtInt(days)}${NBSP}дн`;
}
