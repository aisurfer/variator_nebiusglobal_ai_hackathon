import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { expect, test, type Page, type TestInfo } from '@playwright/test';

// The demonstration script of the MVP (10-MVP-PRD §9) as the CEO walks it, step by step, on the release candidate:
//   PLAYWRIGHT_BASE_URL=http://127.0.0.1:8088 npx playwright test demo.spec.ts --project=desktop
//   make -C deploy/mvp demo URL=https://<domain>
// The URL is the origin of the site; the app is under /workspace/ on it (playwright.config.ts appends it).
// Needs the whole stack: the workspace, the VARIATOR API (catalogue, actor cards, case futures, day set, the live month
// route) and the knowledge base with the day branches. Nothing is stubbed. Each step saves demo-<n>.png and its time to
// DEMO_SHOTS (default: the test's output directory); demo-times.json collects the times of a run.
// paths relative to the base URL (<origin>/workspace/, playwright.config.ts)
const B = 'cases/company_best_year', A = 'cases/delivery_best_way';
const BRANCH = 'company-b-pay_tech_debt-p50';
const QUESTION = 'Why did incidents fall in February?';
const DECISION = 'Use 15% of engineering capacity for technical debt';

test.describe.configure({ mode: 'default' }); // in order, one worker: the steps of one demonstration
test.use({ viewport: { width: 1440, height: 1000 } });
test.setTimeout(120_000); // step 6 waits for an answer of the knowledge base: 240 s
test.beforeEach(({ isMobile }) => test.skip(isMobile, 'the demonstration runs on a desktop screen'));

const shotsDir = (info: TestInfo) => process.env.DEMO_SHOTS ?? dirname(info.outputPath('x'));
async function shot(page: Page, info: TestInfo, n: number) {
  const dir = shotsDir(info);
  mkdirSync(dir, { recursive: true });
  await page.mouse.move(0, 0);
  await page.screenshot({ path: join(dir, `demo-${n}.png`) });
}
/** Runs one step of the script, records its time and saves its screenshot. */
async function step(page: Page, info: TestInfo, n: number, body: () => Promise<void>) {
  const t0 = Date.now();
  await body();
  const s = (Date.now() - t0) / 1000;
  info.annotations.push({ type: 'time', description: `step ${n}: ${s.toFixed(1)} s` });
  await shot(page, info, n);
  // a failed test restarts the worker: merge with the times already written
  const file = join(shotsDir(info), 'demo-times.json');
  const times = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown> : {};
  times[`step ${n}`] = { seconds: +s.toFixed(1), at: new Date().toISOString() };
  writeFileSync(file, JSON.stringify(times, null, 2));
}
/** A browser that has answered the first-visit offer of the tours, with no saved edits. */
async function openCase(page: Page, path: string) {
  await page.goto(path);
  await page.evaluate(() => { localStorage.clear(); for (const id of ['company_best_year', 'delivery_best_way']) localStorage.setItem(`variator:tour:${id}`, 'dismissed'); });
  await page.goto(path);
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-status', 'ready', { timeout: 30_000 });
}
const at = (page: Page, id: string) => page.evaluate(i => document.getElementById(i)!.scrollIntoView({ block: 'start' }), id);
const upToDate = (page: Page) => expect(page.locator('.cs-run-dot')).toHaveAttribute('data-state', 'ok', { timeout: 30_000 });
async function keepForWeek(page: Page, forks: string[]) {
  await at(page, 'week-review');
  for (const f of forks) {
    const box = page.getByTestId('gate-week').getByRole('checkbox', { name: new RegExp(f) });
    if (await box.getAttribute('aria-checked') !== 'true') await box.click();
  }
}
/** The actor ranking of the Compare section: fork -> score × feasible. */
async function scores(page: Page) {
  await expect(page.locator('#compare')).toContainText("On your company's actors");
  const rows = await page.locator('#compare .cs-rank-table tbody tr').allInnerTexts();
  return Object.fromEntries(rows.map(r => r.split('\t').map(x => x.trim()).filter(Boolean)).map(c => [c[1], c[2]]));
}
async function keepForDay(page: Page, fork: string) {
  await at(page, 'day-review');
  const box = page.getByTestId('gate-day').getByRole('checkbox', { name: new RegExp(fork) });
  if (await box.getAttribute('aria-checked') !== 'true') await box.click();
}

test('1 · the CEO opens the app: the brief from the catalogue, a metric down to its records, a dossier', async ({ page }, info) => {
  await step(page, info, 1, async () => {
    const t0 = Date.now();
    await page.goto('./'); // the workspace: <origin>/workspace/
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Avery', { timeout: 60_000 });
    await expect(page.locator('.kb-kpi[data-metric]').first()).toBeVisible();
    info.annotations.push({ type: 'first view', description: `${Date.now() - t0} ms` });
    expect(Date.now() - t0, 'the first screen in under 3 s (NFR7)').toBeLessThan(3_000);
    await expect(page.locator('.kb-brief')).toHaveAttribute('data-source', 'catalogue');
    await expect(page.locator('.eyebrow').first()).toContainText('MORNING BRIEF');
    await expect(page.locator('.eyebrow').first()).not.toContainText('KNOWLEDGE BASE');
    // money, burn, margin, churn and SLA of the year of history
    await expect(page.locator('.kb-headline')).toContainText('Money:');
    for (const m of ['fin.cash', 'fin.burn', 'fin.gross_margin', 'rev.churn', 'ops.sla']) await expect(page.locator(`.kb-kpi[data-metric="${m}"] .metric-value`)).toBeVisible();
    await expect(page.locator('.kb-kpi[data-metric="fin.burn"] .metric-top')).toContainText('Net cash flow per month');
    await expect(page.locator('.kb-kpi[data-metric="fin.gross_margin"] .metric-value')).toHaveText(/^\d+(\.\d)?%$/);
    // revenue: the invoices it is computed from, then the dossier of one of them and of the client
    await page.locator('.kb-kpi[data-metric="fin.revenue"]').click();
    const sheet = page.getByRole('dialog');
    await expect(sheet.getByText('Worked example on this morning')).toBeVisible({ timeout: 20_000 });
    const records = sheet.locator('.g-drill-record-main');
    expect(await records.count()).toBeGreaterThan(0);
    await expect(records.first()).toContainText(/Invoice INV-/);
    await records.filter({ hasText: 'Meridian Freight Group' }).first().click();
    await expect(page).toHaveURL(/entity=sim\.INV-/);
    await expect(sheet.getByText('Version history')).toBeVisible({ timeout: 20_000 });
    await expect(sheet).toContainText(/Payment PAY-/); // the payment that settles it
    // the client among the linked records: a one-line bullet, its kind and relation in the hint
    const client = sheet.locator('.kb-related-group[data-group="out"]').getByRole('button', { name: 'Meridian Freight Group', exact: true });
    await expect(client).toHaveAttribute('title', /organization · counterparty/i);
    await client.click();
    await expect(page).toHaveURL(/entity=org\.CL-001/);
    await expect(sheet.getByRole('heading', { name: 'Meridian Freight Group', exact: true }).first()).toBeVisible({ timeout: 20_000 });
    await expect(sheet.getByText('Version history')).toBeVisible();
    await expect(sheet).toContainText(/contract|agreement/i);
  });
});

test('2 · actors and risks: the Meridian Freight Group card, habits with source tags and citations; the risk register', async ({ page }, info) => {
  await step(page, info, 2, async () => {
    await page.goto('actors?actor=CL-001');
    await expect(page.getByRole('heading', { level: 1, name: 'Actors and risks' })).toBeVisible({ timeout: 60_000 });
    const card = page.locator('[data-tour="actor-card"]');
    await expect(card.getByRole('heading', { level: 2 })).toHaveText('Meridian Freight Group');
    await expect(card.locator('[data-tour="actor-profile"]')).toContainText('days after issue');
    expect(await card.locator('.ac-cite').count(), 'citations in the profile').toBeGreaterThan(2);
    const habits = card.locator('[data-tour="actor-habits"] .ac-hrow[data-habit]');
    expect(await habits.count()).toBeGreaterThanOrEqual(4);
    await expect(card.locator('[data-habit="pay_delay_days"] .ac-src')).toHaveText(/^data/);
    // the source tag of every habit in view (the rest are behind «More habits»)
    const tags = (await card.locator('[data-tour="actor-habits"] .ac-src').allInnerTexts()).filter(Boolean);
    expect(tags.length).toBeGreaterThanOrEqual(4);
    for (const src of tags) expect(src).toMatch(/^(data|group|llm)\b/);
    await shot(page, info, 21);
    // the register of the risks that already fired
    await page.locator('[data-tour="actors-tab-risks"]').click();
    await expect(page).toHaveURL(/tab=risks/);
    const reg = page.locator('[data-tour="risks-register"]');
    await expect(reg).toContainText('Risks that already fired');
    expect(await reg.locator('.ac-rrow[data-risk]').count()).toBeGreaterThanOrEqual(10);
    await expect(reg.locator('[data-risk="R5"]')).toContainText('Client gives notice');
  });
});

test('3 · case B: the actor verdict, the risk calendar with its headline, the fans', async ({ page }, info) => {
  await step(page, info, 3, async () => {
    await openCase(page, B);
    await upToDate(page);
    const v = page.getByTestId('actor-verdict');
    await expect(v).toHaveAttribute('data-state', 'defaults');
    await expect(page.getByTestId('verdict-source')).toHaveAttribute('data-source', 'actors');
    await expect(page.getByTestId('actor-leader')).toHaveText('Keep the plan');
    await expect(page.getByTestId('actor-tie')).toContainText(/equal to\s*Reduce tech debt/);
    // PRD §9 step 3 (2026-10-28): a close call, cash or clients: Raise prices 8% wins most scenarios at a cost in churn
    const close = page.getByTestId('actor-close-call');
    await expect(close).toContainText(/Raise prices 8% comes out on top in \d+% of the scenarios/);
    await expect(close).toContainText(/cash at year end about \$\d+k higher, but client churn/);
    await shot(page, info, 31);
    await at(page, 'overtime');
    await expect(page.getByTestId('fans')).toHaveAttribute('data-source', 'actors');
    await expect(page.getByTestId('fan-status_quo')).toBeVisible();
    await expect(page.locator('#overtime .cs-fan .recharts-area').first()).toBeVisible();
    const cal = page.getByTestId('risk-calendar');
    await expect(cal).toHaveAttribute('data-source', 'actors');
    await expect(page.getByTestId('calendar-headline')).toContainText('1,000 scenarios');
    expect(await cal.locator('tbody tr').count()).toBeGreaterThanOrEqual(5);
    await expect(cal.locator('tbody tr', { hasText: 'Cash below zero' })).toBeVisible();
    // the PRD's line is about «Hire 5 engineers»: its headline is the one the presenter reads out
    await cal.locator('select').selectOption('hire_5');
    await expect(page.getByTestId('calendar-headline')).toContainText('1,000 scenarios');
    info.annotations.push({ type: 'calendar · Hire 5 engineers', description: await page.getByTestId('calendar-headline').innerText() });
    await page.getByTestId('risk-calendar').scrollIntoViewIfNeeded();
  });
});

test('4 · the timeline: Hiring starts to month 4 changes only its fork; payroll tax; Rerun on actors; Cash first', async ({ page }, info) => {
  await step(page, info, 4, async () => {
    await openCase(page, B);
    await upToDate(page);
    const before = await scores(page);
    expect(Object.keys(before)).toHaveLength(5);
    // drag «Hiring starts» from month 0 to month 4
    const marker = page.locator('#forks [data-marker="hire_start"]');
    await marker.scrollIntoViewIfNeeded();
    await expect(marker).toHaveAttribute('aria-valuenow', '0');
    const num = (t: string) => page.getByTestId('decisions-events').locator('text.cs-tk-num', { hasText: new RegExp(`^${t}$`) }).first().boundingBox();
    const [m0, m4] = [await num('0'), await num('4')];
    const b = (await marker.locator('circle.cs-tk-mark').boundingBox())!;
    const dx = (m4!.x + m4!.width / 2) - (m0!.x + m0!.width / 2);
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2 + dx / 2, b.y + b.height / 2, { steps: 6 });
    await expect(page.locator('#forks .cs-tk-ghost')).toBeVisible(); // the old place stays marked during the drag
    await page.mouse.move(b.x + b.width / 2 + dx, b.y + b.height / 2, { steps: 6 });
    await page.mouse.up();
    await expect(marker).toHaveAttribute('aria-valuenow', '4');
    await expect(marker).toHaveAttribute('aria-label', /Hiring starts: Feb/);
    const notice = page.locator('.cs-toast').first();
    await expect(notice).toContainText('Hiring starts', { timeout: 30_000 });
    await expect(notice).toContainText('Effect');
    await expect(notice.getByRole('button', { name: 'Undo' })).toBeVisible();
    await expect(notice).toContainText('For Hire 5 engineers'); // the notice speaks of the fork that changed
    await upToDate(page);
    await shot(page, info, 41);
    // the page reruns on the actors after each edit (WP25 R1): the verdict follows the edits by itself
    // the verdict keeps data-state «current» during the recompute: wait for «Recomputing…» to come (it may be over) and go
    const busy = page.getByText("Recomputing on your company's actors…");
    const rerunActors = async () => {
      await at(page, 'answer');
      await busy.first().waitFor({ state: 'visible', timeout: 3_000 }).catch(() => undefined);
      await expect(busy).toHaveCount(0, { timeout: 30_000 });
      await expect(page.getByTestId('actor-done')).toContainText(/Rerun on actors in \d+\.\d s/, { timeout: 30_000 });
      await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-state', 'current');
      await expect(page.getByTestId('actor-state')).toHaveText('Current');
    };
    // PRD §9 step 4 (2026-10-28): fixed scales, so the move changes only the fork «Hire 5 engineers»
    await rerunActors();
    const others = (s: Record<string, string>) => Object.fromEntries(Object.entries(s).filter(([f]) => f !== 'Hire 5 engineers'));
    expect(others(await scores(page)), 'the scores of the other forks after the move').toEqual(others(before));
    // the payroll tax hypothesis and its effect, then the verdict on the actors again
    await at(page, 'world');
    const tax = page.locator('.cs-hyp', { hasText: 'Payroll tax +5 pp from July' });
    await tax.click();
    await expect(tax).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.cs-toast', { hasText: 'Payroll tax +5 pp from July' }).first()).toContainText('Effect', { timeout: 30_000 });
    await upToDate(page);
    await rerunActors();
    await expect(page.getByTestId('actor-leader')).not.toBeEmpty();
    info.annotations.push({ type: 'verdict after the edits', description: (await page.getByTestId('actor-verdict').innerText()).split('\n').slice(0, 8).join(' | ') });
    // «Cash first»: raising prices becomes the clear leader on the actors
    await page.getByRole('radio', { name: 'Cash first' }).click();
    await expect(page.locator('.cs-toast', { hasText: 'Priorities: Cash first' }).first()).toBeVisible();
    await rerunActors();
    await expect(page.getByTestId('actor-leader')).toHaveText('Raise prices 8%');
    expect(parseInt(await page.getByTestId('actor-pbest').innerText(), 10), 'best in most scenarios').toBeGreaterThanOrEqual(70);
  });
});

test('5 · a fork off; Week: the event tree with labelled splits, a node sheet, a path sheet', async ({ page }, info) => {
  await step(page, info, 5, async () => {
    await openCase(page, B);
    // Reserve GPU capacity is not interesting: off
    const gpu = page.locator('#forks .cs-fork-card', { hasText: 'Reserve GPU capacity' });
    const toggle = gpu.getByRole('checkbox', { name: 'Compare Reserve GPU capacity' });
    await expect(toggle).toBeChecked();
    await gpu.locator('.cs-switch-track').click(); // the switch as people click it
    await expect(toggle).not.toBeChecked();
    await expect(page.locator('.cs-toast').first()).toContainText('Reserve GPU capacity', { timeout: 30_000 });
    await upToDate(page);
    await at(page, 'week-review');
    await expect(page.getByTestId('gate-week').getByRole('checkbox', { name: /Reserve GPU capacity/ })).toHaveCount(0);
    await keepForWeek(page, ['Keep the plan', 'Reduce tech debt']);
    await at(page, 'week');
    const card = page.getByTestId('week-tree');
    await expect(card).toHaveAttribute('data-kind', 'events', { timeout: 60_000 });
    await expect(card).toHaveAttribute('data-source', 'actors');
    const tree = page.getByTestId('event-tree');
    expect(await tree.locator('[data-node]').count()).toBeGreaterThan(4);
    await expect(tree.locator('.cs-enode-label').first()).toContainText(/\d+%$/);
    await shot(page, info, 51);
    const split = tree.locator('[data-node]').filter({ hasNotText: /^(Without|Other events|All scenarios|No such event)/ }).first();
    await split.dispatchEvent('click');
    const ns = page.getByTestId('tree-node-sheet');
    await expect(ns).toContainText('Why it splits.');
    await expect(ns).toContainText('Share of all scenarios');
    await shot(page, info, 52);
    await page.keyboard.press('Escape');
    await expect(ns).toHaveCount(0);
    await tree.getByTestId('leaf-median').dispatchEvent('click');
    const ps = page.getByTestId('tree-path-sheet');
    await expect(ps).toContainText('Week by week');
    await expect(ps.getByTestId('path-basis')).toContainText('by cash at the end of the year');
  });
});

test('6 · Day for Reduce tech debt: the median example, a day card, «Ask this future» opens Ask on the branch with the chain', async ({ page }, info) => {
  test.setTimeout(240_000);
  await step(page, info, 6, async () => {
    await openCase(page, B);
    await keepForWeek(page, ['Reduce tech debt']);
    await keepForDay(page, 'Reduce tech debt');
    await at(page, 'day');
    const ex = page.getByTestId('day-examples');
    await expect(ex).toHaveAttribute('data-source', 'day-file');
    for (const k of ['Pessimistic', 'Median', 'Optimistic']) await expect(ex.getByRole('radio', { name: new RegExp(k) })).toBeVisible();
    await ex.getByRole('radio', { name: /Median/ }).click();
    await expect(page.getByTestId('day-charts')).toHaveAttribute('data-example', 'median');
    await expect(page.getByTestId('open-branch')).toHaveAttribute('data-tenant', BRANCH);
    await expect(page.getByTestId('open-branch')).toHaveAttribute('aria-disabled', 'false');
    // the day card on a click
    const area = page.getByTestId('day-chart-area');
    await area.evaluate(e => e.scrollIntoView({ block: 'center' }));
    const box = (await area.locator('.cs-day-chart').first().locator('.recharts-wrapper').boundingBox())!;
    await page.mouse.click(box.x + box.width * 0.4, box.y + box.height / 2);
    const dayCard = page.getByTestId('day-card');
    await expect(dayCard).toBeVisible();
    await expect(dayCard).toHaveAttribute('data-date', /^\d{4}-\d\d-\d\d$/);
    await shot(page, info, 61);
    // «Ask this future»: the demo question is the first chip and opens Ask on the branch
    const chip = page.getByTestId('day-ask').locator('.cs-day-ask-chip').first();
    await expect(chip).toHaveText(QUESTION);
    await chip.scrollIntoViewIfNeeded();
    await shot(page, info, 62);
    await chip.click();
    await expect(page).toHaveURL(new RegExp(`/workspace/ask\\?.*branch=${BRANCH}`));
    await expect(page.getByRole('status', { name: 'Simulation' })).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('textbox', { name: 'Question' })).toHaveValue(QUESTION, { timeout: 30_000 });
    const answer = page.locator('.kb-answer');
    await expect(answer).toContainText(DECISION, { timeout: 180_000 });
    await expect(answer).toContainText(/Incidents fell in February/i);
  });
});

test('7 · case A: the verdict is a close call; the risk calendar by month', async ({ page }, info) => {
  await step(page, info, 7, async () => {
    await openCase(page, A);
    await expect(page.getByTestId('verdict-source')).toHaveAttribute('data-source', 'actors');
    await expect(page.getByTestId('actor-leader')).not.toBeEmpty();
    await expect(page.getByTestId('actor-close-call')).toContainText(/comes out on top in \d+% of the scenarios/);
    await shot(page, info, 71);
    await at(page, 'overtime');
    const cal = page.getByTestId('risk-calendar');
    await expect(cal).toHaveAttribute('data-source', 'actors');
    await expect(page.getByTestId('calendar-headline')).toContainText(/go-live late|budget/i);
    await expect(cal.locator('tbody tr', { hasText: 'Go-live late' })).toBeVisible();
    await expect(cal.locator('tbody tr', { hasText: /spend above the budget/ })).toBeVisible();
    info.annotations.push({ type: 'calendar · case A', description: `${await cal.locator('select').inputValue()}: ${await page.getByTestId('calendar-headline').innerText()}` });
    await cal.scrollIntoViewIfNeeded();
  });
});

test('8 · a new case on /cases: fire all employees, the chips to the draft, «not available in this version»', async ({ page }, info) => {
  await step(page, info, 8, async () => {
    await page.goto('cases');
    await page.evaluate(() => localStorage.clear());
    await page.goto('cases');
    await expect(page.getByRole('heading', { name: /What decision are you weighing/ })).toBeVisible({ timeout: 30_000 });
    await page.getByRole('textbox', { name: 'Your decision' }).fill('What if I fire all employees in 3 months?');
    await page.getByRole('button', { name: 'Ask' }).click();
    const thread = page.getByTestId('intake-thread');
    const chip = (name: string) => page.locator('.cs-quick .cs-chip:not([disabled])', { hasText: name }).first();
    await expect(thread).toContainText('Larchwood Systems has 20 people');
    await chip('Everyone except the leadership').click();
    await expect(thread).toContainText('What is the goal');
    await chip('Cut costs').click();
    await expect(thread).toContainText('Compare it with what?');
    await chip('Replace delivery with a vendor').click();
    const draft = page.getByTestId('intake-draft');
    await expect(draft.locator('.cs-draft-forks li').first()).toContainText('Keep as is');
    await expect(draft.locator('.cs-draft-notice')).toContainText('not available in this version');
    await expect(draft.getByRole('link', { name: /Best year/ })).toBeVisible();
    await draft.locator('.cs-draft-notice').scrollIntoViewIfNeeded();
  });
});
