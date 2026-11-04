import { test, expect } from '@playwright/test';
import { API_KEY, MEMORY_KEY, ev, journal, mockAdmin } from './fixtures.js';

async function login(page, { remember = false } = {}) {
  await page.goto('./');
  await page.getByLabel('Relay key').fill(API_KEY);
  await page.getByLabel('Запомнить до закрытия вкладки').setChecked(remember);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('heading', { name: 'Обзор' })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Обзор' })).toBeVisible();
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
  await expect(page.getByRole('heading', { name: 'Обзор' })).toBeVisible();
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

test('overview shows balance, forecast, stats, chart, models and the live feed', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await expect(page.locator('.figure-xl')).toContainText('495,09');
  await expect(page.locator('.forecast')).toContainText('хватит примерно на');
  await expect(page.getByText('Retool отвечает')).toBeVisible();
  await expect(page.locator('.stat', { hasText: 'Попадание в кэш' })).toContainText('%');
  await expect(page.locator('.chart')).toBeVisible();
  await expect(page.locator('.feed-row', { hasText: 'POST /v1/messages → 200' })).toBeVisible();
  await expect(page.locator('.rank li')).toHaveCount(2);
  await expect(page.locator('code', { hasText: 'http://127.0.0.1:8787/v1' })).toBeVisible();
});

test('metrics window switcher requests a 6 hour window', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await page.getByRole('group', { name: 'Окно метрик' }).getByRole('button', { name: '6 ч' }).click();
  await expect.poll(() => state.requests.some(({ url }) => url.includes('metrics?window=21600'))).toBe(true);
  await expect(page.getByText(/последние 6 часов/)).toBeVisible();
});

test('accounts block lists every account and marks the reserve', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  const rows = page.locator('.acc');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('pro-main');
  await expect(rows.nth(1)).toContainText('pro-spare');
  await expect(rows.nth(1)).toContainText('резерв');
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

test('journal clear asks for confirmation and empties the buffer into the illustrated empty state', async ({ page }) => {
  const state = await mockAdmin(page);
  await login(page);
  await nav(page, 'Журнал');
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Очистить' }).click();
  await expect(page.getByText('Журнал пуст')).toBeVisible();
  await expect(page.locator('.empty .art .ss-stage')).toBeVisible();
  expect(state.requests.some(({ path, headers }) => path === '/admin/api/events/clear' && headers['x-admin-request'] === '1')).toBe(true);
});

test('turn traces aggregate journal events into a turn tape and steps', async ({ page }) => {
  await mockAdmin(page);
  await login(page);
  await nav(page, 'Ходы');
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
  await nav(page, 'Ходы');
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
  await nav(page, 'Ходы');
  await expect(page.getByText('Ходов пока нет')).toBeVisible();
  await expect(page.locator('.empty .art .ss-stage')).toBeVisible();
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
    for (const name of ['Журнал', 'Ходы', 'Модели', 'Настройки']) {
      await nav(page, name);
      await noOverflow();
    }
  });
}
