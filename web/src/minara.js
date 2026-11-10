/** Minara accounts in the admin: the tokens field of the add form, the token expiry of an account row and the report of
a bulk add. Pure functions, so node --test checks them without a browser. */
import { fmtDuration, isNum } from './format.js';

const DAY = 86400;
/** A token with less time left is shown as expiring; the relay warns in its journal from the same moment. */
export const WARN_DAYS = 3;

/** The non-empty lines of the tokens field, each with its line number (1-based), trimmed. */
export function tokenLines(text) {
  return String(text || '').split(/\r?\n/).map((line, index) => ({ line: index + 1, token: line.trim() }))
    .filter((item) => item.token);
}

/** A moment as a day of the month (3 декабря); with the time when `withTime` (11 ноября, 14:05). */
export function fmtDate(ts, withTime = false) {
  if (!isNum(ts)) return '—';
  const date = new Date(ts * 1000);
  const day = date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
  return withTime ? `${day}, ${date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', hour12: false })}` : day;
}

/** Where a token stands at `now`: state expired | expiring (under WARN_DAYS left) | valid | unknown, and its words. */
export function tokenExpiry(expiresAt, now = Date.now() / 1000) {
  if (!isNum(expiresAt)) return { state: 'unknown', text: 'токен: срок неизвестен' };
  const left = expiresAt - now;
  if (left <= 0) return { state: 'expired', text: `токен истёк ${fmtDate(expiresAt)}, нужен новый` };
  if (left < WARN_DAYS * DAY) {
    return { state: 'expiring', text: `токен до ${fmtDate(expiresAt, true)}, осталось ${fmtDuration(left / DAY)}` };
  }
  return { state: 'valid', text: `токен до ${fmtDate(expiresAt)}, ещё ${fmtDuration(left / DAY)}` };
}

/** Why a line is not a token, in the form's words; the server's own message stands for anything else. */
const INVALID = {
  empty: 'пустая строка',
  format: 'в токене только латиница, цифры и знаки, без пробелов',
  cut: 'начало JWT без остального: похоже, токен обрезан при копировании',
};

/** One token of a bulk add as the report shows it: the tag, its tone and what to know. `lineOf` turns the position of a
token in the request into its line in the field. The answer never holds the token itself. */
export function describeResult(result, lineOf = (position) => position) {
  const name = result.label || result.account || 'аккаунт';
  switch (result.status) {
    case 'added':
      return { tone: 'pos', tag: 'добавлен',
        text: isNum(result.expires_at) ? `${name}, токен до ${fmtDate(result.expires_at)}` : `${name}, срок токена неизвестен` };
    case 'renewed':
      return { tone: 'pos', tag: 'токен обновлён',
        text: isNum(result.expires_at) ? `${name}, новый токен до ${fmtDate(result.expires_at)}` : name };
    case 'duplicate': {
      const user = result.reason === 'same_user';
      if (isNum(result.repeat_of)) {
        return { tone: 'muted', tag: 'повтор',
          text: user ? `тот же пользователь Minara, что в строке ${lineOf(result.repeat_of)}` : `повторяет строку ${lineOf(result.repeat_of)}` };
      }
      return { tone: 'muted', tag: 'уже в пуле',
        text: user ? `тот же пользователь Minara, что у ${name}${result.newer ? '; этот токен новее, но аккаунт берёт токен из переменной окружения' : ''}`
          : name };
    }
    case 'expired':
      return { tone: 'neg', tag: 'истёк', text: isNum(result.expires_at) ? fmtDate(result.expires_at, true) : 'срок прошёл' };
    default:
      return { tone: 'neg', tag: 'не токен', text: INVALID[result.problem] || result.error || 'не похоже на токен Minara' };
  }
}
