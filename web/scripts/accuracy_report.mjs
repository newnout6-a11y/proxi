// Rendered design gate. Synthetic APIs only; reports and screenshots stay outside the repository.
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { API_KEY, mockAdmin } from '../tests/fixtures.js';
import { SYSTEM_OPENCODE, initialDocument, mockPrompts } from '../tests/prompts-fixtures.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const base = 'http://127.0.0.1:4173/admin/';
const output = process.env.ADMIN_QA_DIR || path.join(os.tmpdir(), 'proxi-design-qa');
const rows = [];
const WIDTHS = [280, 320, 414, 1440];
// The prompts screen is judged tab by tab, plain and with everything on it opened: folded editors, an open rule, the diff, the confirmation.
const PROMPT_TABS = { profiles: 'Профили', rules: 'Правила', texts: 'Тексты', preview: 'Предпросмотр', seen: 'Что увидела', pins: 'Пины' };
// The preview is judged on a request that leaves every kind of line under it: edits of the client's system that matched and one that
// did not, twelve reminders folded into one line, mid-dialog system messages, tools edited, hidden and skipped, and an edit with no tool
const RICH_DOCUMENT = initialDocument();
RICH_DOCUMENT.profiles['mine-first'].history.mid_system = 'drop';
RICH_DOCUMENT.profiles['mine-first'].tools.edits = [
  { tool: 'bash', op: 'append', text: 'Осторожно.' },
  { tool: 'edit', op: 'replace_text', find: 'нет такого текста', with: 'x' },
  { tool: 'webfetch', hide: true },
  { tool: 'grep', op: 'append', text: 'не дойдёт' },
];
const RICH_BODY = {
  model: 'claude-sonnet-5-5',
  system: SYSTEM_OPENCODE.slice(0, SYSTEM_OPENCODE.indexOf('<available_skills>')),
  tools: [{ name: 'bash', description: 'Run a command' }, { name: 'edit', description: 'Edit a file' }, { name: 'webfetch', description: 'Fetch a page' }],
  messages: [
    ...Array.from({ length: 12 }, (_, n) => ({ role: 'user', content: [{ type: 'text', text: `<system-reminder>Plan mode is on ${n}</system-reminder>` }] })),
    { role: 'system', content: 'mid one' }, { role: 'system', content: 'mid two' },
  ],
};
async function openPromptsTab(page, tab, dense) {
  await page.getByRole('tab', { name: new RegExp(`^${PROMPT_TABS[tab]}`) }).click();
  if (!dense) return;
  if (tab === 'profiles') {
    const closed = page.locator('.p-disclosure:not([open]) > summary');
    while (await closed.count()) await closed.first().click();
  } else if (tab === 'rules') {
    await page.locator('.rule-list:not(.locked) .rule-main').first().click();
  } else if (tab === 'texts') {
    const change = page.getByRole('button', { name: 'Изменить', exact: true });
    while (await change.count()) await change.first().click();
  } else if (tab === 'preview') {
    await page.getByRole('radio', { name: 'Своё тело запроса' }).check();
    await page.getByLabel('Тело запроса (JSON)').fill(JSON.stringify(RICH_BODY));
    await page.getByLabel('Заголовки').fill('User-Agent: opencode/1.4.2');
    await page.getByRole('region', { name: 'Как сработали правки system клиента' }).waitFor();
    await page.getByRole('region', { name: 'Не применены' }).waitFor();
  } else if (tab === 'seen') {
    await page.getByRole('button', { name: 'Сравнить с другим' }).click();
  } else if (tab === 'pins') {
    await page.getByRole('button', { name: 'Применить ко всем' }).click();
  }
}
let server;
try {
  try { if (!(await fetch(base)).ok) throw new Error('not ready'); }
  catch {
    server = spawn(process.execPath, ['node_modules/vite/bin/vite.js', 'preview', '--host', '127.0.0.1', '--port', '4173', '--strictPort'], { cwd: root, stdio: 'ignore' });
    let ready = false;
    for (let i = 0; i < 60; i += 1) {
      try { ready = (await fetch(base)).ok; } catch { /* starting */ }
      if (ready) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!ready) throw new Error('Preview did not start');
  }
  await mkdir(output, { recursive: true });
  const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce', colorScheme: theme });
      await context.addInitScript((value) => localStorage.setItem('relay.admin.theme', value), theme);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (error) => errors.push(error.message));
      page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
      // Leaving a section with unsaved edits asks first (the settings editing state, the prompts open states): agree
      page.on('dialog', (dialog) => dialog.accept());
      await mockAdmin(page, { rich: true });
      await mockPrompts(page, { document: RICH_DOCUMENT });
      await page.goto(base);
      for (const view of ['login', 'overview', 'activity', 'accounts', 'settings', 'prompts']) {
        if (view === 'overview') {
          await page.getByLabel('Relay key').fill(API_KEY);
          await page.getByRole('button', { name: 'Войти', exact: true }).click();
          await page.getByRole('heading', { name: 'Сводка', exact: true }).waitFor();
        } else if (view !== 'login') {
          const names = { activity: 'Активность', accounts: 'Аккаунты', settings: 'Настройки', prompts: 'Промпты' };
          await page.getByRole('navigation', { name: 'Разделы' }).getByRole('button', { name: new RegExp(names[view]) }).click();
          await page.getByRole('heading', { name: names[view], exact: true }).waitFor();
        }
        if (view === 'prompts') {
          for (const tab of Object.keys(PROMPT_TABS)) {
            for (const dense of [false, true]) {
              await openPromptsTab(page, tab, dense);
              await page.waitForTimeout(450);
              const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
                .exclude('.ss-fit').analyze();
              const violations = result.violations.map((v) => ({ id: v.id, impact: v.impact,
                nodes: v.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })) }));
              rows.push({ theme, view, state: `${tab}${dense ? ' open' : ''}`, check: 'axe', passed: violations.length === 0, violations });
              if (violations.length) console.log(JSON.stringify(rows.at(-1)));
            }
            for (const width of WIDTHS) {
              await page.setViewportSize({ width, height: 1000 });
              const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
              rows.push({ theme, view, state: tab, width, check: 'overflow', passed: !overflow });
            }
            await page.waitForTimeout(250);
            await page.screenshot({ path: path.join(output, `${view}-${tab}-${theme}.png`), fullPage: true });
          }
          continue;
        }
        // Illustrations are tested as illustrations by Stageshow; axe checks the actual application controls.
        const states = view === 'settings' ? ['default', 'hover', 'focus', 'selected', 'editing'] : ['default'];
        for (const state of states) {
          if (state === 'hover') await page.locator('.prompt-mode').first().hover();
          if (state === 'focus') await page.getByRole('radio').first().focus();
          if (state === 'selected') await page.getByRole('radio', { name: /Дополнить клиента/ }).check();
          if (state === 'editing') {
            await page.getByLabel('Стандартная роль relay').uncheck();
            await page.getByLabel('Инструкции для модели', { exact: true }).fill('Работай как инженер проекта.\nПроверяй изменения тестами.');
          }
          // Measure settled states, including the save bar's 200 ms entrance,
          // rather than a transient opacity frame halfway through a fade.
          await page.waitForTimeout(450);
          const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
            .exclude('.ss-fit').analyze();
          const violations = result.violations.map((v) => ({ id: v.id, impact: v.impact,
            nodes: v.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })) }));
          rows.push({ theme, view, state, check: 'axe', passed: violations.length === 0, violations });
          if (violations.length) console.log(JSON.stringify(rows.at(-1)));
        }
        for (const width of WIDTHS) {
          await page.setViewportSize({ width, height: 1000 });
          const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
          rows.push({ theme, view, width, check: 'overflow', passed: !overflow });
        }
        await page.screenshot({ path: path.join(output, `${view}-${theme}.png`), fullPage: true });
        if (view === 'settings') {
          await page.getByRole('region', { name: 'Системные инструкции' }).screenshot({ path: path.join(output, `role-editor-${theme}.png`) });
        }
      }
      rows.push({ theme, check: 'runtime-errors', passed: errors.length === 0, errors });
      await context.close();
    }
  } finally { await browser.close(); }
  await writeFile(path.join(output, 'accuracy.json'), JSON.stringify(rows, null, 2));
  const passed = rows.filter((row) => row.passed).length;
  console.log(`${passed}/${rows.length} checks passed; evidence: ${output}`);
  if (passed !== rows.length) process.exitCode = 1;
} finally { if (server) server.kill(); }
