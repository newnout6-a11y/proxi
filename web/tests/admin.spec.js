import { test, expect } from '@playwright/test';
import { API_KEY, DEFAULT_ROLES, MEMORY_KEY, ev, journal, mockAdmin } from './fixtures.js';

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
