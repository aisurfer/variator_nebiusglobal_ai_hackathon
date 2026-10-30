// Unit tests of the case engine: formula compiler, random numbers, hypothesis layering order, the user layer and its
// reset helpers, the storage fallback, instant reranking, timeline helpers and the change reports (toasts).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  analyseRun, applyMoves, applyPriority, clearState, compile, defaultState, diffReports, effectiveModel, effectivePrefs, evaluateTimeline,
  fromPreset, isModified, loadCase, loadState, moveMarker, mulberry32, pertQ, rankRun, resetAll, resetPath, resetSection, saveState,
  setEdit, setItemOff, setRiskAversion, setWeight, simulateCase, snapTies, solvePlan, stateKey, suggestDates, toggleEvent, togglePreset, withPlan,
  breakText, cautionWords, money, noEmoji, peopleText, peopleValue, scenarios,
  type CaseConfig, type CaseSnapshot, type CaseState, type StorageLike,
} from '../src/variator/engine/index.js';
import { createEngineClient } from '../src/variator/engine/client.js';

const config = JSON.parse(readFileSync(new URL('../config/cases/company_best_year.json', import.meta.url), 'utf8')) as CaseConfig;
const c = loadCase(config);
const N = 1000;
const small = (s: CaseState): CaseState => ({ ...s, n: N, seed: 7 });

test('the formula compiler follows Python precedence, comparisons and functions', () => {
  const f = (src: string, env: Record<string, number> = {}) => compile(src)(env);
  assert.equal(f('1 + 2 * 3 ** 2'), 19);
  assert.equal(f('-2 ** 2'), -4);
  assert.equal(f('7 // 2 + 7 % 3'), 4);
  assert.equal(f('1 < x <= 3', { x: 3 }), 1);
  assert.equal(f('1 < x <= 3', { x: 4 }), 0);
  assert.equal(f('a if a > b else b', { a: 2, b: 5 }), 5);
  assert.equal(f('clip(x, 0, 0.9) + maximum(1, 2) + minimum(3, 4)', { x: 1.5 }), 0.9 + 2 + 3);
  assert.equal(f('x > 1 and not_used < 0 or True', { x: 0, not_used: 1 }), 1);
  assert.deepEqual([...compile('a * b + c').names].sort(), ['a', 'b', 'c']);
  assert.throws(() => compile('foo(1)')({}), /unknown function/);
});

test('mulberry32 is deterministic and PERT quantiles stay inside the range', () => {
  const a = mulberry32(42), b = mulberry32(42);
  const xs = Array.from({ length: 5 }, () => a());
  assert.deepEqual(xs, Array.from({ length: 5 }, () => b()));
  xs.forEach(x => assert.ok(x >= 0 && x < 1));
  for (const u of [1e-12, 0.1, 0.5, 0.9, 1 - 1e-12]) {
    const v = pertQ(2, 4, 7, u);
    assert.ok(v >= 2 && v <= 7, `pert(${u}) = ${v}`);
  }
  assert.ok(pertQ(2, 4, 7, 0.5) < pertQ(2, 4, 7, 0.6));
});

test('loadCase checks the schema and keeps the default weights', () => {
  assert.throws(() => loadCase({ ...config, schema: 'other' } as unknown as CaseConfig), /schema/);
  assert.deepEqual(Object.fromEntries(c.base.criteria.map(x => [x.name, x.weight])), config.defaults.weights);
  assert.equal(config.priorityPresets.find(p => p.id === 'balanced')!.weights.cash, config.defaults.weights.cash);
});

test('hypotheses apply in definition order, edits win, and the trail says which hypothesis changed what', () => {
  const cfg: CaseConfig = JSON.parse(JSON.stringify(config));
  cfg.scenarios = [
    { name: 'plus_one', label: 'Plus one', emoji: '', description: '', ops: [{ kind: 'variable', target: 'cash0', shift: 1 }] },
    { name: 'twice', label: 'Twice', emoji: '', description: '', ops: [{ kind: 'variable', target: 'cash0', scale: 2 }] },
  ];
  const lc = loadCase(cfg), cash0 = (s: CaseState) => effectiveModel(lc, s).model.variables.find(v => v.name === 'cash0')!.dist.value;
  const base = defaultState(lc);
  const ab = togglePreset(togglePreset(base, 'plus_one'), 'twice'), ba = togglePreset(togglePreset(base, 'twice'), 'plus_one');
  assert.equal(cash0(ab), (2_000_000 + 1) * 2);
  assert.equal(cash0(ba), (2_000_000 + 1) * 2, 'activation order does not matter');
  assert.equal(cash0(setEdit(ba, 'v|cash0|value', 5)), 5, 'the edit wins over the hypotheses');
  assert.deepEqual(fromPreset(lc, ba, 'v|cash0'), [['Plus one', 2_000_000, 2_000_001], ['Twice', 2_000_001, 4_000_002]]);
  assert.equal(cash0(base), 2_000_000, 'the template is not changed');
  // a real hypothesis: risk probability layering 1 - (1 - p)(1 - q)
  const pw = effectiveModel(c, togglePreset(defaultState(c), 'price_war')).model.risks.find(r => r.name === 'competitor_event')!;
  assert.ok(Math.abs(pw.p - (1 - 0.7 * 0.5)) < 1e-12);
});

test('timeline moves and placed events change the model; markers snap to half months inside the horizon', () => {
  let s = moveMarker(c, defaultState(c), 'ms_hire', 2.3);
  assert.equal(s.timeline.moves.ms_hire, 2.5);
  s = moveMarker(c, s, 'm_hire', 40);
  assert.equal(s.timeline.moves.m_hire, 12);
  s = toggleEvent(s, 'gpu_event');
  const m = effectiveModel(c, s).model, val = (n: string) => m.variables.find(v => v.name === n)!.dist.value;
  assert.equal(val('ms_hire'), 2.5);
  assert.equal(val('ev_gpu'), 1);
  assert.equal(toggleEvent(s, 'gpu_event').timeline.events.length, 0);
});

function busyState(): CaseState {
  let s = defaultState(c);
  s = setEdit(s, 'v|cash0|value', 1_500_000);
  s = setEdit(s, 'o|hire_5|lever_hire|value', 1);
  s = setWeight(s, 'cash', 2);
  s = setRiskAversion(s, 1);
  s = moveMarker(c, s, 'ms_hire', 5);
  s = toggleEvent(s, 'gpu_event');
  s = setItemOff(s, 'cash_goal', true);
  s = togglePreset(s, 'price_war');
  return s;
}

test('isModified uses prefixes; resetPath removes one change and the paths under it', () => {
  const s = busyState();
  assert.ok(isModified(s, 'v|cash0'));
  assert.ok(isModified(s, 'v|cash0|value'));
  assert.ok(!isModified(s, 'v|opex'));
  assert.ok(isModified(s, 'w'));
  assert.ok(isModified(s, 'v|ms_hire'), 'a moved marker is a modified month variable');
  assert.ok(isModified(s, 'ev|gpu_event') && isModified(s, 'tloff|cash_goal') && isModified(s, 'preset|price_war'));
  assert.ok(!isModified(defaultState(c), 'v'));
  const r1 = resetPath(c, s, 'v|cash0');
  assert.equal(r1.edits['v|cash0|value'], undefined);
  assert.equal(r1.edits['o|hire_5|lever_hire|value'], 1);
  assert.deepEqual(r1.timeline, s.timeline);
  const r2 = resetPath(c, applyPriority(c, s, 'cash_first'), 'w|cash');
  assert.equal(r2.prefs.weights.cash, undefined);
  assert.equal(r2.prefs.priority, null);
  assert.equal(r2.prefs.weights.churn, 0.5);
  assert.equal(resetPath(c, s, 'w|__risk').prefs.riskAversion, null);
  const r3 = resetPath(c, s, 'v');
  assert.deepEqual(r3.edits, { 'o|hire_5|lever_hire|value': 1 }, 'prefix v: variables and moves only, not option overrides');
  assert.deepEqual(r3.timeline.moves, {});
  assert.deepEqual(r3.activePresets, ['price_war']);
});

test('resetSection clears the parts of its section only; resetAll gives the defaults', () => {
  const s = busyState();
  const compare = resetSection(c, s, 'month-compare');
  assert.deepEqual(compare.prefs, { weights: {}, riskAversion: null, priority: null });
  assert.deepEqual(compare.edits, s.edits);
  const plan = resetSection(c, s, 'month-plan');
  assert.deepEqual(plan.timeline, { moves: {}, events: [], off: [] });
  assert.deepEqual(plan.prefs, s.prefs);
  const forks = resetSection(c, s, 'forks');
  assert.deepEqual(forks.activePresets, s.activePresets);
  assert.deepEqual(forks.edits, {});
  assert.deepEqual(forks.timeline, { moves: {}, events: [], off: [] });
  const world = resetSection(c, s, 'world');
  assert.deepEqual(world.activePresets, []);
  assert.deepEqual(world.edits, s.edits);
  assert.deepEqual(resetAll(c), defaultState(c));
  assert.throws(() => resetSection(c, s, 'nope' as never), /no section/);
});

class MemoryStorage implements StorageLike {
  data = new Map<string, string>();
  getItem(k: string) { return this.data.get(k) ?? null; }
  setItem(k: string, v: string) { this.data.set(k, v); }
  removeItem(k: string) { this.data.delete(k); }
}

test('saveState / loadState: round trip under the versioned key; broken data gives the defaults', () => {
  const st = new MemoryStorage(), s = busyState();
  assert.equal(stateKey(c.id), 'variator:case:company_best_year:state:v1');
  assert.ok(saveState(s, st));
  assert.ok(st.data.has('variator:case:company_best_year:state:v1'));
  assert.deepEqual(loadState(c, st), s);
  st.setItem(stateKey(c.id), '{not json');
  assert.deepEqual(loadState(c, st), defaultState(c));
  st.setItem(stateKey(c.id), JSON.stringify({ ...s, caseId: 'other' }));
  assert.deepEqual(loadState(c, st), defaultState(c));
  st.setItem(stateKey(c.id), JSON.stringify({ ...s, activePresets: ['price_war', 'gone'], edits: { 'v|gone|value': 1, 'v|cash0|value': 3 }, n: -5 }));
  const cleaned = loadState(c, st);
  assert.deepEqual(cleaned.activePresets, ['price_war']);
  assert.deepEqual(cleaned.edits, { 'v|cash0|value': 3 });
  assert.equal(cleaned.n, config.defaults.n);
  assert.ok(clearState(c.id, st));
  assert.deepEqual(loadState(c, new MemoryStorage()), defaultState(c));
});

test('storage that throws (or is missing) never breaks the page', () => {
  const bad: StorageLike = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('quota'); }, removeItem() { throw new Error('denied'); } };
  assert.equal(saveState(busyState(), bad), false);
  assert.deepEqual(loadState(c, bad), defaultState(c));
  assert.equal(clearState(c.id, bad), false);
  assert.equal(saveState(busyState(), null), false);
  assert.deepEqual(loadState(c, null), defaultState(c));
  // the global localStorage getter itself throws (blocked site data)
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('SecurityError'); } });
  try {
    assert.equal(saveState(busyState()), false);
    assert.deepEqual(loadState(c), defaultState(c));
  } finally {
    delete (globalThis as { localStorage?: unknown }).localStorage;
  }
});

test('rankRun on an old run equals the ranking of a fresh simulation with the new priorities', () => {
  const s0 = small(defaultState(c));
  const run0 = simulateCase(c, s0);
  const s1 = setRiskAversion(applyPriority(c, s0, 'clients_first'), 1);
  const instant = rankRun(run0, effectivePrefs(c, s1, run0.model));
  const run1 = simulateCase(c, s1);
  const fresh = rankRun(run1, effectivePrefs(c, s1, run1.model));
  assert.deepEqual(instant, fresh);
  assert.notDeepEqual(instant.order, rankRun(run0, effectivePrefs(c, s0, run0.model)).order, 'the priorities change the order');
});

const snap = (over: Partial<CaseSnapshot>): CaseSnapshot => ({
  j: 0, option: 'a', optionLabels: ['Option A', 'Option B'], decision: 0, goals: [], conflicts: [], k0: 100,
  crit0: { name: 'cash', label: 'Cash', unit: 'USD' }, ...over,
});
const goal = (name: string, p: number, extra: Partial<CaseSnapshot['goals'][number]> = {}) =>
  ({ name, label: name.toUpperCase(), emoji: '', p, p80: null, month: 4, movable: false, ...extra });

test('diffReports: P(met) changes of 1 point or more, a new leader, a missed target and the plan', () => {
  const before = snap({ goals: [goal('g1', 0.85), goal('g2', 0.9), goal('g3', 1)] });
  const after = snap({ decision: 1, goals: [goal('g1', 0.7, { p80: 4.2, movable: true }), goal('g2', 0.905), goal('g3', 0.99)] });
  const r = diffReports(before, after, 'Hiring starts: month 2 → 4');
  assert.deepEqual(r.pMetChanges.map(x => [x.goal, x.deltaPp]), [['g1', -15], ['g3', -1]]);
  assert.ok(r.effect.some(e => e.startsWith('G1: 85% → 70%')));
  assert.ok(!r.effect.some(e => e.startsWith('G2')), 'below 1 point is not reported');
  assert.deepEqual(r.leaderChanged, { from: 'Option A', to: 'Option B' });
  assert.ok(r.effect.includes('Best option: Option A → Option B'));
  assert.ok(r.risks.includes('G1 now misses the 80% target'));
  assert.ok(r.recommended.some(x => x.includes('month 4.5 or later')));
  assert.equal(r.missed, 1);
  assert.equal(r.requiredPending, true);
  assert.equal(r.tone, 'bad');
  const plan = { option: 'a', j: 0, steps: [{ variable: 'm_hire', label: 'Start hiring by month', unit: 'month', mode: null, modeValue: null, value: 1.5, current: 0 }], p: [0.85], names: ['g1'], met: true };
  const done = withPlan(r, plan);
  assert.equal(done.requiredPending, false);
  assert.deepEqual(done.required, ['Start hiring by month 1.5', 'Then all milestones and goals are met (button “Apply the plan”).']);
  const quiet = diffReports(before, before, 'nothing');
  assert.equal(quiet.tone, '');
  assert.deepEqual(quiet.effect, []);
  assert.deepEqual(quiet.required, ['Nothing: all milestones and goals are met.']);
});

test('a real change: an earlier hiring milestone and "Clients first" give a toast with the P(met) drop and a new leader', () => {
  const s0: CaseState = { ...small(defaultState(c)), timelineOption: 'hire_5' };
  const a0 = analyseRun(c, s0, simulateCase(c, s0));
  const s1 = applyPriority(c, moveMarker(c, s0, 'ms_hire', 2.5), 'clients_first');
  const a1 = analyseRun(c, s1, simulateCase(c, s1));
  const r = diffReports(a0.snapshot, a1.snapshot, 'New hires productive: month 4 → 2.5');
  const hires = r.pMetChanges.find(x => x.goal === 'hires_ready');
  assert.ok(hires && hires.deltaPp <= -1, JSON.stringify(r.pMetChanges));
  assert.ok(r.leaderChanged, 'Clients first changes the leader');
  assert.equal(r.option, 'hire_5');
  assert.ok(r.supporting.length > 0, 'the inputs in conflict give supporting changes');
});

test('suggested dates and the plan meet the hiring milestone again', () => {
  const s0: CaseState = { ...moveMarker(c, small(defaultState(c)), 'ms_hire', 2), timelineOption: 'hire_5' };
  const run0 = simulateCase(c, s0), j = run0.options.indexOf('hire_5');
  const tl0 = evaluateTimeline(run0, s0.timeline.off);
  const p0 = tl0.goals.find(g => g.name === 'hires_ready')!.p[j]!;
  assert.ok(p0 < 0.8);
  const moves = suggestDates(tl0, run0.model, j);
  assert.ok(moves.ms_hire > 2 && moves.ms_hire % 0.5 === 0, JSON.stringify(moves));
  const s1 = applyMoves(c, s0, moves);
  const p1 = evaluateTimeline(simulateCase(c, s1), s1.timeline.off).goals.find(g => g.name === 'hires_ready')!.p[j]!;
  assert.ok(p1 >= 0.78, `P(met) after the suggested dates: ${p1}`);
  const plan = solvePlan(c, s0, j);
  assert.equal(plan.option, 'hire_5');
  assert.deepEqual(plan.steps.map(x => x.variable), ['m_hire']);
  assert.equal(plan.names.includes('hires_ready'), true);
});

test('the engine client works without a Worker (in-thread fallback)', async () => {
  const client = createEngineClient(null);
  const { seq, result } = client.simulate(config, { ...defaultState(c), n: 200, seed: 3 });
  assert.equal(seq, 1);
  const run = await result;
  assert.equal(run.n, 200);
  assert.equal(run.options.length, 5);
  client.dispose();
});

test('case A (delivery_best_way) runs through the same engine: analysis, events, plan with a mode lever, sizes', () => {
  const cfgA = JSON.parse(readFileSync(new URL('../config/cases/delivery_best_way.json', import.meta.url), 'utf8')) as CaseConfig;
  const a = loadCase(cfgA), s = small(defaultState(a));
  const run = simulateCase(a, s), an = analyseRun(a, s, run);
  assert.deepEqual(run.options, ['in_house', 'nearshore_tm', 'offshore_fixed']);
  assert.deepEqual(run.criteria, ['money', 'time', 'resources', 'delay_months']);
  assert.equal(an.path.decision, 'offshore_fixed');
  const own = an.path.risks.filter(r => r.ref && cfgA.page.risks?.[r.ref]);
  assert.ok(own.every(r => r.emoji === '' && r.headline === cfgA.page.risks![r.ref!]), 'the case A risk headlines carry no emoji');
  const goLive = (st: CaseState) => evaluateTimeline(simulateCase(a, st), st.timeline.off).goals.find(g => g.name === 'go_live')!.p;
  const placed = toggleEvent(s, 'scope_change');
  assert.ok(goLive(placed).every((p, j) => p! <= goLive(s)[j]!), 'a placed scope change never makes the go-live more likely');
  const more = setEdit(s, 'v|add_fte|value', 3);
  assert.ok(simulateCase(a, more).stats[0][1].mean < run.stats[0][1].mean, 'extra people shorten the in-house time to go-live');
  const plan = solvePlan(a, s, 0, { n: 300 });
  assert.equal(plan.option, 'in_house');
  assert.ok(plan.steps.length > 0 && plan.steps.every(x => ['env_start', 'add_fte', 'add_month'].includes(x.variable)));
  for (const it of Object.values(cfgA.items)) if (it.valuePath) assert.ok(isModified(setEdit(s, it.valuePath, 1), it.valuePath));
});

test('snapTies: neighbours closer than the meaningful difference take the best value of their group; 0 changes nothing', () => {
  assert.deepEqual(snapTies([0, 0.004, 1, 1.003], true, 0.01), [0.004, 0.004, 1.003, 1.003]);
  assert.deepEqual(snapTies([0.004, 0, 1.003, 1], false, 0.01), [0, 0, 1, 1]);
  const vals = [3, 1, 2];
  assert.equal(snapTies(vals, true, 0), vals);
  assert.deepEqual(snapTies([0, 0.6, 1.2], true, 1), [1.2, 1.2, 1.2]);
});

test('meaningful differences: gaps below them never change the ranking; all of them 0 is the plain ranking', () => {
  const s = small(defaultState(c));
  const run = simulateCase(c, s), prefs = effectivePrefs(c, s, run.model);
  assert.deepEqual(run.ties, [100_000, 1, 0.005, 0.0005, 1], 'md_cash, md_runway, md_churn, md_sla, md_nps');
  const O = run.options.length, K = run.criteria.length, k = run.criteria.indexOf('churn'), q = run.ties[k];
  const withX = (f: (s: number, j: number) => number) => {
    const X = Float64Array.from(run.X);
    for (let i = 0; i < run.n; i++) for (let j = 0; j < O; j++) X[(i * O + j) * K + k] = f(i, j);
    return { ...run, X };
  };
  const rnd = mulberry32(5), noise = Array.from({ length: run.n * O }, () => 0.9 * q * rnd());
  const near = withX((i, j) => run.X[(i * O) * K + k] + noise[i * O + j]), flat = withX(i => run.X[(i * O) * K + k]);
  // the same anchors in both (the churn anchors of the baseline are kept), so only the tie rule differs
  const same = (r: typeof run) => ({ ...r, anchors: run.anchors });
  const key = (r: ReturnType<typeof rankRun>) => [r.score, r.best, r.order];
  assert.deepEqual(key(rankRun(same(near), prefs)), key(rankRun(same(flat), prefs)), 'a churn spread below 0.5 pp is a tie');
  const zero = (r: typeof run) => ({ ...same(r), ties: r.ties.map(() => 0) });
  assert.notDeepEqual(key(rankRun(zero(near), prefs)), key(rankRun(zero(flat), prefs)), 'without the threshold the noise counts');
  // every md_* at 0 through the edits: the ties of the run are 0, as a run without them
  const off = ['md_cash', 'md_runway', 'md_churn', 'md_sla', 'md_nps'].reduce((st, v) => setEdit(st, `v|${v}|value`, 0), s);
  const run0 = simulateCase(c, off);
  assert.deepEqual(run0.ties, [0, 0, 0, 0, 0]);
  assert.deepEqual(rankRun(run0, prefs), rankRun({ ...run0, ties: [] }, prefs), 'all thresholds 0 = no thresholds');
});

test('fixed anchors: a change of one fork never changes the score of another fork (QA M3)', () => {
  const s0 = small(defaultState(c)), run0 = simulateCase(c, s0), prefs = effectivePrefs(c, s0, run0.model);
  const hire = run0.options.indexOf('hire_5'), others = (r: ReturnType<typeof rankRun>) => r.score.filter((_x, j) => j !== hire);
  for (const month of [0.5, 3, 6]) {
    const s1 = moveMarker(c, s0, 'm_hire', month), run1 = simulateCase(c, s1);
    assert.deepEqual(others(rankRun(run1, prefs)), others(rankRun(run0, prefs)), `hiring starts in month ${month}`);
  }
  const X = Float64Array.from(run0.X), O = run0.options.length, K = run0.criteria.length;
  for (let i = 0; i < run0.n; i++) for (let k = 0; k < K; k++) X[(i * O + hire) * K + k] *= 0.5;
  const halved = { ...run0, X, stats: run0.stats.map((r, j) => (j === hire ? r.map(st => ({ ...st, mean: st.mean * 0.5, cvar: st.cvar * 0.5 })) : r)) };
  assert.deepEqual(others(rankRun(halved, prefs)), others(rankRun(run0, prefs)), 'a very different hire_5');
});

test('the trade-off and the best path name the criteria that are equal within the meaningful difference', () => {
  // clients first: keep the plan and reduce tech debt lead, equal on every criterion within the meaningful difference
  const s = applyPriority(c, small(defaultState(c)), 'clients_first'), run = simulateCase(c, s), a = analyseRun(c, s, run);
  assert.ok(a.ranking.tradeoff.equal.length > 0, a.ranking.tradeoff.text);
  assert.match(a.ranking.tradeoff.text, /equal within the meaningful difference on: .+\./i);
  assert.deepEqual(a.path.ties, a.ranking.tradeoff.equal);
  // a fork that is better says by how much (cash x2 favours the price rise: better on cash, worse on churn)
  const cash = analyseRun(c, setWeight(s, 'cash', 3), run);
  assert.match(cash.ranking.tradeoff.text, /is better than .+ on: .*\(by [^)]+\)/, cash.ranking.tradeoff.text);
  const off = ['md_cash', 'md_runway', 'md_churn', 'md_sla', 'md_nps'].reduce((st, v) => setEdit(st, `v|${v}|value`, 0), s);
  const b = analyseRun(c, off, simulateCase(c, off));
  assert.deepEqual([b.ranking.tradeoff.equal, b.path.ties], [[], []]);
  assert.doesNotMatch(b.ranking.tradeoff.text, /meaningful/);
});

test('case B item sizes are editable: a bigger placed GPU shortage costs more cash than the default one', () => {
  const s = toggleEvent(small(defaultState(c)), 'gpu_event');
  const big = setEdit(s, config.items.gpu_event.valuePath!, 0.8);
  const cash = (st: CaseState) => simulateCase(c, st).stats[0][0].mean;
  assert.equal(config.items.gpu_event.valuePath, 'v|ev_gpu_size|value');
  assert.ok(cash(big) < cash(s));
  assert.equal(config.items.hire_start.value?.variable, 'hire_count');
  assert.equal(config.items.price_start.valuePath, 'o|raise_prices_8|price_change|value');
});

test('people formats: money with $, scenarios not futures, readable breaks; the engine texts stay as on the page', () => {
  assert.equal(money(1_920_000), '$1.92M');
  assert.equal(money(-1_200_000), '−$1.20M');
  assert.equal(money(250_000), '$250k');
  assert.equal(money(900), '$900');
  assert.equal(scenarios(4096), '4,096 scenarios');
  assert.equal(peopleValue(0.9978, 'share'), '99.78%');
  assert.equal(peopleValue(0.214, 'share'), '21.4%');
  assert.equal(peopleValue(1.5e6, 'USD/year'), '$1.50M a year');
  assert.equal(peopleValue(60, 'months'), '60+ months');
  assert.equal(peopleText('Cash at year end below 1.62M USD; expected -2.10M USD, cost 5000 USD, infra 1.50M USD/year'),
    'Cash at year end below $1.62M; expected −$2.10M, cost $5k, infra $1.50M a year');
  assert.equal(peopleText('Set Hires to month 4.5 or later: then it is met in 8 of 10 futures'), 'Set Hires to month 4.5 or later: then it is met in 8 of 10 scenarios');
  assert.equal(peopleText('💰 You may need a credit line'), 'You may need a credit line');
  assert.equal(noEmoji('🧑‍💻 Key engineer leaves ⚡'), 'Key engineer leaves');
  const M = c.base;
  assert.equal(breakText(M, 'weight of nps x2'), 'NPS matters twice as much');
  assert.equal(breakText(M, { change: 'weight of cash x0.5' }), 'cash at year end matters half as much');
  assert.equal(breakText(M, 'risk aversion 1'), 'you judge on the worst 5% of the scenarios');
  assert.equal(cautionWords(0.25), 'the average with a margin');
  // a real run: the engine output keeps the page's form; the helpers only change how it reads
  const s = small(defaultState(c)), run = simulateCase(c, s), a = analyseRun(c, s, run);
  assert.match(a.path.strategy, / USD\./);
  assert.doesNotMatch(peopleText(a.path.strategy), /USD/);
  for (const r of a.path.risks) assert.doesNotMatch(peopleText(r.text), /USD|futures|\p{Extended_Pictographic}/u);
  for (const b of a.path.breaks) assert.doesNotMatch(breakText(M, b), /weight of|risk aversion|x0\.5|x2/);
});
