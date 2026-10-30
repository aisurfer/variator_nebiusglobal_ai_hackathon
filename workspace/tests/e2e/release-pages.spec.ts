import { expect, test, type Page } from '@playwright/test';
import { app } from './app';

// Release quality of every page (WP25), at 1440 px and 390 px: no console error, no failed request, no horizontal page
// scroll, the first content in under 3 s, and people's words: no machine ids, sizes, «USD», «futures», emoji or
// placeholders. Needs the whole stack (the knowledge base with the day branches, the VARIATOR API):
//   KB_E2E=1 PLAYWRIGHT_BASE_URL=http://127.0.0.1:8088 npx playwright test release-pages.spec.ts
test.skip(!process.env.KB_E2E, 'set KB_E2E=1 with the whole MVP stack running');
test.setTimeout(120_000);

const BRANCH = 'company-b-pay_tech_debt-p50';
interface Target { id: string; url: string; ready: string; dialog?: boolean; clean?: boolean; answer?: boolean }
const PAGES: Target[] = [
  { id: 'brief', url: '/brief', ready: '.kb-kpi[data-metric]' },
  { id: 'metric sheet', url: '/brief?metric=fin.gross_margin', ready: '[role=dialog] .g-drill-hero, [role=dialog] .g-formula', dialog: true },
  { id: 'actors', url: '/actors?actor=CL-001', ready: '[data-tour="actor-card"] h2' },
  { id: 'risks', url: '/actors?tab=risks', ready: '[data-tour="risks-register"]' },
  { id: 'ask', url: '/ask', ready: '.kb-examples .button' },
  { id: 'sources', url: '/sources', ready: '.kb-source' },
  { id: 'client dossier', url: '/brief?entity=org.CL-001', ready: '[role=dialog] .kb-related-group', dialog: true },
  { id: 'invoice dossier', url: '/brief?entity=sim.INV-01090', ready: '[role=dialog] h1', dialog: true },
  { id: 'cases', url: '/cases', ready: '.cs-case-card' },
  { id: 'case B', url: '/cases/company_best_year', ready: '[data-testid=leader]', clean: true },
  { id: 'case A', url: '/cases/delivery_best_way', ready: '[data-testid=leader]', clean: true },
  { id: 'branch brief', url: `/brief?branch=${BRANCH}`, ready: '.kb-headline' },
  { id: 'branch dossier', url: `/brief?branch=${BRANCH}&entity=person.lw-e13`, ready: '[role=dialog] .kb-related-group', dialog: true },
  { id: 'branch ask: the demo question', url: `/ask?branch=${BRANCH}&q=${encodeURIComponent('Why did incidents fall in February?')}`, ready: '.kb-answer', answer: true },
];
/** Texts a CEO should never read on a page. */
const RAW: [string, RegExp][] = [
  ['a machine size', /\bsize \d+\.\d/i],
  ['«USD» instead of $', /\bUSD\b/],
  ['«futures» instead of scenarios', /\bfutures\b/i],
  ['an emoji', /\p{Extended_Pictographic}/u],
  ['a placeholder', /\b(undefined|NaN)\b|\[object Object\]|\{\w+\}/],
  ['a snake_case id', /\b[a-z]+_[a-z0-9]+(?:_[a-z0-9]+)*\b/],
  ['a dotted id', /\b(?:sim|org|person|fin|rev|ops|cust|ppl)\.[A-Za-z]/],
  ['a machine timestamp', /\d{4}-\d\d-\d\dT\d\d:\d\d/],
];

async function watch(page: Page) {
  const problems: string[] = [];
  // WP27: every request of the app to its own site stays under the base (/workspace/)
  const base = new URL(test.info().project.use.baseURL ?? page.url());
  page.on('request', r => { const u = new URL(r.url()); if (u.host === base.host && !u.pathname.startsWith(base.pathname)) problems.push(`outside ${base.pathname}: ${r.method()} ${u.pathname}`); });
  page.on('console', m => { if (m.type() === 'error') problems.push(`console: ${m.text().slice(0, 160)}`); });
  page.on('pageerror', e => problems.push(`page error: ${e.message.slice(0, 160)}`));
  page.on('response', r => { if (r.status() >= 400) problems.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
  page.on('requestfailed', r => { if (!/ERR_ABORTED/.test(r.failure()?.errorText ?? '')) problems.push(`failed ${new URL(r.url()).pathname}: ${r.failure()?.errorText}`); });
  return problems;
}

for (const p of PAGES) {
  test(`${p.id}: no errors, no horizontal scroll, the first content in time, people's words`, async ({ page, isMobile }) => {
    if (p.clean) {
      await page.goto(app(p.url));
      await page.evaluate(() => { localStorage.clear(); for (const id of ['company_best_year', 'delivery_best_way']) localStorage.setItem(`variator:tour:${id}`, 'dismissed'); });
    }
    const problems = await watch(page);
    const t0 = Date.now();
    await page.goto(app(p.url));
    await expect(page.locator(p.ready).first()).toBeVisible({ timeout: p.answer ? 60_000 : 30_000 });
    const first = Date.now() - t0;
    if (!p.answer && !p.dialog) expect.soft(first, 'the first content in under 3 s (NFR7)').toBeLessThan(3_000);
    if (p.clean) await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-status', 'ready', { timeout: 30_000 });
    await page.waitForTimeout(1_500); // late requests and renders
    expect.soft(problems, 'console errors, failed requests and requests outside the base').toEqual([]);
    const outside = await page.$$eval('a[href^="/"]', as => as.map(a => a.getAttribute('href') ?? '').filter(h => !h.startsWith('/workspace/')));
    expect.soft(outside, 'every in-app link under /workspace/').toEqual([]);
    const size = await page.evaluate(() => ({ client: document.documentElement.clientWidth, doc: document.documentElement.scrollWidth }));
    expect.soft(size.doc, `no horizontal page scroll at ${size.client} px`).toBeLessThanOrEqual(size.client + 1);
    const text = await page.locator(p.dialog ? '[role=dialog]' : '.main-content').first().innerText();
    for (const [name, re] of RAW) {
      const m = re.exec(text);
      expect.soft(m ? text.slice(Math.max(0, m.index - 60), m.index + 60).replace(/\s+/g, ' ') : null, `${name} on ${isMobile ? 'a phone' : 'a desktop'}`).toBeNull();
    }
  });
}
