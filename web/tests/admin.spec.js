import { test, expect } from '@playwright/test';
import { API_KEY, DEFAULT_ROLES, MEMORY_KEY, ev, journal, mockAdmin } from './fixtures.js';
import { NOW, PARTIAL_BALANCE, RESET_DATE, metricsFor } from './fixtures.js';

// Replaces the journal page the admin loads at start; the live stream stays empty.
const serveEvents = (page, data) => page.route(/\/admin\/api\/events\?/, (route) => route.fulfill({
  json: { data, last_seq: data.reduce((last, event) => Math.max(last, event.seq), 0) },
}));

// A turn event `seconds` from now, with the next sequence number; no turn id makes it a relay event.
let eventSeq = 100;
const eventAt = (turn, seconds, kind, data = {}, extra = {}) => {
  eventSeq += 1;
  return ev(eventSeq, kind, kind, 'info', { ...(turn && { turn_id: turn }), ts: NOW + seconds, data, ...extra });
};

async function login(page, { remember = false } = {}) {
  await page.goto('./');
  await page.getByLabel('Relay key').fill(API_KEY);
  await page.getByLabel('Запомнить до закрытия вкладки').setChecked(remember);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('heading', { name: 'Сводка' })).toBeVisible();
}

const nav = (page, name) => page.getByRole('navigation').getByRole('button', { name: new RegExp(name) }).click();
// Settings adds its own group index with role=navigation, so leaving it needs the section rail by name.
const section = (page, name) => page.getByRole('navigation', { name: 'Разделы' }).getByRole('button', { name: new RegExp(name) });

async function openEvents(page) {
  await nav(page, 'Активность');
  await page.getByRole('tab', { name: /Все события/ }).click();
}

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
  await expect(page.getByRole('heading', { name: 'Сводка' })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Сводка' })).toBeVisible();
  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect(page.getByLabel('Relay key')).toBeVisible();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), MEMORY_KEY)).toBeNull();
});

test('login hero is a stageshow scene and shows its payoff poster under reduced motion', async ({ browser }) => {
  const context = await browser.newContext({ reducedMotion: 'reduce' });
  const page = await context.newPage();
  await mockAdmin(page);
  await page.goto('./');
  const hero = page.locator('.login-hero .ss-stage');
  await expect(hero).toBeVisible();
  await expect(hero).toContainText('claude-opus-5-5');
  await expect(hero).toContainText('ответ получен');
  await context.close();
});

test('summary shows the pool balance, forecast, spend per bucket, cache and models', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await expect(page.locator('.figure-xl')).toContainText('495,09');
  await expect(page.locator('.instrument-label')).toContainText('2 аккаунта');
  await expect(page.locator('.forecast')).toContainText('хватит примерно на');
  await expect(page.getByText('Retool отвечает')).toBeVisible();
  await expect(page.locator('.stat', { hasText: 'Потрачено за окно' })).toBeVisible();
  await expect(page.locator('.cache-facts .figure')).toContainText('%');
  await expect(page.locator('.chart')).toHaveCount(2);
  await expect(page.locator('.legend', { hasText: 'за каждые 1 мин' })).toBeVisible();
  await expect(page.locator('.rank li')).toHaveCount(2);
  // The summary no longer repeats the journal, recent turns or IDE connection.
  await expect(page.locator('.feed-row')).toHaveCount(0);
  await expect(page.locator('.recent-row')).toHaveCount(0);
});

test('spend is a line per bucket that falls back after its peak, and cache write spikes are marked', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  // The line's points are the bucket ends of each curve segment: y grows downwards in SVG
  const ys = await page.locator('.c-spend-line').evaluate((path) =>
    [...path.getAttribute('d').matchAll(/(?:M|\s)(-?[\d.]+),(-?[\d.]+)(?=C|$)/g)].map((m) => Number(m[2])));
  expect(ys.length).toBeGreaterThan(50);
  const peak = ys.indexOf(Math.min(...ys));
  expect(ys.slice(peak + 1).some((y) => y > ys[peak])).toBe(true);
  await expect(page.locator('.c-peak')).toHaveCount(1);
  await expect(page.locator('.c-spend-area')).toHaveCount(1);
  // Reads and writes run on their own lanes; the cold-cache rewrites get the accent
  await expect(page.locator('.c-read-line')).toHaveCount(1);
  await expect(page.locator('.c-write-line')).toHaveCount(1);
  await expect(page.locator('.c-spike')).toHaveCount(2);
  await expect(page.locator('.c-spike').first()).toContainText('+');
  // Spike labels never overlap each other or another spike's dot, on a desktop or a phone
  const overlapping = () => page.evaluate(() => {
    const labels = [...document.querySelectorAll('.c-spike text')].map((t) => t.getBoundingClientRect());
    const dots = [...document.querySelectorAll('.c-spike circle')].map((c) => c.getBoundingClientRect());
    return labels.some((a, i) => labels.some((b, j) => i !== j && a.left < b.right && b.left < a.right))
      || labels.some((a) => dots.filter((d) => d.left < a.right && a.left < d.right && d.top < a.bottom && a.top < d.bottom).length > 0);
  });
  expect(await overlapping()).toBe(false);
  await page.setViewportSize({ width: 390, height: 900 });
  await expect.poll(overlapping).toBe(false);
  await expect(page.locator('.c-spike text')).not.toHaveCount(0);
});

test('metrics window switcher requests a 6 hour window', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await page.getByRole('group', { name: 'Окно метрик' }).getByRole('button', { name: '6 ч' }).click();
  await expect.poll(() => state.requests.some(({ url }) => url.includes('metrics?window=21600'))).toBe(true);
  await expect(page.locator('.page-meta')).toContainText('последние 6 часов');
  await expect(page.locator('.forecast')).toContainText('последние 6 часов');
});

test('accounts page lists the pool and marks the reserve', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Аккаунты');
  const rows = page.locator('.pool-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('основной');
  await expect(rows.nth(0)).toContainText('pro-main');
  await expect(rows.nth(1)).toContainText('резерв');
});

test('accounts can be disabled, reprioritised, checked and renewed with the CSRF header', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Аккаунты');
  await page.getByRole('switch', { name: 'Аккаунт запасной в маршрутизации' }).click();
  await expect.poll(() => state.accountRequests.find((r) => r.method === 'PATCH')?.body).toEqual({ enabled: false });
  await expect(page.locator('.pool-row').nth(1)).toContainText('выключен');

  const priority = page.locator('.pool-row').nth(0).getByLabel('Приоритет: меньше — раньше');
  await priority.fill('5');
  await priority.press('Enter');
  await expect.poll(() => state.accountRequests.some((r) => r.method === 'PATCH' && r.body?.priority === 5)).toBe(true);

  await page.locator('.pool-row').nth(0).getByRole('button', { name: 'Баланс' }).click();
  await expect(page.getByText('Баланс обновлён')).toBeVisible();
  await page.locator('.pool-row').nth(0).getByRole('button', { name: 'Продлить' }).click();
  await expect(page.getByText('Сессия продлена').first()).toBeVisible();
  expect(state.accountRequests.map((r) => r.path)).toEqual(expect.arrayContaining([
    '/admin/api/accounts/pro-main/balance', '/admin/api/accounts/pro-main/renew']));
  expect(state.accountRequests.every((r) => r.headers['x-admin-request'] === '1')).toBe(true);
});

test('a failed renewal shows the redacted reason from the relay', async ({ page }) => {
  const state = await mockAdmin(page);
  state.renewFails = true;
  await login(page);
  await nav(page, 'Аккаунты');
  await page.locator('.pool-row').nth(0).getByRole('button', { name: 'Продлить' }).click();
  await expect(page.getByText('Сессия не продлена')).toBeVisible();
  await expect(page.getByText('session renewal failed: HTTP 401')).toBeVisible();
});

test('the last enabled account cannot be switched off', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Аккаунты');
  await page.getByRole('switch', { name: 'Аккаунт запасной в маршрутизации' }).click();
  await expect(page.locator('.pool-row').nth(1)).toContainText('выключен');
  const main = page.getByRole('switch', { name: 'Аккаунт основной в маршрутизации' });
  await expect(main).toBeDisabled();
  await expect(main).toHaveAttribute('title', /последний включённый/);
  expect(state.accountRequests.filter((r) => r.method === 'PATCH' && r.body?.enabled === false)).toHaveLength(1);
});

test('an account parked by the Retool hourly limit says so and when it returns', async ({ page }) => {
  const state = await mockAdmin(page);
  const until = NOW + 600;
  state.pool[0] = { ...state.pool[0], state: 'rate_limited', routable: false, rate_limited_until: until };
  await login(page);
  await nav(page, 'Аккаунты');
  const row = page.locator('.pool-row').nth(0);
  await expect(row).toContainText('пауза: часовой лимит');
  const clock = new Date(until * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', hour12: false });
  await expect(row).toContainText(`вернётся в ${clock}`);
  await expect(page.locator('.pool-row').nth(1)).not.toContainText('вернётся в');
});

test('accounts can be added and removed; both wait for a restart', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Аккаунты');
  await page.getByRole('button', { name: 'Добавить аккаунт' }).click();
  await page.getByLabel('Название').fill('третий');
  await page.getByLabel('Адрес Retool').fill('https://tenant.retool.com');
  await page.getByLabel('page_uuid приложения').fill('11111111-2222-3333-4444-555555555555');
  await page.getByLabel('xsrf').fill('xsrf-value');
  await page.getByLabel('Cookie').fill('accessToken=abc');
  await page.getByLabel('Ресурс Anthropic (необязательно)').fill('anthropic-res');
  await page.getByRole('button', { name: 'Добавить', exact: true }).click();
  await expect(page.getByText('Аккаунт добавлен')).toBeVisible();
  const post = state.accountRequests.find((r) => r.method === 'POST' && r.path === '/admin/api/accounts');
  // Empty optional fields are not sent.
  expect(post.body).toEqual({ label: 'третий', base: 'https://tenant.retool.com',
    page_uuid: '11111111-2222-3333-4444-555555555555', cookie: 'accessToken=abc', xsrf: 'xsrf-value',
    provider_resource_name: 'anthropic-res' });
  const added = page.locator('.pool-row', { hasText: 'третий' });
  await expect(added).toContainText('подключится после перезапуска');
  await expect(added).toContainText('не подключён');
  await expect(added.getByRole('button', { name: 'Баланс' })).toBeDisabled();
  await expect(page.getByText(/вступят в силу после перезапуска/)).toBeVisible();

  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Удалить аккаунт запасной' }).click();
  await expect(page.locator('.pool-row', { hasText: 'запасной' })).toContainText('удалён');
});

test('journal streams new events, filters, searches and expands details', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await openEvents(page);
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

test('journal clear asks for confirmation and empties the buffer into the illustrated empty state', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await openEvents(page);
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Очистить' }).click();
  await expect(page.getByText('Журнал пуст')).toBeVisible();
  await expect(page.locator('.empty .art .ss-stage')).toBeVisible();
  expect(state.requests.some(({ path, headers }) => path === '/admin/api/events/clear' && headers['x-admin-request'] === '1')).toBe(true);
});

test('the events of the prompts layer have a category chip and a name of their own, and the chip filters them', async ({ page }) => {
  await mockAdmin(page);
  await serveEvents(page, [
    eventAt(null, -50, 'prompt.reload', { rev: 3, source: 'file' }, { message: 'Промпты перечитаны: ревизия 3' }),
    eventAt('t-prompt', -40, 'prompt.plan', { rev: 3, profile: 'mine-first', rule: 'opencode' }, { level: 'debug', message: 'План промптов: mine-first' }),
    eventAt(null, -30, 'prompt.edit_skipped', { op: 'drop_tag', reason: 'unsupported system content' }, { level: 'warn', message: 'Правка пропущена: drop_tag' }),
    eventAt(null, -20, 'admin.prompts', { rev: 4, changed_paths: ['profiles.mine-first'] }, { message: 'Промпты сохранены: ревизия 4' }),
    eventAt(null, -10, 'http.request', { status: 200, route: 'anthropic' }, { message: 'POST /v1/messages → 200' }),
  ]);
  await login(page);
  await openEvents(page);
  const categories = page.getByRole('group', { name: 'Категории' });
  const chip = categories.getByRole('button', { name: /^Промпты/ });
  await expect(chip).toBeVisible();
  await expect(chip.locator('.chip-count')).toHaveText('3');
  await expect(page.locator('.log-row')).toHaveCount(5);
  // the event shows the name of the category, not the raw word of its kind (newest first)
  await expect(page.locator('.log-row .cat')).toHaveText(['HTTP', 'Админ', 'Промпты', 'Промпты', 'Промпты']);

  await chip.click();
  await expect(chip).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.log-row')).toHaveCount(3);
  await expect(page.locator('.log-row .cat')).toHaveText(['Промпты', 'Промпты', 'Промпты']);
  await expect(page.locator('.log-row', { hasText: 'POST /v1/messages' })).toHaveCount(0);
});

test('activity opens on turns, aggregated into a turn tape and steps', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Активность');
  await expect(page.getByRole('tab', { name: /Ходы моделей/ })).toHaveAttribute('aria-selected', 'true');
  const trace = page.locator('.trace').first();
  await expect(trace).toContainText('claude-opus-5-5');
  await expect(trace).toContainText('effort high');
  await expect(trace).toContainText('2,10 с');
  await expect(trace).toContainText('готово');
  await expect(trace.locator('.tape')).toBeVisible();
  await trace.locator('.trace-head').click();
  await expect(trace.locator('.step')).toHaveCount(5);
});

test('turn traces show hidden thinking and tool argument time', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Активность');
  state.stream.push(
    ev(20, 'turn.begin', 'Ход', 'info', { turn_id: 'turn-beta-2', data: { model: 'claude-opus-5-5', effort: 'high' } }),
    ev(21, 'upstream.thinking_end', 'Мышление заняло 6400 мс', 'info', { turn_id: 'turn-beta-2', data: { thinking_ms: 6400 } }),
    ev(22, 'tool.call', 'Инструмент клиента: Write', 'info', { turn_id: 'turn-beta-2', data: { name: 'Write', args_ms: 7100 } }),
  );
  const trace = page.locator('.trace', { hasText: 'effort high' }).first();
  await expect(trace.locator('[title="По событиям thinking от upstream"]')).toContainText('6,40 с', { timeout: 10000 });
  await expect(trace.locator('[title="Генерация аргументов инструментов"]')).toContainText('7,10 с');
});

test('turns left running by a restart or by silence are interrupted and never stretch the shared scale', async ({ page }) => {
  await mockAdmin(page);
  const day = 86400;
  await serveEvents(page, [
    // Restored from journal.jsonl: the process that ran this turn is gone
    eventAt(null, -3 * day, 'relay.start', {}, { restored: true }),
    eventAt('t-lost', -3 * day + 1, 'turn.begin', { model: 'model-lost', effort: 'high' }, { restored: true }),
    eventAt('t-lost', -3 * day + 2, 'upstream.thinking_start', {}, { restored: true }),
    // Ended by the next relay.start in the journal
    eventAt(null, -4000, 'relay.start'),
    eventAt('t-restart', -3900, 'turn.begin', { model: 'model-restart', effort: 'high' }),
    eventAt('t-restart', -3899, 'upstream.thinking_start'),
    eventAt(null, -3800, 'relay.start'),
    // No restart after it, but nothing for 50 minutes
    eventAt('t-silent', -3000, 'turn.begin', { model: 'model-silent', effort: 'high' }),
    eventAt('t-silent', -2999, 'upstream.thinking_start'),
    eventAt('t-done', -60, 'turn.begin', { model: 'model-done', effort: 'low' }),
    eventAt('t-done', -58, 'turn.end', { reason: 'stop', duration_ms: 2000 }),
  ]);
  await login(page);
  await nav(page, 'Активность');
  for (const model of ['model-lost', 'model-restart', 'model-silent']) {
    const row = page.locator('.trace', { hasText: model });
    await expect(row.locator('.tag')).toHaveText('прерван');
    await expect(row).toContainText('всего 1,00 с');
    await expect(row.locator('.live-phase')).toHaveCount(0);
    await expect(row.locator('.tape')).toHaveClass(/is-interrupted/);
    // Half of the finished turn's 2 s, not a fraction of 3 days
    await expect(row.locator('.tape-fill')).toHaveAttribute('style', /width: 50%/);
  }
  const done = page.locator('.trace', { hasText: 'model-done' });
  await expect(done.locator('.tag')).toHaveText('готово');
  await expect(done.locator('.tape-fill')).toHaveAttribute('style', /width: 100%/);
});

test('a turn silent for longer than the default limit keeps running while the relay allows turns that long', async ({ page }) => {
  // The relay runs with a two hour turn timeout: a turn may be silent that long, and five minutes more for its end
  await mockAdmin(page, { overviewPatch: { resources: { turn_timeout: 7200 } } });
  await serveEvents(page, [
    eventAt('t-long', -1800, 'turn.begin', { model: 'model-long', effort: 'high' }),
    eventAt('t-long', -1799, 'upstream.thinking_start'),
    eventAt('t-gone', -8000, 'turn.begin', { model: 'model-gone', effort: 'high' }),
    eventAt('t-gone', -7999, 'upstream.thinking_start'),
  ]);
  await login(page);
  await nav(page, 'Активность');
  // Half an hour of silence is past the 20 minutes of the default limit, but within this relay's turn timeout
  const long = page.locator('.trace', { hasText: 'model-long' });
  await expect(long.locator('.tag')).toHaveText('идёт');
  await expect(long.locator('.live-phase')).toHaveCount(1);
  await expect(long.locator('.tape')).not.toHaveClass(/is-interrupted/);
  // More than two hours and five minutes is not: that turn ended without a word
  const gone = page.locator('.trace', { hasText: 'model-gone' });
  await expect(gone.locator('.tag')).toHaveText('прерван');
  await expect(gone.locator('.live-phase')).toHaveCount(0);
  await expect(gone).toContainText('всего 1,00 с');
});

test('without a turn timeout from the relay a silent turn is interrupted after the default 20 minutes', async ({ page }) => {
  await mockAdmin(page, { overviewPatch: { resources: undefined } });
  await serveEvents(page, [
    eventAt('t-quiet', -1800, 'turn.begin', { model: 'model-quiet', effort: 'high' }),
    eventAt('t-quiet', -1799, 'upstream.thinking_start'),
  ]);
  await login(page);
  await nav(page, 'Активность');
  await expect(page.locator('.trace', { hasText: 'model-quiet' }).locator('.tag')).toHaveText('прерван');
});

test('turn rows price a turn by the relay rules: nothing for a refused attempt, unknown for usage without a price, a lower bound while it runs', async ({ page }) => {
  await mockAdmin(page);
  await serveEvents(page, [
    // Rejected before the upstream was reached
    eventAt('t-failed', -400, 'turn.begin', { model: 'model-failed' }),
    eventAt('t-failed', -399, 'turn.error', {}, { level: 'error' }),
    eventAt('t-failed', -399, 'turn.end', { reason: 'error', duration_ms: 1000 }, { level: 'error' }),
    // An attempt began and the upstream refused it (429): it reported nothing and cost nothing
    eventAt('t-refused', -350, 'turn.begin', { model: 'model-refused' }),
    eventAt('t-refused', -350, 'upstream.usage_start', { attempt: 1 }),
    eventAt('t-refused', -346, 'turn.end', { reason: 'error', duration_ms: 4000 }, { level: 'error' }),
    // Usage came back without a price
    eventAt('t-unpriced', -300, 'turn.begin', { model: 'model-unpriced' }),
    eventAt('t-unpriced', -300, 'upstream.usage_start', { attempt: 1 }),
    eventAt('t-unpriced', -297, 'upstream.usage', { credits: null, attempt: 1, credits_reported: false }),
    eventAt('t-unpriced', -296, 'turn.end', { reason: 'error', duration_ms: 4000 }, { level: 'error' }),
    // The client gave up while the attempt was silent: it may have been billed
    eventAt('t-cancelled', -250, 'turn.begin', { model: 'model-cancelled' }),
    eventAt('t-cancelled', -250, 'upstream.usage_start', { attempt: 1 }),
    eventAt('t-cancelled', -247, 'turn.end', { reason: 'cancelled', duration_ms: 3000 }),
    // Still running: one attempt priced, the next not yet; and one that has reported nothing
    eventAt('t-running', -20, 'turn.begin', { model: 'model-running' }),
    eventAt('t-running', -20, 'upstream.usage_start', { attempt: 1 }),
    eventAt('t-running', -15, 'upstream.usage', { credits: 0.5, attempt: 1, credits_reported: true }),
    eventAt('t-running', -15, 'upstream.usage_start', { attempt: 2 }),
    eventAt('t-waiting', -10, 'turn.begin', { model: 'model-waiting' }),
    eventAt('t-waiting', -10, 'upstream.usage_start', { attempt: 1 }),
  ]);
  await login(page);
  await nav(page, 'Активность');
  const cost = (model) => page.locator('.trace', { hasText: model }).locator('.trace-cost');
  await expect(cost('model-failed')).toHaveText('0,00');
  await expect(cost('model-refused')).toHaveText('0,00');
  await expect(cost('model-unpriced')).toContainText('нет данных');
  await expect(cost('model-cancelled')).toContainText('нет данных');
  await expect(cost('model-running')).toContainText('≥ 0,50');
  await expect(cost('model-running')).toContainText('ход идёт');
  await expect(cost('model-waiting')).toContainText('ход идёт');
  await expect(cost('model-waiting')).not.toContainText('нет данных');
});

test('a local command is shown as its own time and is not counted as the answer', async ({ page }) => {
  await mockAdmin(page);
  await serveEvents(page, [
    eventAt('t-command', -200, 'turn.begin', { model: 'model-command' }),
    eventAt('t-command', -200, 'upstream.usage_start', { attempt: 1 }),
    eventAt('t-command', -195, 'upstream.text_start'),
    eventAt('t-command', -193, 'exec.start'),
    eventAt('t-command', -163, 'exec.result', { duration_ms: 30000, exit: 0 }),
    eventAt('t-command', -163, 'upstream.usage', { credits: 0.5, attempt: 1, credits_reported: true }),
    eventAt('t-command', -163, 'upstream.usage_start', { attempt: 2 }),
    eventAt('t-command', -160, 'upstream.usage', { credits: 0.25, attempt: 2, credits_reported: true }),
    eventAt('t-command', -160, 'turn.end', { reason: 'stop', duration_ms: 40000 }),
  ]);
  await login(page);
  await nav(page, 'Активность');
  const command = page.locator('.trace', { hasText: 'model-command' });
  await expect(command.locator('.trace-cost')).toHaveText('0,75');
  await expect(command.locator('.trace-phases')).toContainText('команды 30,0 с');
  // 2 s of text, then 30 s of command: only the 2 s are the answer
  await expect(command.locator('.tape')).toHaveAttribute('aria-label', /ответ 2,00\sс.*без детализации 30,0\sс/);
});

test('turns empty state shows the stageshow illustration', async ({ page }) => {
  await mockAdmin(page, { empty: true });
  await login(page);
  await nav(page, 'Активность');
  await expect(page.getByText('Ходов пока нет')).toBeVisible();
  await expect(page.locator('.empty .art .ss-stage')).toBeVisible();
});

test('settings group fields by purpose and hold the model catalog and IDE connection', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Настройки');
  await expect(page.getByRole('region', { name: 'Модель и мышление' })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Лимиты и таймауты' })).toBeVisible();
  await expect(page.locator('code', { hasText: 'http://127.0.0.1:8787/v1' })).toBeVisible();
  await expect(page.locator('.model-card')).toHaveCount(2);
  await page.getByLabel('Поиск модели').fill('gpt');
  await expect(page.locator('.model-card')).toHaveCount(1);
  await page.getByLabel('Поиск модели').fill('');
  await page.getByLabel('Только с маршрутом').check();
  await expect(page.locator('.model-card')).toHaveCount(1);
  await expect(page.locator('.model-card')).toContainText('Claude Opus 5.5');
});

test('settings show backend groups, real descriptions and units', async ({ page }) => {
  await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  for (const title of ['Аккаунты и маршрутизация', 'Модель и мышление', 'Повторы и диагностика', 'Кэш промпта', 'Лимиты и таймауты', 'API для клиентов']) {
    await expect(page.getByRole('region', { name: title })).toBeVisible();
  }
  const reserve = page.getByRole('region', { name: 'Аккаунты и маршрутизация' }).locator('.setting', { hasText: 'Порог резерва' });
  await expect(reserve).toContainText('уходит в резерв');
  await expect(reserve).toContainText('сразу');
  await expect(page.getByRole('region', { name: 'Кэш промпта' }).locator('.setting-when')).toHaveCount(0);
  await expect(reserve.locator('.unit')).toHaveText('кредитов');
  await expect(page.locator('#setting-session_mode option', { hasText: 'сессия на каждый диалог' })).toHaveCount(1);
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

test('system role preserves multiline instructions, explicit empty text and reset to default', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Настройки');
  const editor = page.getByLabel('Инструкции для модели', { exact: true });
  await expect(editor).toBeDisabled();
  await page.getByLabel('Стандартная роль relay').uncheck();
  await page.getByRole('radio', { name: /Дополнить клиента/ }).check();
  const policy = 'Ты — инженер проекта.\nНе удаляй данные без подтверждения.';
  await editor.fill(policy);
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect.poll(() => state.settings.relay_system_prompt).toBe(policy);
  expect(state.settings.relay_system_prompt_mode).toBe('append');
  await expect(page.locator('.savebar')).toHaveCount(0);
  await editor.fill('');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect.poll(() => state.settings.relay_system_prompt).toBe('');
  await expect(page.locator('.savebar')).toHaveCount(0);
  await page.getByLabel('Стандартная роль relay').check();
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect.poll(() => state.settings.relay_system_prompt).toBe(null);
});

test('typing during a pending save is preserved as a new draft', async ({ page }) => {
  const state = await mockAdmin(page);
  state.patchDelay = 800;
  await login(page);
  await nav(page, 'Настройки');
  const input = page.locator('#setting-rate_limit_rps');
  await input.fill('2');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect.poll(() => state.patches.length).toBe(1);
  await input.fill('3');
  await expect(page.getByText('Настройки сохранены')).toBeVisible();
  await expect(input).toHaveValue('3');
  await expect(page.locator('.savebar')).toBeVisible();
  expect(state.settings.rate_limit_rps).toBe(2);
});

test('the standard role shows both built-in roles and the requests each one serves', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Настройки');
  const [relay, nativeTools] = DEFAULT_ROLES;
  const main = page.getByLabel('Инструкции для модели', { exact: true });
  await expect(main).toBeDisabled();
  await expect(main).toHaveValue(relay.text);
  await expect(page.locator('#system-instructions-hint')).toContainText(relay.applies_to);
  const tools = page.getByLabel(nativeTools.title, { exact: true });
  await expect(tools).toHaveValue(nativeTools.text);
  await expect(page.locator('#system-default-native_tools-hint')).toContainText(nativeTools.applies_to);
  // A custom role replaces both of them, so the previews give way to the editor.
  await page.getByLabel('Стандартная роль relay').uncheck();
  await expect(tools).toHaveCount(0);
  await expect(main).toBeEnabled();
});

test('clearing the standard role starts from its text and an empty role is shown as disabled', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Настройки');
  const editor = page.getByLabel('Инструкции для модели', { exact: true });
  const standard = page.getByLabel('Стандартная роль relay');
  const disabled = page.getByText('Роль отключена');
  const save = page.getByRole('button', { name: 'Сохранить', exact: true });
  await standard.uncheck();
  await expect(editor).toHaveValue(DEFAULT_ROLES[0].text);
  await expect(page.locator('.savebar')).toContainText('1 несохранённых');
  await expect(disabled).toHaveCount(0);
  await editor.fill('');
  await expect(disabled).toBeVisible();
  // In "replace" mode the client's own instructions are dropped together with the role.
  await expect(disabled).not.toContainText('инструкции IDE');
  await page.getByRole('radio', { name: /Заменить роль клиента/ }).check();
  await expect(disabled).toContainText('инструкции IDE тоже не будут отправлены');
  await page.getByRole('radio', { name: /Самостоятельные запросы/ }).check();
  await expect(disabled).not.toContainText('инструкции IDE');
  await editor.fill('Своя роль');
  await expect(disabled).toHaveCount(0);
  await standard.check();
  await expect(page.locator('.savebar')).toHaveCount(0);

  // Saving right after clearing the box keeps the template, not an empty role.
  await standard.uncheck();
  await save.click();
  await expect.poll(() => state.settings.relay_system_prompt).toBe(DEFAULT_ROLES[0].text);
  await editor.fill('Сохранённая роль');
  await save.click();
  await expect.poll(() => state.settings.relay_system_prompt).toBe('Сохранённая роль');
  // Toggling the box off again brings back the saved text rather than the template.
  await standard.check();
  await standard.uncheck();
  await expect(editor).toHaveValue('Сохранённая роль');
  await expect(page.locator('.savebar')).toHaveCount(0);
});

test('the role counter counts bytes against the server limit and blocks a role that cannot fit', async ({ page }) => {
  const state = await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  await page.getByLabel('Стандартная роль relay').uncheck();
  const editor = page.getByLabel('Инструкции для модели', { exact: true });
  const footer = page.locator('#system-instructions-hint');
  const save = page.getByRole('button', { name: 'Сохранить', exact: true });
  await editor.fill('ж'.repeat(100));
  await expect(footer).toContainText('100 симв. · 200 из 32 768 байт');
  await expect(save).toBeEnabled();

  await editor.fill('ж'.repeat(16385)); // 32 770 bytes: Cyrillic takes two per character
  await expect(footer).toContainText('32 770 из 32 768 байт');
  await expect(footer.getByRole('alert')).toContainText('не больше 32 768 байт');
  await expect(save).toBeDisabled();

  // With a prompt cap the role may take half of it, judged on the draft just as the server will.
  await editor.fill('ж'.repeat(100));
  const cap = page.locator('#setting-max_prompt_bytes');
  await cap.fill('300');
  await expect(footer).toContainText('200 из 150 байт');
  await expect(footer.getByRole('alert')).toContainText('«Размер промпта»');
  await expect(page.locator('.savebar')).toContainText('роль не помещается');
  await expect(save).toBeDisabled();
  await cap.fill('1000');
  await expect(footer).toContainText('200 из 500 байт');
  await expect(save).toBeEnabled();
  await save.click();
  await expect.poll(() => state.settings.max_prompt_bytes).toBe(1000);
  expect(state.settings.relay_system_prompt).toBe('ж'.repeat(100));
});

test('an effort level the model lacks is an error only while approximation is off', async ({ page }) => {
  await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  const effort = page.locator('#setting-reasoning_effort');
  const banner = page.getByRole('status').filter({ hasText: 'не поддерживается моделью' });
  await effort.selectOption('max');
  await page.locator('#setting-model').selectOption('gpt-5.5'); // low, medium and high only
  await expect(banner).toContainText('GPT 5.5');
  await expect(effort.locator('option[value="xhigh"]')).toHaveCount(0);

  const approximate = page.getByRole('switch', { name: 'Подбирать ближайший effort' });
  await approximate.click();
  await expect(banner).toHaveCount(0);
  // Every level the relay knows can be chosen: it takes the nearest one the model has.
  await expect(effort.locator('option[value="xhigh"]')).toHaveCount(1);
  await effort.selectOption('xhigh');
  await expect(banner).toHaveCount(0);
  await approximate.click();
  await expect(banner).toContainText('xhigh');
});

test('the level that xhigh becomes on Claude is a choice of high and max, saved with the rest and held for a restart', async ({ page }) => {
  const state = await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  const choice = page.locator('#setting-claude_xhigh_effort');
  await expect(page.getByText('Уровень вместо xhigh у Claude')).toBeVisible();
  await expect(choice).toHaveValue('high');
  await expect(choice.locator('option')).toHaveText(['high (по умолчанию)', 'max']);
  await choice.selectOption('max');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect.poll(() => state.patches.length).toBe(1);
  expect(state.patches[0].body).toEqual({ settings: { claude_xhigh_effort: 'max' } });
  // Saved, not applied: like the other effort settings, it waits for a restart
  await expect(page.locator('.setting.pending').filter({ has: page.locator('#setting-claude_xhigh_effort') })).toHaveCount(1);
});

test('xhigh is offered for a model that replaces it, labelled with the level it becomes, and not for one that does not', async ({ page }) => {
  await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  const effort = page.locator('#setting-reasoning_effort');
  const banner = page.getByRole('status').filter({ hasText: 'не поддерживается моделью' });
  // The default model is Claude Opus 5.5: low, medium, high and max, and xhigh served as another level
  await expect(effort.locator('option')).toHaveText(['по умолчанию модели', 'low', 'medium', 'high', 'xhigh · станет high', 'max']);
  await page.locator('#setting-claude_xhigh_effort').selectOption('max');
  await expect(effort.locator('option[value="xhigh"]')).toHaveText('xhigh · станет max');
  await effort.selectOption('xhigh');
  await expect(banner).toHaveCount(0);
  // A model with no replacement has no xhigh to offer while approximation is off, and the saved level is a warning
  await page.locator('#setting-model').selectOption('gpt-5.5');
  await expect(banner).toContainText('GPT 5.5');
  await effort.selectOption('high');
  await expect(banner).toHaveCount(0);
  await expect(effort.locator('option')).toHaveText(['по умолчанию модели', 'low', 'medium', 'high']);
});

// The relay takes any known level for these routes and changes nothing (reasoning_effort_control "accepted-ignored").
for (const id of ['zai-org/GLM-5.3', 'claude-haiku-4-5']) {
  test(`a saved effort level on ${id}, whose route ignores it, raises no warning`, async ({ page }) => {
    const state = await mockAdmin(page, { rich: true });
    for (const values of [state.settings, state.effective]) Object.assign(values, { model: id, reasoning_effort: 'high' });
    await login(page);
    await nav(page, 'Настройки');
    await expect(page.locator('#setting-model')).toHaveValue(id);
    await expect(page.locator('#setting-reasoning_effort')).toHaveValue('high');
    await expect(page.getByRole('status').filter({ hasText: 'не поддерживается моделью' })).toHaveCount(0);
  });
}

test('a model that ignores effort offers every level, marks each as ignored, and still warns for one that refuses a level', async ({ page }) => {
  await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  const effort = page.locator('#setting-reasoning_effort');
  const banner = page.getByRole('status').filter({ hasText: 'не поддерживается моделью' });
  await effort.selectOption('max');
  await page.locator('#setting-model').selectOption('zai-org/GLM-5.3'); // lists no level, takes any
  await expect(banner).toHaveCount(0);
  const marked = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].map((level) => `${level} · модель игнорирует`);
  await expect(effort.locator('option')).toHaveText(['по умолчанию модели', ...marked]);
  await expect(effort).toHaveValue('max');
  await effort.selectOption('xhigh');
  await expect(banner).toHaveCount(0);
  // Approximation has nothing to do here: the list and the silence are the same with it on
  const approximate = page.getByRole('switch', { name: 'Подбирать ближайший effort' });
  await approximate.click();
  await expect(banner).toHaveCount(0);
  await expect(effort.locator('option')).toHaveText(['по умолчанию модели', ...marked]);
  await approximate.click();

  // A route that lists levels is judged by them; one that lists none and does not take a level refuses it
  await effort.selectOption('high');
  await page.locator('#setting-model').selectOption('gpt-4.1');
  await expect(banner).toContainText('GPT 4.1');
  await expect(effort.locator('option')).toHaveText(['по умолчанию модели', 'high']);
  await page.locator('#setting-model').selectOption('gpt-5.5');
  await expect(banner).toHaveCount(0);
  await expect(effort.locator('option')).toHaveText(['по умолчанию модели', 'low', 'medium', 'high']);
});

test('the catalog says that a route takes an effort level and ignores it, instead of leaving the choice to the provider', async ({ page }) => {
  await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  const effort = (id) => page.locator('.model-card').filter({ has: page.getByText(id, { exact: true }) }).locator('.model-effort');
  await expect(effort('zai-org/GLM-5.3')).toContainText('Effort: принимает уровень, игнорирует');
  await expect(effort('zai-org/GLM-5.3')).not.toContainText('выбирает провайдер');
  await expect(effort('claude-opus-5-5')).toContainText('Effort: low · medium · high · max');
  await expect(effort('gpt-4.1')).toContainText('Effort: без мышления');
});

test('leaving the settings with unsaved edits asks first and keeps them when declined', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Настройки');
  const input = page.locator('#setting-rate_limit_rps');
  await input.fill('7');
  await expect(page.locator('.savebar')).toContainText('1 несохранённых');
  const asked = [];
  const decline = (dialog) => { asked.push(dialog.message()); dialog.dismiss(); };
  page.on('dialog', decline);
  await section(page, 'Аккаунты').click();
  await expect.poll(() => asked.length).toBe(1);
  expect(asked[0]).toContain('несохранённые настройки (1)');
  await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
  await expect(input).toHaveValue('7');
  // The keyboard shortcuts and signing out ask the same question.
  await page.getByRole('heading', { name: 'Настройки' }).click();
  await page.keyboard.press('2');
  await expect.poll(() => asked.length).toBe(2);
  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect.poll(() => asked.length).toBe(3);
  await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
  await expect(input).toHaveValue('7');
  await expect(page.locator('.savebar')).toBeVisible();

  // Agreeing drops the draft.
  page.off('dialog', decline);
  page.once('dialog', (dialog) => dialog.accept());
  await section(page, 'Аккаунты').click();
  await expect(page.getByRole('heading', { name: 'Аккаунты' })).toBeVisible();
  await nav(page, 'Настройки');
  await expect(page.locator('#setting-rate_limit_rps')).toHaveValue('0');
  await expect(page.locator('.savebar')).toHaveCount(0);

  // Nothing is asked once the edits are saved.
  const unexpected = [];
  page.on('dialog', (dialog) => { unexpected.push(dialog.message()); dialog.dismiss(); });
  await page.locator('#setting-rate_limit_rps').fill('3');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect(page.locator('.savebar')).toHaveCount(0);
  await section(page, 'Аккаунты').click();
  await expect(page.getByRole('heading', { name: 'Аккаунты' })).toBeVisible();
  expect(unexpected).toEqual([]);
});

test('the model catalog says why a model has no route', async ({ page }) => {
  await mockAdmin(page, { rich: true });
  await login(page);
  await nav(page, 'Настройки');
  const route = (id) => page.locator('.model-card').filter({ has: page.getByText(id, { exact: true }) }).locator('.model-route');
  await expect(route('claude-opus-5-5')).toContainText('маршрут есть');
  await expect(route('gpt-5.5')).toContainText('нет ресурса');
  await expect(route('gpt-5.5')).toHaveAttribute('title', /openai_provider_resource_name/);
  await expect(route('gemini-3-pro')).toContainText('нужен BYOK или retention');
  await expect(route('gemini-3-pro')).toHaveAttribute('title', /Google BYOK/);
});

// The relay summarises the enabled accounts; one failing among several must not read as "the session is down".
const SESSION_CASES = [
  ['all enabled accounts renew', { error: null, accounts: { enabled: 3, failing: 0 } }, 'сессия Retool активна', /tone-pos/],
  ['one of three fails', { error: 'session renewal failed: HTTP 401', accounts: { enabled: 3, failing: 1 } },
    'ошибка сессии: 1 из 3', /tone-warn/],
  ['every enabled account fails', { error: 'session renewal failed: HTTP 401', accounts: { enabled: 2, failing: 2 } },
    'сессия Retool с ошибкой', /tone-neg/],
  ['an older relay without the summary', { error: 'session renewal failed: HTTP 401' }, 'сессия Retool с ошибкой', /tone-neg/],
];
for (const [name, session, text, tone] of SESSION_CASES) {
  test(`the connection card reports the pool session: ${name}`, async ({ page }) => {
    await mockAdmin(page, { overviewPatch: { session: { keepalive: true, last_success_at: null, persistence_error: null, ...session } } });
    await login(page);
    await nav(page, 'Настройки');
    const status = page.locator('#settings-connection .status');
    await expect(status).toContainText(text);
    await expect(status.locator('.dot')).toHaveClass(tone);
    if (session.accounts?.failing && session.accounts.failing < session.accounts.enabled) {
      await expect(status).toHaveAttribute('title', /остальные работают/);
    }
  });
}

test('incomplete usage is not shown as free inference or a reliable forecast', async ({ page }) => {
  await mockAdmin(page, { metricsPatch: { totals: { credits: 0, credits_complete: false } },
    overviewPatch: { balance: { available: 100, monthly_allocation: 3000, unreported_usage: true } } });
  await login(page);
  await expect(page.locator('.forecast')).toContainText('часть стоимости не сообщена');
  await expect(page.locator('.stat').first()).toContainText('—');
  await expect(page.getByText('есть неучтённый расход')).toBeVisible();
});

test('a balance known for only some of the accounts says how many are missing, and so does its forecast', async ({ page }) => {
  await mockAdmin(page, { overviewPatch: { balance: PARTIAL_BALANCE } });
  await login(page);
  await expect(page.locator('.figure-xl')).toContainText('1 479,89');
  await expect(page.locator('.figure-xl .tag')).toHaveText('частично: 1 из 2 неизвестны');
  await expect(page.locator('.figure-xl .tag')).toHaveAttribute('title', /только по аккаунтам с известным балансом/);
  await expect(page.locator('.forecast')).toContainText('по известным балансам хватит примерно на');
});

test('a balance with a known total but a missing monthly limit says so, and its forecast needs no qualifier', async ({ page }) => {
  await mockAdmin(page, { overviewPatch: { balance: { ...PARTIAL_BALANCE, unknown_accounts: 0 } } });
  await login(page);
  await expect(page.locator('.figure-xl .tag')).toHaveText('частично: лимит месяца известен не у всех');
  await expect(page.locator('.forecast')).toContainText('хватит примерно на');
  await expect(page.locator('.forecast')).not.toContainText('по известным балансам');
});

for (const [name, patch] of [
  ['every balance is known', {}],
  ['the relay does not report the pool\'s coverage', { balance: { available: 1495.09, monthly_allocation: 6000, used_month: 4504.91, reset_date: RESET_DATE } }],
]) {
  test(`a balance that covers the whole pool carries no partial mark: ${name}`, async ({ page }) => {
    await mockAdmin(page, { overviewPatch: patch });
    await login(page);
    await expect(page.locator('.figure-xl')).toContainText('495,09');
    await expect(page.locator('.figure-xl .tag')).toHaveCount(0);
    await expect(page.locator('.forecast')).toContainText('хватит примерно на');
    await expect(page.locator('.forecast')).not.toContainText('по известным балансам');
  });
}

for (const width of [280, 320]) {
  test(`the partial mark stays inside the balance panel at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 850 });
    await mockAdmin(page, { overviewPatch: { balance: { ...PARTIAL_BALANCE, accounts_total: 12, unknown_accounts: 10 } } });
    await login(page);
    const mark = page.locator('.figure-xl .tag');
    await expect(mark).toHaveText('частично: 10 из 12 неизвестны');
    const inside = await page.evaluate(() => {
      const tag = document.querySelector('.figure-xl .tag').getBoundingClientRect();
      const panel = document.querySelector('.instrument').getBoundingClientRect();
      return { left: tag.left - panel.left, right: panel.right - tag.right, wide: document.documentElement.scrollWidth - innerWidth };
    });
    expect(inside.left).toBeGreaterThanOrEqual(0);
    expect(inside.right).toBeGreaterThanOrEqual(0);
    expect(inside.wide).toBeLessThanOrEqual(1);
  });
}

test('periods without a complete price are marked on the spend chart and summed as a lower bound', async ({ page }) => {
  // Period 21 holds a known part of its price, period 31 none at all; the relay counts the unpriced requests in both
  const buckets = metricsFor(3600).buckets.map((bucket, i) => (i === 20 ? { ...bucket, credits: 0.2, credits_unknown: 1, credits_complete: false }
    : i === 30 ? { ...bucket, credits: 0, credits_unknown: 1, credits_complete: false } : bucket));
  await mockAdmin(page, { metricsPatch: { buckets, totals: { credits_complete: false, credits_unknown: 2 } } });
  await login(page);
  const chart = page.getByRole('slider', { name: /Расход кредитов/ });
  await expect(chart).toHaveAttribute('aria-label', /всего не менее \d+,\d\d, максимум не менее \d+,\d\d\. В 2 периодах стоимость сообщена не полностью/);
  // Marks on the chart itself: a hollow diamond per period and dashed stretches around it
  await expect(page.locator('.c-unsure-dot')).toHaveCount(2);
  await expect(page.locator('.c-spend-line.is-unsure')).toHaveCount(2);
  await expect(page.locator('.c-spend-line:not(.is-unsure)')).toHaveCount(3);
  await expect(page.locator('.legend', { hasText: 'за каждые 1 мин' })).toContainText('стоимость сообщена не полностью');
  // The unknown period is read as unknown, not as zero
  await chart.focus();
  await chart.press('Home');
  for (let i = 0; i < 20; i += 1) await chart.press('ArrowRight');
  await expect(chart).toHaveAttribute('aria-valuenow', '21');
  await expect(chart).toHaveAttribute('aria-valuetext', /кредиты не менее 0,20/);
  await expect(chart).toHaveAttribute('aria-valuetext', /без цены 1/);
  await expect(page.locator('.chart-tip')).toContainText('≥ 0,20');
  for (let i = 0; i < 10; i += 1) await chart.press('ArrowRight');
  await expect(chart).toHaveAttribute('aria-valuenow', '31');
  await expect(chart).toHaveAttribute('aria-valuetext', /кредиты не сообщены/);
  await expect(chart).toHaveAttribute('aria-valuetext', /без цены 1/);
  await expect(page.locator('.chart-tip dd').first()).toContainText('—');
});

test('a spend chart whose prices are all reported has no lower-bound marks', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await expect(page.getByRole('slider', { name: /Расход кредитов/ })).toHaveAttribute('aria-label', /всего \d/);
  await expect(page.getByRole('slider', { name: /Расход кредитов/ })).not.toHaveAttribute('aria-label', /не менее/);
  await expect(page.locator('.c-unsure-dot')).toHaveCount(0);
  await expect(page.locator('.c-spend-line.is-unsure')).toHaveCount(0);
  await expect(page.locator('.legend', { hasText: 'за каждые 1 мин' })).not.toContainText('не полностью');
});

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', hour12: false });
// A moment of another day carries its date ("5 ноября, 23:55"): the suite may run just after midnight.
const since = (ts) => `(?:\\d{1,2} [а-яё]+, )?${clock(ts)}`;

test('a window the journal covers only in part says how much it covers and greys out the charts before that', async ({ page }) => {
  const from = NOW - 600;
  await mockAdmin(page, { metricsPatch: { coverage: { complete: false, window_start: NOW - 3600, covered_from: from, covered_seconds: 600,
    oldest_event_ts: from + 12, buffer_oldest_ts: from - 100, buffer_events: 40, buffer_capacity: 20000 } } });
  await login(page);
  const notice = page.locator('.banner.tone-warn');
  await expect(notice).toContainText('Журнал покрывает окно не полностью');
  await expect(notice).toContainText('данные за 10 мин из окна 60 мин');
  await expect(notice).toContainText(new RegExp(`\\(с ${since(from)}\\)`));
  await expect(notice).toContainText('Суммы за окно неполные');
  // Both charts grey out what the journal does not cover: 49 of the 60 one-minute periods end before it began
  await expect(page.locator('.c-uncovered')).toHaveCount(2);
  const spend = page.locator('svg.chart').first();
  await expect(spend).toHaveAttribute('aria-label', new RegExp(`Данные только с ${since(from)}`));
  await spend.focus();
  await spend.press('Home');
  await expect(spend).toHaveAttribute('aria-valuetext', /нет данных: журнал начат позже/);
  await expect(page.locator('.chart-tip')).toContainText('нет данных');
  await expect(page.locator('.c-hover-dot')).toHaveCount(0);
  await spend.press('End');
  await expect(spend).toHaveAttribute('aria-valuetext', /кредиты .*запросы /);
  await expect(page.locator('.chart-tip')).not.toContainText('журнал начат позже');
  await expect(page.locator('.c-hover-dot')).toHaveCount(1);
  // The line starts where the data does, not inside the grey stretch
  const edge = await page.locator('.c-uncovered').first().locator('rect').evaluate((rect) => rect.x.baseVal.value + rect.width.baseVal.value);
  const starts = await page.locator('.c-spend-line').evaluateAll((paths) => paths.map((path) => Number(path.getAttribute('d').match(/^M(-?[\d.]+)/)[1])));
  expect(Math.min(...starts)).toBeGreaterThanOrEqual(edge - 0.5);
  // The cache chart marks the same stretch and its tooltip says so
  const cache = page.locator('svg.chart').nth(1);
  await cache.focus();
  await cache.press('Home');
  await expect(cache).toHaveAttribute('aria-valuetext', /нет данных: журнал начат позже/);
  await expect(cache).toHaveAttribute('aria-label', new RegExp(`Данные только с ${clock(from)}`));
});

test('the gap follows the window that is chosen', async ({ page }) => {
  const from = NOW - 600;
  await mockAdmin(page, { metricsPatch: { coverage: { complete: false, covered_from: from, covered_seconds: 600 } } });
  await login(page);
  await expect(page.locator('.banner.tone-warn')).toContainText('данные за 10 мин из окна 60 мин');
  await page.getByRole('group', { name: 'Окно метрик' }).getByRole('button', { name: '6 ч' }).click();
  await expect(page.locator('.banner.tone-warn')).toContainText('данные за 10 мин из окна 6 ч');
  await expect(page.locator('.c-uncovered')).toHaveCount(2);
  const spend = page.locator('svg.chart').first();
  await expect(spend).toHaveAttribute('aria-valuemax', '72');
  await spend.focus();
  await spend.press('End');
  await spend.press('ArrowLeft');
  await expect(spend).toHaveAttribute('aria-valuenow', '71');
  await expect(spend).not.toHaveAttribute('aria-valuetext', /журнал начат позже/);
});

// The spend of a half covered window is that of its covered half: dividing by the whole window would halve the rate
for (const [name, coverage, figure, basis] of [
  ['completely', { complete: true, covered_seconds: 3600, covered_from: NOW - 4200 }, '86,40', 'последний час'],
  ['for half of it', { complete: false, covered_seconds: 1800, covered_from: NOW - 1800 }, '172,80', '30 мин'],
  ['for exactly the shortest stretch a forecast needs', { complete: false, covered_seconds: 300, covered_from: NOW - 300 }, '1 036,80', '5 мин'],
]) {
  test(`the burn rate divides the spend by what the journal covers: window covered ${name}`, async ({ page }) => {
    await mockAdmin(page, { metricsPatch: { totals: { credits: 3.6 }, coverage } });
    await login(page);
    await expect(page.locator('.forecast')).toContainText(`При темпе за ${basis}`);
    await expect(page.locator('.forecast b').first()).toHaveText(figure);
    await expect(page.locator('.forecast')).toContainText('хватит примерно на');
  });
}

for (const [name, coverage, reason] of [
  ['shorter than the shortest stretch a forecast needs', { complete: false, covered_seconds: 299, covered_from: NOW - 299 }, 'журнал покрывает меньше 5 минут'],
  ['of an unknown length', { complete: false, covered_from: NOW - 900 }, 'неизвестно, какую часть окна покрывает журнал'],
]) {
  test(`no forecast from a window covered ${name}`, async ({ page }) => {
    await mockAdmin(page, { metricsPatch: { totals: { credits: 3.6 }, coverage } });
    await login(page);
    await expect(page.locator('.forecast')).toContainText(`Прогноз недоступен: ${reason}`);
    await expect(page.locator('.forecast')).not.toContainText('хватит примерно');
  });
}

test('a gap in the price withholds the forecast even when the whole window is covered', async ({ page }) => {
  await mockAdmin(page, { metricsPatch: { totals: { credits: 3.6, credits_complete: false, credits_unknown: 1 } } });
  await login(page);
  await expect(page.locator('.forecast')).toContainText('Прогноз недоступен: часть стоимости не сообщена');
});

test('the spend figure is the relay\'s total: the cache refresh is named beside it, never added to it', async ({ page }) => {
  await mockAdmin(page, { metricsPatch: { totals: { credits: 2, keepwarm_credits: 0.5, keepwarm_pings: 3, credits_in_progress: 2 } } });
  await login(page);
  const spent = page.locator('.stat', { hasText: 'Потрачено за окно' });
  await expect(spent.locator('.stat-value')).toHaveText('2,00');
  // The note stays one short line: a turn still waiting for its price is named, the refresh is not
  await expect(spent.locator('.stat-note')).toContainText('ждём цену: 2 хода');
  await expect(spent.locator('.stat-note')).not.toContainText('часть стоимости');
  await expect(spent.locator('.stat-note')).not.toContainText('прогрев');
  const warm = page.locator('.cache-facts dl div', { hasText: 'прогрев кэша' });
  await expect(warm.locator('dd')).toHaveText('0,50');
  await expect(warm.locator('dd')).toHaveAttribute('title', /Входит в расход за окно\. Обращений для прогрева: 3/);
});

test('without a cache refresh in the window there is no row for it', async ({ page }) => {
  await mockAdmin(page, { metricsPatch: { totals: { keepwarm_credits: 0, keepwarm_pings: 0 } } });
  await login(page);
  await expect(page.locator('.cache-facts dl div')).toHaveCount(3);
  await expect(page.locator('.cache-facts')).not.toContainText('прогрев кэша');
});

test('a lower-bound spend names how many requests have no price; an unpriced cache refresh makes its share a lower bound too', async ({ page }) => {
  await mockAdmin(page, { metricsPatch: { totals: { credits: 2, credits_complete: false, credits_unknown: 3, keepwarm_unknown: 1, keepwarm_credits: 0.5, keepwarm_pings: 2 } } });
  await login(page);
  const spent = page.locator('.stat', { hasText: 'Потрачено за окно' });
  await expect(spent.locator('.stat-value')).toHaveText('≥ 2,00');
  await expect(spent.locator('.stat-note')).toContainText('часть стоимости не сообщена (без цены: 3)');
  const warm = page.locator('.cache-facts dl div', { hasText: 'прогрев кэша' });
  await expect(warm.locator('dd')).toHaveText('≥ 0,50');
  await expect(warm.locator('dd')).toHaveAttribute('title', /без цены: 1/);
});

test('a period of the spend chart names its cache refresh and its unpriced requests', async ({ page }) => {
  const buckets = metricsFor(3600).buckets.map((bucket, i) => (i === 20
    ? { ...bucket, credits: 0.3, keepwarm_credits: 0.05, credits_unknown: 2, credits_complete: false } : bucket));
  await mockAdmin(page, { metricsPatch: { buckets, totals: { credits_complete: false, credits_unknown: 2 } } });
  await login(page);
  const chart = page.getByRole('slider', { name: /Расход кредитов/ });
  await chart.focus();
  await chart.press('Home');
  for (let i = 0; i < 20; i += 1) await chart.press('ArrowRight');
  await expect(chart).toHaveAttribute('aria-valuetext', /кредиты не менее 0,30, в т\. ч\. прогрев кэша 0,05, без цены 2, запросы /);
  await expect(page.locator('.chart-tip')).toContainText('в т. ч. прогрев кэша');
  await expect(page.locator('.chart-tip')).toContainText('без цены');
  // The figure is the bucket's credits as the relay sent them: the refresh is not added on top
  await expect(page.locator('.chart-tip dd').first()).toContainText('≥ 0,30');
  await chart.press('ArrowRight');
  await expect(chart).not.toHaveAttribute('aria-valuetext', /прогрев кэша|без цены/);
});

for (const [name, coverage] of [
  ['covered completely', { complete: true, covered_from: NOW - 99999, covered_seconds: 3600, oldest_event_ts: NOW - 99999 }],
  ['reported by a relay that does not say how much it covers', undefined],
]) {
  test(`a window ${name} shows no gap`, async ({ page }) => {
    await mockAdmin(page, { metricsPatch: coverage ? { coverage } : {} });
    await login(page);
    await expect(page.locator('.forecast')).toContainText('хватит примерно на');
    await expect(page.locator('.banner.tone-warn')).toHaveCount(0);
    await expect(page.locator('.c-uncovered')).toHaveCount(0);
    const spend = page.locator('svg.chart').first();
    await expect(spend).not.toHaveAttribute('aria-label', /Данные только с/);
    await spend.focus();
    await spend.press('Home');
    await expect(spend).not.toHaveAttribute('aria-valuetext', /журнал начат позже/);
  });
}

test('a window that is not covered and has no spend says so instead of "no spend"', async ({ page }) => {
  const from = NOW - 600;
  const buckets = metricsFor(3600).buckets.map((bucket) => ({ ...bucket, credits: 0, cache_read: 0, cache_write: 0 }));
  await mockAdmin(page, { metricsPatch: { buckets, coverage: { complete: false, covered_from: from, covered_seconds: 600 } } });
  await login(page);
  await expect(page.locator('.chart-empty').first()).toHaveText(`С ${clock(from)} расхода не было`);
  await expect(page.locator('.chart-empty').nth(1)).toHaveText(`С ${clock(from)} обращений к кэшу не было`);
});

test('activity tabs and chart values work with the keyboard', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  // the chart takes keys once its periods are in: until the metrics arrive it is a picture, not a slider
  const chart = page.getByRole('slider', { name: /Расход кредитов/ });
  await chart.focus();
  await chart.press('End');
  await expect(page.locator('.chart-tip')).toBeVisible();
  await expect(page.locator('.chart-tip')).toContainText('кредиты');
  await chart.press('Escape');
  await expect(page.locator('.chart-tip')).toHaveCount(0);
  await nav(page, 'Активность');
  const turns = page.getByRole('tab', { name: /Ходы моделей/ });
  await turns.focus();
  await turns.press('ArrowRight');
  await expect(page.getByRole('tab', { name: /Все события/ })).toBeFocused();
  await expect(page.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'activity-tab-events');
});

test('activity tabs follow the WAI-ARIA tabs pattern: each controls a real panel, and a panel is a tab stop only when it holds nothing focusable', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Активность');
  const wiring = () => page.evaluate(() => [...document.querySelectorAll('[role="tab"]')].map((tab) => {
    const panel = document.getElementById(tab.getAttribute('aria-controls'));
    return { tab: tab.id, selected: tab.getAttribute('aria-selected'), role: panel?.getAttribute('role'),
      labelledby: panel?.getAttribute('aria-labelledby'), hidden: panel?.hidden };
  }));
  expect(await wiring()).toEqual([
    { tab: 'activity-tab-turns', selected: 'true', role: 'tabpanel', labelledby: 'activity-tab-turns', hidden: false },
    { tab: 'activity-tab-events', selected: 'false', role: 'tabpanel', labelledby: 'activity-tab-events', hidden: true },
  ]);
  // The turns hold buttons to land on: the panel itself is not an extra stop, and Tab goes straight to the first turn
  const panel = page.getByRole('tabpanel');
  await expect.poll(() => panel.getAttribute('tabindex')).toBeNull();
  await page.getByRole('tab', { name: /Ходы моделей/ }).focus();
  await page.keyboard.press('Tab');
  await expect(page.locator('.trace-head').first()).toBeFocused();
  await page.getByRole('tab', { name: /Все события/ }).click();
  expect(await wiring()).toEqual([
    { tab: 'activity-tab-turns', selected: 'false', role: 'tabpanel', labelledby: 'activity-tab-turns', hidden: true },
    { tab: 'activity-tab-events', selected: 'true', role: 'tabpanel', labelledby: 'activity-tab-events', hidden: false },
  ]);
  await expect.poll(() => panel.getAttribute('tabindex')).toBeNull();
});

test('an empty activity panel is a tab stop of its own, so its message can be reached', async ({ page }) => {
  await mockAdmin(page, { empty: true });
  await login(page);
  await nav(page, 'Активность');
  const panel = page.getByRole('tabpanel');
  await expect(panel).toContainText('Ходов пока нет');
  await expect(panel).toHaveAttribute('tabindex', '0');
  await page.getByRole('tab', { name: /Ходы моделей/ }).focus();
  await page.keyboard.press('Tab');
  await expect(panel).toBeFocused();
});

test('charts are sliders over their periods: Escape clears, an arrow re-enters from its own end, a passing pointer keeps the keyboard choice', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  // An image carries neither focus nor key handlers: both charts are sliders with a value read out in words
  await expect(page.locator('svg.chart')).toHaveCount(2);
  await expect(page.locator('svg.chart[role="img"]')).toHaveCount(0);
  const chart = page.getByRole('slider', { name: /Расход кредитов/ });
  await expect(chart).toHaveAttribute('tabindex', '0');
  await expect(chart).toHaveAttribute('aria-valuemin', '1');
  await expect(chart).toHaveAttribute('aria-valuemax', '60');
  await expect(chart).toHaveAttribute('aria-valuetext', 'период не выбран');
  await chart.focus();
  await chart.press('End');
  await expect(chart).toHaveAttribute('aria-valuenow', '60');
  await expect(chart).toHaveAttribute('aria-valuetext', /кредиты .*запросы .*ходы /);
  await expect(page.locator('.chart-tip')).toBeVisible();
  await chart.press('Escape');
  await expect(page.locator('.chart-tip')).toHaveCount(0);
  await expect(chart).toHaveAttribute('aria-valuetext', 'период не выбран');
  // From nothing, right starts at the first period (it used to skip to the second) and left at the last
  await chart.press('ArrowRight');
  await expect(chart).toHaveAttribute('aria-valuenow', '1');
  await chart.press('ArrowRight');
  await expect(chart).toHaveAttribute('aria-valuenow', '2');
  await chart.press('Escape');
  await chart.press('ArrowLeft');
  await expect(chart).toHaveAttribute('aria-valuenow', '60');
  await chart.press('ArrowDown');
  await expect(chart).toHaveAttribute('aria-valuenow', '59');
  await chart.press('ArrowUp');
  await expect(chart).toHaveAttribute('aria-valuenow', '60');
  // The pointer takes over while it is over the chart and gives the period back when it leaves
  await chart.press('End');
  await chart.scrollIntoViewIfNeeded();
  const box = await chart.boundingBox();
  await page.mouse.move(box.x + 56 + 20, box.y + box.height / 2);
  await expect(chart).toHaveAttribute('aria-valuenow', /^[1-5]$/);
  await page.mouse.move(5, 5);
  await expect(chart).toBeFocused();
  await expect(chart).toHaveAttribute('aria-valuenow', '60');
  await expect(page.locator('.chart-tip')).toBeVisible();
  // Leaving focus drops the keyboard choice
  await chart.blur();
  await expect(page.locator('.chart-tip')).toHaveCount(0);
});

test('a chart reached only by the pointer clears when the pointer leaves', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  const chart = page.getByRole('slider', { name: /Расход кредитов/ });
  await chart.scrollIntoViewIfNeeded();
  const box = await chart.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator('.chart-tip')).toBeVisible();
  await expect(chart).not.toBeFocused();
  await page.mouse.move(5, 5);
  await expect(page.locator('.chart-tip')).toHaveCount(0);
});

test('a chart without periods is a plain image: not focusable, no slider', async ({ page }) => {
  await mockAdmin(page, { metricsPatch: { buckets: [] } });
  await login(page);
  await expect(page.locator('svg.chart[role="img"]')).toHaveCount(2);
  await expect(page.locator('svg.chart[role="slider"]')).toHaveCount(0);
  await expect(page.locator('svg.chart[tabindex]')).toHaveCount(0);
});

test('model controls stay readable on a narrow display', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 850 });
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Настройки');
  await expect(page.locator('.model-effort').first()).toBeVisible();
  await expect(page.locator('.model-effort').first()).toContainText('Effort:');
  await expect(page.locator('.model-effort').first()).toContainText('Verbosity:');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test('keyboard shortcuts switch sections and the theme toggles', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await page.keyboard.press('2');
  await expect(page.getByRole('heading', { name: 'Активность' })).toBeVisible();
  await page.keyboard.press('3');
  await expect(page.getByRole('heading', { name: 'Аккаунты' })).toBeVisible();
  await page.keyboard.press('4');
  await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
  const before = await page.evaluate(() => document.documentElement.dataset.theme);
  await page.getByRole('button', { name: 'Сменить тему' }).click();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).not.toBe(before);
});

test('no glow, gradient, uppercase or heavy weight in any section', async ({ page }) => {
  await mockAdmin(page, { rich: true });
  await login(page);
  const scan = () => page.evaluate(() => {
    const bad = [];
    for (const el of document.querySelectorAll('.shell *')) {
      if (el.closest('.ss-stage')) continue;
      const s = getComputedStyle(el);
      if (s.backgroundImage.includes('gradient')) bad.push(`gradient ${el.className}`);
      if (s.backdropFilter && s.backdropFilter !== 'none') bad.push(`blur ${el.className}`);
      if (Number(s.fontWeight) >= 700) bad.push(`weight ${el.className}`);
      if (s.textTransform === 'uppercase') bad.push(`upper ${el.className}`);
    }
    return bad;
  });
  const found = { overview: await scan() };
  for (const name of ['Активность', 'Аккаунты', 'Настройки']) {
    await nav(page, name);
    if (name === 'Аккаунты') await page.getByRole('button', { name: 'Добавить аккаунт' }).click();
    found[name] = await scan();
  }
  expect(found).toEqual({ overview: [], Активность: [], Аккаунты: [], Настройки: [] });
});

for (const width of [280, 320, 414]) {
  test(`has no horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 850 });
    await mockAdmin(page);
    await page.goto('./');
    const noOverflow = () => expect.poll(() => page.evaluate(() =>
      document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await expect(page.locator('.login-hero .ss-stage')).toBeVisible();
    await noOverflow();
    await login(page);
    await noOverflow();
    for (const name of ['Активность', 'Аккаунты', 'Настройки']) {
      await nav(page, name);
      await noOverflow();
    }
    await page.getByRole('tab', { name: /Все события/ }).count();
  });
}
