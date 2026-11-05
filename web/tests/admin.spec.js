import { test, expect } from '@playwright/test';
import { API_KEY, MEMORY_KEY, ev, journal, mockAdmin } from './fixtures.js';
import { NOW, metricsFor } from './fixtures.js';

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

test('incomplete usage is not shown as free inference or a reliable forecast', async ({ page }) => {
  await mockAdmin(page, { metricsPatch: { totals: { credits: 0, credits_complete: false } },
    overviewPatch: { balance: { available: 100, monthly_allocation: 3000, unreported_usage: true } } });
  await login(page);
  await expect(page.locator('.forecast')).toContainText('часть стоимости не сообщена');
  await expect(page.locator('.stat').first()).toContainText('—');
  await expect(page.getByText('есть неучтённый расход')).toBeVisible();
});

test('periods without a complete price are marked on the spend chart and summed as a lower bound', async ({ page }) => {
  // Period 21 holds a known part of its price, period 31 none at all
  const buckets = metricsFor(3600).buckets.map((bucket, i) => (i === 20 ? { ...bucket, credits: 0.2, credits_complete: false }
    : i === 30 ? { ...bucket, credits: 0, credits_complete: false } : bucket));
  await mockAdmin(page, { metricsPatch: { buckets, totals: { credits_complete: false } } });
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
  await expect(page.locator('.chart-tip')).toContainText('≥ 0,20');
  for (let i = 0; i < 10; i += 1) await chart.press('ArrowRight');
  await expect(chart).toHaveAttribute('aria-valuenow', '31');
  await expect(chart).toHaveAttribute('aria-valuetext', /кредиты не сообщены/);
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

test('activity tabs and chart values work with the keyboard', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  const chart = page.locator('.chart').first();
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
