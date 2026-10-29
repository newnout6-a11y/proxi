import { test, expect } from '@playwright/test';

// All administration traffic is intercepted: these values never reach a relay or a model.
const API_KEY = 'relay-e2e-placeholder-key';
const MEMORY_KEY = 'relay.admin.key.v1';
const NOW = Math.floor(Date.now() / 1000);

const catalog = [
  { id: 'claude-opus-5-5', label: 'Claude Opus 5.5', provider: 'anthropic', context: 1000000, max_output: 128000,
    reasoning: true, tools: true, reasoning_effort_levels: ['low', 'medium', 'high', 'max'], routable: true,
    metadata_status: 'verified' },
  { id: 'gpt-5.5', label: 'GPT 5.5', provider: 'openai', context: 1050000, max_output: 128000,
    reasoning: true, tools: true, reasoning_effort_levels: ['low', 'medium', 'high'], routable: false,
    metadata_status: 'verified' },
];

const schema = {
  model: { type: 'string', label: 'Модель по умолчанию', enum: catalog.map(({ id }) => id), restart_required: true, description: 'После перезапуска.' },
  show_thinking: { type: 'boolean', label: 'Передавать мышление модели', restart_required: true, description: 'После перезапуска.' },
  max_workers: { type: 'integer', label: 'Параллельные запросы', min: 1, restart_required: true, description: 'После перезапуска.' },
  rate_limit_rps: { type: 'number', label: 'Запросов в секунду (0 — без лимита)', min: 0, restart_required: false, description: 'Сразу.' },
};

function ev(seq, kind, message, level = 'info', extra = {}) {
  return { seq, ts: NOW - 60 + seq, kind, category: kind.split('.')[0], level, message, data: {}, ...extra };
}

const journal = [
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

function settingsResponse(settings, effective) {
  const pending = Object.keys(settings).filter((key) => settings[key] !== effective[key]);
  return { settings, effective, schema, pending_restart: pending, restart_required: pending.length > 0, writable: true };
}

async function mockAdmin(page) {
  const initial = { model: 'claude-opus-5-5', show_thinking: true, max_workers: 4, rate_limit_rps: 0 };
  const state = { settings: { ...initial }, effective: { ...initial }, requests: [], patches: [], sessions: [],
    stream: [], streamRequests: 0, cleared: false };
  const overview = {
    health: { ok: true, upstream: 'ok', error: null }, uptime_seconds: 5432,
    balance: { available: 874.25, monthly_allocation: 1000, reset_date: '2026-11-01', stale: false },
    usage: { session_used: 2.75, turns: 2 },
    session: { keepalive: true, last_success_at: NOW - 30, error: null },
    sessions: { active: 1, cached: 3, mode: 'per_conversation' },
    models: { total: 2, routable: 1, default: 'claude-opus-5-5' },
    connection: { openai_base_url: 'http://127.0.0.1:8787/v1', anthropic_base_url: 'http://127.0.0.1:8787' },
  };
  const metrics = {
    buckets: Array.from({ length: 60 }, (_, i) => ({ t: NOW - 3600 + i * 60, requests: i % 7, ttft: i % 5 ? 1800 + i * 10 : null, credits: i % 3 ? 0.1 : 0 })),
    totals: { requests: 41, errors: 3, turns: 12, tools: 5, commands: 2, credits: 1.234, cache_read: 900, cache_write: 100,
      input_tokens: 0, cache_hit_ratio: 0.9, latency_p50: 3500, latency_p95: 7000, ttft_p50: 2100, ttft_p95: 4000 },
    models: { 'claude-opus-5-5': 9, 'gpt-5.5': 3 }, buffered: journal.length, last_seq: journal.length,
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
    if (path === '/admin/api/metrics') return reply(200, metrics);
    if (path === '/admin/api/models') return reply(200, { data: catalog, default_model: 'claude-opus-5-5' });
    if (path === '/admin/api/events') return reply(200, { data: state.cleared ? [] : journal, last_seq: journal.length });
    if (path === '/admin/api/events/stream') {
      state.streamRequests += 1;
      const items = state.stream.splice(0);
      const body = `event: hello\ndata: ${JSON.stringify({ last_seq: journal.length })}\n\n`
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
    if (path === '/admin/api/settings' && method === 'GET') return reply(200, settingsResponse(state.settings, state.effective));
    if (path === '/admin/api/settings' && method === 'PATCH') {
      const body = request.postDataJSON();
      state.patches.push({ body, headers });
      state.settings = { ...state.settings, ...body.settings };
      const applied = Object.keys(body.settings).filter((key) => !schema[key].restart_required);
      for (const key of applied) state.effective[key] = body.settings[key];
      return reply(200, { ...settingsResponse(state.settings, state.effective), saved: true, changed: Object.keys(body.settings), applied });
    }
    return reply(404, { error: { message: 'Unknown admin endpoint' } });
  });
  return state;
}

async function login(page, { remember = false } = {}) {
  await page.goto('./');
  await page.getByLabel('Relay key').fill(API_KEY);
  await page.getByLabel('Запомнить до закрытия вкладки').setChecked(remember);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('heading', { name: 'Всё под контролем.' })).toBeVisible();
}

const nav = (page, name) => page.getByRole('navigation').getByRole('button', { name: new RegExp(name) }).click();

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => { throw error; });
});

test('rejects an invalid key, then logs in and announces the session', async ({ page }) => {
  const state = await mockAdmin(page);
  await page.goto('./');
  await expect(page).toHaveTitle(/relay/i);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('alert')).toContainText('Введите relay key');
  expect(state.requests).toEqual([]);
  await page.getByLabel('Relay key').fill('invalid-e2e-key');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('alert')).toContainText('Неверный ключ');
  await page.getByLabel('Relay key').fill(API_KEY);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('heading', { name: 'Всё под контролем.' })).toBeVisible();
  await expect.poll(() => state.sessions.length).toBe(1);
  expect(state.sessions[0].body).toEqual({ action: 'login' });
  expect(state.sessions[0].headers['x-admin-request']).toBe('1');
  expect(state.requests.every(({ url }) => !url.includes(API_KEY) && !url.includes('invalid-e2e-key'))).toBe(true);
});

test('keeps the key in memory unless tab storage is chosen, and logs out', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  expect(await page.evaluate((key) => sessionStorage.getItem(key), MEMORY_KEY)).toBeNull();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(API_KEY);
  await page.reload();
  await expect(page.getByLabel('Relay key')).toBeVisible();
  await login(page, { remember: true });
  expect(await page.evaluate((key) => sessionStorage.getItem(key), MEMORY_KEY)).toBe(API_KEY);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Всё под контролем.' })).toBeVisible();
  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect(page.getByLabel('Relay key')).toBeVisible();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), MEMORY_KEY)).toBeNull();
});

test('overview shows balance, metrics and the live feed', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await expect(page.getByText('874,25')).toBeVisible();
  await expect(page.getByText('90%')).toBeVisible();
  await expect(page.getByText('upstream ok')).toBeVisible();
  await expect(page.locator('.feed-row', { hasText: 'POST /v1/messages → 200' })).toBeVisible();
  await expect(page.locator('.bars li')).toHaveCount(2);
  await expect(page.locator('code', { hasText: 'http://127.0.0.1:8787/v1' })).toBeVisible();
});

test('journal streams new events, filters, searches and expands details', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Журнал');
  await expect(page.getByRole('heading', { name: 'Журнал событий' })).toBeVisible();
  await expect(page.locator('.log-row')).toHaveCount(journal.length);

  state.stream.push(ev(8, 'tool.call', 'Инструмент клиента: live-marker', 'info', { turn_id: 'turn-alpha-1' }));
  await expect(page.locator('.log-row', { hasText: 'live-marker' })).toBeVisible({ timeout: 10000 });
  await expect(page.locator('.log-row .seq').first()).toHaveText('#8');

  await page.locator('.chip.level-error').click();
  await expect(page.locator('.log-row')).toHaveCount(1);
  await expect(page.locator('.log-row').first()).toContainText('кодом 1');
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();

  await page.getByLabel('Поиск по журналу').fill('relay запущен');
  await expect(page.locator('.log-row')).toHaveCount(1);
  await page.locator('.log-line').first().click();
  await expect(page.locator('.event-detail')).toContainText('relay.start');
  await page.getByLabel('Очистить поиск').click();

  await page.getByRole('button', { name: 'Пауза' }).click();
  state.stream.push(ev(9, 'auth.refresh', 'Сессия Retool продлена (paused-marker)', 'success'));
  await expect(page.getByRole('button', { name: /Продолжить \(\+1\)/ })).toBeVisible({ timeout: 10000 });
  await expect(page.locator('.log-row', { hasText: 'paused-marker' })).toHaveCount(0);
  await page.getByRole('button', { name: /Продолжить/ }).first().click();
  await expect(page.locator('.log-row', { hasText: 'paused-marker' })).toBeVisible();
});

test('journal clear asks for confirmation and empties the buffer', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Журнал');
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Очистить' }).click();
  await expect(page.getByText('Журнал пуст')).toBeVisible();
  expect(state.requests.some(({ path, headers }) => path === '/admin/api/events/clear' && headers['x-admin-request'] === '1')).toBe(true);
});

test('turn traces aggregate journal events into a timeline', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Ходы');
  const trace = page.locator('.trace').first();
  await expect(trace).toContainText('claude-opus-5-5');
  await expect(trace).toContainText('effort high');
  await expect(trace).toContainText('2,10 с');
  await expect(trace).toContainText('готово');
  await trace.locator('.trace-head').click();
  await expect(trace.locator('.step')).toHaveCount(5);
  await expect(trace.locator('.timeline-dot')).toHaveCount(5);
});

test('models can be searched and filtered by route', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Модели');
  await expect(page.locator('.model-card')).toHaveCount(2);
  await page.getByLabel('Поиск модели').fill('gpt');
  await expect(page.locator('.model-card')).toHaveCount(1);
  await page.getByLabel('Поиск модели').fill('');
  await page.getByLabel('Только с маршрутом').check();
  await expect(page.locator('.model-card')).toHaveCount(1);
  await expect(page.locator('.model-card')).toContainText('Claude Opus 5.5');
});

test('settings save only changed values with the CSRF header', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Настройки');
  await page.locator('#setting-rate_limit_rps').fill('2.5');
  await page.getByRole('switch', { name: 'Передавать мышление модели' }).click();
  await expect(page.locator('.savebar')).toContainText('2 несохранённых');
  await page.getByRole('button', { name: 'Сохранить' }).click();
  await expect.poll(() => state.patches.length).toBe(1);
  expect(state.patches[0].body).toEqual({ settings: { rate_limit_rps: 2.5, show_thinking: false } });
  expect(state.patches[0].headers['x-admin-request']).toBe('1');
  await expect(page.getByText('Настройки сохранены')).toBeVisible();
  await expect(page.locator('.savebar')).toHaveCount(0);
  await expect(page.getByText('ждут перезапуска: 1')).toBeVisible();
});

test('keyboard shortcuts switch sections and the theme toggles', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await page.keyboard.press('2');
  await expect(page.getByRole('heading', { name: 'Журнал событий' })).toBeVisible();
  await page.keyboard.press('5');
  await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
  const before = await page.evaluate(() => document.documentElement.dataset.theme);
  await page.getByRole('button', { name: 'Сменить тему' }).click();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).not.toBe(before);
});

for (const width of [320, 414]) {
  test(`has no horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 850 });
    await mockAdmin(page);
    await login(page);
    const noOverflow = () => expect.poll(() => page.evaluate(() =>
      document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await noOverflow();
    for (const name of ['Журнал', 'Ходы', 'Модели', 'Настройки']) {
      await nav(page, name);
      await noOverflow();
    }
  });
}
