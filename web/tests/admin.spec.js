import { test, expect } from '@playwright/test';
import { API_KEY, MEMORY_KEY, ev, journal, mockAdmin } from './fixtures.js';

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

test('spend chart draws one bar per busy bucket, so spend falls back after a peak', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  const heights = await page.locator('.c-spend').evaluateAll((bars) => bars.map((b) => Number(b.getAttribute('height'))));
  expect(heights.length).toBeGreaterThan(10);
  const peak = heights.indexOf(Math.max(...heights));
  expect(heights.slice(peak + 1).some((h) => h < heights[peak])).toBe(true);
  await expect(page.locator('.c-spend.peak')).toHaveCount(1);
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
  await page.getByRole('switch', { name: 'Аккаунт основной в маршрутизации' }).click();
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
  await expect(trace.locator('[title="Скрытое мышление"]')).toContainText('6,40 с', { timeout: 10000 });
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
  await expect(reserve).toContainText('применяется сразу');
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

test('no glow, gradient or backdrop blur remains in the shipped styles', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  const found = await page.evaluate(() => {
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
  expect(found).toEqual([]);
});

for (const width of [320, 414]) {
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
