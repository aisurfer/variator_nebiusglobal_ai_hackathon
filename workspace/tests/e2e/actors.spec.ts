import { expect, test, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

// S6 «Actors and risks». Needs the workspace with the knowledge base (tenant company, with the actor card pages) and
// a VARIATOR API that serves an actor-cards artifact set:
// KB_E2E=1 PLAYWRIGHT_BASE_URL=http://127.0.0.1:<vite port> npx playwright test actors.spec.ts
// ACTORS_SCREENSHOTS=<dir> also saves the screenshots there.
test.skip(!process.env.KB_E2E, 'set KB_E2E=1 with the knowledge base and the VARIATOR API running');

async function shot(page: Page, name: string, isMobile: boolean) {
  const dir = process.env.ACTORS_SCREENSHOTS;
  if (!dir) return;
  mkdirSync(dir, { recursive: true });
  await page.mouse.move(0, 0);
  // desktop: the 1440×1000 viewport; phone: the whole page at 390 px
  await page.screenshot({ path: join(dir, `actors-${name}-${isMobile ? 'mobile' : 'desktop'}.png`), fullPage: isMobile });
}
async function expectViewport(page: Page) {
  const size = await page.evaluate(() => ({ client: document.documentElement.clientWidth, document: document.documentElement.scrollWidth }));
  expect(size.document, `content must stay within ${size.client}px`).toBeLessThanOrEqual(size.client + 1);
}
async function open(page: Page, query = '') {
  await page.goto(`actors${query}`); // relative to the base URL (<origin>/workspace/)
  await expect(page.getByRole('heading', { level: 1, name: 'Actors and risks' })).toBeVisible({ timeout: 60_000 });
}
const card = (page: Page) => page.locator('[data-tour="actor-card"]');

test('a person: list by department, filter, search, the card with its habits and a citation to the dossier', async ({ page, isMobile }) => {
  test.setTimeout(120_000);
  await open(page);
  const list = page.locator('[data-tour="actors-list"]');
  await expect(list.getByRole('heading', { name: /Engineering/ })).toBeVisible();
  await list.getByRole('radio', { name: 'People' }).click();
  await expect(list.getByRole('heading', { name: /Key clients/ })).toHaveCount(0);
  await list.getByRole('searchbox', { name: 'Search actors' }).fill('kowalski');
  await expect(list.locator('.ac-item')).toHaveCount(1);
  await list.locator('.ac-item').first().click();
  await expect(page).toHaveURL(/actor=LW-E06/);
  await expect(card(page).getByRole('heading', { level: 2 })).toHaveText('Daniel Kowalski');
  await expect(card(page).locator('.ac-sentence')).toHaveCount(await card(page).locator('.ac-sentence').count());
  expect(await card(page).locator('.ac-sentence').count()).toBeGreaterThanOrEqual(3);
  await expect(card(page).locator('[data-tour="actor-habits"] .ac-hrow[data-habit]').first()).toBeVisible();
  await expect(card(page).locator('[data-habit="throughput_points_per_sprint"] .ac-src')).toHaveText(/data/);
  await expect(card(page).locator('[data-habit="resignation_hazard"]')).toHaveCount(0); // no score of people
  await expectViewport(page);
  await shot(page, 'person', isMobile);

  const cite = card(page).locator('.ac-cite').first();
  const id = await cite.getAttribute('data-entity');
  await cite.click();
  await expect(page).toHaveURL(new RegExp(`entity=${encodeURIComponent(id!).replace(/\./g, '\\.')}`));
  await expect(page.getByRole('dialog').getByText('Version history')).toBeVisible({ timeout: 30_000 });
});

test('a key client: how it pays, what changes it, the dossier of the organisation', async ({ page, isMobile }) => {
  test.setTimeout(120_000);
  await open(page, '?actor=CL-001');
  await expect(card(page).getByRole('heading', { level: 2 })).toHaveText('Meridian Freight Group');
  await expect(card(page).locator('[data-tour="actor-profile"]')).toContainText('days after issue');
  if (!isMobile) await expect(card(page).locator('[data-habit="pay_delay_days"] .ac-spark')).toBeVisible(); // a phone hides the trends
  await expect(card(page).locator('[data-tour="actor-changes"] li')).not.toHaveCount(0);
  await expect(card(page).locator('[data-tour="actor-changes"]')).toContainText('A key account leaves');
  await expectViewport(page);
  await shot(page, 'client', isMobile);
  await card(page).locator('[data-tour="actor-dossier"]').click();
  await expect(page).toHaveURL(/entity=org\.CL-001/);
  await expect(page.getByRole('dialog').getByText('Version history')).toBeVisible({ timeout: 30_000 });
});

test('a cohort: the standard clients and a future hire are cohorts with habits, not people', async ({ page, isMobile }) => {
  test.setTimeout(120_000);
  await open(page);
  const list = page.locator('[data-tour="actors-list"]');
  await list.getByRole('radio', { name: 'Cohorts' }).click();
  await expect(list.getByRole('heading', { name: /Future hires/ })).toBeVisible();
  await list.locator('[data-actor="clients:standard"]').click();
  await expect(card(page).getByRole('heading', { level: 2 })).toHaveText('Standard clients');
  await expect(card(page).locator('[data-tour="actor-habits"] .ac-hrow[data-habit]')).not.toHaveCount(0);
  await expectViewport(page);
  await shot(page, 'cohort', isMobile);
  if (isMobile) await card(page).getByRole('button', { name: 'All actors' }).click();
  await list.locator('[data-actor="future:backend"]').click();
  await expect(card(page).getByRole('heading', { level: 2 })).toHaveText('Future backend engineer');
  await expect(card(page).locator('[data-tour="actor-dossier"]')).toHaveCount(0);
});

test('the register of the risks that already fired: frequency, last time, examples, shocks of the cases', async ({ page, isMobile }) => {
  test.setTimeout(120_000);
  await open(page);
  await page.locator('[data-tour="actors-tab-risks"]').click();
  await expect(page).toHaveURL(/tab=risks/);
  const reg = page.locator('[data-tour="risks-register"]');
  await expect(reg.locator('.ac-rrow[data-risk]')).toHaveCount(19);
  const churn = reg.locator('[data-risk="R5"]');
  await expect(churn).toContainText('Client gives notice');
  await expect(churn.locator('.ac-rshocks')).toContainText('A key account leaves');
  await churn.getByRole('button', { name: /Client gives notice/ }).click();
  await expect(churn.locator('.ac-rdetail')).toBeVisible();
  await expectViewport(page);
  await shot(page, 'risks', isMobile);
  const example = reg.locator('[data-risk="R1"] .ac-cite').first();
  await example.click();
  await expect(page).toHaveURL(/entity=sim\.INC-/);
  await expect(page.getByRole('dialog').getByText('Version history')).toBeVisible({ timeout: 30_000 });
});

test('a card links to its risks in the register, and the register opens on that risk', async ({ page }) => {
  test.setTimeout(90_000);
  await open(page, '?actor=VN-03');
  await card(page).locator('.ac-risk-chip', { hasText: 'R4' }).click();
  await expect(page).toHaveURL(/tab=risks&risk=R4|risk=R4.*tab=risks/);
  await expect(page.locator('[data-risk="R4"]')).toHaveClass(/is-selected/);
  await expect(page.locator('[data-risk="R4"] .ac-rdetail')).toBeVisible();
});

test('without an actor-card set the page says so', async ({ page }) => {
  await page.route('**/api/variator/artifacts', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sets: [] }) }));
  await page.goto('actors');
  await expect(page.getByRole('heading', { name: 'No actor cards yet' })).toBeVisible({ timeout: 60_000 });
});
