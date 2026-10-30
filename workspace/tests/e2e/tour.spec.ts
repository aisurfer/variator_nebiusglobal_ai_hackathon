import { expect, test, type Page } from '@playwright/test';

// The guided tours of the case pages (config/cases/<case>.tour.json). Needs the workspace dev servers only:
//   PLAYWRIGHT_BASE_URL=http://127.0.0.1:5174 npx playwright test tour.spec.ts
// paths relative to the base URL (<origin>/workspace/, playwright.config.ts)
const B = 'cases/company_best_year', A = 'cases/delivery_best_way';

test.setTimeout(150_000);

/** A clean browser: no saved edits, and the first-visit offer either shown (`offer`) or already answered. */
async function clean(page: Page, path: string, offer = false) {
  await page.goto(path.split('?')[0]);
  await page.evaluate(offer => {
    localStorage.clear();
    if (!offer) for (const id of ['company_best_year', 'delivery_best_way']) localStorage.setItem(`variator:tour:${id}`, 'dismissed');
  }, offer);
  await page.goto(path);
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
}
const card = (page: Page) => page.getByTestId('tour-card');
/** The fork the answer names: the actor verdict's while it is current (the formula model is then a quick estimate),
 *  else the formula decision (the draft after edits, or the only answer without actor data). */
const answerLeader = async (page: Page) => (await page.getByTestId('quick-estimate').count()) ? page.getByTestId('actor-leader').innerText() : page.getByTestId('leader').innerText();
const upToDate = (page: Page) => expect(page.locator('.cs-run-dot')).toHaveAttribute('data-state', 'ok', { timeout: 30_000 });
const next = (page: Page) => card(page).getByRole('button', { name: /^(Next|Finish)/ }).click();
/** The card is filled from the live page: no value is missing once the run is up to date. */
async function settled(page: Page) {
  await upToDate(page);
  await expect(card(page)).not.toContainText('…', { timeout: 15_000 });
  await expect(card(page)).not.toContainText(/undefined|NaN/);
}
/** On a wide screen the card stays off its region (the spotlight is the region plus 6 px). */
async function clearOfRegion(page: Page) {
  await page.waitForTimeout(700); // the smooth scroll ends
  const boxes = await page.evaluate(() => {
    const s = document.querySelector('.vt-spot')?.getBoundingClientRect(), c = document.querySelector('[data-testid="tour-card"]')?.getBoundingClientRect();
    return s && c ? { s: { top: s.top + 6, left: s.left + 6, bottom: s.bottom - 6, right: s.right - 6 }, c: { top: c.top, left: c.left, bottom: c.bottom, right: c.right }, vh: innerHeight } : null;
  });
  expect(boxes, 'a region and a card').not.toBeNull();
  const { s, c, vh } = boxes!;
  const visible = { top: Math.max(s.top, 72), bottom: Math.min(s.bottom, vh) };
  const overlap = c.left < s.right && s.left < c.right && c.top < visible.bottom && visible.top < c.bottom;
  expect(overlap, `card ${JSON.stringify(c)} on region ${JSON.stringify(s)}`).toBe(false);
}
/** The last step: «Your decision» in the spotlight, Finish, and the link on to the other case with its tour. */
async function decisionStep(page: Page, isMobile: boolean) {
  await expect(card(page)).toHaveAttribute('data-step', 'decision');
  await expect(card(page)).toContainText('Step 6 of 6');
  await expect(card(page).locator('.vt-body')).toContainText('the morning Brief checks each warning sign');
  await expect(page.locator('[data-tour="decision"]')).toBeVisible();
  await expect(page.locator('.vt-spot')).toBeVisible();
  const [spot, button] = await Promise.all([page.locator('.vt-spot').boundingBox(), page.locator('[data-tour="decision"]').boundingBox()]);
  expect(Math.abs(spot!.y + 6 - button!.y), 'the spotlight is on «Your decision»').toBeLessThan(4);
  if (!isMobile) await clearOfRegion(page);
  await expect(card(page).getByRole('button', { name: 'Finish' })).toBeVisible();
  await expect(card(page).locator('.vt-link').filter({ hasText: 'Same engine, another decision' })).toHaveCount(1);
}

test('the deep link starts the tour, at a step when asked', async ({ page }) => {
  await clean(page, `${B}?tour=1`);
  await expect(card(page)).toBeVisible();
  await expect(card(page)).toHaveAttribute('data-step', 'request');
  await expect(card(page)).toContainText('Step 1 of 6');
  await expect(card(page)).toBeFocused();
  await expect(page.getByTestId('tour-offer')).toHaveCount(0);
  await page.goto(`${B}?tour=1&step=3`);
  await expect(card(page)).toHaveAttribute('data-step', 'timeline');
  await expect(card(page)).toContainText('Step 3 of 6');
  await settled(page);
});

test('case B end to end: Show me on every step that has it, Next to Finish at «Your decision», the link to case A', async ({ page, isMobile }) => {
  await clean(page, `${B}?tour=1`);
  // 1 · the request: Show me opens the origin modal next to the card
  await card(page).getByTestId('tour-show').click();
  const origin = page.getByRole('dialog', { name: 'Where these forks come from' });
  await expect(origin).toBeVisible();
  await expect(card(page).getByTestId('tour-show')).toHaveText('Done');
  await expect(card(page)).toContainText(/drafted \d+ forks, \d+ world hypotheses and \d+ criteria/);
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  await expect(origin).toHaveCount(0);
  // 2 · world hypotheses: Show me turns two on; the fork is simulated again and the notice explains it
  await expect(card(page)).toHaveAttribute('data-step', 'world');
  await settled(page);
  const leader = await answerLeader(page), ranks = await page.locator('.cs-fork-foot').allInnerTexts();
  await card(page).getByTestId('tour-show').click();
  await expect(page.locator('.cs-hyp', { hasText: 'GPU shortage' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.cs-hyp', { hasText: 'Big client may leave' })).toHaveAttribute('aria-pressed', 'true');
  const notice = page.locator('.cs-toast').first();
  await expect(notice).toContainText('Hypotheses «GPU shortage» and «Big client may leave» on', { timeout: 30_000 });
  await expect(notice).toContainText('Effect');
  await upToDate(page);
  const changed = (await answerLeader(page)) !== leader || (await page.locator('.cs-fork-foot').allInnerTexts()).join() !== ranks.join();
  expect(changed, 'the ranking changes').toBe(true);
  await settled(page);
  await expect(card(page)).toContainText(`Now ${await answerLeader(page)} leads`);
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  // 3 · the timeline: Show me moves «Hiring starts» and P(met) changes
  await expect(card(page)).toHaveAttribute('data-step', 'timeline');
  await expect(page.locator('.cs-toast')).toHaveCount(0); // the notices of step 2 were read
  await settled(page);
  const before = await card(page).locator('.vt-body').innerText();
  await card(page).getByTestId('tour-show').click();
  await expect(page.locator('.cs-toast').first()).toContainText('Hiring starts:', { timeout: 30_000 });
  await expect(page.getByRole('combobox', { name: 'Fork of the plan' })).toHaveValue('hire_5');
  await settled(page);
  await expect(card(page).locator('.vt-body')).not.toHaveText(before);
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  // 4 · the answer, then the risk calendar
  await expect(card(page)).toHaveAttribute('data-step', 'answer');
  await settled(page);
  await expect(card(page).locator('.vt-body')).toContainText(await answerLeader(page));
  if (!isMobile) await clearOfRegion(page);
  await card(page).getByTestId('tour-see').click();
  await expect(card(page).getByTestId('tour-see')).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => page.evaluate(() => { const r = document.querySelector('[data-tour="risk-calendar"]')!.getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight / 2; })).toBe(true);
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  // 5 · week and day: Show me keeps the suggested forks; the tree and the example days appear
  await expect(card(page)).toHaveAttribute('data-step', 'week-day');
  await card(page).getByTestId('tour-show').click();
  await expect(page.locator('[data-tour="week-tree"]')).toBeVisible();
  await card(page).getByTestId('tour-see').click();
  await expect(page.locator('[data-tour="day-examples"]')).toBeVisible();
  await settled(page);
  await expect(card(page).getByRole('button', { name: 'Finish' })).toHaveCount(0);
  await next(page);
  // 6 · the decision: the case ends in action, and the reviewer goes on to case A with its tour
  await decisionStep(page, isMobile);
  await card(page).getByRole('button', { name: /how to deliver a new platform/ }).click();
  await expect(page).toHaveURL(/\/workspace\/cases\/delivery_best_way\?.*tour=1/);
  await expect(card(page)).toHaveAttribute('data-step', 'request');
});

test('step 4 on the defaults: the answer of the actor verdict when it is current, else of the formula model', async ({ page, isMobile }) => {
  await clean(page, `${B}?tour=1&step=4`);
  await expect(page.getByTestId('actor-verdict')).toHaveAttribute('data-status', /ready|missing|error/, { timeout: 30_000 });
  await settled(page);
  const onActors = await page.locator('[data-tour="answer-verdict"]').count();
  if (onActors) {
    await expect(card(page).locator('.vt-body')).toContainText("On your company's actors");
    await expect(card(page).locator('.vt-body')).toContainText(await page.getByTestId('actor-leader').innerText());
    await expect(page.locator('.vt-spot')).toBeVisible();
    const [spot, verdict] = await Promise.all([page.locator('.vt-spot').boundingBox(), page.locator('[data-tour="answer-verdict"]').boundingBox()]);
    expect(Math.abs(spot!.y + 6 - verdict!.y)).toBeLessThan(4); // the spotlight is on the verdict
  } else await expect(card(page).locator('.vt-body')).toContainText(await page.getByTestId('leader').innerText());
  if (!isMobile) await clearOfRegion(page);
  // Show me applies «Cash first»; the page reruns on the actors by itself: the verdict stays the answer
  const rerun = (await page.getByTestId('actor-rerun').count()) ? await page.getByTestId('actor-rerun').getAttribute('aria-disabled') : null;
  await card(page).getByTestId('tour-show').click();
  await expect(page.getByRole('radio', { name: 'Cash first' })).toHaveAttribute('aria-checked', 'true');
  if (onActors && rerun !== null) {
    await expect(page.getByTestId('actor-state')).toHaveText(/Current/, { timeout: 30_000 });
    await expect(page.getByTestId('tour-recomputing')).toHaveCount(0);
    await expect(card(page).locator('.vt-body')).toContainText("On your company's actors");
    await expect(card(page).locator('.vt-body')).toContainText(await page.getByTestId('actor-leader').innerText());
    await expect(card(page).getByTestId('tour-show')).toHaveText('Done');
  }
});

test('Skip closes the tour; the address loses ?tour and the focus goes back', async ({ page }) => {
  await clean(page, B);
  const start = page.locator('.cs-head').getByRole('button', { name: 'Take the tour' });
  await start.click();
  await expect(card(page)).toBeFocused();
  await expect(page).toHaveURL(/tour=1&step=1/);
  await next(page);
  await expect(page).toHaveURL(/step=2/);
  await card(page).getByRole('button', { name: 'Skip' }).click();
  await expect(card(page)).toHaveCount(0);
  await expect(page.locator('.vt-spot, .vt-dim')).toHaveCount(0);
  await expect(page).not.toHaveURL(/tour=/);
  await expect(start).toBeFocused();
});

test('Esc closes the origin modal first, then the tour', async ({ page }) => {
  await clean(page, `${B}?tour=1`);
  await card(page).getByTestId('tour-show').click();
  const origin = page.getByRole('dialog', { name: 'Where these forks come from' });
  await expect(origin).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(origin).toHaveCount(0);
  await expect(card(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(card(page)).toHaveCount(0);
});

test('the first visit offers the tour once', async ({ page }) => {
  await clean(page, B, true);
  const offer = page.getByTestId('tour-offer');
  await expect(offer).toContainText('New here?');
  await expect(offer).toContainText(/A \d+-minute tour of this case/);
  await offer.getByRole('button', { name: 'Not now' }).click();
  await expect(offer).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(800);
  await expect(page.getByTestId('tour-offer')).toHaveCount(0);
  // the other case still offers its own tour; Start starts it and the offer does not come back
  await page.goto(A);
  await expect(page.getByTestId('tour-offer')).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('tour-offer').getByRole('button', { name: 'Start' }).click();
  await expect(card(page)).toHaveAttribute('data-step', 'request');
  await page.keyboard.press('Escape');
  await page.reload();
  await expect(page.getByTestId('leader')).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(800);
  await expect(page.getByTestId('tour-offer')).toHaveCount(0);
});

test('Back to the defaults restores the case; Keep my changes keeps them', async ({ page }) => {
  await clean(page, `${B}?tour=1&step=2`);
  await upToDate(page);
  const leader = await page.getByTestId('leader').innerText();
  await card(page).getByTestId('tour-show').click();
  await expect(page.locator('.cs-toast').first()).toContainText('Hypotheses', { timeout: 30_000 });
  await upToDate(page);
  await card(page).getByRole('button', { name: 'Skip' }).click();
  await expect(card(page)).toContainText('Keep what you changed?');
  await expect(card(page)).toContainText(/You changed \d+ values?/);
  await card(page).getByRole('button', { name: 'Back to the defaults' }).click();
  await expect(card(page)).toHaveCount(0);
  await expect(page.locator('.cs-menu-count')).toHaveCount(0);
  await upToDate(page);
  await expect(page.getByTestId('leader')).toHaveText(leader);
  await expect(page.locator('.cs-hyp', { hasText: 'GPU shortage' })).toHaveAttribute('aria-pressed', 'false');
  // the other way: keep
  await page.goto(`${B}?tour=1&step=2`);
  await card(page).getByTestId('tour-show').click();
  await expect(page.locator('.cs-menu-count')).toBeVisible();
  await card(page).getByRole('button', { name: 'Skip' }).click();
  await card(page).getByRole('button', { name: 'Keep my changes' }).click();
  await expect(card(page)).toHaveCount(0);
  await expect(page.locator('.cs-hyp', { hasText: 'GPU shortage' })).toHaveAttribute('aria-pressed', 'true');
});

test('case A end to end: the request, Low effort re-ranks, the timeline, the answer, why, the decision', async ({ page, isMobile }) => {
  await clean(page, `${A}?tour=1`);
  await card(page).getByTestId('tour-show').click();
  await expect(page.getByRole('dialog', { name: 'Where these forks come from' })).toBeVisible();
  await expect(card(page)).toContainText(/drafted \d+ delivery forks: In-house team, Nearshore T&M and Offshore fixed price/);
  await next(page);
  await expect(card(page)).toHaveAttribute('data-step', 'priorities');
  await upToDate(page);
  await card(page).getByTestId('tour-show').click();
  await expect(page.locator('.cs-toast').first()).toContainText('Priorities: Low effort');
  // the step names the leader the screen shows after the re-ranking (whether the preset moves it depends on the model)
  await upToDate(page);
  const leader = await answerLeader(page);
  await expect(page.locator('[data-testid="ranking"] tbody tr').first()).toContainText(leader);
  await expect(card(page)).toContainText(`Now ${leader} leads`);
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  await expect(card(page)).toHaveAttribute('data-step', 'timeline');
  await card(page).getByTestId('tour-show').click();
  await expect(page.locator('.cs-toast').first()).toContainText('Load-test environment work starts', { timeout: 30_000 });
  await settled(page);
  await next(page);
  await expect(card(page)).toHaveAttribute('data-step', 'answer');
  await settled(page);
  await expect(card(page)).toContainText(/goes live around [\w-]+ ’\d\d/);
  if (!isMobile) await clearOfRegion(page);
  await card(page).getByTestId('tour-see').click();
  await next(page);
  await expect(card(page)).toHaveAttribute('data-step', 'why');
  await settled(page);
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  await decisionStep(page, isMobile);
  await expect(card(page).getByRole('button', { name: /the best year of the company/ })).toBeVisible();
  await card(page).getByRole('button', { name: 'Finish' }).click();
  await expect(card(page)).toContainText('Keep what you changed?');
  await card(page).getByRole('button', { name: 'Back to the defaults' }).click();
  await expect(card(page)).toHaveCount(0);
  await expect(page.locator('.cs-menu-count')).toHaveCount(0);
});

test('on a phone the card is a bottom sheet and the page still scrolls', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'phone layout');
  await clean(page, `${B}?tour=1&step=2`);
  const box = (await card(page).boundingBox())!;
  const vp = page.viewportSize()!;
  expect(Math.round(box.y + box.height)).toBeGreaterThanOrEqual(vp.height - 1);
  expect(Math.round(box.width)).toBe(vp.width);
  const y = await page.evaluate(() => scrollY);
  await page.mouse.wheel(0, 400);
  await expect.poll(() => page.evaluate(() => scrollY)).not.toBe(y);
});

test('the reviewer path: the company in three pages, then the case tour', async ({ page, isMobile }) => {
  test.skip(!(await page.request.get('api/kb/company/brief')).ok(), 'needs the knowledge base (the brief)');
  await page.goto('brief');
  await page.evaluate(() => localStorage.clear());
  await page.goto('brief?tour=reviewer');
  await expect(card(page)).toHaveAttribute('data-step', 'brief', { timeout: 60_000 });
  await expect(card(page)).toContainText('Step 1 of 3');
  await expect(page.locator('[data-tour="brief-top"]')).toBeVisible();
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  await expect(page).toHaveURL(/\/workspace\/actors\?actor=CL-001&tour=reviewer&step=2/);
  await expect(card(page)).toHaveAttribute('data-step', 'actor', { timeout: 30_000 });
  await expect(page.locator('[data-tour="actor-habits"]')).toBeVisible();
  if (!isMobile) await clearOfRegion(page);
  await next(page);
  await expect(page).toHaveURL(/\/workspace\/actors\?tab=risks&risk=R5&tour=reviewer&step=3/);
  await expect(card(page)).toHaveAttribute('data-step', 'risks', { timeout: 30_000 });
  await expect(page.locator('[data-tour="risk-selected"]')).toContainText('Client gives notice');
  if (!isMobile) await clearOfRegion(page);
  await card(page).getByRole('button', { name: 'Continue to the decision' }).click();
  await expect(page).toHaveURL(/\/workspace\/cases\/company_best_year\?tour=1/);
  await expect(card(page)).toHaveAttribute('data-step', 'request', { timeout: 60_000 });
  await expect(card(page)).toContainText('Step 1 of 6');
});

test('the reviewer path: Skip closes it and leaves the page clean', async ({ page }) => {
  test.skip(!(await page.request.get('api/kb/company/brief')).ok(), 'needs the knowledge base (the brief)');
  await page.goto('brief?tour=reviewer');
  await expect(card(page)).toBeVisible({ timeout: 60_000 });
  await card(page).getByRole('button', { name: 'Skip' }).click();
  await expect(card(page)).toHaveCount(0);
  await expect(page).not.toHaveURL(/tour=/);
});
