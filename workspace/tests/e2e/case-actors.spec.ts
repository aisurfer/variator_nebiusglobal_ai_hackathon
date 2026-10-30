import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';

// WP23: case B on the actor simulation. Needs the workspace dev servers and a VARIATOR API that serves the future set
// of case B (and WP18's month route for the rerun):
//   VARIATOR_ARTIFACTS_DIR=/tmp/variator-1000/mvp-artifacts/future uv run uvicorn variator.web.app:app --port <api>
//   HOST=127.0.0.1 PORT=<srv> VARIATOR_API_URL=http://127.0.0.1:<api> npm run dev:server
//   API_PORT=<srv> VITE_PORT=<ui> npx vite --host 127.0.0.1
//   PLAYWRIGHT_BASE_URL=http://127.0.0.1:<ui> npx playwright test case-actors.spec.ts
// Without an actor set every test but the fallback is skipped.
// paths relative to the base URL (<origin>/workspace/, playwright.config.ts); MONTH: the end of the route's URL
const CASE = 'cases/company_best_year';
const MONTH_API = 'api/variator/cases/company_best_year/month', MONTH = `/${MONTH_API}`;

test.setTimeout(120_000);

async function open(page: Page, path = CASE) {
  await page.goto(path);
  await page.waitForLoadState('domcontentloaded');
  // a clean browser that has answered the first-visit offer of the guided tour (the page may still be settling: retry)
  for (let i = 0; i < 3; i++) {
    try { await page.evaluate(() => { localStorage.clear(); for (const id of ['company_best_year', 'delivery_best_way']) localStorage.setItem(`variator:tour:${id}`, 'dismissed'); }); break; }
    catch { await page.waitForTimeout(500); }
  }
  await page.goto(path);
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
}
async function hasActors(page: Page) {
  const r = await page.request.get('api/variator/artifacts');
  if (!r.ok()) return false;
  const sets = ((await r.json()) as { sets: { manifest?: { schema?: string; id?: string } }[] }).sets;
  return sets.some(s => s.manifest?.schema === 'variator.future/1' && s.manifest.id === 'future:company_best_year');
}
const config = JSON.parse(readFileSync(new URL('../../config/cases/company_best_year.json', import.meta.url), 'utf8')) as { forks: { id: string; label: string }[] };
const label = (id: string) => config.forks.find(f => f.id === id)!.label;
/** The forks with an actor week tree (the two best of the actor ranking), as people read them. */
async function treeForks(page: Page): Promise<{ id: string; label: string }[]> {
  const sets = ((await (await page.request.get('api/variator/artifacts')).json()) as { sets: { id: string; manifest?: { schema?: string; id?: string } }[] }).sets;
  const set = sets.find(s => s.manifest?.schema === 'variator.future/1' && s.manifest.id === 'future:company_best_year')!;
  const month = await (await page.request.get(`api/variator/artifacts/${set.id}/month.json`)).json() as { weekTrees: Record<string, unknown> };
  return Object.keys(month.weekTrees).map(id => ({ id, label: label(id) }));
}
async function hasMonthRoute(page: Page) {
  const r = await page.request.get(MONTH_API);
  return r.status() !== 404 && r.status() < 500;
}
const at = (page: Page, id: string) => page.evaluate(i => document.getElementById(i)!.scrollIntoView({ block: 'start' }), id);
/** Keep forks for the week level (and one for the day level) at the review gates. */
async function keep(page: Page, week: string[], day?: string) {
  await at(page, 'week-review');
  for (const f of week) {
    const box = page.getByTestId('gate-week').getByRole('checkbox', { name: new RegExp(f) });
    if (await box.getAttribute('aria-checked') !== 'true') await box.click();
  }
  if (day) {
    await at(page, 'day-review');
    const box = page.getByTestId('gate-day').getByRole('checkbox', { name: new RegExp(day) });
    if (await box.getAttribute('aria-checked') !== 'true') await box.click();
  }
}

test('the verdict, the fans and the risk calendar come from the actors', async ({ page }) => {
  await open(page);
  test.skip(!(await hasActors(page)), 'no actor set of case B on this API');
  const v = page.getByTestId('actor-verdict');
  await expect(v).toHaveAttribute('data-status', 'ready', { timeout: 30_000 });
  await expect(v).toHaveAttribute('data-state', 'defaults');
  await expect(page.getByTestId('actor-state')).toHaveText('For the defaults');
  // «Rerun on actors» says in words why it cannot run now (not only in a tooltip)
  await expect(page.getByTestId('actor-rerun')).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByTestId('actor-rerun-why')).toBeVisible();
  await expect(page.getByTestId('actor-rerun-why')).toHaveText((await hasMonthRoute(page)) ? 'Up to date: the actors already answered for this page. Edit a fork, an event or a priority to rerun.'
    : 'Not available here: the actor engine does not answer on this server. The verdict shown is for the default inputs.');
  await expect(page.getByTestId('actor-leader')).not.toBeEmpty();
  const actorLeader = await page.getByTestId('actor-leader').innerText();
  await expect(page.getByTestId('actor-pbest')).toHaveText(/^\d+%$/);
  // a runner-up, or (a full tie within the meaningful differences) the two together
  await expect(page.getByTestId('actor-runner').or(page.getByTestId('actor-tie')).first()).toBeVisible();
  if (await page.getByTestId('actor-tie').count()) {
    await expect(page.getByTestId('actor-tie')).toContainText(/equal to .+together best in \d+%/);
    // the same cards as the formula answer: «Equal choices», or a «Close call» when a third fork wins more scenarios
    // than the tied pair together
    const card = page.getByTestId('actor-equal-choices').or(page.getByTestId('actor-close-call'));
    await expect(card.first()).toBeVisible();
    if (await page.getByTestId('actor-equal-choices').count()) await expect(page.getByTestId('actor-equal-choices')).toContainText(/are equal within the meaningful difference on .+ wins the tie/);
    else await expect(page.getByTestId('actor-close-call')).toContainText(/comes out on top/);
  }
  else await expect(page.getByTestId('actor-runner')).toContainText('Runner-up');
  // the tradeoff of the engine, or the tie within the meaningful difference (a line, or the «Equal choices» card)
  await expect(page.getByTestId('actor-tradeoff').or(page.getByTestId('actor-ties')).or(page.getByTestId('actor-equal-choices')).first()).toBeVisible();
  if (await page.getByTestId('actor-ties').count()) await expect(page.getByTestId('actor-ties')).toContainText(/^Equal to .+ within the meaningful difference on: .+\.$/);
  else if (await page.getByTestId('actor-tradeoff').count()) await expect(page.getByTestId('actor-tradeoff')).toContainText(/is better on /);
  // the source badge and its tooltip
  const badge = page.getByTestId('verdict-source');
  await expect(badge).toHaveAttribute('data-source', 'actors');
  await badge.locator('button').hover();
  await expect(page.getByRole('tooltip')).toContainText(/Simulated on your company's actors · 1,000 scenarios · learned from 12 months of history/);
  // Over time: fans with the 5–95% band, the headline of the calendar
  await at(page, 'overtime');
  await expect(page.locator('#overtime [data-testid=future-source]')).toHaveAttribute('data-source', 'actors');
  await expect(page.getByTestId('fans')).toHaveAttribute('data-source', 'actors');
  // the default forks of the charts and the calendar follow the actor ranking (the answer)
  const leaderId = config.forks.find(f => f.label === actorLeader)!.id;
  await expect(page.getByTestId(`fan-${leaderId}`)).toBeVisible();
  await expect(page.getByTestId('risk-calendar').locator('select')).toHaveValue(leaderId);
  await expect(page.locator('#overtime .cs-legend')).toContainText('5–95%');
  await expect(page.locator('#overtime .cs-fan .recharts-area').first()).toBeVisible();
  // a link of the chain on the fans
  await page.getByTestId('chain-metric').selectOption({ label: 'Team load' });
  await expect(page.locator('#overtime .cs-fan .recharts-line').first()).toBeVisible();
  const cal = page.getByTestId('risk-calendar');
  await expect(cal).toHaveAttribute('data-source', 'actors');
  await expect(page.getByTestId('calendar-headline')).toContainText(/1,000 scenarios/);
  // the goals of the actor result (WP18 options[f].goals and milestones), not the formula's
  const goals = page.getByTestId('goals');
  if (await goals.count()) {
    await expect(goals).toHaveAttribute('data-source', 'actors');
    await expect(goals.locator('.cs-pill').first()).toHaveText(/\d+%/);
    await expect(goals).toContainText('Cash at year end at least the goal');
  }
  await expect(cal.locator('tbody tr')).toHaveCount(await cal.locator('tbody tr').count());
  expect(await cal.locator('tbody tr').count()).toBeGreaterThanOrEqual(5);
  await expect(cal.locator('tr[data-row=burnout]')).toBeVisible();
  await expect(cal.locator('.cs-cell-first').first()).toBeAttached();
});

test('after an edit the page reruns on actors by itself: recomputing, then current', async ({ page }) => {
  await open(page);
  test.skip(!(await hasActors(page)) || !(await hasMonthRoute(page)), 'no actor set or no month route on this API');
  const v = page.getByTestId('actor-verdict');
  await expect(v).toHaveAttribute('data-state', 'defaults', { timeout: 30_000 });
  // an edit: a world hypothesis; the page posts the new state to the actors without a click
  const posted = page.waitForRequest(r => r.url().endsWith(MONTH) && r.method() === 'POST' && !(r.postDataJSON() as { include?: string[] }).include, { timeout: 30_000 });
  await at(page, 'world');
  await page.locator('.cs-hyp', { hasText: 'Price war' }).click();
  // meanwhile the previous actor numbers stay (dimmed, «Recomputing…»), never «stale» or the formula draft
  await expect(page.getByTestId('actor-recomputing').or(v.and(page.locator('[data-state=current]'))).first()).toBeVisible();
  await expect(v).not.toHaveAttribute('data-state', 'stale');
  await expect(page.getByTestId('goals-draft')).toHaveCount(0);
  const body = (await posted).postDataJSON() as { activePresets: string[]; forks: string[]; n: number; seed: number };
  expect(body.activePresets).toEqual(['price_war']);
  expect(body.forks.length).toBe(5); expect(body.n).toBe(1000); expect(body.seed).toBe(7);
  await expect(v).toHaveAttribute('data-state', 'current', { timeout: 30_000 });
  await expect(page.getByTestId('actor-recomputing')).toHaveCount(0);
  await expect(page.getByTestId('actor-state')).toHaveText('Current');
  await expect(page.getByTestId('actor-rerun-why')).toHaveText(/^Up to date: the actors already answered for this page\./);
  await expect(page.locator('#overtime [data-testid=future-source]')).toHaveAttribute('data-state', 'current');
  await expect(page.getByTestId('fans')).toHaveAttribute('data-source', 'actors');
  // one source per state: no formula goal numbers next to the actor result
  await expect(page.locator('[data-testid=goals][data-source=formula], [data-testid=goals][data-source=draft]')).toHaveCount(0);
  // at Week, the week of this state is computed on actors for the forks kept (include week, weekForks)
  const weekPosted = page.waitForRequest(r => r.url().endsWith(MONTH) && r.method() === 'POST' && !!(r.postDataJSON() as { include?: string[] }).include, { timeout: 60_000 });
  await keep(page, (await treeForks(page)).map(f => f.label));
  await at(page, 'week');
  const wb = (await weekPosted).postDataJSON() as { include?: string[]; weekForks?: string[] };
  expect(wb.include).toEqual(['week']); expect(wb.weekForks?.length).toBeGreaterThan(0);
  await expect(page.getByTestId('week-tree')).toHaveAttribute('data-source', 'actors');
  await expect(page.getByTestId('week-tree')).toHaveAttribute('data-kind', 'events', { timeout: 60_000 });
  await expect(page.locator('#week [data-testid=future-source]')).toHaveAttribute('data-state', 'current', { timeout: 60_000 });
  await expect(page.getByTestId('tree-source')).toHaveCount(0);
  // back to the defaults: the precomputed result again
  await at(page, 'world');
  await page.locator('.cs-hyp', { hasText: 'Price war' }).click();
  await expect(v).toHaveAttribute('data-state', 'defaults');
});

test('a failed rerun leaves a quiet message, the previous result and the formula draft', async ({ page }) => {
  // the month route fails on POST (its GET description still answers)
  await page.route(`**${MONTH}`, r => r.request().method() === 'POST' ? r.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: 'the learned actors are missing' } }) }) : r.fallback());
  await open(page);
  test.skip(!(await hasActors(page)), 'no actor set of case B on this API');
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-status', 'ready', { timeout: 60_000 });
  const leader = await page.getByTestId('actor-leader').innerText();
  await at(page, 'world');
  await page.locator('.cs-hyp', { hasText: 'Price war' }).click();
  // the automatic rerun fails: a quiet message, the actor numbers of the previous state, marked stale
  await at(page, 'answer');
  await expect(page.getByTestId('actor-error')).toContainText('the learned actors are missing', { timeout: 30_000 });
  await expect(page.getByTestId('actor-leader')).toHaveText(leader);
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-state', 'stale');
  await expect(page.getByTestId('actor-state')).toHaveText('Stale after your edits');
  // the goals fall back to the formula model, as a draft
  await expect(page.getByTestId('goals')).toHaveAttribute('data-source', 'draft');
  await expect(page.getByTestId('goals-draft')).toContainText('Draft after your edits');
  // the manual button stays as a retry (no reason beside it: it can run)
  await expect(page.getByTestId('actor-rerun')).toHaveAttribute('aria-disabled', 'false');
  await expect(page.getByTestId('actor-rerun-why')).toHaveCount(0);
  const retried = page.waitForRequest(r => r.url().endsWith(MONTH) && r.method() === 'POST');
  await page.getByTestId('actor-rerun').click();
  await retried;
  await expect(page.getByTestId('actor-error')).toBeVisible();
});

test('a server whose actor engine does not answer: «Rerun on actors» says so beside the button', async ({ page }) => {
  // the month route is missing (its GET answers 404): the precomputed verdict stays, the rerun cannot run
  await page.route(`**${MONTH}`, r => r.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' }));
  await open(page);
  test.skip(!(await hasActors(page)), 'no actor set of case B on this API');
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-status', 'ready', { timeout: 60_000 });
  await at(page, 'answer');
  await expect(page.getByTestId('actor-rerun')).toHaveAttribute('aria-disabled', 'true');
  await expect(page.getByTestId('actor-rerun-why')).toHaveText('Not available here: the actor engine does not answer on this server. The verdict shown is for the default inputs.');
  await expect(page.getByTestId('actor-rerun-why')).toBeVisible();
});

test('P(best) says which forks it is computed over, everywhere; the gates show the numbers of the fork cards', async ({ page }) => {
  await open(page);
  test.skip(!(await hasActors(page)), 'no actor set of case B on this API');
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-status', 'ready', { timeout: 60_000 });
  // the forks of the actor ranking, from the month file
  const sets = ((await (await page.request.get('api/variator/artifacts')).json()) as { sets: { id: string; manifest?: { schema?: string; id?: string } }[] }).sets;
  const set = sets.find(s => s.manifest?.schema === 'variator.future/1' && s.manifest.id === 'future:company_best_year')!;
  const ranked = ((await (await page.request.get(`api/variator/artifacts/${set.id}/month.json`)).json()) as { ranking: { options: { option: string; pBest: number }[] } }).ranking.options;
  const scope = `of ${ranked.length} forks`, pctOf = (p: number) => `${Math.round(p * 100)}%`;
  // the fork cards: «Rank 1 · best in 15% · of 5 forks»
  for (const o of ranked) await expect(page.locator('.cs-fork-card', { hasText: label(o.option) }).first().locator('.cs-fork-foot')).toContainText(`best in ${pctOf(o.pBest)} · ${scope}`);
  // the verdict and Compare
  await expect(page.getByTestId('actor-pbest-scope')).toHaveText(`· ${scope}`);
  await expect(page.getByTestId('pbest-scope')).toHaveText(scope);
  // the gates: the same P(best) as the fork cards (the page's numbers), out of all the forks, also where the gate lists
  // fewer of them
  await keep(page, ranked.slice(0, 2).map(o => label(o.option)));
  for (const gate of ['gate-week', 'gate-day']) {
    await at(page, gate === 'gate-week' ? 'week-review' : 'day-review');
    const rows = page.getByTestId(gate).locator('.cs-gate-list li');
    expect(await rows.count()).toBeGreaterThan(0);
    for (let i = 0; i < await rows.count(); i++) {
      const row = rows.nth(i), text = await row.innerText(), o = ranked.find(x => text.includes(label(x.option)))!;
      await expect(row.getByTestId('gate-pbest')).toContainText(`best in ${pctOf(o.pBest)}`);
      await expect(row.getByTestId('gate-pbest')).toContainText(scope);
    }
  }
  // the decision panel: the answer's leader and its P(best) with the scope (opened, never committed)
  await page.locator('.cs-decision-btn').first().click();
  await expect(page.locator('.cs-sheet .cs-decision')).toContainText(new RegExp(`The leader now: .+ · best in \\d+% of the scenarios \\(${scope}\\)`));
});

test('without an actor set the page falls back to the preview, with its badge', async ({ page }) => {
  await page.route('**/api/variator/artifacts', r => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sets: [] }) }));
  await open(page);
  const v = page.getByTestId('actor-verdict');
  await expect(v).toHaveAttribute('data-status', 'missing');
  await expect(page.getByTestId('verdict-source')).toHaveAttribute('data-source', 'preview');
  await at(page, 'overtime');
  await expect(page.locator('#overtime [data-testid=future-source]')).toHaveAttribute('data-source', 'preview');
  await expect(page.getByTestId('fans')).toHaveAttribute('data-source', 'preview');
  await expect(page.locator('#overtime .cs-legend')).toContainText('10–90%');
  await expect(page.getByTestId('risk-calendar')).toHaveAttribute('data-source', 'preview');
  await keep(page, ['Reduce tech debt']);
  await at(page, 'week');
  await expect(page.getByTestId('week-tree')).toHaveAttribute('data-source', 'preview');
});

// ------------------------------------------------------------------------------------------------ the week event tree
// month.json of WP18c carries an event tree for every fork (eventTrees); the live route computes them for the forks
// asked for (include week, weekForks). Screenshots go to $ACTORS_SHOTS when it is set.
const SHOTS = process.env.ACTORS_SHOTS;
const shot = async (page: Page, sel: string, name: string, isMobile: boolean) => { if (SHOTS) await page.locator(sel).first().screenshot({ path: `${SHOTS}/${name}-${isMobile ? 390 : 1440}.png` }); };
async function hasEventTrees(page: Page) {
  const sets = ((await (await page.request.get('api/variator/artifacts')).json()) as { sets: { id: string; manifest?: { schema?: string; id?: string } }[] }).sets;
  const set = sets.find(s => s.manifest?.schema === 'variator.future/1' && s.manifest.id === 'future:company_best_year');
  if (!set) return false;
  const m = await (await page.request.get(`api/variator/artifacts/${set.id}/month.json`)).json() as { eventTrees?: Record<string, unknown> };
  return Object.keys(m.eventTrees ?? {}).length === 5;
}

test('the week is an event tree: split labels, a node sheet, a scenario sheet', async ({ page, isMobile }) => {
  await open(page);
  test.skip(!(await hasEventTrees(page)), 'no event trees of case B on this API (WP18c)');
  await keep(page, ['Keep the plan', 'Reduce tech debt', 'Raise prices 8%']);
  await at(page, 'week');
  const card = page.getByTestId('week-tree');
  await expect(card).toHaveAttribute('data-kind', 'events');
  await expect(card).toHaveAttribute('data-source', 'actors');
  const tree = page.getByTestId('event-tree');
  expect(await tree.locator('[data-node]').count()).toBeGreaterThan(4);
  // the labels, desktop and phone
  await expect(tree.locator('.cs-enode-label').first()).toContainText(/\d+%$/);
  if (!isMobile) await expect(tree.locator('.cs-enode-label', { hasText: / · w\d+ · \d+%$/ }).first()).toBeAttached();
  await expect(tree.locator('.cs-enode-end').first()).toContainText(/^\$/);
  const drawn = await tree.evaluate(el => {
    const texts = [...el.querySelectorAll<SVGTextElement>('.cs-enode-label, .cs-enode-end')];
    const r = [...texts, ...el.querySelectorAll('.cs-epath-mark, .cs-dpath-letter')].map(t => (t as SVGGraphicsElement).getBoundingClientRect());
    // no two texts overlap (their drawn boxes)
    let overlaps = 0;
    for (let i = 0; i < r.length; i++) for (let j = i + 1; j < r.length; j++) if (!(r[i].right <= r[j].left + 0.5 || r[i].left >= r[j].right - 0.5 || r[i].bottom <= r[j].top + 0.5 || r[i].top >= r[j].bottom - 0.5)) overlaps++;
    // no line runs through a text: each stands on a plate that holds its glyphs, drawn after every path
    const paths = [...el.querySelectorAll('.cs-epath, .cs-dpath, .cs-enode')], last = paths[paths.length - 1];
    let bare = 0, under = 0;
    for (const t of texts) {
      const plate = t.previousElementSibling, g = t.getBBox(), p = plate?.classList.contains('cs-enode-plate') ? (plate as SVGRectElement).getBBox() : null;
      if (!p || g.x < p.x - 0.5 || g.x + g.width > p.x + p.width + 0.5 || g.y + 2 < p.y || g.y + g.height - 2 > p.y + p.height) bare++;
      if (last && (last.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING) === 0) under++;
    }
    return { overlaps, bare, under, n: texts.length };
  });
  expect(drawn.n).toBeGreaterThan(3);
  expect(drawn.overlaps).toBe(0);
  expect(drawn.bare, 'every label on its plate').toBe(0);
  expect(drawn.under, 'the labels above the paths').toBe(0);
  // words are never cut: a label too long for its ribbon reads short («GPU shortage · w23 · 24%») or not at all
  expect(await tree.locator('.cs-enode-label', { hasText: '…' }).count()).toBe(0);
  for (const k of ['pessimistic', 'median', 'optimistic']) await expect(tree.getByTestId(`leaf-${k}`)).toBeAttached();
  await shot(page, '[data-testid=week-tree]', 'event-tree', isMobile);
  if (!isMobile) await card.getByRole('radio', { name: 'Team load' }).click();
  // a split by an event: why, the share, the bands, the scenarios listed on it
  const split = tree.locator('[data-node]').filter({ hasNotText: /^(Without|Other events|All scenarios|No such event)/ }).first();
  await split.dispatchEvent('click');
  const ns = page.getByTestId('tree-node-sheet');
  await expect(ns).toContainText('Why it splits.');
  await expect(ns).toContainText('Share of all scenarios');
  await expect(ns.locator('.cs-multiple')).toHaveCount(4);
  await shot(page, '.cs-sheet', 'event-node-sheet', isMobile);
  const listed = ns.getByRole('button', { name: /Scenario \d+|example/ }).first();
  if (await listed.count()) {
    await listed.click();
    await expect(page.getByTestId('tree-path-sheet')).toContainText('What happened');
  }
  await page.keyboard.press('Escape');
  // the tree's median path: by cash at the end of the year, week by week (no day actions: it is not a day example)
  await tree.getByTestId('leaf-median').dispatchEvent('click');
  const ps = page.getByTestId('tree-path-sheet');
  await expect(ps).toContainText('Week by week');
  await expect(ps.getByTestId('path-basis')).toContainText('by cash at the end of the year');
  await expect(ps.getByTestId('open-example-days')).toHaveCount(0);
  await shot(page, '.cs-sheet', 'event-path-sheet', isMobile);
  await page.keyboard.press('Escape');
  // the pinned day example: apart from P/M/O, with its basis, the example days and the KB branch
  // one compact legend: the tree's P, M, O and, apart, the day examples with their basis
  await expect(page.getByTestId('tree-examples')).toContainText('P, M, O by cash at the end of the year');
  await expect(page.getByTestId('tree-day-examples')).toContainText(/Day examples by cash (on |at the end of the example days)/);
  await expect(page.getByTestId('tree-examples')).not.toContainText('futures');
  await tree.getByTestId('day-median').dispatchEvent('click');
  await expect(ps.getByTestId('path-basis')).toContainText(/^Day example · median by /);
  await expect(ps.getByTestId('path-branch')).toBeVisible();
  await shot(page, '.cs-sheet', 'event-day-sheet', isMobile);
  await ps.getByTestId('open-example-days').click();
  await expect(ps).toHaveCount(0);
  await expect(page.locator('[data-tour="day-examples"]')).toBeVisible();
});

test('after edits, every fork kept for the week is computed on actors (no preview)', async ({ page, isMobile }) => {
  test.setTimeout(150_000);
  await open(page);
  test.skip(!(await hasEventTrees(page)) || !(await hasMonthRoute(page)), 'no event trees or no month route on this API');
  await at(page, 'world');
  await page.locator('.cs-hyp', { hasText: 'Price war' }).click();
  // the month reruns on actors by itself; the week of the kept forks is still asked for by Week
  await expect(page.getByTestId('actor-verdict')).not.toHaveAttribute('data-state', 'stale');
  const forks = ['Keep the plan', 'Reduce tech debt', 'Raise prices 8%'];
  const bodies: { include?: string[]; weekForks?: string[]; activePresets: string[] }[] = [];
  page.on('request', r => { if (r.url().endsWith(MONTH) && r.method() === 'POST') bodies.push(r.postDataJSON()); });
  await keep(page, forks);
  await at(page, 'week');
  const card = page.getByTestId('week-tree');
  await expect(card).toHaveAttribute('data-kind', /computing|events/);
  await expect(card).toHaveAttribute('data-kind', 'events', { timeout: 60_000 });
  // the week of the kept forks is computed on actors for this state: include week, the forks without a tree
  await expect.poll(() => [...new Set(bodies.flatMap(b => b.weekForks ?? []))].sort(), { timeout: 60_000 }).toEqual(['pay_tech_debt', 'raise_prices_8', 'status_quo']);
  // every request is for the edited state; the week's own requests carry include week (the auto month rerun does not)
  for (const b of bodies) expect(b.activePresets).toEqual(['price_war']);
  for (const b of bodies.filter(x => x.weekForks)) expect(b.include).toEqual(['week']);
  for (const f of forks) {
    await page.locator('#week').getByRole('radio', { name: f, exact: true }).click();
    await expect(card).toHaveAttribute('data-kind', 'events', { timeout: 60_000 });
    await expect(card).toHaveAttribute('data-source', 'actors');
  }
  await expect(page.getByTestId('tree-source')).toHaveCount(0);
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-state', 'current');
  await shot(page, '[data-testid=week-tree]', 'week-after-edits', isMobile);
});

test('case A: the event tree steps by months', async ({ page, isMobile }) => {
  const failed: string[] = [];
  page.on('response', r => { if (r.url().includes('/api/variator/artifacts/') && r.status() >= 400) failed.push(`${r.status()} ${r.url()}`); });
  await open(page, 'cases/delivery_best_way');
  const sets = ((await (await page.request.get('api/variator/artifacts')).json()) as { sets: { id: string; manifest?: { schema?: string; id?: string } }[] }).sets;
  const set = sets.find(x => x.manifest?.schema === 'variator.future/1' && x.manifest.id === 'future:delivery_best_way');
  const m = set ? await (await page.request.get(`api/variator/artifacts/${set.id}/month.json`)).json() as { eventTrees?: Record<string, unknown> } : {};
  test.skip(!Object.keys((m as { eventTrees?: object }).eventTrees ?? {}).length, 'no event trees of case A on this API');
  await at(page, 'week-review');
  const box = page.getByTestId('gate-week').getByRole('checkbox').first();
  if (await box.getAttribute('aria-checked') !== 'true') await box.click();
  await at(page, 'week');
  const card = page.getByTestId('week-tree');
  await expect(card).toHaveAttribute('data-kind', 'events');
  await expect(page.getByTestId('event-tree').locator('.cs-tree-week', { hasText: /^m\d+$/ }).first()).toBeAttached();
  await shot(page, '[data-testid=week-tree]', 'event-tree-caseA', isMobile);
  await page.getByTestId('event-tree').locator('[data-node]').nth(1).dispatchEvent('click');
  await expect(page.getByTestId('tree-node-sheet')).toContainText('Months');
  await expect(page.getByTestId('tree-node-sheet')).toContainText('Money at the end');
  // only the files the sets list are asked for (no 404 in the console)
  expect(failed.filter(u => !/\/day\//.test(u))).toEqual([]);
});

test('an unrelated edit keeps a fork\'s week tree: turning off Reserve GPU leaves Keep the plan\'s tree', async ({ page }) => {
  await open(page);
  test.skip(!(await hasEventTrees(page)), 'no event trees of case B on this API (WP18c)');
  await keep(page, ['Keep the plan']);
  await at(page, 'week');
  const card = page.getByTestId('week-tree'), tree = page.getByTestId('event-tree');
  await expect(card).toHaveAttribute('data-kind', 'events');
  const splits = () => tree.locator('[data-node] > title').allTextContents();
  const before = await splits();
  expect(before.length).toBeGreaterThan(2);
  // another fork off: the actors rerun the month (ranking), but Keep the plan's tree stays the precomputed one
  const weekAsked: string[][] = [];
  page.on('request', r => { if (r.url().endsWith(MONTH) && r.method() === 'POST') { const b = r.postDataJSON() as { weekForks?: string[] }; if (b.weekForks) weekAsked.push(b.weekForks); } });
  await at(page, 'forks');
  // the switch's input is drawn by its label (visually hidden): a click on the input itself
  const sw = page.getByRole('checkbox', { name: 'Compare Reserve GPU capacity' });
  await sw.evaluate((el: HTMLInputElement) => el.click());
  await expect(sw).not.toBeChecked();
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-state', 'current', { timeout: 30_000 });
  await at(page, 'week');
  await expect(card).toHaveAttribute('data-kind', 'events');
  await page.waitForTimeout(1500);
  expect(await splits()).toEqual(before);
  expect(weekAsked.flat()).not.toContain('status_quo');
});
