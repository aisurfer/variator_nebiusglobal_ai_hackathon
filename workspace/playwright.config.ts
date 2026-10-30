import { defineConfig, devices } from '@playwright/test';

// PLAYWRIGHT_BASE_URL: the origin of the site (http://127.0.0.1:8088, https://<domain>); the app lives under
// /workspace/ on it (vite.config.ts), so the base URL of the tests is <origin>/workspace/. A value that already ends
// with /workspace is taken as it is. Specs use relative paths ('brief', 'api/kb/...'): a leading '/' drops the base.
const origin = (process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:5173').replace(/\/+$/, '');
const baseURL = `${origin.endsWith('/workspace') ? origin : `${origin}/workspace`}/`;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 4,
  timeout: 30_000,
  expect: { timeout: 7_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { baseURL, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { ...devices['iPhone 13'], defaultBrowserType: 'chromium' } },
  ],
});
