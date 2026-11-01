const integer = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

export const isNum = (value) => typeof value === 'number' && Number.isFinite(value);

export function fmtInt(value) {
  return isNum(value) ? integer.format(Math.round(value)) : '—';
}

export function fmtNum(value, digits = 2) {
  if (!isNum(value)) return '—';
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits }).format(value);
}

export function fmtMs(ms) {
  if (!isNum(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} мс`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1).replace('.', ',')} с`;
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

export function fmtAgo(ts, now = Date.now() / 1000) {
  if (!isNum(ts)) return '—';
  const delta = Math.max(0, now - ts);
  if (delta < 5) return 'только что';
  if (delta < 60) return `${Math.floor(delta)} с назад`;
  if (delta < 3600) return `${Math.floor(delta / 60)} мин назад`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} ч назад`;
  return `${Math.floor(delta / 86400)} д назад`;
}

export function fmtUptime(seconds) {
  if (!isNum(seconds)) return '—';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} д ${hours} ч`;
  if (hours) return `${hours} ч ${minutes} мин`;
  return `${minutes} мин ${Math.floor(seconds % 60)} с`;
}

export function fmtTokens(value) {
  if (!isNum(value)) return '—';
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)}k`;
  return String(Math.round(value));
}

export function fmtPercent(value) {
  return isNum(value) ? `${Math.round(value * 100)}%` : '—';
}
