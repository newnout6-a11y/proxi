// Rendered design gate. Synthetic APIs only; reports and screenshots stay outside the repository.
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { API_KEY, mockAdmin } from '../tests/fixtures.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const base = 'http://127.0.0.1:4173/admin/';
const output = process.env.ADMIN_QA_DIR || path.join(os.tmpdir(), 'proxi-design-qa');
const rows = [];
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
      await mockAdmin(page, { rich: true });
      await page.goto(base);
      for (const view of ['login', 'overview', 'activity', 'accounts', 'settings']) {
        if (view === 'overview') {
          await page.getByLabel('Relay key').fill(API_KEY);
          await page.getByRole('button', { name: 'Войти', exact: true }).click();
          await page.getByRole('heading', { name: 'Сводка', exact: true }).waitFor();
        } else if (view !== 'login') {
          const names = { activity: 'Активность', accounts: 'Аккаунты', settings: 'Настройки' };
          await page.getByRole('navigation', { name: 'Разделы' }).getByRole('button', { name: new RegExp(names[view]) }).click();
          await page.getByRole('heading', { name: names[view], exact: true }).waitFor();
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
        for (const width of [280, 320, 414, 1440]) {
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
