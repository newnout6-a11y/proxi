import { defineConfig, devices } from '@playwright/test';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.spec.js',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 2,
  // On the CI runner the tests that scan the whole page (axe, the computed style of every element) take several
  // times as long as on a workstation, past 30 s on the prompts screen
  timeout: process.env.CI ? 60_000 : 30_000,
  reporter: 'list',
  outputDir: path.join(os.tmpdir(), 'relay-admin-playwright'),
  use: {
    baseURL: 'http://127.0.0.1:4173/admin/',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  // PW_CHROMIUM lets an already installed Chromium build be reused instead of downloading one.
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'],
    ...(process.env.PW_CHROMIUM ? { launchOptions: { executablePath: process.env.PW_CHROMIUM } } : {}) } }],
  webServer: {
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 4173 --strictPort',
    cwd: path.dirname(fileURLToPath(import.meta.url)),
    url: 'http://127.0.0.1:4173/admin/',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
