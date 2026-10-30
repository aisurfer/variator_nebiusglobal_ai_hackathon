import { expect, test, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

// Needs the workspace with a live knowledge base behind it (tenant company loaded):
// KB_E2E=1 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3002 npm run test:e2e -- kb.spec.ts
// KB_SCREENSHOTS=<dir> also saves the evidence screenshots there.
test.skip(!process.env.KB_E2E, 'set KB_E2E=1 with the knowledge base running');

// the one user of the MVP: the CEO of config/company.json
const CEO = 'Avery';
const AREAS = ['Money', 'Clients and growth', 'Delivery and service'];

async function expectViewport(page: Page) {
  const size = await page.evaluate(() => ({ client: document.documentElement.clientWidth, document: document.documentElement.scrollWidth }));
  expect(size.document, `content must stay within ${size.client}px`).toBeLessThanOrEqual(size.client + 1);
}
async function shot(page: Page, name: string, isMobile: boolean, fullPage = false) {
  const dir = process.env.KB_SCREENSHOTS;
  if (!dir) return;
  mkdirSync(dir, { recursive: true });
  await page.mouse.move(0, 0);
  await page.screenshot({ path: join(dir, `${name}-${isMobile ? 'mobile' : 'desktop'}.png`), fullPage: fullPage && !isMobile });
}
// paths relative to the base URL (<origin>/workspace/, playwright.config.ts)
async function openBrief(page: Page, path = 'brief') {
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1 })).toContainText(CEO, { timeout: 60_000 });
}

/** The VARIATOR API down: the brief falls back to the knowledge base. */
async function withoutCatalogue(page: Page) {
  await page.route('**/api/variator/artifacts', route => route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ error: 'The VARIATOR API is unreachable (test)', service: 'variator-api' }) }));
}

test('the CEO brief from the knowledge base: key numbers, three areas, metric sheet and dossier', async ({ page, isMobile }) => {
  test.setTimeout(120_000);
  await withoutCatalogue(page);
  await openBrief(page);
  await expect(page.locator('.kb-brief')).toHaveAttribute('data-source', 'kb');
  await expect(page.locator('.eyebrow').first()).toContainText('KNOWLEDGE BASE');
  // the note is secondary text: it is behind «Know more»
  await page.getByRole('button', { name: 'About this brief' }).click();
  await expect(page.getByRole('note')).toContainText('The VARIATOR metric catalogue is not available, so this brief is computed from the knowledge base.');
  await page.getByRole('button', { name: 'About this brief' }).click();
  await expect(page.getByRole('note')).toHaveCount(0);
  await expect(page.locator('.kb-headline p').first()).toBeVisible();
  await expect(page.getByRole('region', { name: 'Key numbers' }).locator('.kb-kpi')).toHaveCount(await page.locator('.kb-top .kb-kpi').count());
  const top = await page.locator('.kb-top .kb-kpi').count();
  expect(top).toBeGreaterThanOrEqual(4);
  expect(top).toBeLessThanOrEqual(6);
  for (const area of AREAS) await expect(page.getByRole('heading', { level: 2, name: area, exact: true })).toBeVisible();
  await expectViewport(page);
  await shot(page, 'kb-brief-fallback', isMobile, true);

  await page.locator('.kb-more').first().click();
  const sheet = page.getByRole('dialog');
  await expect(sheet.getByText('How it is calculated')).toBeVisible({ timeout: 30_000 });
  await expect(sheet.getByText(/^METRIC · /)).toBeVisible();
  await expectViewport(page);
  await shot(page, 'kb-metric', isMobile);

  const open = sheet.locator('button.kb-item:not(.kb-item-locked)').first();
  if (await open.count()) {
    await open.click();
    await expect(sheet.getByText('Version history')).toBeVisible({ timeout: 20_000 });
    await expect(sheet.getByRole('button', { name: 'Back' })).toBeVisible();
    await shot(page, 'kb-dossier', isMobile);
    await sheet.getByRole('button', { name: 'Back' }).click();
    await expect(sheet.getByText('How it is calculated')).toBeVisible();
  }
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('one role: the shell has no role select, the profile shows the CEO, an old role link is dropped', async ({ page, isMobile }) => {
  test.setTimeout(90_000);
  await openBrief(page, 'brief?role=finance');
  // the old parameter changes nothing: the brief is the CEO brief with every area
  for (const area of AREAS) await expect(page.locator('.kb-area h2', { hasText: area })).toHaveCount(1);
  await expect(page.locator('select[aria-label="Executive perspective"], .genui-role-select, .perspective-switch-nav')).toHaveCount(0);
  await expect(page.getByText(/perspective/i)).toHaveCount(0);
  await expect(page.locator('.eyebrow').first()).not.toContainText('CEO');
  if (isMobile) await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(page.locator('.organization-switch')).toContainText('Larchwood Systems');
  await expect(page.locator('.profile')).toContainText('Avery Collins');
  await expect(page.locator('.profile')).toContainText('Chief Executive Officer');
  await expect(page.locator('.sidebar select')).toHaveCount(0);
  await expectViewport(page);
  await shot(page, 'shell', isMobile);
  // the next navigation drops the old role parameter
  await page.getByRole('link', { name: 'Ask the knowledge base' }).click();
  await expect(page).toHaveURL(/\/workspace\/ask/);
  await expect(page).not.toHaveURL(/role=/);
  await expect(page.locator('.kb-examples .button')).toHaveCount(7);
});

test('the CEO brief from the metric catalogue: every tile opens its drill-down, a record opens its dossier', async ({ page, request, isMobile }) => {
  test.setTimeout(120_000);
  const list = await request.get('api/variator/artifacts');
  const sets = list.ok() ? ((await list.json()).sets as { manifest?: { schema?: number; brief?: unknown } }[]).filter(s => (s.manifest?.schema ?? 0) >= 2 && s.manifest?.brief) : [];
  test.skip(!sets.length, 'no metric catalogue set with a CEO brief behind /api/variator');
  await openBrief(page);
  const brief = page.locator('.kb-brief');
  await expect(brief).toHaveAttribute('data-source', 'catalogue', { timeout: 30_000 });
  await expect(page.locator('.kb-brief-note')).toHaveCount(0);
  for (const area of AREAS) await expect(page.locator('.kb-area h2', { hasText: area })).toHaveCount(1);
  const tiles = page.locator('.kb-kpi[data-metric]');
  expect(await tiles.count()).toBeGreaterThan(15);
  // numbers that mislead alone: a capped runway, a negative burn, NPS on its own scale
  const runway = page.locator('.kb-kpi[data-metric="fin.runway"]');
  await expect(runway.locator('.metric-value')).toHaveText('60+ mo');
  await expect(runway.getByRole('img', { name: /profitable.*capped/ })).toHaveCount(1);
  const burn = page.locator('.kb-kpi[data-metric="fin.burn"]');
  await expect(burn.locator('.metric-top')).toContainText('Net cash flow per month');
  await expect(burn.locator('.metric-value')).toHaveText(/^\+\$/);
  await expect(page.locator('.kb-kpi[data-metric="cust.nps"] .metric-value')).not.toContainText('/');
  await expect(page.locator('.kb-headline')).toContainText('runway over 5 years');
  await expectViewport(page);
  await shot(page, 'catalogue-brief', isMobile, true);

  // a tile opens the drill-down of its metric file: value, formula, steps, history, parameters, records
  await page.locator('.kb-kpi[data-metric="ops.incidents"]').click();
  const sheet = page.getByRole('dialog');
  await expect(sheet.getByText('Worked example on this morning')).toBeVisible({ timeout: 20_000 });
  await expect(sheet.getByText('VARIATOR metric catalogue').first()).toBeVisible();
  await expect(sheet.locator('.g-formula, .g-drill-hero').first()).toBeVisible();
  await expect(sheet.getByText('History', { exact: true })).toBeVisible();
  await expectViewport(page);
  await shot(page, 'catalogue-drilldown', isMobile);
  // Escape with a popover open inside the sheet closes only the popover; a second Escape closes the sheet
  await sheet.getByRole('button', { name: 'About the question' }).click();
  await expect(sheet.getByRole('note')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(sheet.getByRole('note')).toHaveCount(0);
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(sheet.getByText('Worked example on this morning')).toBeVisible();
  // the steps and the specification are disclosures closed by default
  await expect(sheet.getByText('As the metric catalogue computes it')).toBeHidden();
  await sheet.locator('summary h3', { hasText: 'How it’s calculated' }).click();
  await expect(sheet.getByText('As the metric catalogue computes it')).toBeVisible();
  await sheet.locator('summary h3', { hasText: 'Specification' }).click();
  await expect(sheet.getByRole('heading', { name: 'Parameters' })).toBeVisible();

  // a step of the breakdown is a step of the sheet: Back returns to the whole metric
  await sheet.locator('.g-tree-main').nth(1).click();
  await expect(page).toHaveURL(/metric=ops\.incidents(~|%7E)root\./);
  await expect(sheet.getByRole('navigation', { name: 'Position in the breakdown' })).toBeVisible();
  await sheet.getByRole('button', { name: 'Back' }).click();
  await expect(page).toHaveURL(/metric=ops\.incidents(&|$)/);

  // a record behind the number opens its knowledge-base dossier
  await sheet.locator('.g-drill-record-main').first().click();
  await expect(page).toHaveURL(/entity=sim\./);
  await expect(sheet.getByText('Version history')).toBeVisible({ timeout: 20_000 });
  await shot(page, 'catalogue-dossier', isMobile);
  await sheet.getByRole('button', { name: 'Back' }).click();
  await expect(sheet.getByText('Worked example on this morning')).toBeVisible();
  // no popover open: Escape closes the sheet
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('the churn tile and its sheet say the same thing, in words and in the app date format', async ({ page, request, isMobile }) => {
  test.setTimeout(120_000);
  const list = await request.get('api/variator/artifacts');
  const sets = list.ok() ? ((await list.json()).sets as { manifest?: { schema?: number; brief?: unknown } }[]).filter(s => (s.manifest?.schema ?? 0) >= 2 && s.manifest?.brief) : [];
  test.skip(!sets.length, 'no metric catalogue set with a CEO brief behind /api/variator');
  await openBrief(page);
  await expect(page.locator('.kb-brief')).toHaveAttribute('data-source', 'catalogue', { timeout: 30_000 });
  // the view date reads «26 Oct 2026», as the rest of the app, over the native date field
  await expect(page.locator('.kb-asof-text')).toHaveText(/^\d{1,2} [A-Z][a-z]{2,3} \d{4}$/);
  await expect(page.getByLabel('View date', { exact: true })).toHaveValue(/^\d{4}-\d{2}-\d{2}$/);

  // tile and sheet: the same status word and the same number; no «−0 pts»
  const tile = page.locator('.kb-kpi[data-metric="rev.churn"]');
  const value = (await tile.locator('.metric-value').innerText()).trim();
  const status = (await tile.locator('.badge').first().innerText()).split('·')[0].trim();
  await tile.click();
  const sheet = page.getByRole('dialog');
  const hero = sheet.locator('.g-drill-hero').first();
  await expect(hero.locator('.g-value')).toHaveText(value, { timeout: 20_000 });
  await expect(hero.locator('.g-status')).toHaveText(status);
  await expect(hero).not.toContainText(/[−-]0 pts/);
  // the groups are money, not «408,000%»
  await expect(sheet.locator('.g-bar-value').first()).toHaveText(/^\$/);

  // the operation: its worked formula in money; one level down the window reads forward with its years
  await sheet.locator('.g-tree-main', { hasText: 'Gross ARR churn' }).click();
  await expect(sheet.locator('.g-formula code')).toHaveText(/^\$[\d,]+ \/ \$[\d,]+ × 100 = [\d.]+%$/);
  await sheet.locator('.g-tree-main', { hasText: 'ARR lost' }).click();
  await expect(sheet.locator('.g-drill-expl')).toHaveText(/^Effective from \d{1,2} [A-Z][a-z]{2,3} \d{4} to \d{1,2} [A-Z][a-z]{2,3} \d{4}$/);
  // the events: who, what, when; the amount instead of «Unknown»; no ids, codes or timestamps
  const events = sheet.locator('.g-tree-node.is-record .g-tree-copy > span');
  await expect(events.first()).toHaveText(/^.+ · Contract terminated · \d{1,2} [A-Z][a-z]{2,3} \d{4}$/);
  await expect(sheet.locator('.g-tree-node.is-record .g-tree-value b').first()).toHaveText(/^\$[\d,]+$/);
  const text = await sheet.locator('.detail-scroll-body').innerText();
  for (const machine of [/sim\.[A-Z]/, /\b[a-z]+_[a-z]+\b/, /\d{4}-\d{2}-\d{2}/, /\bUnknown\b/, /known at/]) expect(text, `no ${machine} on the records level`).not.toMatch(machine);
  await expectViewport(page);
  await shot(page, 'churn-records', isMobile);
});

test('a view date without a catalogue set: the knowledge-base brief with a quiet note', async ({ page, request }) => {
  test.setTimeout(120_000);
  const dates = await (await request.get('api/kb/company/dates')).json() as { default: string };
  const earlier = new Date(Date.parse(`${dates.default}T12:00:00Z`) - 3 * 86_400_000).toISOString().slice(0, 10);
  await openBrief(page, `brief?asOf=${earlier}`);
  await expect(page.locator('.kb-brief')).toHaveAttribute('data-source', 'kb');
  await page.getByRole('button', { name: 'About this brief' }).click();
  await expect(page.getByRole('note')).toContainText(/this brief is computed from the knowledge base/);
  for (const area of AREAS) await expect(page.locator('.kb-area h2', { hasText: area })).toHaveCount(1);
});

test('fewer small texts: titles, numbers and the headline stay; intros, captions and notes are behind «Know more»', async ({ page }) => {
  test.setTimeout(90_000);
  await openBrief(page);
  await expect(page.locator('.kb-headline p').first()).toBeVisible();
  await expect(page.locator('.page-intro p.muted')).toHaveCount(0); // the intro line
  await expect(page.locator('.kb-footnote')).toHaveCount(0);
  await expect(page.locator('.kb-kpi .kb-caveat')).toHaveCount(0); // caveats are in the metric sheet
  await expect(page.locator('.kb-area-head p')).toHaveCount(0); // area questions
  await page.getByRole('button', { name: 'About money' }).click();
  await expect(page.getByRole('note')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('note')).toHaveCount(0);
  await expectViewport(page);
});

test('a simulated future: its brief says so, Ask keeps the branch, a why question is answered from its graph', async ({ page, request, isMobile }) => {
  test.setTimeout(240_000);
  const orgs = await (await request.get('api/kb/organizations')).json() as { branches?: { tenant: string; label: string; kind: string; from: string }[] }[];
  const branches = orgs[0]?.branches ?? [];
  test.skip(!branches.length, 'no simulated future is loaded in the knowledge base');
  const b = branches.find(x => x.kind === 'median') ?? branches[0];
  await openBrief(page, `brief?branch=${b.tenant}`);
  const banner = page.getByRole('status', { name: 'Simulation' });
  await expect(banner).toContainText(b.label);
  await expect(banner).toContainText('not real history');
  // its place: the true percentile among the scenarios when the day set gives it, else its order among the three
  // examples; never the nominal 10/50/90 of the tenant name
  await expect(banner).toHaveAttribute('data-place', /^(Lowest|Middle|Highest) of the three example days$|^≈ \d+(st|nd|rd|th) percentile of cash/);
  expect(b.label).not.toMatch(/median|percentile/i);
  await expect(page.locator('.kb-headline p').first()).toContainText('Simulated future');
  // its own catalogue brief (WP19's branch set, at its last day) or its own knowledge-base brief; never the history
  const brief = page.locator('.kb-brief');
  await expect(brief).toHaveAttribute('data-branch', b.tenant);
  if (await brief.getAttribute('data-source') === 'catalogue') await expect(brief).toHaveAttribute('data-set', `branch-metrics-${b.tenant}`);
  else await expect(brief).toHaveAttribute('data-source', 'kb');
  await expect(page.locator('.kb-headline')).not.toContainText('grounds that have since changed');
  await expect(page.locator('.kb-kpi .kb-kpi-status, .kb-kpi').filter({ hasText: /^Unknown$/ })).toHaveCount(0);
  // the view dates are the branch's: they start at its fork date
  await expect(page.getByLabel('View date', { exact: true })).toHaveAttribute('min', b.from);
  await expectViewport(page);
  await shot(page, 'branch-brief', isMobile, true);

  // Ask keeps the branch; the question goes to the branch tenant
  if (isMobile) await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('link', { name: 'Ask the knowledge base' }).click();
  await expect(page).toHaveURL(new RegExp(`/ask\\?.*branch=${b.tenant}`));
  await expect(banner).toBeVisible();
  // the branch's own example questions, or none: never the company's history questions
  await expect(page.locator('.kb-examples[data-examples="company"]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Why did our last client to give notice/ })).toHaveCount(0);
  const answered = page.waitForResponse(r => r.url().includes('/ask') && r.url().includes(`branch=${b.tenant}`), { timeout: 180_000 });
  await page.getByLabel('Question', { exact: true }).fill('Why did incidents rise?');
  await page.getByRole('button', { name: 'Ask', exact: true }).click();
  const res = await answered;
  expect(res.status()).toBe(200);
  const answer = await res.json() as { answer: string; asOf: string };
  expect(answer.answer.length).toBeGreaterThan(0);
  expect(answer.asOf >= b.from).toBeTruthy();
  await expect(page.locator('.kb-answer')).toBeVisible({ timeout: 30_000 });
  await shot(page, 'branch-ask', isMobile, true);

  // back to the company: the banner goes, and so does the branch
  await banner.getByRole('link', { name: 'Back to the company' }).click();
  await expect(page).not.toHaveURL(/branch=/);
  await expect(page.getByRole('status', { name: 'Simulation' })).toHaveCount(0);
});

test('a person dossier lists the objects of the person, each opening its own dossier; never the person again', async ({ page, isMobile }) => {
  test.setTimeout(120_000);
  await page.goto('brief?entity=person.lw-e06');
  const sheet = page.getByRole('dialog');
  await expect(sheet.getByRole('heading', { name: 'Daniel Kowalski' })).toBeVisible({ timeout: 60_000 });
  await expect(sheet.locator('.kb-people span', { hasText: 'Daniel Kowalski' })).toHaveCount(0); // the bug: «Assignee Daniel Kowalski» × N
  const groups = sheet.locator('.kb-related-group');
  await expect(groups.first()).toBeVisible();
  await expect(groups.first().locator('h3')).toContainText(/^(Assigned|Authored|On call for|Took part in|Meetings)/);
  // the objects: one-line bullets (the kind, status and date in the hint), the newest 5 until «All N»
  const items = groups.first().locator('.kb-source-list li');
  expect(await items.count()).toBeGreaterThan(0);
  expect(await items.count()).toBeLessThanOrEqual(5);
  const first = items.first().getByRole('button');
  await expect(first).toHaveAttribute('title', /\n.+/);
  await shot(page, 'dossier-person', isMobile);
  // an object of a group opens its own dossier, with the person on the trail; Back returns to the person
  await first.click();
  await expect(page).not.toHaveURL(/entity=person\.lw-e06/);
  await expect(sheet.getByRole('heading', { name: 'Daniel Kowalski' })).toHaveCount(0);
  await expect(sheet.getByRole('navigation', { name: 'Dossiers opened before' })).toContainText('Daniel Kowalski');
  await sheet.getByRole('button', { name: 'Back to Daniel Kowalski' }).click();
  await expect(sheet.getByRole('heading', { name: 'Daniel Kowalski' })).toBeVisible({ timeout: 30_000 });
});

test('a person dossier on a simulated future: what the person did there, marked as simulation', async ({ page, request, isMobile }) => {
  test.setTimeout(120_000);
  const orgs = await (await request.get('api/kb/organizations')).json() as { branches?: { tenant: string; kind: string }[] }[];
  const b = (orgs[0]?.branches ?? []).find(x => x.kind === 'median') ?? orgs[0]?.branches?.[0];
  test.skip(!b, 'no simulated future is loaded in the knowledge base');
  await page.goto(`brief?branch=${b!.tenant}&entity=person.lw-e13`);
  const sheet = page.getByRole('dialog');
  await expect(sheet.getByText(/^SIMULATION · PERSON/)).toBeVisible({ timeout: 60_000 });
  await expect(sheet.locator('.detail-bottom-bar')).toContainText('SIMULATION');
  await expect(sheet.locator('.kb-related-group').first()).toBeVisible();
  await expect(sheet.locator('.kb-people span', { hasText: 'Rhys Llewellyn' })).toHaveCount(0);
  await shot(page, 'dossier-person-branch', isMobile);
});

test('Ask with ?q= (the «Ask this future» chips): asked once on arrival, the chain shown, q dropped, branch kept', async ({ page, request }) => {
  test.setTimeout(240_000);
  const tenant = 'company-b-pay_tech_debt-p50';
  const orgs = await (await request.get('api/kb/organizations')).json() as { branches?: { tenant: string }[] }[];
  test.skip(!(orgs[0]?.branches ?? []).some(b => b.tenant === tenant), `${tenant} is not loaded in the knowledge base`);
  let asked = 0;
  page.on('request', r => { if (r.method() === 'POST' && r.url().includes('/ask')) asked++; });
  await page.goto(`ask?branch=${tenant}&q=Why%20did%20incidents%20fall%20in%20February%3F`);
  await expect(page.getByLabel('Question', { exact: true })).toHaveValue('Why did incidents fall in February?', { timeout: 60_000 });
  const answer = page.locator('.kb-answer');
  await expect(answer).toContainText('Use 15% of engineering capacity for technical debt', { timeout: 180_000 });
  // for people (M5): the subject with its date, the chain as linked titles, the root cause; nothing technical in sight
  await expect(answer).toHaveAttribute('data-answer', 'why');
  await expect(answer.locator('.kb-why-lead')).toContainText(/Incidents fell in February 2027 · \d+ Feb 2027/);
  await expect(answer.locator('.kb-why-chains li').first()).toContainText('←');
  await expect(answer.locator('.kb-why-root')).toContainText(/Root cause: decision “Use 15% of engineering capacity for technical debt” \(\d+ Oct 2026, /);
  expect(await answer.innerText()).not.toMatch(/@\d|\bsim\.[A-Z]|\d{4}-\d\d-\d\dT|Knowledge cut|Interpretation:|matched:/);
  const why = answer.locator('.kb-why-chains .kb-why-link').filter({ hasText: 'Debt work shows an effect' }).first();
  if (await why.count()) { await why.click(); await expect(page.getByRole('dialog')).toContainText('Debt work shows an effect'); await page.keyboard.press('Escape'); }
  await expect(page).not.toHaveURL(/[?&]q=/);
  await expect(page).toHaveURL(new RegExp(`branch=${tenant}`));
  expect(asked).toBe(1);
  // a reload does not ask again
  await page.reload();
  await expect(page.getByRole('heading', { level: 1 })).toContainText('What do you want to know', { timeout: 60_000 });
  await expect(page.locator('.kb-answer')).toHaveCount(0);
  expect(asked).toBe(1);
});

test('every company question chip answers about the record it names, with declared causes (M6)', async ({ request, isMobile }) => {
  test.skip(isMobile, 'an API check: once is enough');
  test.setTimeout(240_000);
  const orgs = await (await request.get('api/kb/organizations')).json() as { id: string; askExamples?: { ceo?: string[] } }[];
  const chips = orgs[0]?.askExamples?.ceo ?? [];
  expect(chips.length).toBeGreaterThanOrEqual(6);
  // the record each chip is about: a word of its title
  const about: [RegExp, RegExp][] = [[/Brightwell/, /Brightwell/], [/Maple Energy/, /Maple Energy/], [/Vale Homes/, /Vale Homes/], [/auth incident/, /^auth: GPU inference timeouts$/],
    [/billing engine/, /^billing-engine: payment run failures$/], [/Frontend Engineer/, /Requisition .*Frontend Engineer/], [/DevOps/, /DevOps Engineer/]];
  for (const q of chips) {
    const res = await request.post(`api/kb/${orgs[0].id}/ask`, { data: { question: q }, timeout: 180_000 });
    expect(res.status(), q).toBe(200);
    const a = await res.json() as { why: { subject: { title: string }; chains: unknown[] } | null; answer: string };
    expect(a.why, `${q}: a why answer`).not.toBeNull();
    expect(a.why!.chains.length, `${q}: declared causes`).toBeGreaterThan(0);
    const want = about.find(([k]) => k.test(q))?.[1];
    if (want) expect(a.why!.subject.title, q).toMatch(want);
  }
});

test('a branch the knowledge base does not list: a clear state and a way back', async ({ page }) => {
  await page.goto('brief?branch=company-b-no_such_fork-p50');
  await expect(page.getByRole('heading', { name: 'This simulated future is not in the knowledge base' })).toBeVisible({ timeout: 60_000 });
  await page.getByRole('link', { name: 'Back to the company' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText(CEO, { timeout: 60_000 });
  await expect(page).not.toHaveURL(/branch=/);
});

test('data sources show the real connection state', async ({ page, isMobile }) => {
  await page.goto('sources');
  await expect(page.getByRole('heading', { name: /Where the brief comes from/ })).toBeVisible();
  await expect(page.locator('.kb-source').first()).toContainText('Connected', { timeout: 20_000 });
  await expectViewport(page);
  await shot(page, 'kb-sources', isMobile, true);
});

test('one view date: the header date drives the brief, «Ask» and the dossier', async ({ page, isMobile }) => {
  test.setTimeout(180_000);
  await openBrief(page);
  const date = page.getByLabel('View date', { exact: true });
  const last = await date.inputValue(); // the last day of data by default
  const earlier = new Date(Date.parse(`${last}T12:00:00Z`) - 7 * 86_400_000).toISOString().slice(0, 10);

  // a week earlier: the brief moves, the date stays in the URL across pages
  await date.fill(earlier);
  await expect(page).toHaveURL(new RegExp(`asOf=${earlier}`));
  if (isMobile) await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('link', { name: 'Ask the knowledge base' }).click();
  await expect(page).toHaveURL(new RegExp(`/ask\\?.*asOf=${earlier}`));
  await expect(date).toHaveValue(earlier);

  await page.getByLabel('Question', { exact: true }).fill('Which meetings did the team hold internally last week?');
  await page.getByRole('button', { name: 'Ask', exact: true }).click();
  const plan = page.getByLabel('How the question was understood');
  await expect(plan).toBeVisible({ timeout: 120_000 });
  await shot(page, 'kb-ask-asof', isMobile, true);

  // moving the date marks the answer as asked for another day
  await date.fill(last);
  await expect(page.getByRole('button', { name: /Ask again as of/ })).toBeVisible();
  await expect(page).not.toHaveURL(/asOf=/); // the default date is not kept in the URL
});
