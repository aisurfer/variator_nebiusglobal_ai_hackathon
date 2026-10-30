import { readFileSync } from 'node:fs';
import { expect, test, type Page } from '@playwright/test';
import { app } from './app';

// The case pages on the case engine: case B (Best year, 12 months) and case A (Best way to deliver, 24 months), both
// read from config/cases. Needs the workspace dev servers only (the case runs in the browser; the shell reads the
// company from the workspace server):
//   PLAYWRIGHT_BASE_URL=http://127.0.0.1:5174 npx playwright test case.spec.ts
const CASE = '/cases/company_best_year';
const CASE_A = '/cases/delivery_best_way';

test.setTimeout(90_000);

async function open(page: Page, path = CASE) {
  await page.goto(app(path));
  // a clean browser that has answered the first-visit offer of the guided tour (tour.spec.ts tests the offer)
  await page.evaluate(() => { localStorage.clear(); for (const id of ['company_best_year', 'delivery_best_way']) localStorage.setItem(`variator:tour:${id}`, 'dismissed'); });
  await page.goto(app(path));
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
}
const sub = (page: Page, name: string) => page.locator('.cs-sub-sections button', { hasText: name });
/** The fork the answer names: the actor verdict's while the actors answer (the formula model is then a collapsed quick
 *  estimate), else the formula decision. */
const answerLeader = async (page: Page) => (await page.getByTestId('quick-estimate').count()) ? page.getByTestId('actor-leader').innerText() : page.getByTestId('leader').innerText();
const upToDate = (page: Page) => expect(page.locator('.cs-run-dot')).toHaveAttribute('data-state', 'ok', { timeout: 30_000 });

test('scrollspy follows the sections and a click scrolls to one', async ({ page }) => {
  await open(page);
  await page.evaluate(() => document.getElementById('plan')!.scrollIntoView({ block: 'start' }));
  await expect(sub(page, 'Plan')).toHaveAttribute('aria-current', 'location');
  await expect(page.locator('.cs-step.is-current')).toContainText('Month');
  await page.evaluate(() => document.getElementById('answer')!.scrollIntoView({ block: 'start' }));
  await expect(sub(page, 'Answer')).toHaveAttribute('aria-current', 'location');
  // a click without Playwright's scroll-into-view (it would scroll the page under the sticky bar)
  await sub(page, 'Why').evaluate((b: HTMLElement) => b.click());
  await expect(sub(page, 'Why')).toHaveAttribute('aria-current', 'location');
  await expect.poll(() => page.evaluate(() => Math.abs(document.getElementById('why')!.getBoundingClientRect().top - 72))).toBeLessThan(60);
  await page.evaluate(() => document.getElementById('world')!.scrollIntoView({ block: 'start' }));
  await expect(page.locator('.cs-step.is-current')).toContainText('Forks');
});

// A preset re-ranks the forks at once, without a new simulation; the leader, the ranking and the briefing strip follow.
// Which preset moves the leader depends on the model, so the test looks for one (and needs only the re-ranking).
async function presetReranks(page: Page) {
  await upToDate(page);
  const leader = page.getByTestId('leader'), first = page.locator('[data-testid="ranking"] tbody tr').first();
  const ranking = async () => (await page.locator('[data-testid="ranking"] tbody').innerText()).replace(/\s+/g, ' ');
  const radios = page.locator('[data-testid="priorities"] [role="radiogroup"][aria-label="Priority presets"] [role="radio"]');
  let moved = false;
  for (let i = 0; i < await radios.count(); i++) {
    const r = radios.nth(i);
    if (await r.getAttribute('aria-checked') === 'true') continue;
    const before = await ranking(), lead = await answerLeader(page), name = (await r.innerText()).split('\n')[0].trim();
    await r.click();
    await expect.poll(ranking).not.toBe(before);
    await expect(page.locator('.cs-toast', { hasText: `Priorities: ${name}` }).first()).toBeVisible({ timeout: 30_000 });
    await upToDate(page);
    // the ranking names the answer's leader; the formula strategy names the formula leader (a quick estimate on actors)
    await expect(first).toContainText(await answerLeader(page));
    await expect(page.locator('.briefing-strip strong').first()).toHaveText(await leader.innerText());
    if (await answerLeader(page) !== lead) { moved = true; break; }
  }
  return moved;
}

test('a priority preset re-ranks the forks at once', async ({ page }) => {
  await open(page);
  expect(await presetReranks(page), 'some preset changes the leader of case B').toBe(true);
});

test('the Answer starts with the actor verdict; one honest card on how firm the decision is; a preset changes it', async ({ page }) => {
  // case B defaults (WP16 meaningful differences, 2026-10-28): Keep the plan and Reduce tech debt are equal within the
  // meaningful difference, the baseline wins the tie («Equal choices»); a «Close call» only when a fork outside the tie
  // wins more scenarios; the actor verdict shows its own close call when its ranking has one
  await open(page);
  await upToDate(page);
  const verdict = page.locator('#answer [data-slot="actor-verdict"]').getByTestId('actor-verdict');
  await expect(verdict).toBeVisible();
  await expect(verdict).toHaveAttribute('data-status', /ready|missing|error/, { timeout: 30_000 });
  expect(await page.evaluate(() => { const s = document.querySelector('#answer [data-slot="actor-verdict"]')!, b = document.querySelector('#answer .cs-strategy')!; return !!(s.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING); })).toBe(true);
  const cards = page.locator('[data-testid="close-call"], [data-testid="equal-choices"], [data-testid="actor-close-call"]');
  expect(await cards.count()).toBeLessThanOrEqual(1);
  // under a current actor verdict the formula block is a collapsed quick estimate: open it to use its presets
  const quick = page.getByTestId('quick-estimate');
  if (await quick.count() && !(await quick.evaluate(d => (d as HTMLDetailsElement).open))) await quick.locator('summary').click();
  const equal = page.getByTestId('equal-choices');
  if (await equal.count()) {
    await expect(equal).toContainText('Equal choices');
    await expect(equal).toContainText(/.+ and .+ are equal within the meaningful difference on .+\./);
    await expect(equal).toContainText(/wins the tie/);
    const p = equal.locator('.cs-close-presets .button').first();
    if (await p.count()) {
      const name = (await p.innerText()).split('→')[0].trim(), winner = (await p.locator('.cs-close-then').innerText()).replace('→', '').trim();
      await p.click();
      await expect(page.locator('.cs-toast').first()).toContainText(`Priorities: ${name}`);
      await expect(page.getByTestId('leader')).toHaveText(winner);
    }
  } else if (await page.getByTestId('close-call').count()) {
    const card = page.getByTestId('close-call');
    await expect(card).toContainText(/.+ comes out on top in \d+% of the scenarios/);
    await expect(card).toContainText(/on average (it scores about the same as|.+ scores higher)/);
    const text = await card.innerText(), leader = await page.getByTestId('leader').innerText();
    const preset = card.locator('.cs-close-presets .button').first(), name = await preset.innerText();
    await preset.click();
    await expect(page.locator('.cs-toast').first()).toContainText(`Priorities: ${name}`);
    await expect.poll(async () => (await cards.count()) === 0 || (await cards.first().innerText()) !== text || (await page.getByTestId('leader').innerText()) !== leader).toBe(true);
  }
  expect(await cards.count()).toBeLessThanOrEqual(1);
  // the trade-off of the ranking says where the two are equal or by how much one is better
  await expect(page.getByTestId('tradeoff')).toContainText(/equal (to .+ )?within the meaningful difference|is better (than|on)/);
});

for (const path of [CASE, CASE_A]) {
  test(`${path}: the actor verdict is the answer; an edit reruns on the actors by itself; the formula only when they cannot answer`, async ({ page }) => {
    await open(page, path);
    const verdict = page.getByTestId('actor-verdict');
    await expect(verdict).toHaveAttribute('data-status', /ready|missing|error/, { timeout: 30_000 });
    if ((await verdict.getAttribute('data-status')) !== 'ready') { await expect(page.getByTestId('quick-estimate')).toHaveCount(0); return; }
    await upToDate(page);
    const quick = page.getByTestId('quick-estimate');
    await expect(quick).toBeVisible();
    expect(await quick.evaluate(d => (d as HTMLDetailsElement).open)).toBe(false);
    await expect(page.locator('#answer .cs-decision')).toBeHidden();
    const actor = await page.getByTestId('actor-leader').innerText(), formula = await quick.getByTestId('leader').innerText();
    if (actor !== formula) await expect(page.getByTestId('quick-disagree')).toContainText(`The quick formula model ranks ${formula} first`);
    else await expect(page.getByTestId('quick-disagree')).toHaveCount(0);
    // an edit reruns on the actors by itself (WP25 R1): the verdict stays the answer, never the formula draft
    await page.locator('.cs-hyp').first().click();
    await upToDate(page);
    await expect(page.getByTestId('actor-state')).toHaveText('Current');
    await expect(page.getByTestId('answer-draft')).toHaveCount(0);
    await expect(page.getByTestId('quick-estimate')).toBeVisible();
    // the actors cannot answer (the rerun fails): the formula model is a quick estimate, said so
    await page.route('**/api/variator/cases/*/month', r => r.request().method() === 'POST' ? r.fulfill({ status: 503, body: '{"error":"down for the test"}', contentType: 'application/json' }) : r.continue());
    await page.locator('.cs-hyp').nth(1).click();
    await expect(page.getByTestId('answer-draft')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('answer-draft')).toContainText('Quick estimate (formula model) — actors unavailable');
    await expect(page.locator('#answer .cs-decision')).toBeVisible();
    await expect(page.getByTestId('quick-estimate')).toHaveCount(0);
    await expect(page.getByTestId('actor-rerun')).toHaveAttribute('aria-disabled', 'false'); // a manual retry
  });
}

for (const [path, lever, leverFork, event, risk] of [[CASE, 'hire_start', 'Hire 5 engineers', 'account_event', 'R5'], [CASE_A, 'env_work', null, 'key_person_leaves', 'R10']] as const) {
  test(`${path}: the timeline shows your decisions and the events apart; the inspector says which is which`, async ({ page }) => {
    await open(page, path);
    const card = page.getByTestId('decisions-events'), inspector = page.getByTestId('inspector');
    await expect(card).toContainText('Timeline: your decisions and events');
    await expect(card.locator('[data-row="group-decisions"]')).toContainText(/Your decisions · (one per fork|the same in every fork)/);
    await expect(card.locator('[data-row="group-events"]')).toContainText('Events you can place · the same for every fork');
    // nothing is preselected: the inspector is not «the settings of the best fork»
    await expect(inspector).toContainText('Selected on the timeline');
    await expect(inspector).toContainText('Click any marker to see and change it.');
    await expect(inspector.getByTestId('inspector-kind')).toHaveCount(0);
    await page.locator(`#forks [data-marker="${lever}"] circle.cs-tk-mark`).click();
    await expect(inspector.getByTestId('inspector-kind')).toHaveText(leverFork ? `Your decision in the fork ${leverFork}. Its date and size change only that fork.` : 'Your decision, the same in every fork. Its date and size change every fork alike.');
    await expect(inspector).not.toContainText(/best fork|Decision · Decisions/);
    await page.locator(`#forks [data-marker="${event}"] circle.cs-tk-mark`).click();
    await expect(inspector.getByTestId('inspector-kind')).toHaveText('An event in the world. Placed events hit every fork the same way.');
    // where it comes from: its actor and the risk of the register (when the actor cards are served here)
    const origin = inspector.getByTestId('event-origin');
    if (await origin.count()) {
      await expect(origin).toContainText(new RegExp(`Already happened in the history: \\d+ times? \\(${risk}\\)`));
      await expect(inspector.getByTestId('event-actor')).toContainText(/ is (our|the) /);
      await inspector.getByTestId('event-risk').click();
      await expect(page).toHaveURL(new RegExp(`/workspace/actors\\?.*tab=risks.*risk=${risk}`));
      await expect(page.locator(`[data-risk="${risk}"]`)).toBeVisible({ timeout: 30_000 });
    }
  });
}

test('one source of numbers: the actors, kept and dimmed while an edit reruns on them, never the formula in between', async ({ page }) => {
  await open(page);
  await upToDate(page);
  const verdict = page.getByTestId('actor-verdict');
  await expect(verdict).toHaveAttribute('data-status', /ready|missing|error/, { timeout: 30_000 });
  test.skip((await verdict.getAttribute('data-status')) !== 'ready', 'needs the actor result');
  const leader = await page.getByTestId('actor-leader').innerText();
  // the fork cards, the ranking and the verdict name the same leader
  await expect(page.locator('.cs-fork-foot').first()).toHaveAttribute('data-source', 'actors');
  await expect(page.locator('.cs-fork-card', { hasText: leader }).first().locator('.cs-fork-foot')).toContainText('Rank 1');
  await expect(page.locator('[data-testid="ranking"] tbody tr').first()).toContainText(leader);
  await expect(page.locator('#compare .cs-section-head')).toContainText("On your company's actors");
  // the plan's P(met) and the rules read the actors' goals, milestones and checks (month.json); the formula tools of the
  // plan (inputs in conflict, levers plan) are a closed formula estimate; the chain of causes is labelled
  await expect(page.locator('#plan .cs-section-head')).toContainText("On your company's actors");
  const fork = await page.locator('#plan select[aria-label="Fork of the plan"]').inputValue();
  const month = await (await page.request.get('api/variator/artifacts/future-company_best_year/month.json')).json();
  const rows = [...(month.options[fork].milestones ?? []), ...(month.options[fork].goals ?? [])] as { label: string; pMet: number }[];
  const table = page.locator('#plan .cs-ms-table tbody tr');
  await expect(table.first()).toBeVisible();
  for (let i = 0; i < await table.count(); i++) {
    const label = await table.nth(i).locator('th').innerText(), row = rows.find(r => r.label === label);
    if (row) await expect(table.nth(i).locator('.cs-pill')).toHaveText(`${(100 * row.pMet).toFixed(0)}%`);
  }
  await expect(page.locator('#plan').getByTestId('formula-estimate')).toHaveCount(2);
  expect(await page.locator('#plan').getByTestId('formula-estimate').first().evaluate(d => (d as HTMLDetailsElement).open)).toBe(false);
  await expect(page.locator('#why .cs-section-head')).toContainText('Quick formula model');
  // the chain of causes is the formula model's (QA m3): open by default, as every chain to the root cause, but muted and
  // labelled as the quick formula model, with a note that the numbers above are the actors' and these can differ
  const whyQuick = page.getByTestId('why-quick');
  await expect(whyQuick.locator('summary')).toContainText('How the levers connect');
  await expect(whyQuick.locator('summary')).toContainText('(quick formula model)');
  expect(await whyQuick.evaluate(d => (d as HTMLDetailsElement).open)).toBe(true);
  await expect(whyQuick.locator('.cs-why-note')).toContainText("The numbers above come from the simulation on your company's actors.");
  await expect(whyQuick.locator('.cs-why-note')).toContainText("they can differ from the actors' numbers");
  expect(Number(await whyQuick.locator('.cs-why-muted').evaluate(e => getComputedStyle(e).opacity)), 'the formula values are muted').toBeLessThan(1);
  // what churn counts against the brief, from the actors' breakdown (QA m2)
  if (month.options[fork]?.churnBreakdown) {
    const note = page.getByTestId('churn-note');
    await expect(note).toContainText(/^Churn here counts notices given after \d+ \w+ \(\d+% for .+\)\. Counted like the brief \(contracts ending\): about \d+% in year 1/);
  }
  await expect(page.locator('#why .cs-rules').locator('..').locator('..')).toContainText("On your company's actors");
  const sla = month.options[fork].checks.find((x: { rule: string | null }) => x.rule === 'sla_breach');
  const col = await page.locator('#why .cs-rules thead th').evaluateAll((ths, f) => ths.findIndex(th => th.getAttribute('title') === f), await page.locator('#plan select[aria-label="Fork of the plan"] option:checked').innerText().then(t => t.replace(' (leader)', '')));
  if (sla && col > 0) await expect(page.locator('#why .cs-rules tbody tr', { hasText: /SLA/i }).first().locator('td').nth(col - 1)).toContainText(`${(100 * sla.pEver).toFixed(0)}%`);
  // an edit: the page reruns on the actors; meanwhile the previous actor numbers stay (dimmed), never the formula's
  const foots = async () => (await page.locator('.cs-fork-foot').allInnerTexts()).join(' / ');
  const shown = await foots();
  let release: () => void = () => {};
  const held = new Promise<void>(r => { release = r; });
  await page.route('**/api/variator/cases/*/month', async r => { if (r.request().method() === 'POST') await held; await r.continue(); });
  await page.locator('.cs-hyp', { hasText: 'Payroll tax' }).click();
  await expect(page.locator('.cs-page')).toHaveAttribute('data-actors', 'pending');
  // the verdict says so too (WP25 W3), not «Current»
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-state', 'recomputing');
  await expect(page.getByTestId('actor-state')).toHaveText("Recomputing on your company's actors…");
  await expect(page.locator('#compare .cs-section-head')).toContainText("Recomputing on your company's actors");
  await expect(page.locator('.cs-fork-foot').first()).toHaveAttribute('data-source', 'pending');
  expect(await foots(), 'the fork cards keep the actor numbers while the rerun runs').toBe(shown);
  await expect(page.getByTestId('answer-draft')).toHaveCount(0);
  release();
  await upToDate(page);
  await expect(page.locator('.cs-page')).not.toHaveAttribute('data-actors', /./);
  await expect(page.locator('.cs-fork-foot').first()).toHaveAttribute('data-source', 'actors');
  // the notice compares the actor results, for the fork it is about
  await expect(page.locator('.cs-toast').first()).toContainText("on your company's actors");
});

test('a notice never covers an open side sheet: bottom left beside it, or held until it closes (QA m4)', async ({ page, isMobile }) => {
  await open(page);
  await upToDate(page);
  await page.locator('.cs-hyp').first().click();
  const toast = page.locator('.cs-toast').first(), stack = page.locator('.cs-toasts');
  await expect(toast).toBeVisible({ timeout: 30_000 });
  await expect(stack).toHaveAttribute('data-place', /^(top|bottom)$/);
  // a node of the chain of causes opens its side sheet (the chain is collapsed while the actors answer: open it)
  const quick = page.getByTestId('why-quick');
  if (await quick.count() && !(await quick.evaluate(d => (d as HTMLDetailsElement).open))) { await quick.locator('summary').scrollIntoViewIfNeeded(); await quick.locator('summary').click(); }
  await page.locator('#why .cs-chain-node').first().scrollIntoViewIfNeeded();
  await page.locator('#why .cs-chain-node').first().click();
  const panel = page.locator('.detail-panel');
  await expect(panel).toBeVisible();
  if (isMobile) {
    await expect(stack).toHaveAttribute('data-place', 'held');
    await expect(toast).toBeHidden();
  } else {
    await expect(stack).toHaveAttribute('data-place', 'aside');
    const [t, p] = await Promise.all([toast.boundingBox(), panel.boundingBox()]);
    expect(t!.x + t!.width, 'the notice is left of the sheet').toBeLessThanOrEqual(p!.x);
  }
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await expect(stack).toHaveAttribute('data-place', /^(top|bottom)$/);
  await expect(toast).toBeVisible();
});

test('a notice never covers the Answer\'s controls: «Rerun on actors» near the top or the bottom (WP25 R2)', async ({ page }) => {
  await open(page);
  await upToDate(page);
  test.skip(!(await page.locator('.cs-av .cs-card-tools').count()), 'needs the actor verdict');
  await page.locator('.cs-hyp').first().click();
  const toast = page.locator('.cs-toast').first(), tools = page.locator('.cs-av .cs-card-tools');
  await expect(toast).toBeVisible({ timeout: 30_000 });
  // apart: on the other edge, compact (heads only) when no edge has room in full, or held while none has room at all
  const apart = async () => {
    if (await page.locator('.cs-toasts').getAttribute('data-place') === 'held') return true;
    const [t, c] = [(await page.locator('.cs-toasts').boundingBox())!, (await tools.boundingBox())!];
    return t.x + t.width <= c.x || c.x + c.width <= t.x || t.y + t.height <= c.y || c.y + c.height <= t.y;
  };
  const vh = page.viewportSize()!.height;
  for (const y of [90, vh - 70]) {
    // the verdict's tools at this height of the window
    await page.evaluate(([y]) => { const el = document.querySelector('.cs-av .cs-card-tools')!; window.scrollBy(0, el.getBoundingClientRect().top - y); }, [y]);
    await expect.poll(apart, { message: `the notices clear of «Rerun on actors» at ${y}px` }).toBe(true);
  }
});

test('a drag on the timeline recomputes and shows a notice with Undo', async ({ page, isMobile }) => {
  test.skip(isMobile, 'mouse drag');
  await open(page);
  const marker = page.locator('#forks [data-marker="hire_start"] circle.cs-tk-mark');
  await marker.scrollIntoViewIfNeeded();
  const b = (await marker.boundingBox())!;
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + 100, b.y + b.height / 2, { steps: 6 });
  await expect(page.locator('#forks .cs-tk-ghost')).toBeVisible();
  await page.mouse.move(b.x + 140, b.y + b.height / 2, { steps: 4 });
  await page.mouse.up();
  const toast = page.locator('.cs-toast').first();
  await expect(toast).toContainText('Hiring starts', { timeout: 30_000 });
  await expect(toast).toContainText('Effect');
  await expect(toast.getByRole('button', { name: 'Undo' })).toBeVisible();
  await expect(page.locator('#forks .cs-mod.is-edited').first()).toBeVisible();
  // the inspector is a control panel too: the notices stay clear of it (WP25 W2)
  const clear = async () => {
    if (await page.locator('.cs-toasts').getAttribute('data-place') === 'held') return true;
    const [t, i] = [await page.locator('.cs-toasts').boundingBox(), await page.getByTestId('inspector').boundingBox()];
    return !t || !i || t.x + t.width <= i.x || i.x + i.width <= t.x || t.y + t.height <= i.y || i.y + i.height <= t.y || i.y >= 1000 || i.y + i.height <= 0;
  };
  await expect.poll(clear, { message: 'the notices clear of the inspector' }).toBe(true);
});

test('Esc during a drag puts the marker back', async ({ page, isMobile }) => {
  test.skip(isMobile, 'mouse drag');
  await open(page);
  const marker = page.locator('#forks [data-marker="hire_start"] circle.cs-tk-mark');
  await marker.scrollIntoViewIfNeeded();
  const b = (await marker.boundingBox())!;
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + 120, b.y + b.height / 2, { steps: 6 });
  await page.keyboard.press('Escape');
  await page.mouse.up();
  await expect(page.locator('.cs-toast')).toHaveCount(0);
  await expect(page.locator('.cs-menu-count')).toHaveCount(0);
});

test('Return to original restores the defaults', async ({ page }) => {
  test.setTimeout(120_000); // three full recomputes of 4,096 scenarios
  await open(page);
  const leader = page.getByTestId('leader');
  const original = await leader.innerText();
  await page.getByRole('radio', { name: 'Cash first' }).click();
  await page.locator('.cs-hyp', { hasText: 'Price war' }).click();
  await expect(page.locator('.cs-menu-count')).toBeVisible();
  await upToDate(page);
  await page.getByRole('button', { name: 'Case options' }).click();
  await expect(page.getByRole('dialog', { name: 'Case options' })).toContainText('Up to date');
  await page.locator('.cs-menu .cs-reset-all').click();
  await expect(page.getByRole('alertdialog', { name: 'Return to the original' })).toBeVisible();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Return to original' }).click();
  await expect(page.locator('.cs-menu-count')).toHaveCount(0);
  await upToDate(page);
  await expect(leader).toHaveText(original);
  await expect(page.getByRole('radio', { name: 'Balanced' })).toHaveAttribute('aria-checked', 'true');
  await expect(page.locator('.cs-hyp', { hasText: 'Price war' })).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.cs-toast').first()).toContainText('Returned to the original');
});

test('help: a hover shows the short text, a click pins the full text', async ({ page, isMobile }) => {
  test.skip(isMobile, 'hover');
  await open(page);
  const q = page.locator('#answer .cs-section-head .cs-help').first();
  await q.hover();
  await expect(page.getByRole('tooltip')).toBeVisible();
  await q.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
});

test('the options menu holds the run status, compare and reset; Esc closes it', async ({ page }) => {
  await open(page);
  await upToDate(page);
  await page.getByRole('button', { name: 'Case options' }).click();
  const menu = page.getByRole('dialog', { name: 'Case options' });
  await expect(menu).toContainText('4,096 scenarios');
  await expect(menu.getByRole('checkbox')).not.toBeChecked();
  await expect(menu.locator('.cs-reset-all')).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(menu).toHaveCount(0);
  const bar = await page.locator('.cs-progress').boundingBox();
  expect(bar!.height).toBeLessThan(64);
});

test('no horizontal page scroll', async ({ page }) => {
  await open(page);
  const size = await page.evaluate(() => ({ client: document.documentElement.clientWidth, doc: document.documentElement.scrollWidth }));
  expect(size.doc).toBeLessThanOrEqual(size.client + 1);
});

test('an inspector edit of the number of hires recomputes and marks the value', async ({ page }) => {
  await open(page);
  await upToDate(page);
  await page.locator('#forks [data-marker="hire_start"] circle.cs-tk-mark').click();
  const field = page.getByTestId('inspector').locator('[data-value="v|hire_count"]');
  await field.getByRole('slider', { name: 'Engineers to hire' }).focus();
  await page.keyboard.press('ArrowRight');
  const toast = page.locator('.cs-toast').first();
  await expect(toast).toContainText('Engineers to hire: 5 → 6', { timeout: 30_000 });
  await expect(toast).toContainText('Effect');
  await expect(field.locator('.cs-mod.is-edited')).toBeVisible();
  await expect(field).toContainText('6');
  // the fork's fixed name («Hire 5 engineers») carries the edited size
  await expect(toast).toContainText('(edited: 6)');
  await expect(page.locator('#forks .cs-fork-card', { hasText: 'Hire 5 engineers' })).toContainText('edited: 6');
});

test.describe('the cases page: intake chat and the cases', () => {
  async function cases(page: Page) {
    await page.goto('cases');
    await page.evaluate(() => localStorage.clear());
    await page.goto('cases');
    await expect(page.getByRole('heading', { name: /What decision are you weighing/ })).toBeVisible({ timeout: 30_000 });
  }
  const chip = (page: Page, name: string) => page.locator('.cs-quick .cs-chip:not([disabled])', { hasText: name }).first();

  test('the ready cases sit under the input, each with its question in one line', async ({ page }) => {
    await cases(page);
    const cards = page.getByTestId('case-cards').locator('.cs-case-card');
    await expect(cards).toHaveCount(2);
    await expect(cards.filter({ hasText: 'Best year' }).locator('.cs-case-question')).toHaveText('Which lever gives Larchwood Systems the best year?');
    await expect(cards.filter({ hasText: 'Best way to deliver' }).locator('.cs-case-question')).toHaveText('How should Larchwood Systems deliver the new platform?');
    await expect(page.locator('.kb-examples .button')).toHaveCount(4);
    // the subtitles sit behind «Know more» and «?»
    await expect(page.locator('.cs-intake .page-intro p')).toHaveCount(0);
    await expect(page.locator('.cs-cases-head p')).toHaveCount(0);
  });

  test('fire all employees: three questions, then the draft, the notice and the ready cases', async ({ page }) => {
    await cases(page);
    await page.getByRole('textbox', { name: 'Your decision' }).fill('What if I fire all employees in 3 months?');
    await page.getByRole('button', { name: 'Ask' }).click();
    const thread = page.getByTestId('intake-thread');
    await expect(thread).toContainText('Larchwood Systems has 20 people');
    await expect(thread).toContainText('Do you mean all 20, including yourself, or everyone except the leadership?');
    await chip(page, 'Everyone except the leadership').click();
    await expect(thread).toContainText('What is the goal');
    await chip(page, 'Cut costs').click();
    await expect(thread).toContainText('Compare it with what?');
    await chip(page, 'Replace delivery with a vendor').click();
    const draft = page.getByTestId('intake-draft');
    await expect(draft).toContainText('lets everyone except the leadership (15 people) go in January 2027, to cut costs');
    await expect(draft.locator('.cs-draft-forks li').first()).toContainText('Keep as is');
    await expect(draft).toContainText('Vistula Code Works or Kaveri Tech Services');
    await expect(draft).toContainText('Connect your LLM API key');
    await expect(draft.locator('.cs-draft-notice')).toContainText('Running a new case is not available in this version');
    await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveCount(0);
    await draft.getByRole('link', { name: /Best year/ }).click();
    await expect(page).toHaveURL(/\/workspace\/cases\/company_best_year/);
  });

  test('an example chip starts a thread; the thread survives a reload; Start over clears it', async ({ page }) => {
    await cases(page);
    await page.locator('.kb-examples .button', { hasText: 'What if we lose our biggest client?' }).click();
    const thread = page.getByTestId('intake-thread');
    await expect(thread).toContainText('Meridian Freight Group renews in May 2027');
    await page.reload();
    await expect(page.getByTestId('intake-thread')).toContainText('Meridian Freight Group renews in May 2027');
    await page.getByRole('button', { name: 'Start over' }).click();
    await expect(page.getByTestId('intake-thread')).toHaveCount(0);
    await expect(page.locator('.kb-examples .button')).toHaveCount(4);
    await expect(page.getByRole('textbox', { name: 'Your decision' })).toHaveValue('');
  });

  test('a question it cannot place gets one friendly question with the kinds of decision', async ({ page }) => {
    await cases(page);
    await page.getByRole('textbox', { name: 'Your decision' }).fill('blorp zzz qwerty');
    await page.getByRole('button', { name: 'Ask' }).click();
    await expect(page.getByTestId('intake-thread')).toContainText('I could not tell what decision this is about');
    await expect(page.locator('.cs-quick .cs-chip:not([disabled])')).toHaveCount(8);
  });
});

test.describe('where the forks come from', () => {
  const request = (page: Page) => page.locator('.cs-head').getByRole('button', { name: 'Your request' });
  // the chips count what the origin file says was drafted (the model team may add a hypothesis)
  const drafted = (id: string) => { const d = JSON.parse(readFileSync(new URL(`../../config/cases/${id}.origin.json`, import.meta.url), 'utf8')).drafted; return [`${d.forks.length} forks`, `${d.hypotheses.length} hypotheses`, `${d.criteria.length} criteria`]; };
  for (const [path, words, n] of [[CASE, 'We are an IT services company', drafted('company_best_year')], [CASE_A, 'We have to deliver a new platform', drafted('delivery_best_way')]] as const) {
    test(`the origin of ${path} opens in a modal with the request, the questions and the drafted chips`, async ({ page }) => {
      await open(page, path);
      await expect(page.locator('#forks .cs-origin, #forks [data-testid="origin"]')).toHaveCount(0);
      await request(page).click();
      const origin = page.getByRole('dialog', { name: 'Where these forks come from' });
      await expect(origin).toBeVisible();
      await expect(origin.getByTestId('origin-request')).toContainText(words);
      await expect(origin).toContainText('Avery Collins');
      await expect(origin.locator('.cs-origin-qa > div')).toHaveCount(2);
      for (const x of n) await expect(origin.locator('.cs-origin-chip', { hasText: x })).toBeVisible();
      await expect(origin).toContainText('Reviewed and frozen by you on 26 Oct 2026');
      await origin.locator('.cs-origin-chip', { hasText: 'hypotheses' }).click();
      await expect(origin).toHaveCount(0);
      await expect.poll(() => page.evaluate(() => Math.abs(document.getElementById('world')!.getBoundingClientRect().top - 72))).toBeLessThan(80);
    });
  }

  test('the Forks header opens the same modal; Esc closes it and gives the focus back', async ({ page }) => {
    await open(page);
    const small = page.locator('#forks .cs-section-head').getByRole('button', { name: 'Your request' });
    await small.click();
    const origin = page.getByRole('dialog', { name: 'Where these forks come from' });
    await expect(origin).toBeVisible();
    // the focus stays in the modal
    for (let i = 0; i < 8; i++) await page.keyboard.press('Tab');
    expect(await page.evaluate(() => !!document.activeElement?.closest('[data-testid="origin"]'))).toBe(true);
    await page.keyboard.press('Escape');
    await expect(origin).toHaveCount(0);
    await expect(small).toBeFocused();
  });

  test('Edit request and regenerate explains and changes nothing', async ({ page }) => {
    await open(page);
    await upToDate(page);
    const leader = await page.getByTestId('leader').innerText();
    await request(page).click();
    const origin = page.getByTestId('origin');
    const text = await origin.getByTestId('origin-request').innerText();
    await origin.getByRole('button', { name: 'Edit request and regenerate' }).click();
    const box = page.getByRole('textbox', { name: 'Your request' });
    await expect(box).toHaveValue(text);
    await box.fill('What if we only raise prices?');
    await page.getByRole('button', { name: 'Regenerate', exact: true }).click();
    const notice = page.getByRole('alertdialog');
    await expect(notice).toContainText('already drafted from this request and reviewed by you');
    await expect(notice).toContainText('Regeneration is not available in this version');
    await expect(notice.getByRole('button', { name: 'Regenerate everything' })).toBeDisabled();
    await expect(notice).toContainText('Not available in this version');
    await notice.getByRole('button', { name: 'Keep current forks' }).click();
    // back in the origin view, the request as it was
    await expect(origin.getByTestId('origin-request')).toHaveText(text);
    await origin.getByRole('button', { name: 'Edit request and regenerate' }).click();
    await expect(page.getByRole('textbox', { name: 'Your request' })).toHaveValue(text);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('.cs-toast')).toHaveCount(0);
    await expect(page.locator('.cs-menu-count')).toHaveCount(0);
    await expect(page.getByTestId('leader')).toHaveText(leader);
  });
});

test.describe('few words on the page: one short purpose per section; intros and notes sit behind «?», «Know more» or a closed disclosure', () => {
  for (const [path, file] of [[CASE, 'company_best_year'], [CASE_A, 'delivery_best_way']] as const) {
    test(`each section of ${path} says its purpose in at most two lines; no other inline intro`, async ({ page }) => {
      await open(page, path);
      const cfg = JSON.parse(readFileSync(new URL(`../../config/cases/${file}.json`, import.meta.url), 'utf8')) as { lead: string; sections: { summary: string }[]; steps: { id: string; help: string }[]; help: Record<string, { body: string }> };
      const plain = (t: string) => t.replace(/\p{Extended_Pictographic}\uFE0F?/gu, '').replace(/\s+/g, ' ').trim();
      // the section heads: the purpose of the section (its config summary) beside the title, and nothing else
      await expect(page.locator('.cs-section-head p:not(.cs-section-purpose)')).toHaveCount(0);
      const purposes = page.locator('.cs-section-head .cs-section-purpose');
      expect(await purposes.count()).toBeGreaterThan(0);
      const summaries = cfg.sections.map(s => plain(s.summary));
      for (const p of await purposes.all()) {
        const text = plain(await p.innerText());
        // the Day says what the actor days show when they are served (sections/Day.tsx); every other one is its summary
        expect(summaries.includes(text) || /^Three example scenarios of a fork, day by day/.test(text), `«${text}» is a section's purpose`).toBe(true);
        const [h, line] = await p.evaluate(e => [e.getBoundingClientRect().height, parseFloat(getComputedStyle(e).lineHeight)]);
        expect(h, `«${text}» takes at most two lines`).toBeLessThanOrEqual(2 * line + 1);
      }
      // the step dividers: one watermark of nouns per step (what the step gives), one line, and nothing else
      await expect(page.locator('.cs-step-divider p:not(.cs-step-gain)')).toHaveCount(0);
      const gains = page.locator('.cs-step-divider .cs-step-gain');
      await expect(gains).toHaveCount(cfg.steps.length);
      for (const g of await gains.all()) {
        await expect(g).toHaveAttribute('aria-label', /^In this step: /);
        const [h, line] = await g.evaluate(e => [e.getBoundingClientRect().height, parseFloat(getComputedStyle(e).lineHeight)]);
        expect(h).toBeLessThanOrEqual(line + 1);
      }
      await expect(page.locator('.cs-head p')).toHaveCount(1); // the one short question
      await expect(page.getByTestId('case-question')).not.toContainText(':');
      const body = await page.locator('.main-content').innerText();
      expect(body).not.toContain(cfg.lead);
      // the help of a section stays behind its «?»: its intro is the first line there
      for (const s of cfg.steps) { const h = cfg.help[s.help]?.body; if (h) expect(body, `the help of step ${s.id} is behind «?»`).not.toContain(h); }
      const q = page.locator('#world .cs-section-head .cs-help');
      await q.focus();
      await expect(page.getByRole('tooltip')).toContainText(cfg.sections.find(x => x.summary.startsWith('World hypotheses'))?.summary ?? '');
    });
  }
});

test.describe('case A, Best way to deliver', () => {
  test('scrollspy follows the sections of a 24-month case', async ({ page }) => {
    await open(page, CASE_A);
    await expect(page.locator('.cs-head')).toContainText('24 MONTHS');
    await page.evaluate(() => document.getElementById('plan')!.scrollIntoView({ block: 'start' }));
    await expect(sub(page, 'Plan')).toHaveAttribute('aria-current', 'location');
    await expect(page.locator('#plan')).toContainText('Go-live');
    await sub(page, 'Why').evaluate((b: HTMLElement) => b.click());
    await expect(sub(page, 'Why')).toHaveAttribute('aria-current', 'location');
    await expect(page.locator('#why')).toContainText('Main drivers of money at month 24');
    await page.evaluate(() => document.getElementById('week-review')!.scrollIntoView({ block: 'start' }));
    await expect(page.locator('.cs-step.is-current')).toContainText('Week');
  });

  test('a priority preset re-ranks the forks at once', async ({ page }) => {
    await open(page, CASE_A);
    await expect(page.getByTestId('kpi-delay_months')).toContainText('Months late');
    await presetReranks(page);
  });

  test('a drag of the extra people recomputes and shows a notice', async ({ page, isMobile }) => {
    test.skip(isMobile, 'mouse drag');
    await open(page, CASE_A);
    await upToDate(page);
    const marker = page.locator('#forks [data-marker="extra_people"] circle.cs-tk-mark');
    await marker.scrollIntoViewIfNeeded();
    const b = (await marker.boundingBox())!;
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + 60, b.y + b.height / 2, { steps: 6 });
    await expect(page.locator('#forks .cs-tk-ghost')).toBeVisible();
    await page.mouse.move(b.x + 90, b.y + b.height / 2, { steps: 4 });
    await page.mouse.up();
    const toast = page.locator('.cs-toast').first();
    await expect(toast).toContainText('Extra people requested', { timeout: 30_000 });
    await expect(toast).toContainText('Effect');
    await expect(toast.getByRole('button', { name: 'Undo' })).toBeVisible();
    // the inspector edits the count and hire or vendor of the same item
    await expect(page.getByTestId('inspector')).toContainText('Extra people requested');
    await expect(page.getByTestId('inspector').getByRole('radio', { name: 'Vendor' })).toBeVisible();
  });

  test('Return to original restores the defaults', async ({ page }) => {
    test.setTimeout(120_000);
    await open(page, CASE_A);
    const leader = page.getByTestId('leader');
    const original = await leader.innerText();
    await page.getByRole('radio', { name: 'Low effort' }).click();
    await page.locator('.cs-hyp', { hasText: 'Requirements change often' }).click();
    await expect(page.locator('.cs-menu-count')).toBeVisible();
    await upToDate(page);
    await page.getByRole('button', { name: 'Case options' }).click();
    await page.locator('.cs-menu .cs-reset-all').click();
    await page.getByRole('alertdialog').getByRole('button', { name: 'Return to original' }).click();
    await expect(page.locator('.cs-menu-count')).toHaveCount(0);
    await upToDate(page);
    await expect(leader).toHaveText(original);
    await expect(page.getByRole('radio', { name: 'Balanced' })).toHaveAttribute('aria-checked', 'true');
    await expect(page.locator('.cs-hyp', { hasText: 'Requirements change often' })).toHaveAttribute('aria-pressed', 'false');
  });

  test('no horizontal page scroll', async ({ page }) => {
    await open(page, CASE_A);
    const size = await page.evaluate(() => ({ client: document.documentElement.clientWidth, doc: document.documentElement.scrollWidth }));
    expect(size.doc).toBeLessThanOrEqual(size.client + 1);
  });
});

test.describe('the decision panel', () => {
  test.use({ timezoneId: 'America/Los_Angeles' });
  test('a committed decision reads its date as the app does («30 Oct 2026, 14:05»), with the scope of its P(best)', async ({ page }) => {
    // the decision store answers from the test (nothing is committed on the server)
    const committed = { fork: 'status_quo', forkLabel: 'Keep the plan', committedAt: '2026-10-30T21:05:00.000Z', pBest: 0.154, pBestOf: 5, why: '', warnings: [], backups: [], fixes: [] };
    await page.route('**/api/decisions/company_best_year', r => r.request().method() === 'GET' ? r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ draft: null, committed }) }) : r.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' }));
    await open(page);
    await page.locator('.cs-decision-btn').first().click();
    const body = page.locator('.cs-sheet .cs-decision');
    await expect(body).toContainText('Keep the plan, committed 30 Oct 2026, 14:05 · best in 15% of the scenarios (of 5 forks)');
    await expect(body).not.toContainText(/\d+\/\d+\/\d+|AM|PM/);
  });
});
