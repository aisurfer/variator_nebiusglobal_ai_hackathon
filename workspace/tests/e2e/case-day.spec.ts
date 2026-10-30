import { mkdirSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

// WP24: the Day section of case B on WP19's day file (variator.day/1). Needs the workspace and a VARIATOR API that
// serves the day set of case B (the MVP stack on :8088, or a Vite dev server proxied to it):
//   API_PORT=3002 VITE_PORT=<ui> npx vite --host 127.0.0.1
//   PLAYWRIGHT_BASE_URL=http://127.0.0.1:<ui> npx playwright test case-day.spec.ts
// Screenshots go to DAY_SHOTS when it is set. Without a day file every test is skipped.
// paths relative to the base URL (<origin>/workspace/, playwright.config.ts); hrefs of the app start with /workspace/
const CASE = 'cases/company_best_year';
const SHOTS = process.env.DAY_SHOTS;
test.setTimeout(120_000);

interface DayFile { dayExamples: Record<string, { kind: string; tenant?: string; days: { date: string }[]; events: { date: string; type: string; text: string; entityId?: string; person?: string }[] }[]>; dayMetrics: { id: string; label: string }[] }
const config = { forks: [['status_quo', 'Keep the plan'], ['pay_tech_debt', 'Reduce tech debt'], ['hire_5', 'Hire 5 engineers'], ['reserve_gpu', 'Reserve GPU capacity'], ['raise_prices_8', 'Raise prices 8%']] as const };
const label = (id: string) => config.forks.find(f => f[0] === id)![1];

async function dayFile(page: Page): Promise<DayFile | null> {
  const r = await page.request.get('api/variator/artifacts');
  if (!r.ok()) return null;
  const sets = ((await r.json()) as { sets: { id: string; manifest?: { schema?: string; case?: string } }[] }).sets;
  const set = sets.find(s => s.manifest?.schema === 'variator.day/1' && s.manifest.case === 'company_best_year');
  if (!set) return null;
  const f = await page.request.get(`api/variator/artifacts/${set.id}/day_examples.json`);
  return f.ok() ? await f.json() as DayFile : null;
}
async function open(page: Page) {
  await page.goto(CASE);
  await page.evaluate(() => { localStorage.clear(); for (const id of ['company_best_year', 'delivery_best_way']) localStorage.setItem(`variator:tour:${id}`, 'dismissed'); });
  await page.goto(CASE);
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
}
const at = (page: Page, id: string) => page.evaluate(i => document.getElementById(i)!.scrollIntoView({ block: 'start' }), id);
/** Keep forks for the week level and one for the day level at the review gates. */
async function keep(page: Page, week: string[], day: string) {
  await at(page, 'week-review');
  for (const f of week) {
    const box = page.getByTestId('gate-week').getByRole('checkbox', { name: new RegExp(f) });
    if (await box.getAttribute('aria-checked') !== 'true') await box.click();
  }
  await at(page, 'day-review');
  const box = page.getByTestId('gate-day').getByRole('checkbox', { name: new RegExp(day) });
  if (await box.getAttribute('aria-checked') !== 'true') await box.click();
  await at(page, 'day');
}
async function shot(page: Page, name: string, mobile: boolean, el = '#day', below = 0) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  const path = `${SHOTS}/${name}-${mobile ? '390' : 'desktop'}.png`;
  if (!below) { await page.locator(el).first().screenshot({ path }); return; }
  // an element and what opens under it (a menu)
  await page.locator(el).first().scrollIntoViewIfNeeded();
  const b = (await page.locator(el).first().boundingBox())!, vw = page.viewportSize()!.width;
  await page.screenshot({ path, clip: { x: 0, y: Math.max(0, b.y - 8), width: vw, height: b.height + below } });
}
/** Acknowledge every change notice on the page. */
async function acknowledge(page: Page) {
  const acks = page.locator('.cs-toast .cs-toast-ack');
  // a notice may close by itself meanwhile: a short wait per click, then none is left
  for (let i = 0; i < 6 && await acks.count(); i++) await acks.first().click({ timeout: 3_000 }).catch(() => {});
  await expect(page.locator('.cs-toast')).toHaveCount(0);
}
/** The forks of the day file, and one fork without example days (if any). */
async function forks(page: Page) {
  const f = await dayFile(page);
  const covered = Object.keys(f?.dayExamples ?? {}).filter(k => f!.dayExamples[k].length);
  return { file: f, covered, missing: config.forks.map(x => x[0]).find(x => !covered.includes(x)) ?? null };
}
const examples = (page: Page) => page.getByTestId('day-examples');
/** Catalogue metrics the page leaves out: the same quantity as a run series. */
const SAME_AS = ['fin.cash', 'rev.arr', 'ppl.headcount'];
const radio = (page: Page, kind: string) => examples(page).getByRole('radio', { name: new RegExp(kind) });

test('each example shows its own days, events and header', async ({ page, isMobile }) => {
  await open(page);
  const { covered } = await forks(page);
  test.skip(!covered.length, 'no day file of case B on this API');
  // the owner's case: Reserve GPU capacity, once WP19 serves its examples
  const fork = covered.includes('reserve_gpu') ? 'reserve_gpu' : covered[0];
  await keep(page, [label(fork)], label(fork));
  await expect(examples(page)).toHaveAttribute('data-source', 'day-file');
  const seen: { digest: string; path: string; cash: string; first: string }[] = [];
  for (const kind of ['Pessimistic', 'Median', 'Optimistic']) {
    await radio(page, kind).click();
    await expect(radio(page, kind)).toHaveAttribute('aria-checked', 'true');
    const charts = page.getByTestId('day-charts');
    await expect(charts).toHaveAttribute('data-example', kind.toLowerCase());
    await expect(charts.locator('.recharts-line-curve').first()).toBeVisible();
    seen.push({
      digest: (await charts.getAttribute('data-digest'))!,
      path: (await charts.locator('.recharts-line-curve').first().getAttribute('d'))!,
      cash: await radio(page, kind).locator('.cs-example-cash').innerText(),
      first: await page.getByTestId('day-events').locator('li').first().innerText(),
    });
  }
  // M7: the place among the three, and a percentile only when it is the true one (never «p10» or «percentile path»)
  const places = await examples(page).getByTestId('day-example-position').allInnerTexts();
  expect(places.map(p => p.split(' · ')[0])).toEqual(['Lowest of the three', 'Middle of the three', 'Highest of the three']);
  for (const p of places) expect(p).not.toMatch(/\bp\d+\b|percentile path/);
  expect(new Set(seen.map(s => s.digest)).size).toBe(3);
  expect(new Set(seen.map(s => s.path)).size).toBe(3);
  expect(new Set(seen.map(s => s.cash)).size).toBe(3);
  // people's words: no machine sizes, no «ACV»
  await expect(page.getByTestId('day-events')).not.toContainText(/size \d|ACV/);
  await shot(page, 'day-examples', isMobile);
});

test('a fork without example days says so plainly', async ({ page, isMobile }) => {
  // every fork of case B has example days now: the day file is served without Reserve GPU capacity here
  await page.route('**/day_examples.json', async r => {
    const res = await r.fetch(), doc = await res.json() as DayFile;
    delete doc.dayExamples.reserve_gpu;
    await r.fulfill({ response: res, json: doc });
  });
  await open(page);
  const { covered } = await forks(page);
  test.skip(!covered.length, 'no day file of case B on this API');
  const other = covered.find(f => f !== 'reserve_gpu')!;
  await keep(page, [label('reserve_gpu'), label(other)], label('reserve_gpu'));
  const none = examples(page);
  await expect(none).toHaveAttribute('data-source', 'none');
  await expect(none).toHaveAttribute('data-tour', 'day-examples');
  await expect(none).toContainText('No example days for this fork yet');
  await expect(none).toContainText(label(other));
  await expect(page.getByTestId('day-charts')).toHaveCount(0);
  await shot(page, 'day-none', isMobile);
});

test('a click on a day shows its card and highlights its events; keys move and clear it', async ({ page, isMobile }) => {
  await open(page);
  const { file, covered } = await forks(page);
  test.skip(!covered.length, 'no day file of case B on this API');
  const fork = covered[0];
  await keep(page, [label(fork)], label(fork));
  await radio(page, 'Median').click();
  const median = file!.dayExamples[fork].find(e => e.kind === 'median')!;
  const area = page.getByTestId('day-chart-area');
  // keyboard: Enter picks the first day with an event
  await area.focus();
  await page.keyboard.press('Enter');
  const card = page.getByTestId('day-card');
  await expect(card).toBeVisible();
  const first = median.events[0].date;
  await expect(card).toHaveAttribute('data-date', first);
  await expect(area).toHaveAttribute('data-selected', first);
  // the lane highlights exactly the events of that day, and shows the first of them
  const lane = page.getByTestId('day-events');
  const n = median.events.filter(e => e.date === first).length;
  await expect(lane.locator('li.is-day')).toHaveCount(n);
  for (const li of await lane.locator('li.is-day').all()) await expect(li).toHaveAttribute('data-date', first);
  await expect(card.locator('.cs-dayitem')).toHaveCount(n);
  // the day's metric values
  // the charted metrics' values that day; all of them behind «All metrics that day»
  const nMetrics = file!.dayMetrics.filter(m => !SAME_AS.includes(m.id)).length;
  await expect(card.locator('.cs-daycard-values > div')).toHaveCount(Math.min(4, nMetrics));
  if (nMetrics > 4) {
    await card.getByTestId('day-card-all').click();
    await expect(card.locator('.cs-daycard-values > div')).toHaveCount(nMetrics);
    await area.focus();
  }
  // arrows move the day; Escape clears it
  await page.keyboard.press('ArrowRight');
  const next = median.days[median.days.findIndex(d => d.date === first) + 1].date;
  await expect(card).toHaveAttribute('data-date', next);
  await page.keyboard.press('Escape');
  await expect(card).toHaveCount(0);
  // a click on the chart picks the day under it; the lane scrolls to the day's events
  const busy = median.days.find(d => median.events.filter(e => e.date === d.date).length >= 2 && d.date > median.days[30].date)?.date ?? first;
  const idx = median.days.findIndex(d => d.date === busy);
  // one band per day on the plot: the y axis (58 px) on the left, 12 px on the right
  // the charts in the middle of the screen (the progress bar sits over the top of the page); the notices of the gates
  // read first: they sit top right in a readable size, and on a phone that is over the charts until acknowledged
  await acknowledge(page);
  await area.evaluate(e => e.scrollIntoView({ block: 'center' }));
  const box = (await area.locator('.cs-day-chart').nth(1).locator('.recharts-wrapper').boundingBox())!;
  await page.mouse.click(box.x + 58 + (idx + 0.5) * (box.width - 70) / median.days.length, box.y + box.height / 2);
  // a day is about 2 px wide on a phone: one day either side there
  if (isMobile) await expect.poll(async () => { const got = await card.getAttribute('data-date'); return Math.abs(median.days.findIndex(d => d.date === got) - idx); }).toBeLessThanOrEqual(1);
  else await expect(card).toHaveAttribute('data-date', busy);
  await expect(page.locator('.cs-day-chart .recharts-reference-line')).toHaveCount(await page.locator('.cs-day-chart').count());
  if ((await lane.locator('li.is-day').count()) > 0) {
    await expect.poll(async () => {
      const [l, f] = await Promise.all([lane.boundingBox(), lane.locator('li.is-day').first().boundingBox()]);
      return !!l && !!f && f.y >= l.y - 1 && f.y < l.y + l.height;
    }).toBe(true);
  }
  // an event of the lane picks its day
  await lane.locator('li').nth(3).click();
  await expect(card).toHaveAttribute('data-date', (await lane.locator('li').nth(3).getAttribute('data-date'))!);
  await shot(page, 'day-card', isMobile);
});

test('the metric picker changes the charts and stays for this viewer', async ({ page, isMobile }) => {
  await open(page);
  const { file, covered } = await forks(page);
  test.skip(!covered.length, 'no day file of case B on this API');
  test.skip(!['arr', 'availability'].every(id => file!.dayMetrics.some(m => m.id === id)), 'the day file has no ARR or availability');
  await keep(page, [label(covered[0])], label(covered[0]));
  const charts = page.getByTestId('day-charts');
  await expect(charts).toHaveAttribute('data-metrics', 'cash,arr,load,incidents');
  await expect(charts.locator('.cs-day-chart')).toHaveCount(4);
  await page.getByTestId('day-metrics').click();
  const menu = page.getByTestId('day-metrics-menu');
  await expect(menu.getByRole('menuitemcheckbox')).toHaveCount(file!.dayMetrics.filter(m => !SAME_AS.includes(m.id)).length);
  if (file!.dayMetrics.some(m => m.id.includes('.'))) await expect(menu.getByRole('group', { name: /CEO brief/ })).toBeVisible();
  // four on: the others wait
  await expect(menu.getByRole('menuitemcheckbox', { name: 'Availability', exact: true })).toHaveAttribute('aria-disabled', 'true');
  await menu.getByRole('menuitemcheckbox', { name: 'Team load', exact: true }).click();
  await menu.getByRole('menuitemcheckbox', { name: 'Availability', exact: true }).click();
  await expect(charts).toHaveAttribute('data-metrics', 'cash,arr,incidents,availability');
  await expect(charts.locator('.cs-day-chart[data-metric=availability]')).toBeVisible();
  await expect(charts.locator('.cs-day-chart[data-metric=load]')).toHaveCount(0);
  await shot(page, 'day-metrics', isMobile, '.cs-day-toolbar', 380);
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  // kept in this browser
  await page.reload();
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  await at(page, 'day');
  await expect(page.getByTestId('day-charts')).toHaveAttribute('data-metrics', 'cash,arr,incidents,availability');
  await page.getByTestId('day-metrics').click();
  await page.getByTestId('day-metrics-menu').getByRole('button', { name: 'Default' }).click();
  await expect(page.getByTestId('day-charts')).toHaveAttribute('data-metrics', 'cash,arr,load,incidents');
});

test('event links open the dossier on the example’s branch, else the company history with a note', async ({ page, isMobile }) => {
  await open(page);
  const { file, covered } = await forks(page);
  test.skip(!covered.length, 'no day file of case B on this API');
  const fork = covered[0], median = file!.dayExamples[fork].find(e => e.kind === 'median')!;
  test.skip(!median.tenant, 'the examples of this day file have no branch tenants');
  // the knowledge base lists this branch (stubbed: it may still be loading on the stack)
  await page.route('**/api/kb/organizations', async r => {
    const res = await r.fetch(), orgs = await res.json() as Record<string, unknown>[];
    await r.fulfill({ response: res, json: orgs.map(o => ({ ...o, branches: [{ tenant: median.tenant, label: 'Median example', from: '2026-10-26', percentile: 50 }] })) });
  });
  await page.reload();
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  await keep(page, [label(fork)], label(fork));
  await radio(page, 'Median').click();
  const lane = page.getByTestId('day-events');
  await expect(page.getByTestId('day-history-note')).toHaveCount(0);
  const links = lane.locator(`a[data-branch="${median.tenant}"]`);
  expect(await links.count()).toBeGreaterThan(5);
  for (const a of (await links.all()).slice(0, 6)) await expect(a).toHaveAttribute('href', new RegExp(`^/workspace/brief\\?(.*branch=${median.tenant}.*entity=|.*entity=.*branch=${median.tenant})`));
  const person = lane.locator('a.cs-kb-person').first();
  if (await person.count()) await expect(person).toHaveAttribute('href', /entity=person\./);
  await links.first().click();
  await expect(page).toHaveURL(new RegExp(`/workspace/brief\\?.*branch=${median.tenant}`));
  await expect(page).toHaveURL(/entity=/);
  await expect(page.locator('.branch-banner')).toBeVisible({ timeout: 30_000 });
  if (SHOTS) { await page.waitForTimeout(1500); mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: `${SHOTS}/day-branch-dossier-${isMobile ? '390' : 'desktop'}.png` }); }
});

test('without a listed branch, links open the company history and say so', async ({ page, isMobile }) => {
  await page.route('**/api/kb/organizations', async r => {
    const res = await r.fetch(), orgs = await res.json() as Record<string, unknown>[];
    await r.fulfill({ response: res, json: orgs.map(o => ({ ...o, branches: [] })) });
  });
  await open(page);
  const { covered } = await forks(page);
  test.skip(!covered.length, 'no day file of case B on this API');
  await keep(page, [label(covered[0])], label(covered[0]));
  await expect(page.getByTestId('day-history-note')).toHaveText('This future is not in the knowledge base; showing the company’s history');
  const lane = page.getByTestId('day-events');
  await expect(lane.locator('a[data-branch]:not([data-branch=""])')).toHaveCount(0);
  const person = lane.locator('a.cs-kb-person').first();
  await expect(person).toHaveAttribute('href', /^\/workspace\/cases\/company_best_year\?.*entity=person\./);
  await expect(person).not.toHaveAttribute('href', /branch=/);
  await person.click();
  await expect(page).toHaveURL(/\/workspace\/cases\/company_best_year\?.*entity=person\./);
  if (SHOTS) { await page.waitForTimeout(1500); mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: `${SHOTS}/day-history-dossier-${isMobile ? '390' : 'desktop'}.png` }); }
});

test('a future in the knowledge base offers its questions; the first opens Ask on its branch', async ({ page, isMobile }) => {
  await open(page);
  const { file, covered } = await forks(page);
  const fork = 'pay_tech_debt';
  const median = file?.dayExamples[fork]?.find(e => e.kind === 'median') as (DayFile['dayExamples'][string][number] & { kb?: boolean; askExamples?: string[] }) | undefined;
  test.skip(!covered.includes(fork) || !median?.tenant || !median.askExamples?.length, 'no branch questions for Reduce tech debt on this API');
  await page.route('**/api/kb/organizations', async r => {
    const res = await r.fetch(), orgs = await res.json() as Record<string, unknown>[];
    await r.fulfill({ response: res, json: orgs.map(o => ({ ...o, branches: [{ tenant: median!.tenant, label: 'Reduce tech debt · median example', from: '2026-10-26', percentile: 50 }] })) });
  });
  await page.reload();
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  await keep(page, [label(fork)], label(fork));
  await radio(page, 'Median').click();
  const ask = page.getByTestId('day-ask');
  await expect(ask).toContainText('Ask this future:');
  const chips = ask.locator('.cs-day-ask-chip');
  expect(await chips.count()).toBeLessThanOrEqual(4);
  // demo step six: incidents first
  if (median!.askExamples!.includes('Why did incidents fall in February?')) await expect(chips.first()).toHaveText('Why did incidents fall in February?');
  const q = await chips.first().innerText();
  await expect(chips.first()).toHaveAttribute('href', new RegExp(`^/workspace/ask\\?.*branch=${median!.tenant}`));
  expect(new URL(await chips.first().getAttribute('href') ?? '', 'http://x').searchParams.get('q')).toBe(q);
  await shot(page, 'day-ask', isMobile, '[data-testid=day-ask]');
  await chips.first().click();
  await expect(page).toHaveURL(new RegExp(`/workspace/ask\\?.*branch=${median!.tenant}`));
  await expect(page.locator('.branch-banner')).toBeVisible({ timeout: 30_000 });
  // WP15: Ask fills the question in and asks it once (q then leaves the URL)
  await expect(page.getByRole('textbox', { name: 'Question' })).toHaveValue(q, { timeout: 30_000 });
  await expect(page.locator('.kb-answer h2')).toHaveText(q, { timeout: 30_000 });
  if (SHOTS) { await page.waitForTimeout(1000); await page.screenshot({ path: `${SHOTS}/day-ask-page-${isMobile ? '390' : 'desktop'}.png` }); }
});

test('a records-only future says it is not in the knowledge base', async ({ page }) => {
  await open(page);
  const { file, covered } = await forks(page);
  const fork = covered.find(f => file!.dayExamples[f].every(e => (e as { kb?: boolean }).kb === false));
  test.skip(!fork, 'every fork of this day file is in the knowledge base');
  await keep(page, [label(fork!)], label(fork!));
  await expect(examples(page)).toHaveAttribute('data-source', 'day-file');
  await expect(page.getByTestId('day-ask')).toHaveCount(0);
  await expect(page.getByTestId('open-branch')).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByTestId('open-branch')).toHaveAttribute('title', /records only/);
  await expect(page.getByTestId('day-history-note')).toBeVisible();
  await expect(page.getByTestId('day-events').locator('a[href*="entity=sim."]')).toHaveCount(0);
});

test('case A has no day file: its Day points at case B’s Day', async ({ page, isMobile }) => {
  // m2: no request for a day file the sets do not list (no 404 in the console)
  const missing: string[] = [];
  page.on('response', r => { if (r.status() === 404 && r.url().includes('day_examples')) missing.push(r.url()); });
  await page.goto('cases/delivery_best_way');
  await page.evaluate(() => { localStorage.clear(); for (const id of ['company_best_year', 'delivery_best_way']) localStorage.setItem(`variator:tour:${id}`, 'dismissed'); });
  await page.goto('cases/delivery_best_way');
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  const sets = ((await (await page.request.get('api/variator/artifacts')).json()) as { sets: { manifest?: { schema?: string; case?: string; id?: string } }[] }).sets;
  test.skip(!sets.some(s => s.manifest?.id === 'future:delivery_best_way'), 'no actor set of case A on this API');
  test.skip(sets.some(s => s.manifest?.schema === 'variator.day/1' && s.manifest.case === 'delivery_best_way'), 'case A has a day file now');
  await at(page, 'week-review');
  await page.getByTestId('gate-week').getByRole('checkbox').first().click();
  await at(page, 'day-review');
  await page.getByTestId('gate-day').getByRole('checkbox').first().click();
  await at(page, 'day');
  const ex = examples(page);
  await expect(ex).toHaveAttribute('data-reason', 'other-case');
  await expect(ex).toHaveAttribute('data-tour', 'day-examples');
  await expect(ex).toContainText('Example days are made for case B in this version');
  expect(missing).toEqual([]);
  const link = page.getByTestId('day-other-case');
  await expect(link).toHaveAttribute('href', '/workspace/cases/company_best_year#day');
  await shot(page, 'day-case-a', isMobile);
  await link.click();
  await expect(page).toHaveURL(/\/workspace\/cases\/company_best_year#day$/);
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  // no fork kept for the day yet in case B: it lands on the day review, the start of step four
  await expect.poll(() => page.evaluate(() => { const r = document.getElementById('day-review')!.getBoundingClientRect(); return r.top > -40 && r.top < innerHeight / 2; }), { timeout: 15_000 }).toBe(true);
});

test('tour step 5 counts the actor example days of the leader', async ({ page }) => {
  await page.goto(CASE);
  await page.evaluate(() => localStorage.clear());
  await page.goto(`${CASE}?tour=1&step=5`);
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  const { file } = await forks(page);
  test.skip(!file, 'no day file of case B on this API');
  const card = page.getByTestId('tour-card');
  await expect(card).toHaveAttribute('data-step', 'week-day');
  await expect(card).not.toContainText('…', { timeout: 30_000 });
  const leader = (await page.getByTestId('leader').innerText()).trim();
  const id = config.forks.find(f => leader.includes(f[1]))?.[0];
  const n = id ? file!.dayExamples[id]?.length ?? 0 : null;
  if (n != null) await expect(card).toContainText(`${n} example days of`);
  await expect(card).not.toContainText(/\b0 example days/);
  await expect(card).toContainText('ask why incidents fell in February');
});
