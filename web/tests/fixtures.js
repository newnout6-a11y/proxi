// All administration traffic is intercepted: these values never reach a relay or a model.
export const API_KEY = 'relay-e2e-placeholder-key';
export const MEMORY_KEY = 'relay.admin.key.v1';
export const NOW = Math.floor(Date.now() / 1000);

// reasoning_effort_control is what the relay publishes: "selectable" levels are forwarded, "accepted-ignored" routes take
// any level and drop it (they list none), "provider-managed" ones list none and refuse a level.
export const catalog = [
  { id: 'claude-opus-5-5', label: 'Claude Opus 5.5', provider: 'anthropic', context: 1000000, max_output: 128000,
    reasoning: true, tools: true, reasoning_effort_levels: ['low', 'medium', 'high', 'max'], reasoning_effort_control: 'selectable',
    routable: true, metadata_status: 'verified' },
  { id: 'gpt-5.5', label: 'GPT 5.5', provider: 'openai', context: 1050000, max_output: 128000,
    reasoning: true, tools: true, reasoning_effort_levels: ['low', 'medium', 'high'], reasoning_effort_control: 'selectable',
    routable: false, metadata_status: 'verified', route_status: 'unrouted', route_blocker: 'no_resource',
    route_reason: "No enabled account that can serve this model has a Retool resource for openai ('openai_provider_resource_name')" },
];

const extraCatalog = [
  { id: 'claude-sonnet-5-5', label: 'Claude Sonnet 5.5', provider: 'anthropic', context: 1000000, max_output: 64000,
    reasoning: true, tools: true, reasoning_effort_levels: ['low', 'medium', 'high'], reasoning_effort_control: 'selectable',
    routable: true, metadata_status: 'verified' },
  { id: 'claude-haiku-4-5', label: 'Claude Haiku 4.5', provider: 'anthropic', context: 200000, max_output: 32000,
    reasoning: false, tools: true, reasoning_effort_levels: [], reasoning_effort_control: 'accepted-ignored',
    routable: true, metadata_status: 'verified' },
  { id: 'gpt-5.5-codex', label: 'GPT 5.5 Codex', provider: 'openai', context: 400000, max_output: 128000,
    reasoning: true, tools: true, reasoning_effort_levels: ['low', 'medium', 'high'], reasoning_effort_control: 'selectable',
    routable: true, metadata_status: 'verified' },
  { id: 'gemini-3-pro', label: 'Gemini 3 Pro', provider: 'google', context: 2000000, max_output: 64000,
    reasoning: true, tools: true, reasoning_effort_levels: ['low', 'high'], reasoning_effort_control: 'selectable',
    routable: false, metadata_status: 'estimated',
    route_status: 'unverified', route_blocker: 'blocked', route_reason: 'Requires Google BYOK; no configured route was tested.' },
  { id: 'zai-org/GLM-5.3', label: 'Retool Power (GLM 5.3)', provider: 'retool-fast', context: 1000000, max_output: 128000,
    reasoning: true, tools: true, reasoning_effort_levels: [], reasoning_effort_control: 'accepted-ignored',
    routable: true, metadata_status: 'partial' },
  { id: 'gpt-4.1', label: 'GPT 4.1', provider: 'openai', context: 1047576, max_output: 32768,
    reasoning: false, tools: true, reasoning_effort_levels: [], reasoning_effort_control: 'provider-managed',
    routable: true, metadata_status: 'verified' },
];

// Mirrors admin._default_roles: the texts the relay really sends while relay_system_prompt is null.
export const DEFAULT_ROLES = [
  { id: 'relay', title: 'Роль relay по умолчанию',
    text: '[ROLE]\nYou are a coding assistant behind a local OpenAI/Anthropic relay. Follow the user\'s task and language. '
      + 'Use only provided capabilities; inspect before editing and verify changes.',
    applies_to: 'Запросы без инструментов и без инструкций клиента (curl, скрипты), а также режимы «Дополнить клиента» и «Заменить роль клиента»' },
  { id: 'native_tools', title: 'Роль для клиентов с инструментами',
    text: '[ROLE]\nYou are a coding assistant using the client\'s tools. Use those tools to inspect, edit and test that project.',
    applies_to: 'Клиент со своими инструментами (IDE), который не прислал собственные инструкции' },
];

// Mirrors admin._schema: every field has a group, a real description and, where it applies, a unit.
export const baseSchema = {
  relay_system_prompt_mode: { type: 'string', label: 'Применение инструкций', group: 'instructions',
    enum: ['standalone', 'append', 'replace'], restart_required: false },
  relay_system_prompt: { type: 'string', label: 'Инструкции для модели', group: 'instructions', nullable: true,
    max_bytes: 32768, max_prompt_share: 0.5, restart_required: false },
  model: { type: 'string', label: 'Модель по умолчанию', group: 'model', enum: catalog.map(({ id }) => id), restart_required: true,
    description: 'Модель для запросов, в которых клиент не указал свою' },
  show_thinking: { type: 'boolean', label: 'Передавать мышление модели', group: 'model', restart_required: true,
    description: 'Отдавать клиенту блоки мышления модели, когда Retool их присылает' },
  max_workers: { type: 'integer', label: 'Параллельные запросы', group: 'limits', min: 1, max: 512, restart_required: true,
    description: 'Сколько запросов relay обслуживает одновременно; остальные ждут' },
  rate_limit_rps: { type: 'number', label: 'Запросов в секунду', group: 'limits', min: 0, max: 10000, restart_required: false,
    description: 'Сколько запросов в секунду принимать от клиентов; 0 — без ограничения' },
};

const richSchema = {
  ...baseSchema,
  // Any model of the catalog can be the default, as on the relay.
  model: { ...baseSchema.model, enum: [...catalog, ...extraCatalog].map(({ id }) => id) },
  reasoning_effort: { type: 'string', label: 'Уровень мышления', group: 'model', restart_required: true,
    enum: ['auto', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
    description: 'Уровень мышления, если клиент его не передал; auto оставляет выбор модели' },
  allow_effort_downgrade: { type: 'boolean', label: 'Подбирать ближайший effort', group: 'model', restart_required: true,
    description: 'Если модель не поддерживает запрошенный уровень мышления, взять ближайший вместо ошибки' },
  max_prompt_bytes: { type: 'integer', label: 'Размер промпта', group: 'limits', unit: 'байт', min: 0, max: 1073741824,
    restart_required: true, description: 'Отклонять запросы с промптом больше этого размера; 0 — без ограничения' },
  session_mode: { type: 'string', label: 'Режим сессий', group: 'routing', enum: ['per_conversation', 'per_request', 'shared'], restart_required: true,
    description: 'Как диалоги клиентов привязываются к тредам Retool: свой тред на диалог, новый на каждый запрос или один общий' },
  min_account_balance: { type: 'number', label: 'Порог резерва', group: 'routing', unit: 'кредитов', min: 0, max: 1000000, restart_required: false,
    description: 'Аккаунт с балансом ниже порога уходит в резерв и получает запросы, только когда ниже порога все' },
  cache_keepwarm_seconds: { type: 'integer', label: 'Интервал прогрева', group: 'cache', unit: 'с', min: 0, max: 3600, restart_required: true,
    description: 'Как часто перечитывать начало диалога, чтобы кэш промпта не остыл; 0 — прогрев выключен' },
  upstream_empty_retries: { type: 'integer', label: 'Повторы пустого ответа', group: 'retries', min: 0, max: 4, restart_required: true,
    description: 'Сколько раз повторить запрос, если Retool вернул пустой ответ или одно мышление; каждый повтор платный' },
  enable_anthropic: { type: 'boolean', label: 'Anthropic API', group: 'api', restart_required: true,
    description: 'Принимать запросы Anthropic Messages на /v1/messages' },
  enable_chat: { type: 'boolean', label: 'Chat Completions API', group: 'api', restart_required: true,
    description: 'Принимать запросы OpenAI Chat Completions на /v1/chat/completions' },
  turn_timeout: { type: 'integer', label: 'Ожидание ответа', group: 'limits', unit: 'с', min: 1, max: 86400, restart_required: true,
    description: 'Максимальная длительность одного хода модели' },
};

export function ev(seq, kind, message, level = 'info', extra = {}) {
  return { seq, ts: NOW - 60 + seq, kind, category: kind.split('.')[0], level, message, data: {}, ...extra };
}

export const journal = [
  ev(1, 'relay.start', 'Relay запущен на 127.0.0.1:8787', 'success'),
  ev(2, 'turn.begin', 'Ход claude-opus-5-5 · effort high · direct', 'info',
    { turn_id: 'turn-alpha-1', data: { model: 'claude-opus-5-5', effort: 'high', transport: 'direct', dialect: 'anthropic' } }),
  ev(3, 'upstream.first_token', 'Первый токен через 2100 мс', 'success', { turn_id: 'turn-alpha-1', data: { ttft_ms: 2100 } }),
  ev(4, 'upstream.usage', 'Кредиты 0.25 · кэш read 900 / write 100', 'info',
    { turn_id: 'turn-alpha-1', data: { credits: 0.25, cache_read: 900, cache_write: 100 } }),
  ev(5, 'exec.result', 'Команда завершилась с кодом 1', 'error', { turn_id: 'turn-alpha-1', data: { command: 'dir', exit: 1 } }),
  ev(6, 'turn.end', 'Ход завершён: stop за 3.4 с', 'success', { turn_id: 'turn-alpha-1', data: { reason: 'stop', duration_ms: 3400 } }),
  ev(7, 'http.request', 'POST /v1/messages → 200', 'success', { request_id: 'req-1', data: { status: 200, route: 'anthropic' } }),
];

// Longer history with phases of different shape, used for screenshots only.
function turnEvents(seq, id, ago, o) {
  const t = NOW - ago;
  const out = [];
  let n = seq;
  const push = (kind, message, level, dt, data) => out.push({ seq: n++, ts: t + dt, kind, category: kind.split('.')[0], level, message, turn_id: id, data });
  push('turn.begin', `Ход ${o.model} · effort ${o.effort} · direct`, 'info', 0, { model: o.model, effort: o.effort, transport: 'direct', dialect: o.dialect || 'anthropic' });
  push('upstream.first_token', `Первый токен через ${o.ttft} мс`, 'success', o.ttft / 1000, { ttft_ms: o.ttft });
  if (o.thinking) push('upstream.thinking_end', `Мышление заняло ${o.thinking} мс`, 'info', (o.ttft + o.thinking) / 1000, { thinking_ms: o.thinking });
  if (o.args) push('tool.call', `Инструмент клиента: ${o.tool || 'Edit'}`, 'info', (o.ttft + (o.thinking || 0) + o.args) / 1000, { name: o.tool || 'Edit', args_ms: o.args });
  push('upstream.usage', `Кредиты ${o.credits} · кэш read ${o.read} / write ${o.write}`, 'info', o.duration / 1000 - 0.05,
    { credits: o.credits, cache_read: o.read, cache_write: o.write, input_tokens: o.input || 0 });
  if (o.error) push('upstream.error', 'Upstream вернул пустой ответ, повтор', 'warn', o.duration / 1000 - 0.04, {});
  if (o.status !== 'running') push('turn.end', `Ход завершён: ${o.status || 'stop'}`, o.status === 'error' ? 'error' : 'success', o.duration / 1000, { reason: o.status || 'stop', duration_ms: o.duration });
  return out;
}

function richJournal() {
  const items = [
    ev(1, 'relay.start', 'Relay запущен на 127.0.0.1:8787', 'success', { ts: NOW - 5400 }),
    ev(2, 'auth.refresh', 'Сессия Retool продлена (pro-main)', 'success', { ts: NOW - 5300 }),
    ...turnEvents(10, 'turn-1a2b3c', 2400, { model: 'claude-opus-5-5', effort: 'high', ttft: 1900, thinking: 6400, args: 2300, duration: 21800, credits: 0.84, read: 118400, write: 4200, status: 'tool_calls' }),
    ...turnEvents(30, 'turn-4d5e6f', 1700, { model: 'claude-sonnet-5-5', effort: 'medium', ttft: 1100, thinking: 1800, args: 900, duration: 9200, credits: 0.31, read: 84000, write: 1200 }),
    ...turnEvents(50, 'turn-7a8b9c', 900, { model: 'claude-opus-5-5', effort: 'high', ttft: 2600, thinking: 11200, duration: 26400, credits: 1.12, read: 126800, write: 9800 }),
    ...turnEvents(70, 'turn-0d1e2f', 520, { model: 'claude-haiku-4-5', effort: 'low', ttft: 420, duration: 2100, credits: 0.04, read: 22400, write: 300 }),
    ...turnEvents(90, 'turn-3a4b5c', 300, { model: 'claude-opus-5-5', effort: 'high', ttft: 3100, thinking: 4200, args: 5100, tool: 'Write', duration: 18900, credits: 0.67, read: 131000, write: 2400, error: true, status: 'tool_calls' }),
    ...turnEvents(110, 'turn-6d7e8f', 140, { model: 'gpt-5.5-codex', effort: 'medium', ttft: 1500, thinking: 2600, duration: 7400, credits: 0.22, read: 40800, write: 5200, dialect: 'openai', status: 'error' }),
    ...turnEvents(130, 'turn-9a0b1c', 40, { model: 'claude-sonnet-5-5', effort: 'medium', ttft: 1300, thinking: 900, duration: 6000, credits: 0.1, read: 52000, write: 800, status: 'running' }),
  ];
  const http = [
    ev(160, 'http.request', 'POST /v1/messages → 200', 'success', { ts: NOW - 36, request_id: 'req-81', data: { status: 200, route: 'anthropic' } }),
    ev(161, 'cache.keepwarm', 'Прогрев кэша диалога c-7f2a', 'info', { ts: NOW - 28, data: { conversation: 'c-7f2a' } }),
    ev(162, 'http.request', 'POST /v1/messages → 200', 'success', { ts: NOW - 17, request_id: 'req-82', data: { status: 200, route: 'anthropic' } }),
    ev(163, 'session.rotate', 'Диалог c-7f2a привязан к аккаунту pro-main', 'info', { ts: NOW - 9 }),
  ];
  return [...items, ...http].sort((a, b) => a.seq - b.seq);
}

function nextReset() {
  const d = new Date();
  const next = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1));
  return next.toISOString().slice(0, 10);
}
export const RESET_DATE = nextReset();

// What aggregate_balance reports: the sums over the accounts whose balance is known, and how many were left out.
export const COMPLETE_BALANCE = { available: 1495.09, monthly_allocation: 6000, used_month: 4504.91, reset_date: RESET_DATE, stale: false,
  accounts_total: 2, unknown_accounts: 0, partial: false };
// A pool whose second balance is unknown: the sums are those of the first account alone.
export const PARTIAL_BALANCE = { ...COMPLETE_BALANCE, available: 1479.89, monthly_allocation: 3000, used_month: 1520.11,
  unknown_accounts: 1, partial: true };

const pseudo = (i) => Math.abs((Math.sin((i + 1) * 12.9898) * 43758.5453) % 1);

/**
 * Deterministic metrics for a window: buckets plus totals that match them, in the shape of the relay's journal.metrics():
 * the credits include the cache refresh pings (keepwarm_* is their part), a window is covered completely, no attempt
 * is unknown and no turn waits for its price. A test overrides what it needs through mockAdmin's metricsPatch.
 */
export function metricsFor(windowSec) {
  const count = windowSec <= 3600 ? 60 : windowSec <= 21600 ? 72 : 96;
  const step = windowSec / count;
  const buckets = Array.from({ length: count }, (_, i) => {
    const t = Math.floor(NOW - windowSec + (i + 1) * step);
    const idle = pseudo(i * 3) < 0.22;
    const wave = 0.55 + 0.45 * Math.sin(i / (count / 9) + 1);
    const load = idle ? 0 : Math.max(0.15, wave * (0.5 + pseudo(i)));
    const requests = Math.round(load * 3.4 * (step / 60) ** 0.5);
    const credits = Number((load * 0.075 * (step / 60)).toFixed(4));
    return {
      t, requests, credits,
      keepwarm_credits: i % 7 === 3 ? Number((credits * 0.25).toFixed(4)) : 0,
      credits_unknown: 0, credits_complete: true,
      cache_read: Math.round(load * 4200 * (step / 60)),
      cache_write: (i === Math.floor(count * 0.55) || i === Math.floor(count * 0.82)) && !idle
        // The cache went cold: the whole prefix is written again
        ? Math.round(64000 * (step / 60))
        : Math.round(load * pseudo(i + 40) * 520 * (step / 60)),
      input: Math.round(load * pseudo(i + 80) * 90 * (step / 60)),
      ttft: requests ? Math.round(1300 + pseudo(i + 7) * 2400) : null,
    };
  });
  const sum = (key) => buckets.reduce((acc, b) => acc + b[key], 0);
  const read = sum('cache_read');
  const write = sum('cache_write');
  const input = sum('input');
  const scale = windowSec / 3600;
  return {
    window: windowSec,
    buckets,
    totals: {
      requests: sum('requests'), errors: Math.round(3 * scale), turns: Math.round(12 * scale), tools: Math.round(5 * scale),
      commands: Math.round(2 * scale), credits: Number(sum('credits').toFixed(4)), cache_read: read, cache_write: write,
      keepwarm_credits: Number(sum('keepwarm_credits').toFixed(4)), keepwarm_pings: buckets.filter((b) => b.keepwarm_credits > 0).length,
      keepwarm_unknown: 0, credits_unknown: 0, credits_complete: true, credits_in_progress: 0,
      input_tokens: input, cache_hit_ratio: read / (read + write + input), latency_p50: 3500, latency_p95: 7000,
      ttft_p50: 2100, ttft_p95: 4000,
    },
    models: { 'claude-opus-5-5': Math.round(9 * scale), 'gpt-5.5': Math.round(3 * scale) },
    // The journal reaches back past the window: covered_from <= now - window is what "complete" means
    coverage: { complete: true, window_start: Math.floor((NOW - windowSec) / 60) * 60, covered_from: NOW - windowSec - 600,
      covered_seconds: windowSec, oldest_event_ts: NOW - windowSec - 600, buffer_oldest_ts: NOW - 2400, buffer_events: 0, buffer_capacity: 20000 },
  };
}

const accounts = [
  { account: 'pro-main', state: 'active', routable: true, reserve: false, available: 1479.89, monthly_allocation: 3000,
    used_month: 1520.11, reset_date: RESET_DATE, stale: false, session_used: 2.75, turns: 12, auth_failures: 0,
    last_renewed_at: NOW - 30, error: null },
  { account: 'pro-spare', state: 'exhausted', routable: false, reserve: true, available: 15.2, monthly_allocation: 3000,
    used_month: 2984.8, reset_date: RESET_DATE, stale: false, session_used: 0, turns: 0, auth_failures: 1,
    last_renewed_at: NOW - 940, error: null },
];

function poolAccount(account, label, priority) {
  return { account, label, enabled: true, priority, state: 'active', routable: true, reserve: false, available: 0,
    monthly_allocation: 3000, used_month: 0, reset_date: RESET_DATE, stale: false, session_used: 0, turns: 0,
    auth_failures: 0, last_renewed_at: NOW - 60, last_balance_at: NOW - 45, error: null, pending: null };
}

function poolSummary(pool) {
  const live = pool.filter((a) => a.pending !== 'removed');
  const states = {};
  for (const a of live) states[a.state] = (states[a.state] || 0) + 1;
  return { total: live.length, enabled: live.filter((a) => a.enabled !== false).length,
    routable: live.filter((a) => a.routable && a.enabled !== false).length, reserve: live.filter((a) => a.reserve).length, states };
}

export function settingsResponse(settings, effective, schema) {
  const pending = Object.keys(settings).filter((key) => settings[key] !== effective[key]);
  return { settings, effective, schema, default_system_prompt: DEFAULT_ROLES[0].text, default_system_prompts: DEFAULT_ROLES,
    pending_restart: pending, restart_required: pending.length > 0, writable: true };
}

export async function mockAdmin(page, { rich = false, empty = false, overviewPatch = {}, metricsPatch = {} } = {}) {
  const schema = rich ? richSchema : baseSchema;
  const initial = { model: 'claude-opus-5-5', show_thinking: true, max_workers: 4, rate_limit_rps: 0,
    relay_system_prompt_mode: 'standalone', relay_system_prompt: null,
    ...(rich ? { reasoning_effort: 'high', session_mode: 'per_conversation', enable_anthropic: true, enable_chat: true,
      allow_effort_downgrade: false, max_prompt_bytes: 0,
      min_account_balance: 20, cache_keepwarm_seconds: 240, upstream_empty_retries: 1, turn_timeout: 900 } : {}) };
  const events = empty ? [] : rich ? richJournal() : journal;
  const models = rich ? [...catalog, ...extraCatalog] : catalog;
  const state = { settings: { ...initial }, effective: { ...initial }, requests: [], patches: [], sessions: [],
    stream: [], streamRequests: 0, cleared: false, accountRequests: [],
    pool: accounts.map((a, i) => ({ ...poolAccount(a.account, i ? 'запасной' : 'основной', i), ...a })) };
  const overview = {
    health: { ok: true, upstream: 'ok', error: null }, uptime_seconds: 5432,
    // The rich overview is the densest one: it carries the partial balance, the longest mark the summary can show.
    balance: rich ? PARTIAL_BALANCE : COMPLETE_BALANCE,
    accounts, accounts_summary: { total: 2, routable: 1, reserve: 1, states: { active: 1, exhausted: 1 } },
    limits: { min_account_balance: 20, account_balance_interval: 60, upstream_empty_retries: 2, max_workers: 4, max_conversations: 64 },
    usage: { session_used: 2.75, turns: 2 },
    session: { keepalive: true, last_success_at: NOW - 30, error: null, persistence_error: null, accounts: { enabled: 2, failing: 0 } },
    sessions: { active: 1, cached: 3, mode: 'per_conversation' },
    models: { total: models.length, routable: models.filter((m) => m.routable).length, default: 'claude-opus-5-5' },
    // The values in force: what the relay is running with, not what is saved for the next start.
    resources: { turn_timeout: 900 },
    connection: { openai_base_url: 'http://127.0.0.1:8787/v1', anthropic_base_url: 'http://127.0.0.1:8787' },
    ...overviewPatch,
  };

  await page.route('**/admin/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname: path } = url;
    const headers = request.headers();
    state.requests.push({ path, method: request.method(), headers, url: request.url() });
    const reply = (status, json) => route.fulfill({ status, json });
    if (headers.authorization !== `Bearer ${API_KEY}`) {
      return reply(401, { error: { message: 'invalid relay API key', code: 'unauthorized' } });
    }
    const method = request.method();
    if (path === '/admin/api/overview') return reply(200, overview);
    if (path === '/admin/api/metrics') {
      const windowSec = Number(url.searchParams.get('window')) || 3600;
      const metrics = metricsFor(windowSec);
      return reply(200, { ...metrics, ...metricsPatch, totals: { ...metrics.totals, ...metricsPatch.totals }, buffered: events.length, last_seq: events.length });
    }
    if (path === '/admin/api/models') return reply(200, { data: models, default_model: 'claude-opus-5-5' });
    if (path === '/admin/api/events') return reply(200, { data: state.cleared ? [] : events, last_seq: events.length ? events[events.length - 1].seq : 0 });
    if (path === '/admin/api/events/stream') {
      state.streamRequests += 1;
      const items = state.stream.splice(0);
      const body = `event: hello\ndata: ${JSON.stringify({ last_seq: events.length })}\n\n`
        + items.map((item) => `event: journal\ndata: ${JSON.stringify(item)}\n\n`).join('');
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body });
    }
    if (path === '/admin/api/session' && method === 'POST') {
      state.sessions.push({ body: request.postDataJSON(), headers });
      return reply(200, { ok: true });
    }
    if (path === '/admin/api/events/clear' && method === 'POST') {
      state.cleared = true;
      return reply(200, { ok: true, last_seq: 0 });
    }
    if (path === '/admin/api/accounts' && method === 'GET') {
      return reply(200, { accounts: state.pool, summary: poolSummary(state.pool), restart_required: state.pool.some((a) => a.pending),
        limits: { min_account_balance: 20, account_balance_interval: 300, account_refresh_parallel: 3 } });
    }
    if (path === '/admin/api/accounts' && method === 'POST') {
      const body = request.postDataJSON();
      state.accountRequests.push({ method, path, body, headers });
      const added = { ...poolAccount('acc-new', body.label || '', 0), available: null, monthly_allocation: null, used_month: null,
        reset_date: null, last_renewed_at: null, last_balance_at: null, pending: 'added', state: 'pending', routable: false };
      state.pool.push(added);
      return reply(200, { account: added, restart_required: true });
    }
    const accountMatch = path.match(/^\/admin\/api\/accounts\/([^/]+)(?:\/(balance|renew))?$/);
    if (accountMatch) {
      const [, tag, action] = accountMatch;
      const account = state.pool.find((a) => a.account === tag);
      if (!account) return reply(404, { error: { message: 'Unknown account' } });
      state.accountRequests.push({ method, path, body: request.postData() ? request.postDataJSON() : null, headers });
      if (action === 'balance' && method === 'POST') { account.last_balance_at = NOW; return reply(200, { account }); }
      if (action === 'renew' && method === 'POST') {
        if (state.renewFails) return reply(200, { ok: false, error: 'session renewal failed: HTTP 401', account });
        account.last_renewed_at = NOW;
        return reply(200, { ok: true, error: null, account });
      }
      if (!action && method === 'PATCH') { Object.assign(account, request.postDataJSON()); return reply(200, { account }); }
      if (!action && method === 'DELETE') { account.pending = 'removed'; return reply(200, { ok: true, pending: 'removed', restart_required: true }); }
      return reply(405, { error: { message: 'Method not allowed' } });
    }
    if (path === '/admin/api/settings' && method === 'GET') return reply(200, settingsResponse(state.settings, state.effective, schema));
    if (path === '/admin/api/settings' && method === 'PATCH') {
      const body = request.postDataJSON();
      state.patches.push({ body, headers });
      if (state.patchDelay) await new Promise((resolve) => setTimeout(resolve, state.patchDelay));
      state.settings = { ...state.settings, ...body.settings };
      const applied = Object.keys(body.settings).filter((key) => !schema[key].restart_required);
      for (const key of applied) state.effective[key] = body.settings[key];
      return reply(200, { ...settingsResponse(state.settings, state.effective, schema), saved: true, changed: Object.keys(body.settings), applied });
    }
    return reply(404, { error: { message: 'Unknown admin endpoint' } });
  });
  return state;
}
