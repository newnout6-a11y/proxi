import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { API_KEY, mockAdmin } from './fixtures.js';
import { BUILTIN, SYSTEM_OPENCODE, initialDocument, mockPrompts } from './prompts-fixtures.js';

async function login(page) {
  await page.goto('./');
  await page.getByLabel('Relay key').fill(API_KEY);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('heading', { name: 'Сводка' })).toBeVisible();
}

const rail = (page, name) => page.getByRole('navigation', { name: 'Разделы' }).getByRole('button', { name: new RegExp(name) });
const tab = (page, name) => page.getByRole('tab', { name: new RegExp(`^${name}`) });

/** The prompts section, open on its first tab; `options` go to the prompts mock. */
async function openPrompts(page, options = {}, name = 'Профили') {
  const admin = await mockAdmin(page);
  const prompts = await mockPrompts(page, options);
  await login(page);
  await rail(page, 'Промпты').click();
  await expect(page.getByRole('heading', { name: 'Промпты', exact: true })).toBeVisible();
  if (name !== 'Профили') await tab(page, name).click();
  return { admin, prompts };
}

const profileBar = (page) => page.getByRole('group', { name: 'Профили', exact: true });
const head = (page) => page.getByLabel('Шаблон head', { exact: true });
const live = (page) => page.getByRole('region', { name: 'Что получит модель' });
const saveButton = (page) => page.getByRole('button', { name: 'Сохранить', exact: true });
const savebar = (page) => page.locator('.savebar');
const ruleTitles = (page) => page.locator('.rule-list:not(.locked) .rule-title').allTextContents();
const userRule = (page, index) => page.locator('.rule-list:not(.locked) > .rule').nth(index);
const section = (page, label) => page.locator('.p-disclosure', { hasText: label }).first();

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => { throw error; });
});

// The app polls and debounces on timers. Left running while the test is torn down, a timer can fire after the routes
// that answer for the page are gone and before the page is, and its request goes out to whatever the preview server
// proxies to. A passing test has nothing left to show, so its page is closed here; a failing one keeps it for the screenshot.
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status === testInfo.expectedStatus) await page.close();
});

test('the section opens from the rail and with key 5, and shows the revision and the profiles', async ({ page }) => {
  await mockAdmin(page);
  await mockPrompts(page);
  await login(page);
  await page.keyboard.press('5');
  await expect(page.getByRole('heading', { name: 'Промпты', exact: true })).toBeVisible();
  await expect(rail(page, 'Промпты')).toHaveAttribute('aria-current', 'page');
  await expect(rail(page, 'Промпты').locator('kbd')).toHaveText('5');
  await expect(page).toHaveURL(/#prompts\/profiles$/);
  await expect(page.locator('.page-head .tag')).toHaveText('ревизия 3');
  await expect(tab(page, 'Профили')).toHaveAttribute('aria-selected', 'true');
  await expect(profileBar(page).getByRole('button')).toHaveText([/^passthrough\s+встроенный$/, 'mine-first', 'exact-opus', 'Новый профиль']);
  await expect(profileBar(page).getByRole('button', { name: 'mine-first' })).toHaveAttribute('aria-current', 'true');
  await expect(page.getByRole('region', { name: 'Профиль mine-first' })).toBeVisible();
});

test('without a prompts file the screen says the legacy path is in force and starts with a first profile', async ({ page }) => {
  const { prompts } = await openPrompts(page, { legacy: true });
  await expect(page.locator('.page-head .tag')).toHaveText('legacy');
  const intro = page.getByRole('region', { name: 'Файл промптов не создан' });
  await expect(intro).toContainText('прежнем режиме (legacy)');
  await expect(intro).toContainText('Самостоятельные запросы (standalone)');
  await expect(intro).toContainText('329 симв. (встроенная)');
  await expect(intro).toContainText('C:\\relay\\prompts.json');
  await expect(savebar(page)).toHaveCount(0);

  await intro.getByRole('button', { name: 'Создать первый профиль' }).click();
  await expect(page.getByLabel('Имя профиля')).toHaveValue('mine');
  await page.getByLabel('Имя профиля').fill('первый');
  await expect(page.getByRole('button', { name: 'Создать профиль' })).toBeDisabled();
  await page.getByLabel('Имя профиля').fill('first');
  await page.getByRole('radio', { name: /Только мой текст/ }).check();
  await page.getByRole('button', { name: 'Создать профиль' }).click();
  await expect(page.getByRole('region', { name: 'Профиль first' })).toBeVisible();
  await expect(head(page)).toHaveValue(/Ты — ассистент по программированию/);
  await expect(savebar(page)).toContainText('1 несохранённых');
  await expect(page.getByText('Ни одно правило не использует этот профиль')).toBeVisible();

  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.base_rev).toBeNull();
  expect(prompts.saves[0].body.document).toEqual({ version: 1, profiles: { first: { head: 'Ты — ассистент по программированию. Отвечай кратко и проверяй изменения.' } }, rules: [] });
  expect(prompts.saves[0].headers['x-admin-request']).toBe('1');
  await expect(page.getByText('Промпты сохранены')).toBeVisible();
  await expect(page.locator('.page-head .tag')).toHaveText('ревизия 1');
  await expect(savebar(page)).toHaveCount(0);
});

test('legacy mode still previews and lists pins, only the editors wait for a profile', async ({ page }) => {
  await openPrompts(page, { legacy: true }, 'Правила');
  await expect(page.getByRole('region', { name: 'Файл промптов не создан' })).toBeVisible();
  await tab(page, 'Тексты').click();
  await expect(page.getByRole('region', { name: 'Файл промптов не создан' })).toBeVisible();
  await tab(page, 'Пины').click();
  await expect(page.locator('.pin-row')).toHaveCount(2);
});

test('a file the relay refused is reported with its reason and the revision that keeps working', async ({ page }) => {
  await openPrompts(page, { errors: [{ message: 'profiles.mine-first.head: unknown variable {{typo}}', param: 'profiles.mine-first.head', ts: 1 }] });
  const banner = page.locator('.p-file-errors');
  await expect(banner).toContainText('на диске не принят');
  await expect(banner).toContainText('Продолжает действовать ревизия 3');
  await expect(banner).toContainText('unknown variable {{typo}}');
  await expect(banner).toContainText('profiles.mine-first.head');
});

test('a file the relay refused while no revision was ever accepted is told apart from no file at all', async ({ page }) => {
  await openPrompts(page, { legacy: true, errors: [{ message: 'prompts.json: invalid JSON at line 3', param: '', ts: 1 }] });
  await expect(page.locator('.p-file-errors')).toContainText('Действует прежний режим');
  const intro = page.getByRole('region', { name: 'Файл промптов не действует' });
  await expect(intro).toContainText('Файл есть, но relay его не принял');
  await expect(intro).toContainText('сохранение заменит файл');
  await expect(page.getByRole('region', { name: 'Файл промптов не создан' })).toHaveCount(0);
});

test('the head template counts UTF-8 bytes, inserts a variable at the caret and flags references the relay would refuse', async ({ page }) => {
  await openPrompts(page);
  const field = head(page);
  await field.fill('ж'.repeat(100));
  await expect(page.getByText('100 симв. · 200 из 262 144 байт')).toBeVisible();
  await field.fill('Начало  конец');
  await field.evaluate((element) => element.setSelectionRange(7, 7));
  const palette = page.getByRole('group', { name: 'Вставить переменную в курсор' });
  await palette.getByRole('button', { name: '{{role}}', exact: true }).click();
  await expect(field).toHaveValue('Начало {{role}} конец');
  await expect(field).toBeFocused();
  expect(await field.evaluate((element) => [element.selectionStart, element.selectionEnd])).toEqual([15, 15]);
  // a selection is replaced
  await field.evaluate((element) => element.setSelectionRange(0, 6));
  await palette.getByRole('button', { name: '{{text:my_rules}}', exact: true }).click();
  await expect(field).toHaveValue('{{text:my_rules}} {{role}} конец');

  await expect(page.getByText(/Неизвестная переменная/)).toHaveCount(0);
  await field.fill('{{typo}} {{text:gone}} {{client_system}}');
  await expect(page.getByText('Неизвестная переменная {{typo}}')).toBeVisible();
  await expect(page.getByText('Нет именованного текста «gone»: создайте его на вкладке «Тексты»')).toBeVisible();
  await field.fill('{{relay_task}}');
  await expect(page.getByText('только для запросов, которыми владеет relay')).toBeVisible();
  await expect(page.getByText('В шаблоне нет {{client_system}}')).toBeVisible();
  await field.fill('ж'.repeat(131073));
  await expect(page.getByText('Текст больше 262 144 байт: relay его не примет')).toBeVisible();
  await expect(field).toHaveAttribute('aria-invalid', 'true');
});

test('hovering or focusing a variable says what it means', async ({ page }) => {
  await openPrompts(page);
  const palette = page.getByRole('group', { name: 'Вставить переменную в курсор' });
  await palette.getByRole('button', { name: '{{native_role}}', exact: true }).focus();
  await expect(page.locator('.p-palette-hint')).toContainText('роль для клиента с tools');
  await palette.getByRole('button', { name: '{{text:my_rules}}', exact: true }).hover();
  await expect(page.locator('.p-palette-hint')).toContainText('именованный текст');
});

test('edits of the client system are forms by operation: add, change, reorder and remove', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const system = section(page, 'Правки system клиента');
  const rows = system.locator('.p-edit');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0).getByLabel('Заголовок')).toHaveValue('# Code comments');
  await expect(rows.nth(1).getByLabel('Тег')).toHaveValue('available_skills');

  await system.getByLabel('Новая правка').selectOption('replace_text');
  await system.getByRole('button', { name: 'Добавить правку' }).click();
  await expect(rows).toHaveCount(3);
  await rows.nth(2).getByLabel('Найти').fill('IMPORTANT');
  await rows.nth(2).getByLabel('Заменить на').fill('NOTE');
  await rows.nth(2).getByLabel('Все вхождения').check();
  await system.getByLabel('Новая правка').selectOption('remove_regex');
  await system.getByRole('button', { name: 'Добавить правку' }).click();
  await rows.nth(3).getByLabel('Регулярное выражение').fill('<env>[\\s\\S]*?</env>');
  await rows.nth(3).getByLabel('s · точка берёт и перевод строки').check();
  await rows.nth(3).getByLabel('i · без учёта регистра').check();

  await system.getByRole('button', { name: 'Поднять правку 4' }).click();
  await system.getByRole('button', { name: 'Удалить правку 1' }).click();
  await expect(rows).toHaveCount(3);
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.profiles['mine-first'].client_system.edits).toEqual([
    { op: 'remove_tag', tag: 'available_skills' },
    { op: 'remove_regex', pattern: '<env>[\\s\\S]*?</env>', flags: 'is' },
    { op: 'replace_text', find: 'IMPORTANT', with: 'NOTE', all: true },
  ]);
});

test('the focus stays with the user in a list of edits: a new edit takes it, a move keeps it, a removal hands it on', async ({ page }) => {
  await openPrompts(page);
  const system = section(page, 'Правки system клиента');
  const rows = system.locator('.p-edit');
  const button = (name) => system.getByRole('button', { name });

  // an added edit is for typing into
  await system.getByLabel('Новая правка').selectOption('replace_text');
  await button('Добавить правку').click();
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(2).getByLabel('Найти')).toBeFocused();
  const tools = section(page, 'Инструменты');
  await tools.getByLabel('Новая правка').selectOption('hide');
  await tools.getByRole('button', { name: 'Добавить правку' }).click();
  await expect(tools.locator('.p-edit').nth(1).getByLabel('Инструмент')).toBeFocused();

  // a move leaves the focus on the same button of the moved row, so the key can be pressed again
  await button('Поднять правку 3').focus();
  await page.keyboard.press('Enter');
  await expect(button('Поднять правку 2')).toBeFocused();
  await expect(rows.nth(1).getByLabel('Найти')).toBeVisible();
  // a row that reached the top has no button to go up: the focus takes the other one of the row
  await page.keyboard.press('Enter');
  await expect(button('Опустить правку 1')).toBeFocused();
  await expect(rows.nth(0).getByLabel('Найти')).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(button('Опустить правку 2')).toBeFocused();
  await expect(rows.nth(1).getByLabel('Найти')).toBeVisible();

  // a removed row hands the focus to the one that took its place, and the last one to the button that adds
  await button('Удалить правку 1').click();
  await expect(rows).toHaveCount(2);
  await expect(button('Удалить правку 1')).toBeFocused();
  await button('Удалить правку 2').click();
  await expect(rows).toHaveCount(1);
  await expect(button('Удалить правку 1')).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(system.getByText('Правок пока нет')).toBeVisible();
  await expect(button('Добавить правку')).toBeFocused();
});

test('the operation of an edit can be switched and keeps the fields both forms share', async ({ page }) => {
  await openPrompts(page);
  const first = section(page, 'Правки system клиента').locator('.p-edit').nth(1);
  await first.getByLabel('Операция правки 2').selectOption('keep_tag_inner');
  await expect(first.getByLabel('Тег')).toHaveValue('available_skills');
  await first.getByLabel('Операция правки 2').selectOption('prepend');
  await expect(first.getByLabel('Текст')).toHaveValue('');
  await expect(first.getByLabel('Текст')).toHaveAttribute('aria-invalid', 'true');
  await expect(first).toContainText('Заполните поле');
});

test('history, tools, generation and publishing are saved only when they say something', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const history = section(page, 'История диалога');
  await history.getByLabel('Системные сообщения посреди диалога').selectOption('drop');
  await expect(history).toContainText('Системные сообщения посреди диалога не доходят до модели');
  const blocks = history.locator('.p-edit');
  await expect(blocks).toHaveCount(1);
  await expect(blocks.first().getByLabel('Тег')).toHaveValue('system-reminder');
  await expect(blocks.first().getByLabel('Только если содержит')).toHaveValue('Plan mode');

  const tools = section(page, 'Инструменты');
  await tools.getByLabel('Новая правка').selectOption('hide');
  await tools.getByRole('button', { name: 'Добавить правку' }).click();
  const hidden = tools.locator('.p-edit').nth(1);
  await hidden.getByLabel('Инструмент').fill('webfetch');
  await expect(hidden).toContainText('Инструмент не попадёт к модели');

  const generation = section(page, 'Генерация');
  await generation.locator('summary').click();
  await generation.getByLabel('Effort').selectOption('high');
  await generation.getByLabel('Verbosity').selectOption('low');
  const publish = section(page, 'Публикация как модель');
  await publish.locator('summary').click();
  await publish.getByRole('radio', { name: 'Только для моделей по маске' }).check();
  const masks = publish.getByLabel('Маски моделей: добавить значение');
  await masks.fill('claude-opus-*');
  await masks.press('Enter');
  await expect(publish).toContainText('claude-opus-5-5@mine-first');

  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  const saved = prompts.saves[0].body.document.profiles['mine-first'];
  expect(saved.history).toEqual({ mid_system: 'drop', user_blocks: [{ op: 'drop_tag', tag: 'system-reminder', contains: 'Plan mode' }] });
  expect(saved.tools.edits).toEqual([{ tool: 'bash', op: 'append', text: 'Не запускай команды, меняющие систему, без подтверждения.' }, { tool: 'webfetch', hide: true }]);
  expect(saved.generation).toEqual({ effort: 'high', verbosity: 'low' });
  expect(saved.publish_as_model).toEqual(['claude-*', 'claude-opus-*']);
});

test('emptying the list of model masks keeps the list open for the next mask, and "all" and "none" are saved as they are', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const publish = section(page, 'Публикация как модель');
  await publish.locator('summary').click();
  await publish.getByRole('radio', { name: 'Только для моделей по маске' }).check();
  const masks = publish.getByRole('group', { name: 'Маски моделей' });
  await expect(masks.locator('.p-token')).toHaveText(['claude-*']);
  await masks.getByRole('button', { name: 'Убрать значение claude-*' }).click();
  await expect(masks).toBeVisible();
  await expect(publish.getByRole('radio', { name: 'Только для моделей по маске' })).toBeChecked();
  await expect(publish).toContainText('Без масок профиль не публикуется');
  await masks.getByLabel('Маски моделей: добавить значение').fill('gpt-*');
  await masks.getByLabel('Маски моделей: добавить значение').press('Enter');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.profiles['mine-first'].publish_as_model).toEqual(['gpt-*']);
  await publish.getByRole('radio', { name: 'Опубликовать для всех моделей каталога' }).check();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(2);
  expect(prompts.saves[1].body.document.profiles['mine-first'].publish_as_model).toBe(true);
  await publish.getByRole('radio', { name: 'Не публиковать' }).check();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(3);
  expect('publish_as_model' in prompts.saves[2].body.document.profiles['mine-first']).toBe(false);
});

test('back to nothing: a profile whose sections are emptied is saved without them', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const system = section(page, 'Правки system клиента');
  await system.getByRole('button', { name: 'Удалить правку 2' }).click();
  await system.getByRole('button', { name: 'Удалить правку 1' }).click();
  await expect(system.getByText('Правок пока нет')).toBeVisible();
  // the section stays open after its last edit is gone: the user may add another
  await expect(system.getByRole('button', { name: 'Добавить правку' })).toBeVisible();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect('client_system' in prompts.saves[0].body.document.profiles['mine-first']).toBe(false);
});

test('a profile can be created, duplicated, renamed everywhere it is named and removed when no rule uses it', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  await page.getByRole('button', { name: 'Новый профиль' }).click();
  await page.getByLabel('Имя профиля').fill('mine-first');
  await expect(page.getByText('Профиль с таким именем уже есть')).toBeVisible();
  await page.getByLabel('Имя профиля').fill('scratch');
  await page.getByRole('radio', { name: /Мои правила в начале/ }).check();
  await page.getByRole('button', { name: 'Создать профиль' }).click();
  await expect(page.getByRole('region', { name: 'Профиль scratch' })).toBeVisible();
  await expect(head(page)).toHaveValue(/Отвечай по-русски/);
  // unused: removable at once, and the notice offers a rule
  await expect(page.getByRole('button', { name: 'Удалить', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Дублировать' }).click();
  await expect(profileBar(page).getByRole('button', { name: 'scratch-copy' })).toHaveAttribute('aria-current', 'true');

  await page.getByRole('button', { name: 'Переименовать' }).click();
  await page.getByLabel('Новое имя').fill('bad name');
  await expect(page.getByText(/латинские буквы/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Переименовать' }).last()).toBeDisabled();
  await page.getByLabel('Новое имя').fill('copy2');
  await page.getByRole('button', { name: 'Переименовать' }).last().click();
  await expect(profileBar(page).getByRole('button', { name: 'copy2' })).toBeVisible();

  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Удалить', exact: true }).click();
  await expect(profileBar(page).getByRole('button', { name: 'copy2' })).toHaveCount(0);

  await profileBar(page).getByRole('button', { name: 'mine-first' }).click();
  await page.getByRole('button', { name: 'Переименовать' }).click();
  await page.getByLabel('Новое имя').fill('mine');
  await page.getByRole('button', { name: 'Переименовать' }).last().click();
  await expect(page.getByText('3. opencode')).toBeVisible();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  const saved = prompts.saves[0].body.document;
  expect(Object.keys(saved.profiles)).toEqual(['mine', 'exact-opus', 'scratch']);
  expect(saved.rules.map((rule) => rule.use)).toEqual(['exact-opus', '$header', 'mine']);
});

test('a profile the rules use cannot be removed, and one that no rule uses can get a rule in one click', async ({ page }) => {
  await openPrompts(page);
  await expect(page.getByRole('button', { name: 'Удалить', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Удалить', exact: true })).toHaveAttribute('title', /сначала измените/);
  await page.getByRole('button', { name: 'Новый профиль' }).click();
  await page.getByLabel('Имя профиля').fill('later');
  await page.getByRole('button', { name: 'Создать профиль' }).click();
  await page.getByRole('button', { name: 'Добавить правило' }).click();
  await expect(tab(page, 'Правила')).toHaveAttribute('aria-selected', 'true');
  expect(await ruleTitles(page)).toEqual(['opus свой', 'по заголовку', 'opencode', 'Новое правило']);
  const row = userRule(page, 3);
  await expect(row.getByRole('button', { name: /Новое правило/ }).first()).toHaveAttribute('aria-expanded', 'true');
  await expect(row.getByLabel('Применить профиль')).toHaveValue('later');
});

test('the live preview applies the profile under edit to the chosen sample and shows what it cut and added', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const panel = live(page);
  await expect(panel).toContainText('Образец запроса');
  await expect(panel).toContainText('opencode · claude-sonnet-5-5');
  // the profile is judged by the draft, whatever the rules say, next to the same request through passthrough
  await expect.poll(() => prompts.previews.length).toBeGreaterThanOrEqual(2);
  const draftRequest = prompts.previews.find((item) => item.body.document?.default_profile === 'mine-first').body;
  expect(draftRequest.request_id).toBe('req-open-2');
  expect(draftRequest.document.rules).toEqual([]);
  expect(draftRequest.document.profiles['mine-first'].head).toBe('{{text:my_rules}}\n\n{{client_system}}');
  const baseline = prompts.previews.find((item) => item.body.document?.default_profile === 'passthrough').body;
  expect(baseline.document).toEqual({ version: 1, default_profile: 'passthrough', profiles: {}, rules: [] });

  const removed = panel.locator('.diff-del .diff-text');
  await expect(removed.filter({ hasText: '# Code comments' })).toBeVisible();
  await expect(removed.filter({ hasText: 'IMPORTANT: DO NOT ADD ***ANY*** COMMENTS unless asked.' })).toBeVisible();
  await expect(removed.filter({ hasText: '<available_skills>' })).toBeVisible();
  await expect(panel.locator('.diff-add .diff-text', { hasText: 'Отвечай по-русски' })).toBeVisible();
  await expect(panel.locator('.diff-stats')).toContainText('+');
  // what is unchanged is folded away behind a button
  await panel.getByRole('button', { name: /Показать неизменённые строки/ }).first().click();
  await expect(panel.locator('.diff-same', { hasText: 'Tool usage policy' })).toBeVisible();
  await expect(panel.getByText('512 байт · ≈ 128 токенов')).toBeVisible();

  // the facts of a forced profile leave out the rule and the revision
  await expect(panel.locator('.p-facts dt')).toHaveText(['Назначение', 'Клиент', 'Модель', 'Размер head']);

  // the edits of the client's system of the profile under edit are reported one by one under the diff
  const edited = panel.getByRole('region', { name: 'Как сработали правки system клиента' });
  await expect(edited.locator('li')).toHaveCount(2);
  await expect(edited.locator('li .tag', { hasText: 'сработала' })).toHaveCount(2);

  // an edit changes the answer within a moment
  const before = prompts.previews.length;
  await page.getByRole('button', { name: 'Удалить правку 1' }).first().click();
  await expect(panel.locator('.diff-del .diff-text', { hasText: '# Code comments' })).toHaveCount(0);
  await expect(edited.locator('li')).toHaveCount(1);
  expect(prompts.previews.length).toBeGreaterThan(before);
  await panel.getByRole('button', { name: 'Итоговый head' }).click();
  await expect(panel.locator('.head-text')).toContainText('IMPORTANT: DO NOT ADD');
  await expect(panel.locator('.head-text [data-source="text:my_rules"]')).toHaveText('Отвечай по-русски. Не трогай файлы без явной просьбы.');
});

test('the preview is asked again only after typing pauses, and an answer that comes late never replaces a newer one', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const failed = [];
  page.on('requestfailed', (request) => { if (request.url().includes('/prompts/preview')) failed.push(request.failure()?.errorText); });
  await expect.poll(() => prompts.previews.length).toBeGreaterThanOrEqual(2);
  await live(page).getByRole('button', { name: 'Итоговый head' }).click();
  prompts.previewDelay = (body) => (body.document?.profiles?.['mine-first']?.head?.includes('SLOW') ? 1500 : 0);
  const field = head(page);
  const settled = prompts.previews.length;
  await field.fill('SLOW {{client_system}}');
  await expect.poll(() => prompts.previews.length).toBe(settled + 1);
  // typing on cancels the slow request and asks again
  await field.fill('FAST {{client_system}}');
  await expect(live(page).locator('.head-text')).toContainText('FAST You are opencode');
  await page.waitForTimeout(1800);
  await expect(live(page).locator('.head-text')).toContainText('FAST You are opencode');
  await expect(live(page).locator('.head-text')).not.toContainText('SLOW');
  expect(failed).toContain('net::ERR_ABORTED');
  expect(prompts.previews.length).toBe(settled + 2);
  // a burst of keystrokes inside one task is one request
  const burst = prompts.previews.length;
  await field.evaluate((element) => {
    const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
    for (const letter of 'abcdef') {
      setValue.call(element, element.value + letter);
      element.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });
  await expect(field).toHaveValue('FAST {{client_system}}abcdef');
  await expect.poll(() => prompts.previews.length).toBe(burst + 1);
  await page.waitForTimeout(600);
  expect(prompts.previews.length).toBe(burst + 1);
});

test('a draft the relay refuses is said so in the live panel with the path it blames', async ({ page }) => {
  await openPrompts(page);
  await head(page).fill('{{typo}}');
  await expect(live(page).getByRole('alert')).toContainText('Черновик не принят relay: profiles.mine-first.head: unknown variable {{typo}} (profiles.mine-first.head)');
});

test('without a kept request the live panel asks for a sample and works from a pasted body', async ({ page }) => {
  await mockAdmin(page);
  const prompts = await mockPrompts(page);
  prompts.recent.forEach((entry) => { entry.keep = false; });
  await login(page);
  await rail(page, 'Промпты').click();
  const panel = live(page);
  await expect(panel).toContainText('Выберите образец');
  await expect(panel.getByText('Relay пока не хранит тел запросов')).toBeVisible();
  await panel.getByRole('radio', { name: 'Своё тело запроса' }).check();
  await panel.getByLabel('Тело запроса (JSON)').fill('{"model": "claude-sonnet-5-5", "system": "# Code comments\\nno\\n# Tools\\nuse"}');
  await panel.getByLabel('Заголовки').fill('User-Agent: opencode/1.4.2');
  await expect(panel.locator('.diff-del .diff-text', { hasText: '# Code comments' })).toBeVisible();
  await expect(panel.getByRole('option', { name: 'Определить по телу (anthropic)' })).toBeAttached();
  await panel.getByLabel('Тело запроса (JSON)').fill('{"model": ');
  await expect(panel.getByText(/Не JSON/)).toBeVisible();
});

// --- rules ---------------------------------------------------------------------------------------------------------

test('protected rules come first and are locked, and turning protection off says what it costs', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Правила');
  const locked = page.getByRole('list', { name: 'Встроенные защитные правила' });
  await expect(locked.locator('.rule-title')).toHaveText(['protected: service requests', 'protected: Claude Code compaction']);
  await expect(locked).toContainText('Назначение: session-title или auto-mode-check или connection-probe или client-service → passthrough');
  await expect(locked.getByRole('switch')).toHaveCount(0);
  await expect(locked.getByRole('button')).toHaveCount(0);
  // they stand above the user's rules
  const order = await page.locator('.rule-list').evaluateAll((lists) => lists.map((list) => list.classList.contains('locked')));
  expect(order).toEqual([true, false]);

  const toggle = page.getByRole('switch', { name: 'Защита служебных запросов' });
  await expect(toggle).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByText('Защита выключена. Заголовки диалогов')).toHaveCount(0);
  await toggle.click();
  const warning = page.getByRole('alert').filter({ hasText: 'Защита выключена' });
  await expect(warning).toContainText('может сломать названия чатов и сжатие');
  await expect(locked).toContainText('не действует');
  await expect(savebar(page)).toContainText('защита служебных запросов');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.protected).toEqual({ enabled: false });
  await toggle.click();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(2);
  expect('protected' in prompts.saves[1].body.document).toBe(false);
});

test('rules can be switched off, renamed, pointed at another profile and made to override protection with a warning', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Правила');
  expect(await ruleTitles(page)).toEqual(['opus свой', 'по заголовку', 'opencode']);
  await expect(userRule(page, 0)).toContainText('Модель: claude-opus-* · Клиент: opencode · Есть инструменты: да → exact-opus');
  await expect(userRule(page, 1)).toContainText('Заголовок x-proxi-profile: * → $header');
  await page.getByRole('switch', { name: 'Правило по заголовку включено' }).click();
  await expect(userRule(page, 1).getByRole('switch')).toHaveAttribute('aria-checked', 'false');
  await expect(userRule(page, 1)).toContainText('выключено');

  await userRule(page, 2).getByRole('button', { name: /opencode/ }).first().click();
  const editor = userRule(page, 2).locator('.rule-editor');
  await editor.getByLabel('Название').fill('все opencode');
  await editor.getByLabel('Применить профиль').selectOption('exact-opus');
  await expect(editor.getByLabel('Применить профиль').locator('option')).toHaveText(['passthrough', 'mine-first', 'exact-opus', '$header — Профиль из заголовка', '$virtual — Профиль из имени модели', '$legacy — Прежний путь']);
  await editor.getByLabel('Применить профиль').selectOption('$legacy');
  await expect(editor).toContainText('Всё как без файла промптов');
  await expect(page.getByText('Правило сработает и на служебных запросах')).toHaveCount(0);
  await editor.getByLabel('Применять и к защищённым запросам (override_protected)').check();
  await expect(page.getByRole('alert').filter({ hasText: 'Правило сработает и на служебных запросах' })).toBeVisible();
  await expect(userRule(page, 2)).toContainText('обходит защиту');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.rules).toEqual([
    initialDocument().rules[0],
    { ...initialDocument().rules[1], enabled: false },
    { name: 'все opencode', if: { client: 'opencode' }, use: '$legacy', override_protected: true },
  ]);
});

test('conditions are values with masks: several mean "or", a yes/no condition is a choice, and a condition can be removed', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Правила');
  await userRule(page, 2).getByRole('button', { name: /opencode/ }).first().click();
  const editor = userRule(page, 2).locator('.rule-editor');
  const client = editor.getByRole('group', { name: 'Клиент' });
  await expect(client.locator('.p-token')).toHaveText(['opencode']);
  await client.getByRole('button', { name: 'codex', exact: true }).click();
  await expect(client.locator('.p-token')).toHaveText(['opencode', 'codex']);
  await client.getByLabel('Клиент: добавить значение').fill('claude-*');
  await client.getByLabel('Клиент: добавить значение').press('Enter');
  await expect(userRule(page, 2)).toContainText('Клиент: opencode или codex или claude-*');
  await client.getByRole('button', { name: 'Убрать значение codex' }).click();

  await editor.getByLabel('Добавить условие').selectOption('tools');
  await expect(editor.getByLabel('Есть инструменты', { exact: true })).toHaveValue('true');
  await editor.getByLabel('Есть инструменты', { exact: true }).selectOption('false');
  await editor.getByLabel('Добавить условие').selectOption('model');
  const model = editor.getByRole('group', { name: 'Модель' });
  await expect(model.locator('.p-token')).toHaveCount(0);
  // a condition with no values yet is not written, and the row stays until it is removed
  await expect(userRule(page, 2)).toContainText('Клиент: opencode или claude-* · Есть инструменты: нет');
  await model.getByLabel('Модель: добавить значение').fill('claude-opus-*');
  await model.getByLabel('Модель: добавить значение').press('Enter');
  await editor.getByRole('button', { name: 'Убрать условие «Модель»' }).click();
  await editor.getByLabel('Добавить условие').selectOption('purpose');
  await editor.getByRole('group', { name: 'Назначение' }).getByRole('button', { name: 'client-tools' }).click();
  await editor.getByRole('button', { name: 'Убрать условие «Клиент»' }).click();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.rules[2].if).toEqual({ tools: false, purpose: 'client-tools' });
});

test('a rule is moved with its buttons, announced aloud, and the focus stays on the button that was pressed', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Правила');
  const status = page.locator('.p-rules-panel [role="status"]');
  await page.getByRole('button', { name: 'Выше: правило opencode' }).click();
  expect(await ruleTitles(page)).toEqual(['opus свой', 'opencode', 'по заголовку']);
  await expect(status).toHaveText(/Правило «opencode»: позиция 2 из 3/);
  await expect(page.getByRole('button', { name: 'Выше: правило opencode' })).toBeFocused();
  await page.keyboard.press('Enter');
  expect(await ruleTitles(page)).toEqual(['opencode', 'opus свой', 'по заголовку']);
  await expect(page.getByRole('button', { name: 'Выше: правило opencode' })).toBeDisabled();
  // the first rule cannot go up; the focus falls to the other way instead of getting lost
  await expect(page.getByRole('button', { name: 'Ниже: правило opencode' })).toBeFocused();
  await page.keyboard.press('Enter');
  expect(await ruleTitles(page)).toEqual(['opus свой', 'opencode', 'по заголовку']);
  await expect(page.getByRole('button', { name: 'Ниже: правило по заголовку' })).toBeDisabled();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.rules.map((rule) => rule.name)).toEqual(['opus свой', 'opencode', 'по заголовку']);
});

test('a rule is moved from the keyboard with Alt and the arrows on its name', async ({ page }) => {
  await openPrompts(page, {}, 'Правила');
  const main = (name) => userRule(page, 0).page().locator('.rule-main', { hasText: name });
  await main('по заголовку').focus();
  await expect(main('по заголовку')).toHaveAttribute('aria-keyshortcuts', 'Alt+ArrowUp Alt+ArrowDown');
  await page.keyboard.press('Alt+ArrowUp');
  expect(await ruleTitles(page)).toEqual(['по заголовку', 'opus свой', 'opencode']);
  await expect(main('по заголовку')).toBeFocused();
  await page.keyboard.press('Alt+ArrowUp');
  expect(await ruleTitles(page)).toEqual(['по заголовку', 'opus свой', 'opencode']);
  await page.keyboard.press('Alt+ArrowDown');
  await page.keyboard.press('Alt+ArrowDown');
  expect(await ruleTitles(page)).toEqual(['opus свой', 'opencode', 'по заголовку']);
  await expect(main('по заголовку')).toBeFocused();
  await expect(page.locator('.p-rules-panel [role="status"]')).toContainText('Правило «по заголовку»: позиция 3 из 3');
  // a plain arrow does not move anything
  await page.keyboard.press('ArrowUp');
  expect(await ruleTitles(page)).toEqual(['opus свой', 'opencode', 'по заголовку']);
});

test('a rule is moved by dragging its grip, with a mark where it will land, and Escape cancels the drag', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1100 });
  await openPrompts(page, {}, 'Правила');
  const grip = userRule(page, 0).locator('.rule-grip');
  const last = userRule(page, 2);
  await grip.scrollIntoViewIfNeeded();
  const from = await grip.boundingBox();
  const target = await last.boundingBox();
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + 4, target.y + target.height * 0.75, { steps: 8 });
  await expect(userRule(page, 0)).toHaveClass(/is-dragging/);
  await expect(last).toHaveAttribute('data-drop', 'after');
  await page.keyboard.press('Escape');
  await page.mouse.up();
  expect(await ruleTitles(page)).toEqual(['opus свой', 'по заголовку', 'opencode']);
  await expect(page.locator('.rule.is-dragging')).toHaveCount(0);

  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + 4, target.y + target.height * 0.75, { steps: 8 });
  await page.mouse.up();
  expect(await ruleTitles(page)).toEqual(['по заголовку', 'opencode', 'opus свой']);
  await expect(savebar(page)).toContainText('правила');
  // dragging up: to the top half of the first row
  const bottom = await userRule(page, 2).locator('.rule-grip').boundingBox();
  const top = await userRule(page, 0).boundingBox();
  await page.mouse.move(bottom.x + bottom.width / 2, bottom.y + bottom.height / 2);
  await page.mouse.down();
  await page.mouse.move(bottom.x + 4, top.y + 6, { steps: 8 });
  await expect(userRule(page, 0)).toHaveAttribute('data-drop', 'before');
  await page.mouse.up();
  expect(await ruleTitles(page)).toEqual(['opus свой', 'по заголовку', 'opencode']);
});

test('rules can be added and removed, the first catch-all makes later rules unreachable, and the default profile is chosen last', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Правила');
  await page.getByRole('button', { name: 'Добавить правило' }).first().click();
  expect(await ruleTitles(page)).toEqual(['opus свой', 'по заголовку', 'opencode', 'Новое правило']);
  const added = userRule(page, 3);
  await expect(added.getByRole('button', { name: /Новое правило/ }).first()).toHaveAttribute('aria-expanded', 'true');
  await expect(added).toContainText('любой запрос');
  await expect(added.getByLabel('Применить профиль')).toHaveValue('mine-first');
  await added.getByLabel('Название').fill('остальные');
  await page.getByRole('button', { name: 'Выше: правило остальные' }).click();
  await page.getByRole('button', { name: 'Выше: правило остальные' }).click();
  expect(await ruleTitles(page)).toEqual(['opus свой', 'остальные', 'по заголовку', 'opencode']);
  await expect(userRule(page, 2).locator('.tag', { hasText: 'недостижимо' })).toBeVisible();
  await expect(userRule(page, 3).locator('.tag', { hasText: 'недостижимо' })).toBeVisible();
  await expect(userRule(page, 1).locator('.tag', { hasText: 'недостижимо' })).toHaveCount(0);

  await page.getByRole('button', { name: 'Удалить правило по заголовку' }).click();
  await expect(page.locator('.p-rules-panel [role="status"]')).toContainText('Правило «по заголовку» удалено из черновика');
  await page.getByLabel('Если ни одно правило не подошло').selectOption('exact-opus');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  const saved = prompts.saves[0].body.document;
  expect(saved.rules.map((rule) => rule.name)).toEqual(['opus свой', 'остальные', 'opencode']);
  expect(saved.default_profile).toBe('exact-opus');
  await page.getByLabel('Если ни одно правило не подошло').selectOption('');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(2);
  expect('default_profile' in prompts.saves[1].body.document).toBe(false);
});

// --- texts ---------------------------------------------------------------------------------------------------------

test('named texts are added, edited, renamed with their references and removed, and a text from a file is shown as such', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Тексты');
  const named = page.getByRole('region', { name: 'Именованные тексты' });
  await expect(named.locator('article h3')).toHaveText(['my_rules', 'opus_role']);
  await expect(named.getByRole('textbox', { name: 'Текст my_rules', exact: true })).toHaveValue('Отвечай по-русски. Не трогай файлы без явной просьбы.');
  await expect(named).toContainText('из файла: prompts/opus.md');
  await expect(named).toContainText('Используется в профилях: mine-first');
  await expect(named.getByRole('textbox', { name: 'Текст opus_role', exact: true })).toHaveCount(0);

  await named.getByLabel('Новый текст: имя').fill('has space');
  await expect(named.getByRole('button', { name: 'Добавить текст' })).toBeDisabled();
  await named.getByLabel('Новый текст: имя').fill('footer');
  await named.getByRole('button', { name: 'Добавить текст' }).click();
  await expect(named.getByRole('textbox', { name: 'Текст footer', exact: true })).toHaveValue('');
  await named.getByRole('textbox', { name: 'Текст footer', exact: true }).fill('Конец ответа.');
  await expect(named.getByText('Пока нигде не используется')).toBeVisible();

  const mine = named.getByRole('article', { name: 'Текст my_rules' });
  await mine.getByRole('button', { name: 'Переименовать' }).click();
  await mine.getByLabel('Новое имя').fill('footer');
  await expect(mine.getByText('Текст с таким именем уже есть')).toBeVisible();
  await mine.getByLabel('Новое имя').fill('rules_ru');
  await mine.getByRole('button', { name: 'Переименовать' }).last().click();
  await expect(named.locator('article h3')).toHaveText(['rules_ru', 'opus_role', 'footer']);
  await expect(named).toContainText('Используется в профилях: mine-first');

  await named.getByRole('button', { name: 'Удалить текст footer' }).click();
  await expect(named.locator('article h3')).toHaveText(['rules_ru', 'opus_role']);
  const asked = [];
  page.on('dialog', (dialog) => { asked.push(dialog.message()); dialog.dismiss(); });
  await named.getByRole('button', { name: 'Удалить текст rules_ru' }).click();
  await expect.poll(() => asked.length).toBe(1);
  expect(asked[0]).toContain('используется в профилях: mine-first');
  await expect(named.locator('article h3')).toHaveText(['rules_ru', 'opus_role']);

  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  const saved = prompts.saves[0].body.document;
  expect(Object.keys(saved.texts)).toEqual(['rules_ru', 'opus_role']);
  expect(saved.texts.opus_role).toEqual({ file: 'prompts/opus.md' });
  expect(saved.profiles['mine-first'].head).toBe('{{text:rules_ru}}\n\n{{client_system}}');
});

test('a text kept in a file can be replaced by text in the document', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Тексты');
  const named = page.getByRole('region', { name: 'Именованные тексты' });
  await named.getByRole('button', { name: 'Заменить текстом в документе' }).click();
  await expect(named.getByRole('textbox', { name: 'Текст opus_role', exact: true })).toHaveValue('');
  await named.getByRole('textbox', { name: 'Текст opus_role', exact: true }).fill('Ты — Opus.');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.texts.opus_role).toBe('Ты — Opus.');
});

test('relay texts show the built-in text, start from it when changed, and go back to it', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Тексты');
  const relay = page.getByRole('region', { name: 'Тексты relay' });
  await expect(relay.locator('article h3')).toHaveText(['Роль relay', 'Роль для клиента с инструментами', 'Обёртка структурированного ответа', 'Обёртка напоминаний', 'Базовые инструкции Codex']);
  await expect(relay.locator('article .tag')).toHaveText(['встроенный', 'встроенный', 'встроенный', 'встроенный', 'встроенный']);
  const role = relay.getByRole('article', { name: 'Роль relay' });
  await expect(role.getByLabel('Встроенный текст: Роль relay')).toHaveValue(/You are a coding assistant behind a local OpenAI\/Anthropic relay/);
  await expect(role.getByLabel('Встроенный текст: Роль relay')).not.toBeEditable();
  const reminder = relay.getByRole('article', { name: 'Обёртка напоминаний' });
  await expect(reminder).toContainText('Переменные: {{text}}');
  await expect(relay.getByRole('article', { name: 'Обёртка структурированного ответа' })).toContainText('{{default}} {{schema}} {{schema_name}}');

  await role.getByRole('button', { name: 'Изменить' }).click();
  await expect(role.locator('.tag')).toHaveText('свой текст');
  await expect(role.getByLabel('Роль relay', { exact: true })).toHaveValue(/You are a coding assistant behind a local/);
  await expect(savebar(page)).toContainText('текст relay: role');
  await role.getByLabel('Роль relay', { exact: true }).fill('Ты — мой инженер.');
  await expect(role.getByText('Встроенный текст', { exact: true })).toBeVisible();
  await reminder.getByRole('button', { name: 'Изменить' }).click();
  await reminder.getByLabel('Обёртка напоминаний', { exact: true }).fill('[НАПОМИНАНИЕ] {{text}}');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.relay_texts).toEqual({ role: 'Ты — мой инженер.', native_role: null, structured: null, reminder: '[НАПОМИНАНИЕ] {{text}}', codex_base: null });

  await role.getByRole('button', { name: 'Вернуть встроенный' }).click();
  await expect(role.locator('.tag')).toHaveText('встроенный');
  await expect(role.getByLabel('Встроенный текст: Роль relay')).toHaveValue(/You are a coding assistant behind a local/);
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(2);
  // built-in again: the key is gone, and nulls that were already there stay as they were
  expect(prompts.saves[1].body.document.relay_texts).toEqual({ native_role: null, structured: null, reminder: '[НАПОМИНАНИЕ] {{text}}', codex_base: null });
});

test('the Codex base instructions are one text or a text per model mask', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Тексты');
  const codex = page.getByRole('article', { name: 'Базовые инструкции Codex' });
  await codex.getByRole('button', { name: 'Изменить' }).click();
  await codex.getByRole('button', { name: 'Разделить по маскам моделей' }).click();
  await expect(codex.getByLabel('Маска модели')).toHaveValue('*');
  await codex.getByLabel('Маска модели').fill('gpt-6*');
  await codex.getByRole('button', { name: 'Добавить маску' }).click();
  await codex.getByLabel('Маска модели').nth(1).fill('*');
  await codex.getByLabel('Инструкции для *').fill('Общий текст');
  await codex.getByLabel('Инструкции для gpt-6*').fill('Текст для gpt-6');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.relay_texts.codex_base).toEqual({ 'gpt-6*': 'Текст для gpt-6', '*': 'Общий текст' });
  await codex.getByRole('button', { name: 'Одна строка для всех моделей' }).click();
  await expect(codex.getByLabel('Базовые инструкции Codex', { exact: true })).toHaveValue('Текст для gpt-6');
  await codex.getByRole('button', { name: 'Вернуть встроенный' }).click();
  await expect(codex.locator('.tag')).toHaveText('встроенный');
});

test('a relay text the relay did not describe says so and starts empty', async ({ page }) => {
  await openPrompts(page, { builtin: { ...BUILTIN, relay_texts: { role: 'ROLE', native_role: 'N' } } }, 'Тексты');
  const reminder = page.getByRole('article', { name: 'Обёртка напоминаний' });
  await expect(reminder).toContainText('Relay не сообщил встроенный текст');
  await reminder.getByRole('button', { name: 'Изменить' }).click();
  await expect(reminder.getByLabel('Обёртка напоминаний', { exact: true })).toHaveValue('');
});

// --- preview -------------------------------------------------------------------------------------------------------

test('a pasted request is previewed with its headers and dialect: rule, profile, revision, parts and what was changed', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Предпросмотр');
  const sample = page.getByRole('region', { name: 'Образец' });
  await sample.getByRole('radio', { name: 'Своё тело запроса' }).check();
  const body = { model: 'claude-sonnet-5-5', system: SYSTEM_OPENCODE, tools: [{ name: 'bash' }], messages: [{ role: 'user', content: [{ type: 'text', text: '<system-reminder>Plan mode is on</system-reminder>' }] }] };
  await sample.getByLabel('Тело запроса (JSON)').fill(JSON.stringify(body));
  await sample.getByLabel('Заголовки').fill('User-Agent: opencode/1.4.2\nx-test-purpose: client-tools');
  await expect(sample.getByRole('option', { name: 'Определить по телу (anthropic)' })).toBeAttached();

  const result = page.getByRole('region', { name: 'Результат предпросмотра' });
  await expect(result.locator('.p-facts')).toContainText('mine-first');
  const facts = await result.locator('.p-facts div').evaluateAll((items) => Object.fromEntries(items.map((item) => [item.querySelector('dt').textContent, item.querySelector('dd').textContent])));
  expect(facts).toEqual({ Профиль: 'mine-first', Правило: 'opencode', Ревизия: '3', Назначение: 'client-tools', Клиент: 'opencode', Модель: 'claude-sonnet-5-5', 'Размер head': expect.stringMatching(/^\d+ байт · ≈ \d+ токенов$/) });
  // no draft: the saved revision answers, and the relay is sent what was pasted
  const request = prompts.previews.at(-1).body;
  expect(request).toEqual({ request: body, dialect: 'anthropic', headers: { 'User-Agent': 'opencode/1.4.2', 'x-test-purpose': 'client-tools' } });

  // the head is tinted by source and every tint is named in the legend and in the list of parts
  const text = result.locator('.head-text');
  await expect(text.locator('[data-source="text:my_rules"]')).toHaveText('Отвечай по-русски. Не трогай файлы без явной просьбы.');
  await expect(text.locator('[data-source="literal"]')).toHaveText('\n\n');
  await expect(text.locator('[data-source="client_edited"]')).toContainText('You are opencode');
  await expect(text.locator('[data-source="client_edited"]')).not.toContainText('# Code comments');
  await expect(result.locator('.src-legend li')).toHaveText(['именованный текст53', 'текст шаблона2', /^system клиента\d+$/]);
  await result.locator('.head-parts summary').click();
  await expect(result.locator('.head-parts li')).toHaveCount(3);
  await expect(result.locator('.head-parts li').nth(0)).toContainText('текст «my_rules»');
  await expect(result.locator('.head-parts li').nth(1)).toContainText('текст шаблона');
  await expect(result.locator('.head-parts li').nth(2)).toContainText('system клиента после правок');

  const edited = result.getByRole('region', { name: 'Как сработали правки system клиента' });
  await expect(edited.locator('li')).toHaveCount(2);
  await expect(edited.locator('li').nth(0)).toContainText('Убрать раздел');
  await expect(edited.locator('li').nth(0)).toContainText('правка 1');
  await expect(edited.locator('li').nth(0)).toContainText('сработала');
  await expect(edited.locator('li').nth(0)).toContainText('цель: # Code comments');
  await expect(edited.locator('li').nth(1)).toContainText('Убрать блок тега');
  await expect(edited.locator('li').nth(1)).toContainText('сработала');
  const history = result.getByRole('region', { name: 'Изменения истории' });
  await expect(history).toContainText('Убрать блок тега');
  await expect(history).toContainText('история · правка 1');
  await expect(history).toContainText('цель: system-reminder');
  await expect(history).toContainText('позиции в теле: 0');
  await expect(result.getByRole('region', { name: 'Изменения инструментов' })).toContainText('Добавить в конец описания');
  await expect(result.getByRole('region', { name: 'Изменения инструментов' })).toContainText('инструменты · правка 1');
  await expect(result.getByRole('region', { name: 'Изменения инструментов' })).toContainText('инструмент: bash');
  await expect(result.getByRole('region', { name: 'Не сработали' })).toHaveCount(0);
  await expect(result.getByRole('region', { name: 'Не применены' })).toHaveCount(0);
});

test('edits that found nothing are listed as not applied, and relay-only variables in a client request are warned about', async ({ page }) => {
  await openPrompts(page, {}, 'Предпросмотр');
  const sample = page.getByRole('region', { name: 'Образец' });
  await sample.getByRole('radio', { name: 'Своё тело запроса' }).check();
  await sample.getByLabel('Тело запроса (JSON)').fill(JSON.stringify({ model: 'claude-sonnet-5-5', system: 'Короткий system без разделов' }));
  await sample.getByLabel('Заголовки').fill('User-Agent: opencode/1.4.2');
  const result = page.getByRole('region', { name: 'Результат предпросмотра' });
  // the edits of the client's system are all listed, each with what it looked for; the misses are not repeated below
  const edited = result.getByRole('region', { name: 'Как сработали правки system клиента' });
  await expect(edited.locator('li')).toHaveCount(2);
  await expect(edited.locator('li').nth(0)).toContainText('Убрать раздел');
  await expect(edited.locator('li').nth(0)).toContainText('не нашла');
  await expect(edited.locator('li').nth(0)).toContainText('искали: # Code comments');
  await expect(edited.locator('li').nth(1)).toContainText('искали: available_skills');
  // the edits of the history and the tools are named by their place in the profile
  const unmatched = result.getByRole('region', { name: 'Не сработали' });
  await expect(unmatched.locator('li')).toHaveCount(2);
  await expect(unmatched.locator('li').nth(0)).toContainText('Убрать блок тега');
  await expect(unmatched.locator('li').nth(0)).toContainText('история · правка 1');
  await expect(unmatched.locator('li').nth(1)).toContainText('Добавить в конец описания');
  await expect(unmatched.locator('li').nth(1)).toContainText('инструменты · правка 1');
  await expect(unmatched).toContainText('Это не ошибка');
  await expect(result.getByRole('region', { name: 'Изменения истории' })).toHaveCount(0);

  await page.getByRole('tab', { name: /^Профили/ }).click();
  await head(page).fill('{{relay_task}}{{client_system}}');
  await page.getByRole('tab', { name: /^Предпросмотр/ }).click();
  await expect(page.getByRole('region', { name: 'Предупреждения' })).toContainText('{{relay_task}} пуст: запрос принадлежит клиенту');
});

test('a big body folds the entries of one edit into one line with a count, and an edit the relay skipped is listed apart with its reason', async ({ page }) => {
  const document = initialDocument();
  document.profiles['mine-first'].history.mid_system = 'drop';
  document.profiles['mine-first'].tools.edits = [
    { tool: 'bash', op: 'append', text: 'Осторожно.' },
    { tool: 'edit', op: 'replace_text', find: 'нет такого текста', with: 'x' },
    { tool: 'webfetch', hide: true },
    { tool: 'grep', op: 'append', text: 'не дойдёт' },
  ];
  await openPrompts(page, { document }, 'Предпросмотр');
  const sample = page.getByRole('region', { name: 'Образец' });
  await sample.getByRole('radio', { name: 'Своё тело запроса' }).check();
  const reminder = (n) => ({ role: 'user', content: [{ type: 'text', text: `<system-reminder>Plan mode is on ${n}</system-reminder>` }] });
  const body = {
    model: 'claude-sonnet-5-5', system: SYSTEM_OPENCODE,
    tools: [{ name: 'bash', description: 'Run a command' }, { name: 'edit', description: 'Edit a file' }, { name: 'webfetch', description: 'Fetch a page' }],
    messages: [...Array.from({ length: 12 }, (_, n) => reminder(n)), { role: 'system', content: 'mid one' }, { role: 'system', content: 'mid two' }],
  };
  await sample.getByLabel('Тело запроса (JSON)').fill(JSON.stringify(body));
  await sample.getByLabel('Заголовки').fill('User-Agent: opencode/1.4.2');
  const result = page.getByRole('region', { name: 'Результат предпросмотра' });

  // twelve reminders and two mid-dialog system messages are two lines, each with how often it happened
  const history = result.getByRole('region', { name: 'Изменения истории' });
  await expect(history.locator('h3 .chip-count')).toHaveText('14');
  await expect(history.locator('li')).toHaveCount(2);
  const blocks = history.locator('li').nth(0);
  await expect(blocks).toContainText('Убрать блок тега');
  await expect(blocks).toContainText('история · правка 1');
  await expect(blocks).toContainText('12 раз');
  await expect(blocks).toContainText('цель: system-reminder');
  await expect(blocks).toContainText('позиции в теле: 0, 1, 2, 3, 4, 5 и ещё 6');
  await expect(blocks).toContainText('убрано симв.: 626');
  const mid = history.locator('li').nth(1);
  await expect(mid).toContainText('Системные сообщения посреди диалога: удалить');
  await expect(mid).toContainText('2 раза');
  await expect(mid).toContainText('позиции в теле: 12, 13');
  await expect(mid).toContainText('удалено: 2');
  await expect(mid).not.toContainText('правка');

  const tools = result.getByRole('region', { name: 'Изменения инструментов' });
  await expect(tools.locator('li')).toHaveCount(2);
  await expect(tools.locator('li').nth(0)).toContainText('Добавить в конец описания');
  await expect(tools.locator('li').nth(0)).toContainText('инструмент: bash');
  await expect(tools.locator('li').nth(0)).toContainText('добавлено симв.: 10');
  await expect(tools.locator('li').nth(1)).toContainText('Скрыть инструмент');
  await expect(tools.locator('li').nth(1)).toContainText('инструменты · правка 3');
  await expect(tools.locator('li').nth(1)).toContainText('инструмент: webfetch');
  await expect(tools.locator('li').nth(1)).toContainText('удалено: 1');

  // an edit the relay skipped is not among the applied ones: it has a place of its own, and says why
  const skipped = result.getByRole('region', { name: 'Не применены' });
  await expect(skipped.locator('li')).toHaveCount(1);
  await expect(skipped.locator('li')).toContainText('Заменить текст в описании');
  await expect(skipped.locator('li')).toContainText('инструменты · правка 2');
  await expect(skipped.locator('li')).toContainText('инструмент: edit');
  await expect(skipped.locator('li')).toContainText('не применена: the text to replace is not in the description');
  await expect(tools).not.toContainText('Заменить текст в описании');
  // an edit that matched nothing at all is another thing again
  await expect(result.getByRole('region', { name: 'Не сработали' }).locator('li')).toHaveText([/Добавить в конец описания\s*инструменты · правка 4/]);
});

test('the client system has one tint, as sent or after the edits, and the legend counts it once while the parts keep their names', async ({ page }) => {
  await openPrompts(page);
  await head(page).fill('{{client_system}}\n--\n{{client_system_raw}}');
  await page.getByRole('tab', { name: /^Предпросмотр/ }).click();
  const result = page.getByRole('region', { name: 'Результат предпросмотра' });
  const text = result.locator('.head-text');
  await expect(text.locator('[data-source="client_edited"]')).toBeVisible();
  await expect(text.locator('[data-source="client"]')).toBeVisible();
  const tints = await text.locator('[data-source^="client"]').evaluateAll((items) => items.map((item) => getComputedStyle(item).backgroundColor));
  expect(tints).toHaveLength(2);
  expect(tints[0]).toBe(tints[1]);
  await expect(result.locator('.src-legend li')).toHaveText([/^system клиента[\d\s]+$/, 'текст шаблона4']);
  await result.locator('.head-parts summary').click();
  await expect(result.locator('.head-parts li')).toHaveText([/^system клиента после правок/, /^текст шаблона/, /^system клиента\s*\d/]);
});

test('a protected service request says so, and a request no rule takes falls back to the legacy path', async ({ page }) => {
  await openPrompts(page, {}, 'Предпросмотр');
  const sample = page.getByRole('region', { name: 'Образец' });
  await sample.getByRole('radio', { name: 'Своё тело запроса' }).check();
  await sample.getByLabel('Тело запроса (JSON)').fill(JSON.stringify({ model: 'claude-haiku-4-5', system: 'Generate a title' }));
  await sample.getByLabel('Заголовки').fill('User-Agent: opencode/1.4.2\nx-test-purpose: session-title');
  const result = page.getByRole('region', { name: 'Результат предпросмотра' });
  await expect(result).toContainText('Это служебный запрос под защитой');
  await expect(result.locator('.p-facts')).toContainText('protected: service requests');
  await expect(result.locator('.head-text')).toHaveText('Generate a title');
  await expect(result.locator('.head-text [data-source="client"]')).toBeVisible();

  await sample.getByLabel('Заголовки').fill('User-Agent: curl/8');
  await sample.getByLabel('Тело запроса (JSON)').fill(JSON.stringify({ model: 'gpt-5.5', messages: [{ role: 'user', content: 'hi' }] }));
  await expect(result).toContainText('Ни одно правило не сработало и профиля по умолчанию нет');
  await expect(result.locator('.p-facts')).toContainText('legacy: прежний путь');
  await expect(result.getByText('Head пуст')).toBeVisible();
});

test('a recent request is previewed by its id, and only requests whose body the relay still holds are offered', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Предпросмотр');
  const sample = page.getByRole('region', { name: 'Образец' });
  const select = sample.getByLabel('Запрос', { exact: true });
  await expect(select.locator('option')).toHaveCount(5);
  await expect(select.locator('option').nth(1)).toContainText('opencode · claude-sonnet-5-5 · client-tools');
  await expect(select.locator('option', { hasText: 'session-title' })).toHaveCount(0);
  await expect(select).toHaveValue('req-open-2');
  await select.selectOption('req-cc-1');
  const result = page.getByRole('region', { name: 'Результат предпросмотра' });
  await expect(result.locator('.p-facts')).toContainText('legacy');
  expect(prompts.previews.at(-1).body).toEqual({ request_id: 'req-cc-1' });
  await select.selectOption('req-open-1');
  await expect(result.locator('.p-facts')).toContainText('mine-first');
});

test('an unsaved draft is previewed until the saved revision is asked for, and the head is cut by code points', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Предпросмотр');
  await expect(page.getByRole('group', { name: 'Документ' })).toHaveCount(0);
  await page.getByRole('tab', { name: /^Профили/ }).click();
  await head(page).fill('Эмодзи 😀 в начале\n\n{{client_system}}');
  await page.getByRole('tab', { name: /^Предпросмотр/ }).click();
  const choice = page.getByRole('group', { name: 'Документ' });
  await expect(choice.getByRole('button', { name: 'Черновик' })).toHaveAttribute('aria-pressed', 'true');
  const result = page.getByRole('region', { name: 'Результат предпросмотра' });
  await expect(result.locator('.p-facts')).toContainText('черновик');
  expect(prompts.previews.at(-1).body.document.profiles['mine-first'].head).toBe('Эмодзи 😀 в начале\n\n{{client_system}}');
  // parts are offsets in code points; an emoji must not shift the cut by one
  const text = result.locator('.head-text');
  await expect(text.locator('[data-source="literal"]')).toHaveText('Эмодзи 😀 в начале\n\n');
  await expect(text.locator('[data-source="client_edited"]')).toContainText('You are opencode');
  await expect(text.locator('[data-source="client_edited"]').first()).toHaveText(/^You are opencode/);
  await expect(text.locator('[data-source="other"]')).toHaveCount(0);

  const before = prompts.previews.length;
  await choice.getByRole('button', { name: 'Сохранённая ревизия' }).click();
  await expect(result.locator('.p-facts')).toContainText('3');
  await expect(result.locator('.p-facts')).not.toContainText('черновик');
  await expect.poll(() => prompts.previews.length).toBeGreaterThan(before);
  expect('document' in prompts.previews.at(-1).body).toBe(false);
  await expect(result.locator('.head-text [data-source="text:my_rules"]')).toBeVisible();
});

// --- what the model saw ----------------------------------------------------------------------------------------------

test('the recent requests are listed with the rule, the profile and the revision, and the first one is open with its full head', async ({ page }) => {
  await openPrompts(page, {}, 'Что увидела модель');
  const list = page.getByRole('region', { name: 'Последние запросы' });
  await expect(list.locator('.seen-item')).toHaveCount(5);
  await expect(list.locator('.seen-item').nth(0)).toContainText('claude-sonnet-5-5');
  await expect(list.locator('.seen-item').nth(0)).toContainText('opencode → mine-first');
  await expect(list.locator('.seen-item').nth(0)).toContainText('ревизия 3');
  await expect(list.locator('.seen-item').nth(0)).toContainText('закреплён');
  await expect(list.locator('.seen-item').nth(3)).toContainText('legacy');
  await expect(list.locator('.seen-item').nth(4)).toContainText('protected: service requests → passthrough');
  await expect(list.locator('.seen-item').nth(0)).toHaveAttribute('aria-current', 'true');

  const detail = page.getByRole('region', { name: 'Выбранный запрос' });
  await expect(detail).toContainText('req-open-2');
  await expect(detail.locator('.p-facts')).toContainText('mine-first');
  const text = detail.locator('.head-text');
  await expect(text).toContainText('/home/dev/other');
  await expect(text.locator('[data-source="text:my_rules"]')).toBeVisible();
  await expect(text.locator('[data-source="client_edited"]')).toBeVisible();
  await expect(detail.getByRole('region', { name: 'Изменения инструментов' })).toContainText('инструмент: bash');
  await expect(detail.getByRole('region', { name: 'Изменения истории' })).toContainText('история · правка 1');
  // what the relay keeps in memory for a request is the changes of the history and the tools, not the edits of the system
  await expect(detail.getByRole('region', { name: 'Как сработали правки system клиента' })).toHaveCount(0);
  await expect(detail.getByRole('region', { name: 'Не сработали' })).toHaveCount(0);
  await list.locator('.seen-item').nth(4).click();
  await expect(detail).toContainText('req-title-1');
  await expect(detail.locator('.head-text')).toHaveText('Generate a title');
  // the relay no longer holds the body of this one
  await expect(detail.getByRole('button', { name: 'Проверить в предпросмотре' })).toHaveCount(0);
});

test('two neighbouring requests are compared line by line, and the same head is said to be the same', async ({ page }) => {
  await openPrompts(page, {}, 'Что увидела модель');
  const detail = page.getByRole('region', { name: 'Выбранный запрос' });
  await detail.getByRole('button', { name: 'Сравнить с другим' }).click();
  await expect(detail.getByLabel('Сравнить с')).toHaveValue('prev');
  const diff = detail.getByRole('region', { name: 'Различия между двумя head' });
  await expect(diff.locator('.diff-del .diff-text')).toHaveText('Working directory: /home/dev/project');
  await expect(diff.locator('.diff-add .diff-text')).toHaveText('Working directory: /home/dev/other');
  await expect(detail.locator('.diff-stats')).toHaveText('+1 −1');
  await expect(diff.locator('.p-sr')).toContainText(['убрано: ', 'добавлено: ']);
  await expect(diff.getByRole('button', { name: /Показать неизменённые строки/ })).toHaveCount(1);

  // against a request of another client the whole head differs
  await detail.getByLabel('Сравнить с').selectOption('req-title-1');
  await expect(diff.locator('.diff-del .diff-text')).toHaveText('Generate a title');
  // a request and its predecessor with one head
  await page.locator('.seen-item').nth(1).click();
  await expect(detail.getByLabel('Сравнить с')).toHaveValue('prev');
  await expect(detail).toContainText('Head не изменился между этими запросами');
  await detail.getByLabel('Сравнить с').selectOption('req-open-2');
  await expect(detail.locator('.diff-add .diff-text')).toHaveText('Working directory: /home/dev/project');
});

test('a recent request can be sent to the preview', async ({ page }) => {
  await openPrompts(page, {}, 'Что увидела модель');
  await page.locator('.seen-item').nth(1).click();
  await page.getByRole('button', { name: 'Проверить в предпросмотре' }).click();
  await expect(tab(page, 'Предпросмотр')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByLabel('Запрос', { exact: true })).toHaveValue('req-open-1');
  await expect(page.getByRole('region', { name: 'Результат предпросмотра' }).locator('.p-facts')).toContainText('mine-first');
});

test('with no requests the list says what will appear there', async ({ page }) => {
  await openPrompts(page, { recent: [] }, 'Что увидела модель');
  await expect(page.getByText('Запросов пока нет')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Выбранный запрос' })).toHaveCount(0);
});

// --- pins --------------------------------------------------------------------------------------------------------------

test('pinned conversations show their revision against the current one and can be unpinned one by one', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Пины');
  const rows = page.locator('.pin-row');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('c-7f2a');
  await expect(rows.nth(0)).toContainText('client-tools');
  await expect(rows.nth(0)).toContainText('mine-first');
  await expect(rows.nth(0)).toContainText('1 ч назад');
  await expect(rows.nth(0).locator('.tag')).toHaveText('2');
  await expect(rows.nth(0).locator('.tag')).toHaveClass(/tone-warn/);
  await expect(rows.nth(0).locator('.tag')).toHaveAttribute('title', /не видит новых правок/);
  await expect(rows.nth(1).locator('.tag')).toHaveClass(/tone-pos/);
  await rows.nth(0).getByRole('button', { name: 'Открепить диалог c-7f2a' }).click();
  await expect(page.getByText('Диалог откреплён')).toBeVisible();
  await expect(rows).toHaveCount(1);
  expect(prompts.pinActions).toHaveLength(1);
  expect(prompts.pinActions[0].body).toEqual({ action: 'unpin', conversation: 'c-7f2a' });
  expect(prompts.pinActions[0].headers['x-admin-request']).toBe('1');
});

test('applying the revision to every conversation asks first and says it costs a cache write', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Пины');
  await page.getByRole('button', { name: 'Применить ко всем' }).click();
  const confirm = page.getByRole('group').filter({ hasText: 'Применить текущую ревизию ко всем диалогам?' });
  await expect(confirm).toContainText('у 2 диалогов');
  await expect(confirm).toContainText('ревизию 3');
  await expect(confirm).toContainText('сбрасывает кэш промпта и подписи thinking');
  await expect(confirm).toContainText('10–44 кредитов');
  await expect(confirm.getByRole('heading')).toBeFocused();
  expect(prompts.pinActions).toHaveLength(0);
  await confirm.getByRole('button', { name: 'Отмена' }).click();
  await expect(confirm).toHaveCount(0);
  expect(prompts.pinActions).toHaveLength(0);

  await page.getByRole('button', { name: 'Применить ко всем' }).click();
  await page.getByRole('button', { name: 'Да, применить ко всем' }).click();
  await expect(page.getByText('Ревизия применена ко всем')).toBeVisible();
  await expect(page.getByText('Снято закреплений: 2')).toBeVisible();
  expect(prompts.pinActions).toHaveLength(1);
  expect(prompts.pinActions[0].body).toEqual({ action: 'apply_all' });
  await expect(page.getByText('Закреплённых диалогов нет')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Применить ко всем' })).toBeDisabled();
});

// --- saving ------------------------------------------------------------------------------------------------------------

test('saving sends the whole document with the revision it was read at and the CSRF header, then shows the new revision', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  await head(page).fill('Новый head {{client_system}}');
  await expect(savebar(page)).toContainText('1 несохранённых: профиль mine-first');
  await saveButton(page).click();
  await expect(page.getByText('Промпты сохранены')).toBeVisible();
  await expect(page.getByText('Ревизия 4. Новые диалоги получат её сразу, начатые остаются на своей')).toBeVisible();
  expect(prompts.saves).toHaveLength(1);
  expect(prompts.saves[0].body.base_rev).toBe(3);
  expect(prompts.saves[0].headers['x-admin-request']).toBe('1');
  expect(prompts.saves[0].headers.authorization).toBe(`Bearer ${API_KEY}`);
  expect(prompts.saves[0].body.document.rules).toEqual(initialDocument().rules);
  expect(prompts.saves[0].body.document.codex).toBeUndefined();
  await expect(savebar(page)).toHaveCount(0);
  await expect(page.locator('.page-head .tag')).toHaveText('ревизия 4');
  await expect(head(page)).toHaveValue('Новый head {{client_system}}');
  // the next save is judged against the new revision
  await head(page).fill('Ещё раз {{client_system}}');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(2);
  expect(prompts.saves[1].body.base_rev).toBe(4);
});

test('an open rule and an open section stay open after a save, so the next edit can follow at once', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Правила');
  await userRule(page, 2).getByRole('button', { name: /opencode/ }).first().click();
  await userRule(page, 2).getByLabel('Название').fill('все opencode');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  await expect(savebar(page)).toHaveCount(0);
  await expect(userRule(page, 2).getByLabel('Название')).toHaveValue('все opencode');
  await tab(page, 'Профили').click();
  const history = section(page, 'История диалога');
  await expect(history.locator('.p-edit')).toHaveCount(1);
  await head(page).fill('x {{client_system}}');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(2);
  await expect(history.locator('.p-edit')).toHaveCount(1);
  await expect(section(page, 'Правки system клиента').locator('.p-edit')).toHaveCount(2);
});

test('cancelling drops every unsaved edit', async ({ page }) => {
  await openPrompts(page);
  await head(page).fill('Не сохранять');
  await page.getByRole('button', { name: 'Удалить правку 1' }).first().click();
  await expect(savebar(page)).toContainText('1 несохранённых');
  await savebar(page).getByRole('button', { name: 'Отменить' }).click();
  await expect(savebar(page)).toHaveCount(0);
  await expect(head(page)).toHaveValue('{{text:my_rules}}\n\n{{client_system}}');
  await expect(section(page, 'Правки system клиента').locator('.p-edit')).toHaveCount(2);
});

test('the save bar names what changed across the sections', async ({ page }) => {
  await openPrompts(page);
  await head(page).fill('x {{client_system}}');
  await tab(page, 'Правила').click();
  await page.getByRole('button', { name: 'Выше: правило opencode' }).click();
  await tab(page, 'Тексты').click();
  await page.getByRole('region', { name: 'Тексты relay' }).getByRole('article', { name: 'Роль relay' }).getByRole('button', { name: 'Изменить' }).click();
  await expect(savebar(page)).toContainText('3 несохранённых');
  await expect(savebar(page).locator('.savebar-keys')).toHaveText('текст relay: role, профиль mine-first, правила');
});

test('a document that changed meanwhile is not overwritten: the message says to reload, the draft stays and the copy keeps it', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  const { prompts } = await openPrompts(page);
  await head(page).fill('мой черновик {{client_system}}');
  // somebody else saved while this tab was open
  prompts.rev = 7;
  prompts.doc = { ...initialDocument(), default_profile: 'exact-opus' };
  await saveButton(page).click();
  const banner = page.locator('.p-conflict');
  await expect(banner).toContainText('Документ изменился, перезагрузите.');
  await expect(banner).toContainText('сейчас ревизия 7');
  await expect(banner).toContainText('Ваши правки не сохранены');
  await expect(head(page)).toHaveValue('мой черновик {{client_system}}');
  await expect(savebar(page)).toBeVisible();
  expect(prompts.saves).toHaveLength(1);
  expect(prompts.doc.profiles['mine-first'].head).toBe('{{text:my_rules}}\n\n{{client_system}}');
  await expect(page.locator('.p-save-error')).toHaveCount(0);

  await banner.getByRole('button', { name: 'Скопировать мой черновик' }).click();
  const copied = JSON.parse(await page.evaluate(() => navigator.clipboard.readText()));
  expect(copied.profiles['mine-first'].head).toBe('мой черновик {{client_system}}');

  await banner.getByRole('button', { name: 'Загрузить актуальный документ' }).click();
  await expect(page.locator('.p-conflict')).toHaveCount(0);
  await expect(savebar(page)).toHaveCount(0);
  await expect(page.locator('.page-head .tag')).toHaveText('ревизия 7');
  await expect(head(page)).toHaveValue('{{text:my_rules}}\n\n{{client_system}}');
  await tab(page, 'Правила').click();
  await expect(page.getByLabel('Если ни одно правило не подошло')).toHaveValue('exact-opus');
});

test('a document the relay refuses is not saved: the message names the path, the field is marked and gets the focus', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  await head(page).fill('{{typo}}');
  await saveButton(page).click();
  const banner = page.locator('.p-save-error');
  await expect(banner).toContainText('Не сохранено.');
  await expect(banner).toContainText('Relay не принял документ: profiles.mine-first.head: unknown variable {{typo}}');
  await expect(banner.locator('code')).toHaveText('profiles.mine-first.head');
  await expect(head(page)).toHaveAttribute('aria-invalid', 'true');
  await expect(head(page)).toBeFocused();
  await expect(page.locator('.p-field', { has: head(page) }).getByRole('alert')).toContainText('unknown variable {{typo}}');
  await expect(savebar(page)).toBeVisible();
  expect(prompts.doc.profiles['mine-first'].head).toBe('{{text:my_rules}}\n\n{{client_system}}');
  // the next edit takes the mark away, and a valid document goes through
  await head(page).fill('{{client_system}}');
  await expect(page.locator('.p-save-error')).toHaveCount(0);
  await expect(head(page)).not.toHaveAttribute('aria-invalid', 'true');
  await saveButton(page).click();
  await expect(page.getByText('Промпты сохранены')).toBeVisible();
});

test('an error in a list item is found by its path however the index is written, and an empty field is marked in place', async ({ page }) => {
  await openPrompts(page);
  const first = section(page, 'Правки system клиента').locator('.p-edit').first();
  await first.getByLabel('Заголовок').fill('');
  await expect(first.getByLabel('Заголовок')).toHaveAttribute('aria-invalid', 'true');
  await saveButton(page).click();
  await expect(page.locator('.p-save-error code')).toHaveText('profiles.mine-first.client_system.edits[0].heading');
  await expect(first.getByLabel('Заголовок')).toBeFocused();
  await expect(first).toContainText('profiles.mine-first.client_system.edits[0].heading: required');
});

test('an error in another profile opens that profile, and an error in a rule opens that rule', async ({ page }) => {
  const { prompts } = await openPrompts(page, {}, 'Тексты');
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('region', { name: 'Именованные тексты' }).getByRole('button', { name: 'Удалить текст opus_role' }).click();
  await saveButton(page).click();
  // the file mock refuses a profile that names the text that is gone
  await expect(tab(page, 'Профили')).toHaveAttribute('aria-selected', 'true');
  await expect(profileBar(page).getByRole('button', { name: 'exact-opus' })).toHaveAttribute('aria-current', 'true');
  await expect(head(page)).toHaveValue('{{text:opus_role}}');
  await expect(head(page)).toHaveAttribute('aria-invalid', 'true');
  await expect(head(page)).toBeFocused();

  await savebar(page).getByRole('button', { name: 'Отменить' }).click();
  prompts.saveFails = { status: 400, json: { error: { message: 'rules[1].use: unknown profile "ghost"', param: 'rules[1].use' } } };
  await tab(page, 'Правила').click();
  await page.getByRole('switch', { name: 'Правило по заголовку включено' }).click();
  await saveButton(page).click();
  await expect(tab(page, 'Правила')).toHaveAttribute('aria-selected', 'true');
  await expect(userRule(page, 1).getByLabel('Применить профиль')).toBeFocused();
  await expect(userRule(page, 1).getByLabel('Применить профиль')).toHaveAttribute('aria-invalid', 'true');
  await expect(userRule(page, 1)).toContainText('unknown profile "ghost"');
});

test('any other failure of a save is shown with what the relay said, and the draft stays', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  prompts.saveFails = { status: 500, json: { error: { message: 'disk is full' } } };
  await head(page).fill('x {{client_system}}');
  await saveButton(page).click();
  await expect(page.locator('.p-save-error')).toContainText('Не сохранено. Ошибка: disk is full');
  await expect(savebar(page)).toBeVisible();
  await expect(head(page)).toHaveValue('x {{client_system}}');
});

for (const [status, lead, message] of [
  [413, 'Документ слишком большой: relay принимает не больше 2 МиБ', 'document is larger than 2097152 bytes'],
  [503, 'Relay не смог записать файл на диск', 'prompts.json: [Errno 28] No space left on device'],
]) {
  test(`a save the relay answers with ${status} says why in words, and the draft stays`, async ({ page }) => {
    const { prompts } = await openPrompts(page);
    prompts.saveFails = { status, json: { error: { message } } };
    await head(page).fill('x {{client_system}}');
    await saveButton(page).click();
    const banner = page.locator('.p-save-error');
    await expect(banner).toContainText(`Не сохранено. ${lead}: ${message}`);
    await expect(banner.locator('code')).toHaveCount(0);
    await expect(savebar(page)).toBeVisible();
    await expect(head(page)).toHaveValue('x {{client_system}}');
    // nothing was marked in the document: this is not about a field
    await expect(head(page)).not.toHaveAttribute('aria-invalid', 'true');
  });
}

test('the relay\'s warnings about the document are shown for as long as they apply, and a save brings the new ones', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const warnings = page.locator('.p-doc-warnings');
  await expect(warnings).toHaveCount(0);
  await tab(page, 'Правила').click();
  await page.getByRole('switch', { name: 'Защита служебных запросов' }).click();
  await userRule(page, 2).getByRole('button', { name: /opencode/ }).first().click();
  await userRule(page, 2).getByLabel('Применять и к защищённым запросам (override_protected)').check();
  // an edit alone changes nothing here: the relay speaks about the document it holds
  await expect(warnings).toHaveCount(0);
  await saveButton(page).click();
  await expect(warnings).toContainText('Предупреждения relay о документе');
  await expect(warnings.locator('li')).toHaveCount(2);
  await expect(warnings).toContainText('protected.enabled: false');
  await expect(warnings).toContainText('rules[2] «opencode»: override_protected');
  // the screen reloaded after the save: the list is the relay's own, and it is still there after another look at the file
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(warnings.locator('li')).toHaveCount(2);

  await page.getByRole('switch', { name: 'Защита служебных запросов' }).click();
  await userRule(page, 2).getByLabel('Применять и к защищённым запросам (override_protected)').uncheck();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(2);
  await expect(warnings).toHaveCount(0);
});

test('a relay that lists no warnings with the document still has the warnings of its last save shown', async ({ page }) => {
  const { prompts } = await openPrompts(page, { listWarnings: false });
  await tab(page, 'Правила').click();
  await page.getByRole('switch', { name: 'Защита служебных запросов' }).click();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  await expect(page.locator('.p-doc-warnings')).toContainText('protected.enabled: false');
  // a document loaded afresh is not the one that was saved: the old answer goes
  await page.getByRole('button', { name: 'Перезагрузить' }).click();
  await expect(page.locator('.p-doc-warnings')).toHaveCount(0);
});

test('a changed document is picked up on its own while the draft is clean, and flagged when there are unsaved edits', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  const poll = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  prompts.rev = 5;
  prompts.doc = { ...initialDocument(), profiles: { ...initialDocument().profiles, 'exact-opus': { head: 'Новый exact' } } };
  await poll();
  await expect(page.locator('.page-head .tag')).toHaveText('ревизия 5');
  await expect(savebar(page)).toHaveCount(0);
  await profileBar(page).getByRole('button', { name: 'exact-opus' }).click();
  await expect(head(page)).toHaveValue('Новый exact');

  await head(page).fill('мой exact');
  prompts.rev = 6;
  await poll();
  await expect(page.locator('.p-conflict')).toContainText('сейчас ревизия 6');
  await expect(head(page)).toHaveValue('мой exact');
  expect(prompts.saves).toHaveLength(0);
});

test('the file errors of the relay appear and go away with the next poll', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  await expect(page.locator('.p-file-errors')).toHaveCount(0);
  prompts.errors = [{ message: 'prompts.json: invalid JSON at line 3', param: '', ts: 1 }];
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.locator('.p-file-errors')).toContainText('invalid JSON at line 3');
  prompts.errors = [];
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.locator('.p-file-errors')).toHaveCount(0);
});

test('leaving the section, signing out and reloading ask first while edits are unsaved, and the tab warns before it closes', async ({ page }) => {
  await openPrompts(page);
  expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(false);
  await head(page).fill('несохранённое {{client_system}}');
  const asked = [];
  const decline = (dialog) => { asked.push(dialog.message()); dialog.dismiss(); };
  page.on('dialog', decline);
  await rail(page, 'Аккаунты').click();
  await expect.poll(() => asked.length).toBe(1);
  expect(asked[0]).toContain('несохранённые настройки (1)');
  await expect(page.getByRole('heading', { name: 'Промпты', exact: true })).toBeVisible();
  await page.getByRole('heading', { name: 'Промпты', exact: true }).click();
  await page.keyboard.press('2');
  await expect.poll(() => asked.length).toBe(2);
  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect.poll(() => asked.length).toBe(3);
  await page.getByRole('button', { name: 'Перезагрузить' }).click();
  await expect.poll(() => asked.length).toBe(4);
  expect(asked[3]).toBe('Загрузить документ заново? Несохранённые правки будут потеряны');
  await expect(head(page)).toHaveValue('несохранённое {{client_system}}');
  expect(await page.evaluate(() => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; })).toBe(true);

  // agreeing to reload drops the draft; nothing is asked once the edits are gone
  page.off('dialog', decline);
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Перезагрузить' }).click();
  await expect(head(page)).toHaveValue('{{text:my_rules}}\n\n{{client_system}}');
  await expect(savebar(page)).toHaveCount(0);
  const unexpected = [];
  page.on('dialog', (dialog) => { unexpected.push(dialog.message()); dialog.dismiss(); });
  await rail(page, 'Аккаунты').click();
  await expect(page.getByRole('heading', { name: 'Аккаунты' })).toBeVisible();
  expect(unexpected).toEqual([]);
});

// --- reading the relay -------------------------------------------------------------------------------------------------

test('lists the relay describes by name, in other shapes or only in part are still understood', async ({ page }) => {
  const builtin = {
    relay_texts: { role: 'ROLE' },
    protected_rules: [{ name: 'only title', if: { purpose: 'session-title' }, use: 'passthrough' }],
    variables: ['client_system', 'role'],
    edit_ops: { client_system: ['remove_tag'], tools: ['hide'], history: { mid_system: ['keep', 'drop'], user_blocks: ['drop_tag'] } },
    conditions: ['client', 'model'],
    profiles: ['passthrough'],
  };
  await openPrompts(page, { builtin });
  const palette = page.getByRole('group', { name: 'Вставить переменную в курсор' });
  await expect(palette.getByRole('button')).toHaveText(['{{client_system}}', '{{role}}', '{{text:my_rules}}', '{{text:opus_role}}']);
  await expect(section(page, 'Правки system клиента').getByLabel('Новая правка').locator('option')).toHaveText(['Убрать блок тега']);
  await expect(section(page, 'История диалога').getByLabel('Системные сообщения посреди диалога').locator('option')).toHaveText(['Оставить как есть', 'Удалить']);
  await expect(section(page, 'Инструменты').getByLabel('Новая правка').locator('option')).toHaveText(['Скрыть инструмент']);
  await tab(page, 'Правила').click();
  await expect(page.getByRole('list', { name: 'Встроенные защитные правила' })).toContainText('only title');
  await expect(page.getByRole('list', { name: 'Встроенные защитные правила' })).toContainText('Назначение: session-title → passthrough');
  await tab(page, 'Тексты').click();
  await expect(page.getByRole('article', { name: 'Роль relay' }).getByLabel('Встроенный текст: Роль relay')).toHaveValue('ROLE');
});

test('the name rule, the size limits, the special targets and the relay-only variables the relay publishes are the ones the screen applies', async ({ page }) => {
  const builtin = { ...BUILTIN, profile_name: '[a-z][a-z0-9-]{0,15}', limits: { max_text_bytes: 120, max_document_bytes: 4096 },
    special_uses: ['$header', '$legacy'], relay_task_variables: ['exec_reminder'] };
  await openPrompts(page, { builtin });
  // the limit of one text is the relay's, not the screen's
  await head(page).fill('ж'.repeat(61));
  await expect(page.getByText('61 симв. · 122 из 120 байт')).toBeVisible();
  await expect(page.getByText('Текст больше 120 байт: relay его не примет')).toBeVisible();
  await head(page).fill('{{relay_task}} {{client_system}}');
  await expect(page.getByText('только для запросов, которыми владеет relay')).toHaveCount(0);
  await head(page).fill('{{exec_reminder}} {{client_system}}');
  await expect(page.getByText('{{exec_reminder}}: только для запросов, которыми владеет relay')).toBeVisible();

  // a profile name is judged by the relay's own pattern
  await page.getByRole('button', { name: 'Новый профиль' }).click();
  await page.getByLabel('Имя профиля').fill('Mine2');
  await expect(page.getByRole('button', { name: 'Создать профиль' })).toBeDisabled();
  await page.getByLabel('Имя профиля').fill('a'.repeat(17));
  await expect(page.getByRole('button', { name: 'Создать профиль' })).toBeDisabled();
  await page.getByLabel('Имя профиля').fill('mine-two');
  await expect(page.getByRole('button', { name: 'Создать профиль' })).toBeEnabled();
  await page.getByRole('button', { name: 'Отмена' }).click();

  await tab(page, 'Правила').click();
  await userRule(page, 2).getByRole('button', { name: /opencode/ }).first().click();
  await expect(userRule(page, 2).getByLabel('Применить профиль').locator('option')).toHaveText(
    ['passthrough', 'mine-first', 'exact-opus', '$header — Профиль из заголовка', '$legacy — Прежний путь']);
  await tab(page, 'Тексты').click();
  const structured = page.getByRole('article', { name: 'Обёртка структурированного ответа' });
  await structured.getByText('Пример контракта, как он выглядит в head').click();
  await expect(structured).toContainText('Reply with one JSON object that matches the schema "answer".');
});

test('a relay that names no limits, special targets or name rule leaves the documented ones', async ({ page }) => {
  const older = Object.fromEntries(Object.entries(BUILTIN).filter(([key]) => !['limits', 'special_uses', 'profile_name', 'relay_task_variables', 'structured_example'].includes(key)));
  await openPrompts(page, { builtin: older });
  await head(page).fill('ж'.repeat(10));
  await expect(page.getByText('10 симв. · 20 из 262 144 байт')).toBeVisible();
  await tab(page, 'Правила').click();
  await userRule(page, 2).getByRole('button', { name: /opencode/ }).first().click();
  await expect(userRule(page, 2).getByLabel('Применить профиль').locator('option')).toHaveText(
    ['passthrough', 'mine-first', 'exact-opus', '$header — Профиль из заголовка', '$virtual — Профиль из имени модели', '$legacy — Прежний путь']);
  await tab(page, 'Тексты').click();
  await expect(page.getByText('Пример контракта')).toHaveCount(0);
});

test('the role settings of the legacy path are applied at once: saving them asks for no restart', async ({ page }) => {
  await mockAdmin(page);
  await mockPrompts(page);
  await login(page);
  await rail(page, 'Настройки').click();
  await expect(page.locator('#system-instructions-live')).toContainText('применяются сразу, в том числе к начатым диалогам: кэш промпта активных диалогов будет записан заново');
  await page.getByLabel('Стандартная роль relay').uncheck();
  await page.getByLabel('Инструкции для модели', { exact: true }).fill('Новая роль');
  await page.getByRole('radio', { name: /Дополнить клиента/ }).check();
  await expect(page.getByText('ждёт перезапуска')).toHaveCount(0);
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  await expect(page.getByText('Настройки сохранены')).toBeVisible();
  await expect(page.getByText(/Применено сразу: .*Инструкции для модели/)).toBeVisible();
  await expect(page.getByText('Изменения вступят в силу после перезапуска relay')).toHaveCount(0);
  await expect(page.getByText(/ждут перезапуска/)).toHaveCount(0);
  await expect(page.getByText('ждёт перезапуска')).toHaveCount(0);
});

test('an operation the screen does not know is kept as it is and said so', async ({ page }) => {
  const document = initialDocument();
  document.profiles['mine-first'].client_system.edits.push({ op: 'future_op', flag: true });
  const { prompts } = await openPrompts(page, { document });
  const unknown = section(page, 'Правки system клиента').locator('.p-edit').nth(2);
  await expect(unknown).toContainText('Операция «future_op» неизвестна экрану и сохранится как есть');
  await page.getByRole('button', { name: 'Поднять правку 3' }).click();
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  expect(prompts.saves[0].body.document.profiles['mine-first'].client_system.edits.map((edit) => edit.op)).toEqual(['remove_section', 'future_op', 'remove_tag']);
  expect(prompts.saves[0].body.document.profiles['mine-first'].client_system.edits[1]).toEqual({ op: 'future_op', flag: true });
});

test('fields and sections the screen does not edit travel with the document untouched', async ({ page }) => {
  const document = { ...initialDocument(), codex: { models: { 'gpt-6*': { persistent_instructions: 'x' } } }, future_key: { a: 1 } };
  document.profiles['mine-first'].future_section = { keep: 'me' };
  const { prompts } = await openPrompts(page, { document });
  await head(page).fill('Изменён {{client_system}}');
  await saveButton(page).click();
  await expect.poll(() => prompts.saves.length).toBe(1);
  const saved = prompts.saves[0].body.document;
  expect(saved.codex).toEqual(document.codex);
  expect(saved.future_key).toEqual({ a: 1 });
  expect(saved.profiles['mine-first'].future_section).toEqual({ keep: 'me' });
  expect(saved.profiles['mine-first'].head).toBe('Изменён {{client_system}}');
});

// --- focus -------------------------------------------------------------------------------------------------------------

test('a removed rule hands the focus to the rule that took its place, and a new rule takes it for its name', async ({ page }) => {
  await openPrompts(page, {}, 'Правила');
  await page.getByRole('button', { name: 'Добавить правило' }).first().click();
  await expect(userRule(page, 3).getByLabel('Название')).toBeFocused();
  const remove = (name) => page.getByRole('button', { name: `Удалить правило ${name}`, exact: true });
  await remove('по заголовку').click();
  await expect(remove('opencode')).toBeFocused();
  // the last one hands it back to the one before
  await remove('Новое правило').click();
  await expect(remove('opencode')).toBeFocused();
  await remove('opencode').click();
  await expect(remove('opus свой')).toBeFocused();
  await remove('opus свой').click();
  await expect(page.locator('.p-rules-panel').getByRole('button', { name: 'Добавить правило' })).toBeFocused();
});

test('a removed condition or value leaves the focus in the list: on the next one, or on the field that adds', async ({ page }) => {
  await openPrompts(page, {}, 'Правила');
  await userRule(page, 2).getByRole('button', { name: /opencode/ }).first().click();
  const editor = userRule(page, 2).locator('.rule-editor');
  const client = editor.getByRole('group', { name: 'Клиент' });
  const field = client.getByLabel('Клиент: добавить значение');
  // the last value of a condition leaves its row open for the next one
  await client.getByRole('button', { name: 'Убрать значение opencode' }).click();
  await expect(field).toBeFocused();
  await expect(client.locator('.p-token')).toHaveCount(0);
  await field.fill('codex');
  await field.press('Enter');
  await field.fill('claude-*');
  await field.press('Enter');
  await expect(client.locator('.p-token')).toHaveText(['codex', 'claude-*']);
  await field.press('Backspace');
  await expect(client.locator('.p-token')).toHaveText(['codex']);
  await expect(field).toBeFocused();

  await editor.getByLabel('Добавить условие').selectOption('tools');
  await editor.getByLabel('Добавить условие').selectOption('model');
  await editor.getByRole('button', { name: 'Убрать условие «Есть инструменты»' }).click();
  await expect(editor.getByRole('button', { name: 'Убрать условие «Модель»' })).toBeFocused();
  await editor.getByRole('button', { name: 'Убрать условие «Модель»' }).click();
  await expect(editor.getByRole('button', { name: 'Убрать условие «Клиент»' })).toBeFocused();
  await editor.getByRole('button', { name: 'Убрать условие «Клиент»' }).click();
  await expect(editor.getByLabel('Добавить условие')).toBeFocused();
});

test('removed texts and masks hand the focus on, and a rename opens on its field and closes onto its button', async ({ page }) => {
  page.on('dialog', (dialog) => dialog.accept());
  await openPrompts(page, {}, 'Тексты');
  const named = page.getByRole('region', { name: 'Именованные тексты' });
  const mine = named.getByRole('article', { name: 'Текст my_rules' });
  await mine.getByRole('button', { name: 'Переименовать' }).click();
  await expect(mine.getByLabel('Новое имя')).toBeFocused();
  await mine.getByRole('button', { name: 'Отмена' }).click();
  await expect(mine.getByRole('button', { name: 'Переименовать' })).toBeFocused();
  await mine.getByRole('button', { name: 'Переименовать' }).click();
  await mine.getByLabel('Новое имя').fill('rules_ru');
  await mine.getByLabel('Новое имя').press('Enter');
  // the text is a new element under its new name: the focus is on its button all the same
  await expect(named.getByRole('article', { name: 'Текст rules_ru' }).getByRole('button', { name: 'Переименовать' })).toBeFocused();

  const remove = (name) => named.getByRole('button', { name: `Удалить текст ${name}`, exact: true });
  await remove('rules_ru').click();
  await expect(remove('opus_role')).toBeFocused();
  await named.getByLabel('Новый текст: имя').fill('footer');
  await named.getByRole('button', { name: 'Добавить текст' }).click();
  await remove('footer').click();
  await expect(remove('opus_role')).toBeFocused();
  await remove('opus_role').click();
  await expect(named.getByLabel('Новый текст: имя')).toBeFocused();

  const codex = page.getByRole('article', { name: 'Базовые инструкции Codex' });
  await codex.getByRole('button', { name: 'Изменить' }).click();
  await codex.getByRole('button', { name: 'Разделить по маскам моделей' }).click();
  await codex.getByRole('button', { name: 'Добавить маску' }).click();
  await expect(codex.getByLabel('Маска модели').nth(1)).toBeFocused();
  await codex.getByLabel('Маска модели').nth(1).fill('gpt-6*');
  await codex.getByRole('button', { name: 'Убрать маску *', exact: true }).click();
  await expect(codex.getByRole('button', { name: 'Убрать маску gpt-6*', exact: true })).toBeFocused();
});

test('the profile forms hand the focus on: a new profile opens on its name, and every way out ends on the chip in view', async ({ page }) => {
  await openPrompts(page);
  const newButton = page.getByRole('button', { name: 'Новый профиль' });
  await newButton.click();
  await expect(page.getByLabel('Имя профиля')).toBeFocused();
  await page.getByRole('button', { name: 'Отмена' }).click();
  await expect(newButton).toBeFocused();
  await newButton.click();
  await page.getByLabel('Имя профиля').fill('scratch');
  await page.getByLabel('Имя профиля').press('Enter');
  await expect(profileBar(page).getByRole('button', { name: 'scratch' })).toBeFocused();
  await page.getByRole('button', { name: 'Дублировать' }).click();
  await expect(profileBar(page).getByRole('button', { name: 'scratch-copy' })).toBeFocused();

  await page.getByRole('button', { name: 'Переименовать' }).click();
  await expect(page.getByLabel('Новое имя')).toBeFocused();
  await page.getByRole('button', { name: 'Отмена' }).click();
  await expect(page.getByRole('button', { name: 'Переименовать' })).toBeFocused();
  await page.getByRole('button', { name: 'Переименовать' }).click();
  await page.getByLabel('Новое имя').fill('copy2');
  await page.getByLabel('Новое имя').press('Enter');
  await expect(profileBar(page).getByRole('button', { name: 'copy2' })).toBeFocused();

  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Удалить', exact: true }).click();
  await expect(profileBar(page).getByRole('button', { name: 'copy2' })).toHaveCount(0);
  await expect(profileBar(page).locator('[aria-current="true"]')).toBeFocused();
});

test('the first profile form opens on its name, cancelling returns to its button, and creating ends on the new chip', async ({ page }) => {
  await openPrompts(page, { legacy: true });
  const start = page.getByRole('button', { name: 'Создать первый профиль' });
  await start.click();
  await expect(page.getByLabel('Имя профиля')).toBeFocused();
  await page.getByRole('button', { name: 'Отмена' }).click();
  await expect(start).toBeFocused();
  await start.click();
  await page.getByLabel('Имя профиля').press('Enter');
  await expect(profileBar(page).locator('[aria-current="true"]')).toBeFocused();
  await expect(profileBar(page).locator('[aria-current="true"]')).toHaveText('mine');
});

test('after an unpin, an apply to all or a cancelled question the focus is on a button that stays', async ({ page }) => {
  await openPrompts(page, {}, 'Пины');
  const refresh = page.locator('.p-pins').getByRole('button', { name: 'Обновить' });
  await page.locator('.pin-row').nth(0).getByRole('button', { name: 'Открепить диалог c-7f2a' }).click();
  await expect(page.locator('.pin-row')).toHaveCount(1);
  await expect(refresh).toBeFocused();
  await page.getByRole('button', { name: 'Применить ко всем' }).click();
  await page.getByRole('button', { name: 'Отмена' }).click();
  await expect(page.getByRole('button', { name: 'Применить ко всем' })).toBeFocused();
  await page.getByRole('button', { name: 'Применить ко всем' }).click();
  await page.getByRole('button', { name: 'Да, применить ко всем' }).click();
  await expect(page.getByText('Закреплённых диалогов нет')).toBeVisible();
  await expect(refresh).toBeFocused();
});

test('the save bar leaves the focus on the tab in view when it goes, unless the user has gone on typing', async ({ page }) => {
  const { prompts } = await openPrompts(page);
  await head(page).fill('x {{client_system}}');
  await page.getByRole('button', { name: 'Отменить' }).focus();
  await page.keyboard.press('Enter');
  await expect(savebar(page)).toHaveCount(0);
  await expect(tab(page, 'Профили')).toBeFocused();

  await head(page).fill('y {{client_system}}');
  await saveButton(page).focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => prompts.saves.length).toBe(1);
  await expect(savebar(page)).toHaveCount(0);
  await expect(tab(page, 'Профили')).toBeFocused();

  await page.route('**/admin/api/prompts', async (route) => {
    if (route.request().method() === 'PUT') await new Promise((resolve) => setTimeout(resolve, 400));
    await route.fallback();
  });
  await head(page).fill('z {{client_system}}');
  await saveButton(page).click();
  await head(page).focus();
  await page.keyboard.type(' ещё');
  await expect.poll(() => prompts.saves.length).toBe(2);
  await expect(page.getByText('Промпты сохранены').first()).toBeVisible();
  await expect(head(page)).toBeFocused();
  await expect(head(page)).toHaveValue('z {{client_system}} ещё');
});

test('"Проверить в предпросмотре" lands on the preview tab', async ({ page }) => {
  await openPrompts(page, {}, 'Что увидела модель');
  await page.getByRole('button', { name: 'Проверить в предпросмотре' }).click();
  await expect(tab(page, 'Предпросмотр')).toHaveAttribute('aria-selected', 'true');
  await expect(tab(page, 'Предпросмотр')).toBeFocused();
});

// --- the screen itself -----------------------------------------------------------------------------------------------

test('the sections follow the WAI-ARIA tabs pattern: arrows, Home and End move the choice, and each tab controls a real panel', async ({ page }) => {
  await openPrompts(page);
  const tabs = page.getByRole('tablist', { name: 'Разделы промптов' }).getByRole('tab');
  await expect(tabs).toHaveCount(6);
  await tab(page, 'Профили').focus();
  await page.keyboard.press('ArrowRight');
  await expect(tab(page, 'Правила')).toHaveAttribute('aria-selected', 'true');
  await expect(tab(page, 'Правила')).toBeFocused();
  await expect(page).toHaveURL(/#prompts\/rules$/);
  await page.keyboard.press('End');
  await expect(tab(page, 'Пины')).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(tab(page, 'Профили')).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(tab(page, 'Пины')).toBeFocused();
  await page.keyboard.press('Home');
  await expect(tab(page, 'Профили')).toBeFocused();
  const wiring = await page.evaluate(() => [...document.querySelectorAll('[role="tab"]')].map((item) => {
    const panel = document.getElementById(item.getAttribute('aria-controls'));
    return [item.getAttribute('aria-selected'), item.tabIndex, panel?.getAttribute('role'), panel?.getAttribute('aria-labelledby') === item.id, panel?.hidden];
  }));
  expect(wiring).toEqual([['true', 0, 'tabpanel', true, false], ...Array.from({ length: 5 }, () => ['false', -1, 'tabpanel', true, true])]);
});

test('the tab is kept in the address, so a reload opens the same section', async ({ page }) => {
  await openPrompts(page, {}, 'Правила');
  await expect(page).toHaveURL(/#prompts\/rules$/);
  await page.reload();
  await page.getByLabel('Relay key').fill(API_KEY);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(tab(page, 'Правила')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('region', { name: 'Ваши правила' })).toBeVisible();
});

test('the session ending while the screen is open signs out, as in the other sections', async ({ page }) => {
  await mockAdmin(page);
  await mockPrompts(page);
  await login(page);
  await page.route('**/admin/api/prompts**', (route) => route.fulfill({ status: 401, json: { error: { message: 'invalid relay API key' } } }));
  await rail(page, 'Промпты').click();
  await expect(page.getByLabel('Relay key')).toBeVisible();
});

// --- design ----------------------------------------------------------------------------------------------------------

const TABS = ['Профили', 'Правила', 'Тексты', 'Предпросмотр', 'Что увидела модель', 'Пины'];

// Opens what is folded or hidden on each tab, so the scans see the densest state of the screen.
async function openEverything(page, name) {
  if (name === 'Профили') {
    const closed = page.locator('.p-disclosure:not([open]) > summary');
    while (await closed.count()) await closed.first().click();
  } else if (name === 'Правила') {
    await userRule(page, 0).getByRole('button', { name: /opus свой/ }).first().click();
    await userRule(page, 0).getByLabel('Применять и к защищённым запросам (override_protected)').check();
    await page.getByRole('switch', { name: 'Защита служебных запросов' }).click();
  } else if (name === 'Тексты') {
    const change = page.getByRole('button', { name: 'Изменить', exact: true });
    while (await change.count()) await change.first().click();
  } else if (name === 'Что увидела модель') {
    await page.getByRole('button', { name: 'Сравнить с другим' }).click();
  } else if (name === 'Пины') {
    await page.getByRole('button', { name: 'Применить ко всем' }).click();
  }
}

test('no glow, gradient, uppercase, endless animation or heavy weight on any tab of the prompts screen', async ({ page }) => {
  const { prompts } = await openPrompts(page, { errors: [{ message: 'profiles.mine-first.head: unknown variable {{typo}}', param: 'profiles.mine-first.head', ts: 1 }] });
  const scan = () => page.evaluate(() => {
    const bad = [];
    for (const el of document.querySelectorAll('.prompts *')) {
      const s = getComputedStyle(el);
      if (s.backgroundImage.includes('gradient')) bad.push(`gradient ${el.className}`);
      if (s.backdropFilter && s.backdropFilter !== 'none') bad.push(`blur ${el.className}`);
      if (s.filter && s.filter !== 'none') bad.push(`filter ${el.className}`);
      if (s.textShadow && s.textShadow !== 'none') bad.push(`text-shadow ${el.className}`);
      if (Number(s.fontWeight) >= 700) bad.push(`weight ${el.className}`);
      if (s.textTransform === 'uppercase') bad.push(`upper ${el.className}`);
      if (s.animationIterationCount === 'infinite' && s.animationName !== 'none') bad.push(`animation ${el.className}`);
    }
    return bad;
  });
  const found = {};
  for (const name of TABS) {
    await tab(page, name).click();
    await openEverything(page, name);
    found[name] = await scan();
  }
  expect(found).toEqual(Object.fromEntries(TABS.map((name) => [name, []])));
  // every banner of the screen as well: the file errors are up already; a refused save, the warnings of the document and a conflict
  await tab(page, 'Профили').click();
  await head(page).fill('{{typo}}');
  await saveButton(page).click();
  await expect(page.locator('.p-save-error')).toBeVisible();
  expect(await scan()).toEqual([]);
  await head(page).fill('{{client_system}}');
  await saveButton(page).click();
  await expect(page.locator('.p-doc-warnings')).toBeVisible();
  expect(await scan()).toEqual([]);
  prompts.rev = 9;
  await head(page).fill('{{client_system}} ещё');
  await saveButton(page).click();
  await expect(page.locator('.p-conflict')).toBeVisible();
  expect(await scan()).toEqual([]);
});

for (const width of [280, 320, 414, 1440]) {
  test(`the prompts screen has no horizontal overflow at ${width}px on any tab, even with long names and unbroken text`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const long = 'a'.repeat(64);
    const document = initialDocument();
    document.profiles[long] = { head: `${'x'.repeat(300)} {{client_system}}`, description: 'y'.repeat(200) };
    document.rules.push({ name: 'z'.repeat(120), if: { model: 'm'.repeat(100) }, use: long });
    const { prompts } = await openPrompts(page, { document });
    prompts.pins.push({ conversation: 'c'.repeat(60), purpose: 'client-tools', rev: 3, profile: long, age_s: 10 });
    const overflow = () => page.evaluate(() => {
      const visibleRight = (el) => {
        let right = el.getBoundingClientRect().right;
        for (let parent = el.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
          if (getComputedStyle(parent).overflowX !== 'visible') right = Math.min(right, parent.getBoundingClientRect().right);
        }
        return right;
      };
      const wide = [...document.querySelectorAll('.prompts *')].filter((el) => el.getBoundingClientRect().width > 0 && visibleRight(el) > innerWidth + 1).map((el) => `${el.tagName}.${el.className}`);
      return { page: document.documentElement.scrollWidth - innerWidth, wide: wide.slice(0, 5) };
    });
    for (const name of TABS) {
      // clicked in the page itself: a sticky bar over the tabs at this width is not what this test is about
      await tab(page, name).evaluate((element) => element.click());
      await page.waitForTimeout(150);
      expect({ name, ...(await overflow()) }).toEqual({ name, page: expect.any(Number), wide: [] });
      expect((await overflow()).page).toBeLessThanOrEqual(1);
      await openEverything(page, name);
      expect({ name, ...(await overflow()) }).toEqual({ name, page: expect.any(Number), wide: [] });
      expect((await overflow()).page).toBeLessThanOrEqual(1);
    }
    await tab(page, 'Профили').click();
    await profileBar(page).getByRole('button', { name: long }).click();
    await expect(live(page).locator('.diff-add .diff-text').first()).toBeVisible();
    expect((await overflow()).wide).toEqual([]);
    expect((await overflow()).page).toBeLessThanOrEqual(1);
    await live(page).getByRole('button', { name: 'Итоговый head' }).click();
    expect((await overflow()).wide).toEqual([]);
    expect((await overflow()).page).toBeLessThanOrEqual(1);
  });
}

test('every control of the prompts screen has a name', async ({ page }) => {
  await openPrompts(page);
  for (const name of TABS) {
    await tab(page, name).click();
    await openEverything(page, name);
    const nameless = await page.evaluate(() => [...document.querySelectorAll('.prompts button, .prompts input, .prompts select, .prompts textarea, .prompts [role="switch"]')]
      .filter((el) => !el.closest('[hidden]'))
      .filter((el) => {
        const label = el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.textContent.trim() || (el.labels && el.labels.length) || el.getAttribute('title');
        return !label;
      }).map((el) => `${el.tagName} ${el.className}`));
    expect({ name, nameless }).toEqual({ name, nameless: [] });
  }
});

// The design gate looks at every tab; this looks at the states it does not reach: refused saves, banners, forms, switched-off rules.
for (const theme of ['dark', 'light']) {
  test(`axe finds nothing in the states with errors, warnings and forms (${theme})`, async ({ page }) => {
    // a dozen axe scans in one test: on a CI runner they take longer than the default 30 s
    test.slow();
    await page.addInitScript((value) => localStorage.setItem('relay.admin.theme', value), theme);
    await page.setViewportSize({ width: 1280, height: 1000 });
    const { prompts } = await openPrompts(page, { errors: [{ message: 'bad file', param: 'profiles.x', ts: 1 }] });
    const violations = [];
    const scan = async (label) => {
      await page.waitForTimeout(300);
      const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
      for (const found of result.violations) violations.push({ label, id: found.id, targets: found.nodes.map((node) => node.target.join(' ')) });
    };
    await scan('the banner of the file errors');
    await head(page).fill('{{typo}} {{text:gone}}');
    await scan('references the relay would refuse');
    await saveButton(page).click();
    await expect(page.locator('.p-save-error')).toBeVisible();
    await scan('a refused save with the field marked');
    await head(page).fill('{{client_system}}');
    await page.getByRole('button', { name: 'Переименовать' }).click();
    await page.getByLabel('Новое имя').fill('bad name');
    await scan('a rename form with an error');
    await page.getByRole('button', { name: 'Отмена' }).click();
    await page.getByRole('button', { name: 'Новый профиль' }).click();
    await scan('the form of a new profile');
    await page.getByRole('button', { name: 'Отмена' }).click();
    await tab(page, 'Правила').click();
    await page.getByRole('switch', { name: 'Защита служебных запросов' }).click();
    await page.getByRole('switch', { name: 'Правило по заголовку включено' }).click();
    await scan('protection off and a rule switched off');
    const rule = userRule(page, 2);
    await rule.getByRole('button', { name: /opencode/ }).first().click();
    await rule.getByLabel('Применять и к защищённым запросам (override_protected)').check();
    await rule.getByLabel('Добавить условие').selectOption('tools');
    await rule.getByLabel('Добавить условие').selectOption('model');
    await scan('a rule with the override warning and conditions');
    prompts.rev = 9;
    await saveButton(page).click();
    await expect(page.locator('.p-conflict')).toBeVisible();
    await scan('the conflict banner');
    await tab(page, 'Тексты').click();
    await page.getByRole('button', { name: 'Переименовать' }).first().click();
    await page.getByLabel('Новое имя').fill('has space');
    await scan('the rename form of a text');
    await tab(page, 'Предпросмотр').click();
    await page.getByRole('radio', { name: 'Своё тело запроса' }).check();
    await page.getByLabel('Тело запроса (JSON)').fill('{oops');
    await scan('a pasted body that is not JSON');
    await page.getByLabel('Тело запроса (JSON)').fill(JSON.stringify({ model: 'm', system: 'Короткий', messages: [] }));
    await page.getByLabel('Заголовки').fill('User-Agent: opencode/1');
    await scan('a preview of a pasted request');
    await tab(page, 'Что увидела модель').click();
    await page.locator('.seen-item').nth(4).click();
    await scan('a protected request among the recent ones');
    expect(violations).toEqual([]);
  });
}
