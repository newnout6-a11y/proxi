export const CATEGORIES = {
  http: { label: 'HTTP' },
  turn: { label: 'Ход' },
  upstream: { label: 'Upstream' },
  tool: { label: 'Инструмент' },
  exec: { label: 'Команда' },
  session: { label: 'Сессия' },
  auth: { label: 'Авторизация' },
  cache: { label: 'Кэш' },
  prompt: { label: 'Промпты' },
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
  interrupted: 'прерван',
};

export const STATUS_TONE = { running: 'accent', stop: 'pos', tool_calls: 'muted', cancelled: 'warn', timeout: 'warn', error: 'neg', interrupted: 'warn' };

export const ACCOUNT_STATE = {
  active: { label: 'в работе', tone: 'pos' },
  exhausted: { label: 'исчерпан', tone: 'warn' },
  auth_dead: { label: 'нет доступа', tone: 'neg' },
  rate_limited: { label: 'пауза: часовой лимит', tone: 'warn' },
  pending: { label: 'не подключён', tone: 'muted' },
};

/**
 * The model the catalog's quick test pings: the one picked, else the default model when it has a route, else the first
 * model with a route. The select lists only models with a route, and the default model without one was pinged while
 * the select showed another model (audit A14-11).
 */
export function pingModelOf(data, picked) {
  const routable = (data?.data || []).filter((model) => model.routable).map((model) => model.id);
  if (picked && routable.includes(picked)) return picked;
  if (routable.includes(data?.default_model)) return data.default_model;
  return routable[0] || '';
}

// The provider the relay keeps OpenCode's model list in (scripts/sync_opencode_catalog.py: PROVIDER).
export const OPENCODE_PROVIDER = 'retool-relay';

/**
 * The OpenCode settings the "Подключение IDE" panel offers: the provider the relay's sync fills in, with the default model
 * to start from. A provider of another name ("proxi") without models showed no models in OpenCode, and the relay's sync,
 * which updates only "retool-relay", failed on it (audit A14-06).
 */
export function opencodeSnippet(baseUrl, defaultModel) {
  return JSON.stringify({
    provider: {
      [OPENCODE_PROVIDER]: {
        npm: '@ai-sdk/openai-compatible',
        name: 'Proxi relay',
        options: { baseURL: baseUrl, apiKey: '{env:PROXI_API_KEY}' },
        // The relay's sync ("Синхр. OpenCode и Harness" or its own after each catalog look) writes every model here.
        models: { [defaultModel]: { name: defaultModel } },
      },
    },
    model: `${OPENCODE_PROVIDER}/${defaultModel}`,
  }, null, 2);
}
