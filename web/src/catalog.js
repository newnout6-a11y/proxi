export const CATEGORIES = {
  http: { label: 'HTTP' },
  turn: { label: 'Ход' },
  upstream: { label: 'Upstream' },
  tool: { label: 'Инструмент' },
  exec: { label: 'Команда' },
  session: { label: 'Сессия' },
  auth: { label: 'Авторизация' },
  cache: { label: 'Кэш' },
  admin: { label: 'Админ' },
  relay: { label: 'Relay' },
};

export const LEVELS = {
  debug: { label: 'debug' },
  info: { label: 'info' },
  success: { label: 'ok' },
  warn: { label: 'warn' },
  error: { label: 'error' },
};

export function category(name) {
  return CATEGORIES[name] || { label: name || 'прочее' };
}

export const TURN_STATUS = {
  running: 'идёт',
  stop: 'готово',
  tool_calls: 'инструменты',
  cancelled: 'отменён',
  timeout: 'таймаут',
  error: 'ошибка',
};

export const STATUS_TONE = { running: 'accent', stop: 'pos', tool_calls: 'muted', cancelled: 'warn', timeout: 'warn', error: 'neg' };

export const ACCOUNT_STATE = {
  active: { label: 'в работе', tone: 'pos' },
  exhausted: { label: 'исчерпан', tone: 'warn' },
  auth_dead: { label: 'нет доступа', tone: 'neg' },
};
