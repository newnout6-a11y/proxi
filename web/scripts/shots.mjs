// Full-page screenshots of every section, both themes, desktop and mobile.
// Usage: node scripts/shots.mjs <before|after|dir> [--only=name,name] [--motion]
// Serves web/dist through `vite preview` on 127.0.0.1:4173; all API traffic is mocked (tests/fixtures.js).
import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '@playwright/test';
import { API_KEY, mockAdmin } from '../tests/fixtures.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHOTS = process.env.ADMIN_SHOTS_DIR || path.join(os.tmpdir(), 'opencode', 'admin-shots');
const args = process.argv.slice(2);
const name = args.find((a) => !a.startsWith('--')) || 'after';
const outDir = path.isAbsolute(name) ? name : path.join(SHOTS, name);
const only = (args.find((a) => a.startsWith('--only=')) || '').slice(7).split(',').filter(Boolean);
const motion = args.includes('--motion');
const BASE = 'http://127.0.0.1:4173/admin/';

const SECTIONS = [
  { id: 'login', login: false },
  { id: 'overview', key: '1' },
  { id: 'activity-turns', key: '2' },
  { id: 'activity-events', key: '2', tab: 'Все события' },
  { id: 'accounts', key: '3' },
  { id: 'settings', key: '4' },
  { id: 'activity-events-empty', key: '2', tab: 'Все события', empty: true },
  { id: 'activity-turns-empty', key: '2', empty: true },
];
const VIEWPORTS = { desktop: { width: 1440, height: 900 }, mobile: { width: 390, height: 844 } };

async function waitForServer() {
  for (let i = 0; i < 60; i += 1) {
    try { if ((await fetch(BASE)).ok) return; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('vite preview did not start');
}

const server = spawn(process.execPath, ['node_modules/vite/bin/vite.js', 'preview', '--host', '127.0.0.1', '--port', '4173', '--strictPort'],
  { cwd: root, stdio: 'ignore' });
const stop = () => { try { server.kill(); } catch { /* gone */ } };
process.on('exit', stop);

try {
  await waitForServer();
  await mkdir(outDir, { recursive: true });
  const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {});
  for (const theme of ['dark', 'light']) {
    for (const [vpName, viewport] of Object.entries(VIEWPORTS)) {
      for (const section of SECTIONS) {
        if (only.length && !only.includes(section.id)) continue;
        const context = await browser.newContext({
          viewport, colorScheme: theme, reducedMotion: motion ? 'no-preference' : 'reduce', deviceScaleFactor: 1,
        });
        await context.addInitScript((t) => { try { localStorage.setItem('relay.admin.theme', t); } catch { /* ignore */ } }, theme);
        const page = await context.newPage();
        await mockAdmin(page, { rich: true, empty: section.empty });
        await page.goto(BASE);
        if (section.login !== false) {
          await page.getByLabel('Relay key').fill(API_KEY);
          await page.getByRole('button', { name: 'Войти' }).click();
          await page.waitForSelector('nav');
          await page.waitForTimeout(400);
          await page.evaluate(() => { location.hash = ''; });
          await page.keyboard.press(section.key);
          if (section.tab) await page.getByRole('tab', { name: new RegExp(section.tab) }).click();
        }
        await page.waitForTimeout(section.id === 'overview' ? 3200 : 1800);
        const file = path.join(outDir, `${section.id}-${theme}-${vpName}.png`);
        await page.screenshot({ path: file, fullPage: true });
        console.log(file);
        await context.close();
      }
    }
  }
  await browser.close();
} finally {
  stop();
}
