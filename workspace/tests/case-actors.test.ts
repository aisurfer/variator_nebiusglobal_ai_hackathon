// WP23: the adapters of the actor engine's files to the case page (case/actors/adapt.ts) and the mapping of the page
// state to a rerun. A small synthetic month file in the schema of docs/knowledge/future-engine.md; the real artifact of
// case B is checked too when it is on this machine (/tmp/variator-1000/mvp-artifacts/future/company_best_year).
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { buildRuntime } from '../src/variator/case/model/view.js';
import { actorInput, calendarHeadline, DEFAULT_KEY, forkKey, futureView, isActorMonth, rankKey, simKey, verdictOf } from '../src/variator/case/actors/adapt.js';
import { monthBody } from '../src/variator/case/actors/pageState.js';
import { actorRivals, formulaScoreSpread } from '../src/variator/case/actors/scoreSpread.js';
import { branchLink, branchesOf } from '../src/variator/case/actors/branch.js';
import { baselineOf, dayPaths, eventTreeView, examplePaths, isEventTree, LABEL_ROW, layoutEventTree, lineBoxes, metricRange, overlaps, pathPoints, pathWeeks, placeLabels, shortEvent, splitLabel, splitShort, splitTone, textWidth, type Box, type EventTreeRaw, type EventTreeView, type PlacedLabel, type TreeLayout } from '../src/variator/case/actors/eventTreeModel.js';
import { dateTime, dayMonth, longDate, money, moneyShort, pBestScope, weekdayDate } from '../src/variator/case/ui/format.js';
import { numbersOf } from '../src/variator/case/model/numbers.js';
import { RERUN_WHY, rerunWhy } from '../src/variator/case/actors/rerun.js';
import type { ActorMonth, DayFile } from '../src/variator/case/actors/types.js';
import type { CaseConfig as EngineConfig, CaseIndexEntry, PreviewFuture } from '../src/variator/engine/index.js';

const dir = new URL('../config/cases/', import.meta.url);
const read = <T>(name: string) => JSON.parse(readFileSync(new URL(name, dir), 'utf8')) as T;
const index = read<{ cases: CaseIndexEntry[] }>('index.json');
function view(id: string) {
  const entry = index.cases.find(c => c.id === id)!;
  const config = read<EngineConfig>(entry.config!), preview = entry.preview ? read<PreviewFuture>(entry.preview) : null;
  return buildRuntime({ entry, config, preview, origin: null });
}
const B = view('company_best_year'), cfg = B.view;

// ------------------------------------------------------------------------------------------------ a synthetic month
const H = 12;
const fan = (v0: number, step: number) => Array.from({ length: H + 1 }, (_, m) => ({ month: m, p5: v0 + step * m - 5 * m, p10: v0 + step * m - 4 * m, p25: v0 + step * m - 2 * m, p50: v0 + step * m, p75: v0 + step * m + 2 * m, p90: v0 + step * m + 4 * m, p95: v0 + step * m + 5 * m }));
const cum = (first: number[]) => first.map((_, i) => +first.slice(0, i + 1).reduce((a, b) => a + b, 0).toFixed(5));
function row(id: string, kind: string, label: string, first: number[], median: number | null) {
  const by = cum(first);
  return { id, label, consequence: `${label}: what it means`, kind, ref: kind === 'threshold' ? null : id, pByMonth: by, pEver: by[by.length - 1], medianFirstMonth: median, pFirstByMonth: first };
}
const zero = Array(H).fill(0);
const churnFirst = [0, 0, 0, 0.01, 0.02, 0.03, 0.04, 0.07, 0.07, 0.07, 0.09, 0.07];
const forks = ['status_quo', 'pay_tech_debt', 'hire_5', 'reserve_gpu', 'raise_prices_8'];
const stats = (cash: number, churn: number) => ({ cash: { mean: cash, p5: cash - 7e5, p50: cash, p95: cash + 7e5 }, runway: { mean: 50, p5: 12, p50: 60, p95: 60 }, churn: { mean: churn, p5: 0.08, p50: churn, p95: 0.35 }, sla: { mean: 0.9994, p5: 0.999, p50: 0.9994, p95: 0.9997 }, nps: { mean: 29, p5: 18, p50: 29, p95: 40 } });
const level = (w: number, n: number) => Array.from({ length: n }, (_, i) => ({ id: `w${w}n${i}`, week: w, parent: w === 0 ? -1 : Math.min(i, w === 1 ? 0 : 1), share: 1 / n, values: { cash: 2e6 - 1e4 * w + 1e4 * i, load: 0.9, incidents: i, arr: 12e6 }, summary: `Week ${w}: node ${i}` }));
const tree = (fork: string) => ({
  preview: false, source: 'actors', option: fork, label: fork, k: 3, maxNodes: 200, weeks: 2,
  levels: [level(0, 1), level(1, 2), level(2, 3)],
  bands: { cash: [0, 1, 2].map(w => ({ week: w, p10: 2e6 - 1e4 * w, p50: 2e6 - 5e3 * w, p90: 2e6 + 1e4 * w })) },
  examples: (['pessimistic', 'median', 'optimistic'] as const).map((kind, i) => ({ kind, fork, leaf: `w2n${i}`, nodes: ['w0n0', `w1n${Math.min(i, 1)}`, `w2n${i}`], events: [{ week: 1, type: 'client', text: `Client event ${i}`, entityId: 'org.CL-004' }], weeks: [] })),
});
const days = (fork: string) => (['optimistic', 'pessimistic', 'median'] as const).map(kind => ({
  preview: false, source: 'actors', option: fork, kind, label: kind[0].toUpperCase() + kind.slice(1), summary: `${kind} example: cash after 52 weeks $2.00M.`, yearEnd: { cash: kind === 'pessimistic' ? 1.7e6 : kind === 'median' ? 2.1e6 : 2.5e6 },
  days: [{ date: '2026-10-27', values: { cash: 2e6, load: 0.9, incidents: 1 } }, { date: '2026-10-28', values: { cash: 1.99e6, load: 0.92, incidents: 0 } }],
  events: [{ date: '2026-10-27', type: 'incident', text: 'SEV3 incident' }, { date: '2026-10-28', type: 'client', text: 'Orchard Media gives notice', entityId: 'org.CL-048' }],
}));
const metric = (id: string, label: string, unit: string, higher: boolean) => ({ id, label, unit, higher, description: label });
function month(over: Partial<ActorMonth> = {}): ActorMonth {
  return {
    preview: false, source: 'actors', caseId: 'company_best_year', note: 'Month Monte Carlo on actors.', seed: 7, n: 1000, anchor: '2026-10-26', horizonMonths: H,
    metrics: [metric('cash', 'Cash', 'USD', true), metric('churn', 'Churn', 'share', false), metric('runway', 'Runway', 'months', true), metric('load', 'Team load', 'ratio', false)],
    weekMetrics: [metric('cash', 'Cash', 'USD', true), metric('incidents', 'Incidents', 'count', false)],
    dayMetrics: [metric('cash', 'Cash', 'USD', true), metric('load', 'Team load', 'ratio', false), metric('incidents', 'Incidents', 'count', false)],
    options: Object.fromEntries(forks.map(f => [f, {
      preview: false, option: f, label: f, fans: { cash: fan(2e6, 2e4), churn: fan(0.1, 0.01), runway: fan(60, 0), load: fan(0.9, 0) },
      riskCalendar: [row('cash_negative', 'threshold', 'Cash below zero', zero, null), row('runway_low', 'check', 'Runway below 6 months', [...zero.slice(0, 11), 0.003], 12), row('churn_high', 'flag', 'Churn above 20%', churnFirst, 9)],
    }])),
    ranking: {
      weights: { cash: 0.27, runway: 0.13, churn: 0.27, sla: 0.2, nps: 0.13 }, riskAversion: 0.25,
      options: [
        { option: 'reserve_gpu', rank: 1, score: 0.75, scoreTimesFeasible: 0.745, pBest: 0.551, pFeasible: 0.992, stats: stats(2.22e6, 0.194) },
        { option: 'pay_tech_debt', rank: 2, score: 0.71, scoreTimesFeasible: 0.708, pBest: 0.287, pFeasible: 0.997, stats: stats(2.48e6, 0.1944) },
        { option: 'status_quo', rank: 3, score: 0.62, pBest: 0.024, pFeasible: 0.997, stats: stats(2.48e6, 0.1948) },
        { option: 'hire_5', rank: 4, score: 0.6, pBest: 0.132, pFeasible: 0.959, stats: stats(1.97e6, 0.1937) },
        { option: 'raise_prices_8', rank: 5, score: 0.4, pBest: 0.006, pFeasible: 0.99, stats: stats(2.95e6, 0.3) },
      ],
      tradeoffs: ['reserve_gpu is better than pay_tech_debt on: churn, sla, nps', 'pay_tech_debt is better than reserve_gpu on: cash, runway'],
    },
    weekTrees: { reserve_gpu: tree('reserve_gpu'), pay_tech_debt: tree('pay_tech_debt') },
    dayExamples: { reserve_gpu: days('reserve_gpu'), pay_tech_debt: days('pay_tech_debt') },
    ...over,
  } as ActorMonth;
}

// ------------------------------------------------------------------------------------------------ futureView
test('a month file of the actor engine is told from a preview', () => {
  assert.ok(isActorMonth(month()));
  assert.ok(!isActorMonth(read<PreviewFuture>('company_best_year.preview.json')));
  assert.ok(!isActorMonth({ source: 'actors', preview: true }));
});

test('futureView keeps the 5–95% band, the first-month row and the example leaves', () => {
  const v = futureView(cfg, month());
  assert.equal(v.source, 'actors'); assert.equal(v.preview, false); assert.equal(v.n, 1000);
  assert.deepEqual(Object.keys(v.fans).sort(), [...forks].sort());
  const end = v.fans.hire_5.cash[H];
  assert.ok(end.p5! < end.p10 && end.p95! > end.p90, 'p5 and p95 outside p10 and p90');
  const churn = v.calendar.status_quo.find(r => r.id === 'churn_high')!;
  assert.deepEqual(churn.firstByMonth, churnFirst);
  assert.equal(churn.medianMonth, 9); assert.ok(Math.abs(churn.total - 0.47) < 1e-9);
  // the tree: parents by id, the example paths marked, the week's events from the example paths
  const nodes = v.tree.reserve_gpu, w2n2 = nodes.find(n => n.id === 'w2n2')!;
  assert.equal(w2n2.parent, 'w1n1'); assert.equal(nodes.find(n => n.id === 'w0n0')!.parent, null);
  assert.deepEqual(v.marked.reserve_gpu.map(m => m.kind), ['pessimistic', 'median', 'optimistic']);
  assert.deepEqual(v.marked.reserve_gpu[2].nodes, ['w0n0', 'w1n1', 'w2n2']);
  assert.ok(nodes.find(n => n.id === 'w1n1')!.events.includes('Client event 1'));
  assert.deepEqual(v.covers.sort(), ['pay_tech_debt', 'reserve_gpu']);
  assert.ok(v.weekly.reserve_gpu.every(r => r.week > 0));
  assert.deepEqual(v.criteria, ['cash', 'churn', 'runway'], 'criteria among the monthly metrics, the rest are chain values');
});

test('the day examples: the bridge of the tree leaves, or WP19\'s day file with its branch tenants', () => {
  const bridge = futureView(cfg, month());
  assert.equal(bridge.daySource, 'bridge');
  assert.deepEqual(bridge.days.reserve_gpu.map(e => e.id), ['pessimistic', 'median', 'optimistic'], 'ordered bad, typical, good');
  const med = bridge.days.reserve_gpu[1];
  assert.equal(med.headline.value, 2.1e6); assert.deepEqual(med.daily.map(d => d.day), [1, 2], 'days count from the fork date');
  assert.equal(med.events[0].title, 'Incident'); assert.equal(med.events[0].tone, 'bad');
  assert.equal(med.tenant, undefined);
  const day: DayFile = {
    preview: false, caseId: 'company_best_year', source: 'actors', note: 'forward run', seed: 7, anchor: '2026-10-26', horizonMonths: 6,
    dayMetrics: [metric('cash', 'Cash', 'USD', true), metric('availability', 'Availability', 'share', true)],
    dayExamples: { pay_tech_debt: days('pay_tech_debt').map(e => ({ ...e, tenant: `company-b-pay_tech_debt-p${e.kind === 'pessimistic' ? 10 : e.kind === 'median' ? 50 : 90}` })) },
  };
  const v = futureView(cfg, month(), day);
  assert.equal(v.daySource, 'day-file'); assert.deepEqual(v.dayFileForks, ['pay_tech_debt']);
  assert.deepEqual(v.days.pay_tech_debt.map(e => e.tenant), ['company-b-pay_tech_debt-p10', 'company-b-pay_tech_debt-p50', 'company-b-pay_tech_debt-p90']);
  assert.deepEqual(v.dayMetrics.map(m => m.id), ['cash', 'availability']);
  // a day file of another tree (its leaves are not this tree's): the bridge of the month file for that fork
  const old = { ...day, dayExamples: { pay_tech_debt: day.dayExamples.pay_tech_debt.map(e => ({ ...e, leaf: 'w52n199' })) } };
  const w = futureView(cfg, month(), old);
  assert.equal(w.daySource, 'bridge'); assert.deepEqual(w.dayFileForks, []);
  assert.equal(w.days.pay_tech_debt[0].tenant, undefined);
  // with the tree's leaves it is used
  const fit = { ...day, dayExamples: { pay_tech_debt: day.dayExamples.pay_tech_debt.map(e => ({ ...e, leaf: `w2n${['pessimistic', 'median', 'optimistic'].indexOf(e.kind)}` })) } };
  assert.equal(futureView(cfg, month(), fit).daySource, 'day-file');
});

test('futureView reads a preview file too (the fallback)', () => {
  const p = read<PreviewFuture>('company_best_year.preview.json');
  const v = futureView(cfg, p as unknown as ActorMonth);
  assert.equal(v.source, 'preview'); assert.equal(v.daySource, 'preview');
  assert.equal(Object.keys(v.fans).length, 5);
  assert.ok(Object.values(v.marked).every(m => m.length === 0));
});

// ------------------------------------------------------------------------------------------------ verdict and close call
test('the verdict: leader, P(best), runner-up and the engine\'s tradeoff in labels', () => {
  const v = verdictOf(cfg, month())!;
  assert.equal(v.leader, 'reserve_gpu'); assert.equal(v.runnerUp, 'pay_tech_debt');
  assert.equal(v.pBest, 0.551); assert.equal(v.runnerPBest, 0.287);
  assert.equal(v.tradeoff, 'Reserve GPU capacity is better on client churn, availability (SLA) and NPS; Reduce tech debt on cash at year end and runway.');
  assert.deepEqual(v.ties, []); assert.equal(v.tieLine, '');
  // forks switched off leave the verdict
  const off = verdictOf(cfg, month(), ['status_quo', 'pay_tech_debt', 'hire_5'])!;
  assert.equal(off.leader, 'pay_tech_debt'); assert.equal(off.runnerUp, 'status_quo');
  // the P(best) shown is still out of every fork the actors ranked (the forks of the month file)
  assert.equal(v.pBestOf, month().ranking!.options.length); assert.equal(off.pBestOf, month().ranking!.options.length);
});

test('ties from the engine leave the tradeoff and are named', () => {
  const m = month();
  m.ranking!.ties = ['sla', 'nps'];
  const v = verdictOf(cfg, m)!;
  assert.equal(v.tradeoff, 'Reserve GPU capacity is better on client churn; Reduce tech debt on cash at year end and runway.');
  assert.equal(v.tieLine, 'Equal to Reduce tech debt within the meaningful difference on: availability (SLA) and NPS.');
  assert.equal(v.allTied, false);
});

test('a full tie: the engine\'s «equal» line and the best path\'s ties; no close call input', () => {
  const m = month();
  const [a, b] = [m.ranking!.options[0], m.ranking!.options[1]];
  a.option = 'status_quo'; b.option = 'pay_tech_debt'; m.ranking!.options[2].option = 'reserve_gpu';
  a.pBest = 0.42; b.pBest = 0.47;
  m.ranking!.tradeoffs = ['status_quo and pay_tech_debt are equal within the meaningful difference on: cash, runway, churn, sla, nps'];
  const v = verdictOf(cfg, m)!;
  assert.equal(v.leader, 'status_quo'); assert.equal(v.tradeoff, '');
  assert.equal(v.tieLine, 'Equal to Reduce tech debt within the meaningful difference on: cash at year end, runway, client churn, availability (SLA) and NPS.');
  assert.ok(v.allTied);
  // the best path's ties win over the line
  m.ranking!.bestPath = { decision: 'status_quo', runner_up: 'pay_tech_debt', ties: ['cash'] };
  assert.deepEqual(verdictOf(cfg, m)!.ties, ['cash at year end']);
  // the close call sees no difference on tied criteria
  m.ranking!.bestPath = { decision: 'status_quo', runner_up: 'pay_tech_debt', ties: ['cash', 'runway', 'churn', 'sla', 'nps'] };
  const pw = actorInput(cfg, m)!.pairwise('status_quo', 'pay_tech_debt');
  assert.ok(pw.every(x => x.bWins === 0 && x.scoreDiff === 0 && x.meanDiff === 0));
});

test('the actor ranking as the input of the close call', () => {
  // (closeCallOf itself imports the engine worker: the browser checks it, case-actors.spec.ts)
  const inp = actorInput(cfg, month())!;
  assert.deepEqual(inp.order, ['reserve_gpu', 'pay_tech_debt', 'status_quo', 'hire_5', 'raise_prices_8']);
  assert.equal(inp.pBest.pay_tech_debt, 0.287);
  assert.equal(inp.weights.cash, 0.27);
  const pw = inp.pairwise('reserve_gpu', 'pay_tech_debt'), cash = pw.find(x => x.crit === 'cash')!, churn = pw.find(x => x.crit === 'churn')!;
  assert.ok(cash.bWins >= 0.5 && cash.medianMargin > 0 && cash.meanDiff < 0 && cash.scoreDiff < 0, 'the runner-up has more cash');
  assert.ok(churn.bWins < 0.5 && churn.scoreDiff > 0, 'the leader has less churn');
  assert.ok(inp.spread('reserve_gpu', 'cash') > 0);
  assert.deepEqual(inp.feasible, { reserve_gpu: 0.992, pay_tech_debt: 0.997, status_quo: 0.997, hire_5: 0.959, raise_prices_8: 0.99 });
  // the engine's exact rows (d = rival − leader in the better direction, scoreDiff after the weight) in the close
  // call's terms: a − b in raw units, before the weight; either way round
  const m = month();
  (m.ranking as unknown as { pairwise: unknown[] }).pairwise = [
    { a: 'reserve_gpu', b: 'pay_tech_debt', crit: 'cash', bWins: 0.81, aWins: 0.1, meanDiff: 2.5e5, medianMargin: 2.4e5, scoreDiff: 0.08, tie: 1e5 },
    { a: 'reserve_gpu', b: 'pay_tech_debt', crit: 'churn', bWins: 0.2, aWins: 0.5, meanDiff: -0.004, medianMargin: -0.003, scoreDiff: -0.05, tie: 0.005 },
    { a: 'reserve_gpu', b: 'pay_tech_debt', crit: 'total', bWins: 0.4, aWins: 0.6, meanDiff: -0.01, medianMargin: 0, scoreDiff: -0.02, tie: 0 },
  ];
  const ex = actorInput(cfg, m)!, fwd = ex.pairwise('reserve_gpu', 'pay_tech_debt');
  assert.deepEqual(fwd.map(x => x.crit), ['cash', 'churn'], 'the total row is not a criterion');
  const c1 = fwd[0], c2 = fwd[1];
  assert.equal(c1.bWins, 0.81); assert.equal(c1.meanDiff, -2.5e5, 'cash: the leader has $250k less'); assert.equal(c1.medianMargin, 2.4e5);
  assert.ok(Math.abs(c1.scoreDiff - -0.08 / 0.27) < 1e-12, 'before the weight');
  assert.equal(c2.meanDiff, -0.004, 'churn (lower is better): d < 0, the rival is worse, so the leader has 0.4 pp less');
  const back = ex.pairwise('pay_tech_debt', 'reserve_gpu');
  assert.equal(back[0].bWins, 0.1); assert.equal(back[0].meanDiff, 2.5e5);
  // equal within the meaningful difference: the mean gap of a row within its tie (no engine ties named here)
  assert.deepEqual(ex.equal!('reserve_gpu', 'pay_tech_debt'), ['churn']);
});

// ------------------------------------------------------------------------------------------------ the calendar headline
test('the headline of the risk calendar is built from its rows', () => {
  const v = futureView(cfg, month());
  assert.equal(calendarHeadline(cfg, v.calendar.status_quo, 1000), 'Cash stays above zero in all 1,000 scenarios. In 47% of them: churn above 20%, typically by Jul ’27.');
  const rows = v.calendar.status_quo.map(r => r.kind === 'threshold' ? { ...r, total: 0.12, medianMonth: 10 } : r);
  assert.equal(calendarHeadline(cfg, rows, 1000), 'In 12% of the 1,000 scenarios: cash below zero, typically by Aug ’27. In 47% of them: churn above 20%, typically by Jul ’27.');
  assert.equal(calendarHeadline(cfg, v.calendar.status_quo.filter(r => r.id !== 'churn_high'), 1000), 'Cash stays above zero in all 1,000 scenarios. No other check fails in more than 1 in 200.');
  // a progress row (case A «Live by the month», P(live by month m)) is no failure
  const live = { id: 'go_live_by', label: 'Live by the month', consequence: 'The platform is live', kind: 'progress', rowType: 'progress' as const, byMonth: Array(12).fill(1), medianMonth: 10, total: 1 };
  assert.equal(calendarHeadline(cfg, [...v.calendar.status_quo, live], 1000), 'Cash stays above zero in all 1,000 scenarios. In 47% of them: churn above 20%, typically by Jul ’27.');
});

// ------------------------------------------------------------------------------------------------ the page state
test('the page state maps to the rerun body: CaseState plus the forks that are on', () => {
  // the body of WP18's route (docs/knowledge/future-engine.md, Live API): activePresets, edits, prefs, timeline, n, seed, forks
  const monthVar = [...B.monthVars][0], cfgVars = B.config.variables.map(v => v.name);
  const sizeVar = cfgVars.find(v => v === 'hire_count')!, risk = B.config.risks[0].name, opt = B.config.options.find(o => Object.keys(o.overrides).length)!;
  const ovr = Object.keys(opt.overrides)[0], event = B.config.timeline!.items.find(i => i.kind === 'event')!.name, goal = B.config.timeline!.items.find(i => i.condition)!.name;
  const hyp = B.config.scenarios[0].name;
  const edits = {
    [`v|${monthVar}`]: { kind: 'fixed' as const, value: 4 }, [`v|${sizeVar}`]: { kind: 'fixed' as const, value: 7 }, [`o|${opt.name}|${ovr}`]: { kind: 'pert' as const, low: 1, mode: 2, high: 4 },
    [`r|${risk}|p`]: 0.4, [`ev|${event}`]: true, [`goal|${goal}|off`]: true, [`hyp|${hyp}`]: true, 'fork|reserve_gpu': false,
    'w|cash': 0.5, caution: 'worst', priority: 'custom', 'sim|n': 4096, week: ['hire_5'], dayExample: 'median',
  };
  const forksOn = ['status_quo', 'pay_tech_debt', 'hire_5', 'raise_prices_8'];
  const body = monthBody(B, edits, forksOn, 1000, 7);
  assert.deepEqual(body.forks, forksOn);
  assert.equal(body.n, 1000, 'the actor run keeps its own n (the formula model runs 4096)'); assert.equal(body.seed, 7);
  assert.equal(body.timeline.moves[monthVar], 4, 'a lever month is a timeline move');
  assert.equal(body.edits[`v|${sizeVar}|value`], 7, 'a lever size is an edit of its variable');
  assert.deepEqual([body.edits[`o|${opt.name}|${ovr}|low`], body.edits[`o|${opt.name}|${ovr}|mode`], body.edits[`o|${opt.name}|${ovr}|high`]], [1, 2, 4]);
  assert.equal(body.edits[`r|${risk}|p`], 0.4);
  assert.deepEqual(body.timeline.events, [event]); assert.deepEqual(body.timeline.off, [goal]);
  assert.deepEqual(body.activePresets, [hyp]);
  assert.equal(body.prefs.weights.cash, 0.5); assert.equal(body.prefs.riskAversion, cfg.caution.find(c => c.id === 'worst')!.value);
  assert.deepEqual(body.selectedForks, [], 'view choices stay out of the body');
  // the defaults: no edit, every fork
  const d = monthBody(B, {}, cfg.forks.map(f => f.id), 1000, 7);
  assert.deepEqual(d.edits, {}); assert.deepEqual(d.activePresets, []); assert.deepEqual(d.timeline.events, []);
});

test('the keys of a result: priorities and forks on or off change the ranking only, view choices nothing', () => {
  assert.equal(simKey({}), DEFAULT_KEY); assert.equal(rankKey({}), DEFAULT_KEY);
  assert.equal(simKey({ week: ['hire_5'], day: ['hire_5'], dayExample: 'pessimistic' }), DEFAULT_KEY);
  assert.equal(rankKey({ week: ['hire_5'] }), DEFAULT_KEY);
  assert.equal(simKey({ 'w|cash': 0.5, caution: 'worst', priority: 'cash', 'fork|hire_5': false }), DEFAULT_KEY);
  assert.notEqual(rankKey({ 'w|cash': 0.5 }), DEFAULT_KEY);
  assert.notEqual(rankKey({ 'fork|hire_5': false }), DEFAULT_KEY);
  assert.notEqual(simKey({ 'hyp|price_war': true }), DEFAULT_KEY);
  assert.equal(simKey({ 'v|a': 1, 'v|b': 2 }), simKey({ 'v|b': 2, 'v|a': 1 }), 'the order of the edits does not matter');
});

// ------------------------------------------------------------------------------------------------ the real artifact of case B
const REAL = '/tmp/variator-1000/mvp-artifacts/future/company_best_year/month.json';
test('the month file of case B on this machine', { skip: !existsSync(REAL) && 'no artifact here' }, () => {
  const m = JSON.parse(readFileSync(REAL, 'utf8')) as ActorMonth;
  assert.ok(isActorMonth(m));
  const v = futureView(cfg, m);
  assert.deepEqual(Object.keys(v.fans).sort(), cfg.forks.map(f => f.id).sort(), 'every fork has fans');
  for (const f of Object.keys(v.fans)) for (const c of v.criteria) for (const p of v.fans[f][c]) assert.ok(p.p5! <= p.p25 && p.p25 <= p.p50 && p.p50 <= p.p75 && p.p75 <= p.p95!, `${f} ${c} month ${p.month}: ordered quantiles`);
  for (const [f, marks] of Object.entries(v.marked)) {
    const ids = new Set(v.tree[f].map(n => n.id));
    for (const mk of marks) assert.ok(mk.nodes.every(id => ids.has(id)), `${f} ${mk.kind}: the path is in the tree`);
  }
  const verdict = verdictOf(cfg, m)!;
  assert.ok(cfg.forks.some(f => f.id === verdict.leader));
  assert.ok(verdict.tradeoff.length > 0 || verdict.tieLine.length > 0, 'a tradeoff or a tie is said');
  assert.match(calendarHeadline(cfg, v.calendar[verdict.leader], m.n), /1,000 scenarios/);
});

// ------------------------------------------------------------------------------------------------ the knowledge-base branch
test('«Open this future in the knowledge base» turns on when the company lists the example\'s branch', () => {
  const t = 'company-b-pay_tech_debt-p50';
  assert.deepEqual(branchLink(undefined, { branches: [{ tenant: t }] }), { open: false, params: null, why: branchLink(undefined, {}).why }, 'a bridge example has no branch');
  assert.match(branchLink(undefined, {}).why, /not a knowledge-base branch yet/);
  const off = branchLink(t, { id: 'larchwood', tenant: 'company' });
  assert.equal(off.open, false); assert.match(off.why, /«company-b-pay_tech_debt-p50»/);
  assert.equal(branchLink(t, { branches: [{ tenant: 'company-b-pay_tech_debt-p10' }] }).open, false, 'another branch is not this one');
  const on = branchLink(t, { branches: [{ tenant: 'company-b-pay_tech_debt-p10' }, { tenant: t, label: 'Reduce tech debt, median' }] });
  assert.deepEqual(on, { open: true, params: { branch: t, entity: null, metric: null }, why: '' });
  assert.deepEqual(branchesOf({ branches: [{ tenant: t }, { label: 'no tenant' }, null] }).map(b => b.tenant), [t]);
  assert.deepEqual(branchesOf(null), []);
});

// ------------------------------------------------------------------------------------------------ the week event tree

const bandsOf = (from: number, to: number, v0: number, slope: number) => ({
  cash: Array.from({ length: to - from + 1 }, (_, i) => { const w = from + i, m = v0 + slope * i; return [w, m - 1e4 * Math.sqrt(i + 1), m, m + 1e4 * Math.sqrt(i + 1)] as [number, number, number, number]; }),
});
const ETREE: EventTreeRaw = {
  option: 'pay_tech_debt', n: 1000, weeks: 52, metrics: ['cash'],
  nodes: [
    { id: 'n0', parent: null, from: 0, to: 6, share: 1, n: 1000, split: null, bands: bandsOf(0, 6, 2e6, -5e3) },
    { id: 'n3', parent: 'n0', from: 6, to: 52, share: 0.04, n: 40, split: { week: 6, type: 'other', text: 'Other events', other: true }, bands: bandsOf(6, 52, 1.97e6, -2e3) },
    { id: 'n1', parent: 'n0', from: 6, to: 52, share: 0.22, n: 220, split: { week: 6, type: 'shock', text: 'GPU capacity shortage at Nimbus Cloud', entityId: 'org.VN-02' }, bands: bandsOf(6, 52, 1.97e6, -8e3) },
    { id: 'n2', parent: 'n0', from: 6, to: 31, share: 0.74, n: 740, split: { week: 6, type: 'none', text: '', rest: true }, bands: bandsOf(6, 31, 1.97e6, 1e3) },
    { id: 'n4', parent: 'n2', from: 31, to: 52, share: 0.13, n: 130, split: { week: 31, type: 'client', text: 'Meridian Freight Group gives notice', entityId: 'org.CL-004' }, bands: bandsOf(31, 52, 2e6, -6e3) },
    { id: 'n5', parent: 'n2', from: 31, to: 52, share: 0.61, n: 610, split: { week: 31, type: 'none', text: '', rest: true }, bands: bandsOf(31, 52, 2e6, 2e3) },
  ],
  paths: [
    { id: 'p', kind: 'pessimistic', seed: 7, scenario: 12, leaf: 'n1', nodes: ['n0', 'n1'], events: [{ week: 6, type: 'shock', text: 'GPU capacity shortage at Nimbus Cloud', material: true }, { week: 9, type: 'incident', text: 'SEV3 incident', material: false }], weeks: [{ week: 0, cash: 2e6 }, { week: 52, cash: 1.6e6 }] },
    { id: 'm', kind: 'median', seed: 7, scenario: 40, leaf: 'n5', nodes: ['n0', 'n2', 'n5'], events: [], weeks: [] },
    { id: 'o', kind: 'optimistic', seed: 7, scenario: 77, leaf: 'n4', nodes: ['n0', 'n2', 'n4'], events: [{ week: 31, type: 'client', text: 'Meridian Freight Group gives notice', entityId: 'org.CL-004' }, { week: 31, type: 'client', text: 'Harbor Analytics signs' }], weeks: [] },
  ],
};

test('the event tree: siblings in order, shares of the branch, plain labels, tones, paths through a node', () => {
  assert.ok(isEventTree(ETREE)); assert.ok(!isEventTree({ levels: [] }));
  const t = eventTreeView(ETREE)!;
  assert.deepEqual(t.root.children, ['n2', 'n3', 'n1'], 'siblings top to bottom by their cash at the end');
  assert.ok(t.root.outcomeEnd! > t.byId.get('n1')!.outcomeEnd!);
  const n4 = t.byId.get('n4')!;
  assert.ok(Math.abs(n4.ofParent - 0.13 / 0.74) < 1e-12);
  assert.equal(n4.label, 'Meridian Freight Group gives notice · w31 · 13%');
  assert.equal(t.byId.get('n2')!.label, 'No such event · 74%'); assert.equal(t.byId.get('n3')!.label, 'Other events · w6 · 4%');
  assert.equal(t.root.label, 'All scenarios');
  assert.equal(n4.tone, 'bad'); assert.equal(t.byId.get('n2')!.tone, 'neutral');
  assert.equal(splitTone({ week: 3, type: 'client', text: 'Harbor Analytics signs a contract' }), 'good');
  assert.equal(splitTone({ week: 3, type: 'client', text: 'Anything', tone: 'good' }), 'good');
  assert.equal(splitLabel({ week: 3, type: 'x', text: 'A rare thing' }, 0.004), 'A rare thing · w3 · <1%');
  assert.deepEqual(t.byId.get('n2')!.paths, ['m', 'o']); assert.deepEqual(t.byId.get('n1')!.paths, ['p']);
  assert.deepEqual(t.metrics, ['cash']);
});

test('the event tree layout: ribbons by share, no overlap, a floor height, weeks on x', () => {
  const t = eventTreeView(ETREE)!, L = layoutEventTree(t, { width: 520, height: 300, left: 10, right: 10, top: 10, bottom: 20, minH: 12 });
  const b = (id: string) => L.boxes.get(id)!;
  assert.deepEqual([b('n0').y0, b('n0').y1], [10, 280]); assert.equal(b('n0').x0, 10); assert.equal(b('n0').x1, 10 + 6 / 52 * 500);
  // the children of the root share its height in their order, with no gap and no overlap
  const kids = ['n2', 'n3', 'n1'].map(b);
  assert.equal(kids[0].y0, 10); assert.ok(Math.abs(kids[2].y1 - 280) < 1e-9);
  for (let i = 1; i < kids.length; i++) assert.ok(Math.abs(kids[i].y0 - kids[i - 1].y1) < 1e-9);
  assert.ok(b('n3').y1 - b('n3').y0 >= 12 - 1e-9, 'the 4% ribbon is lifted to the floor');
  assert.ok(b('n2').y1 - b('n2').y0 > b('n1').y1 - b('n1').y0, '74% is taller than 22%');
  assert.equal(b('n4').x0, b('n5').x0); assert.equal(b('n4').x1, 510);
  // a path steps at the split week and runs along each ribbon's middle
  const pts = pathPoints(t, L, ETREE.paths![2]);
  assert.deepEqual(pts[0], [10, (b('n0').y0 + b('n0').y1) / 2]);
  assert.deepEqual(pts[pts.length - 1], [510, (b('n4').y0 + b('n4').y1) / 2]);
  // with room kept for the labels, a path runs below the label row of a ribbon tall enough for both; in a thin one, in
  // its middle; the root has no label
  const kept = pathPoints(t, L, ETREE.paths![2], { labelRoom: 14 }), n2 = b('n2'), n4 = b('n4');
  assert.equal(kept[0][1], (b('n0').y0 + b('n0').y1) / 2, 'the root: its middle');
  assert.ok(n2.y1 - n2.y0 >= 32);
  assert.equal(kept.find(([x]) => x === n2.x1)![1], n2.y0 + 14 + (n2.y1 - n2.y0 - 14) / 2, 'below the label row of n2');
  if (n4.y1 - n4.y0 < 32) assert.equal(kept[kept.length - 1][1], (n4.y0 + n4.y1) / 2, 'a thin ribbon: its middle');
  assert.deepEqual(metricRange(t, 'cash').map(Math.round), [Math.round(Math.min(...ETREE.nodes.flatMap(n => n.bands.cash.map(x => x[1])))), Math.round(Math.max(...ETREE.nodes.flatMap(n => n.bands.cash.map(x => x[3]))))]);
});

test('a scenario week by week: its material events grouped by week', () => {
  assert.deepEqual(pathWeeks(ETREE.paths![0]).map(w => [w.week, w.events.map(e => e.text)]), [[6, ['GPU capacity shortage at Nimbus Cloud']]], 'the SEV3 incident is not material');
  assert.deepEqual(pathWeeks(ETREE.paths![2]).map(w => [w.week, w.events.length]), [[31, 2]]);
  assert.deepEqual(pathWeeks(ETREE.paths![1]), []);
});

test('the tree\'s own P/M/O are ranked by the outcome at the end of the year; the pinned day examples apart', () => {
  // the engine lists its own P/M/O: they win
  const t = eventTreeView(ETREE)!;
  assert.deepEqual(examplePaths(t).map(p => p.id), ['p', 'm', 'o']);
  assert.deepEqual(dayPaths(t), []);
  // only pinned day examples carry kinds: P/M/O come from the listed paths by their cash at the end
  const end = (c: number) => [{ week: 0, cash: 2e6 }, { week: 52, cash: c }];
  const raw: EventTreeRaw = { ...ETREE, paths: [
    { id: 'fixed-pessimistic', kind: 'pessimistic', fixed: true, leaf: 'n5', nodes: ['n0', 'n2', 'n5'], events: [], weeks: end(2.9e6) },
    { id: 'fixed-optimistic', kind: 'optimistic', fixed: true, leaf: 'n1', nodes: ['n0', 'n1'], events: [], weeks: end(1.5e6) },
    ...[2.4e6, 1.6e6, 2.0e6, 2.2e6, 1.8e6].map((c, i) => ({ id: `s${i}`, scenario: i, leaf: 'n5', nodes: ['n0', 'n2', 'n5'], events: [], weeks: end(c) })),
  ] };
  const u = eventTreeView(raw)!, ex = examplePaths(u);
  assert.deepEqual(ex.map(p => [p.kind, p.id]), [['pessimistic', 's1'], ['median', 's2'], ['optimistic', 's0']]);
  assert.deepEqual(dayPaths(u).map(p => p.id), ['fixed-pessimistic', 'fixed-optimistic']);
});

/** The drawn boxes of the placed labels: each inside its ribbon, in a row of it. */
function labelBoxes(L: TreeLayout, placed: PlacedLabel[]): [string, Box][] {
  const out: [string, Box][] = [];
  for (const p of placed) {
    const b = L.boxes.get(p.id)!;
    for (const [text, box] of [[p.left, p.leftBox], [p.end, p.endBox]] as const) {
      if (!text) { assert.equal(box, null); continue; }
      assert.ok(box, `${text} has a box`);
      assert.ok(box[0] >= b.x0 + 4 - 1e-9 && box[2] <= b.x1 - 4 + 1e-9 && box[1] >= b.y0 && box[3] <= b.y1 + 1e-9, `${text} stays in its ribbon`);
      const row = (box[1] - b.y0 - 1) / LABEL_ROW;
      assert.ok(Math.abs(row - Math.round(row)) < 1e-9 && row > -1e-9, `${text} sits in a row of its ribbon`);
      assert.equal(baselineOf(box), box[1] + 10);
      out.push([text, box]);
    }
  }
  return out;
}
const noOverlap = (boxes: [string, Box][], what: string) => {
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++)
    assert.ok(!overlaps(boxes[i][1], boxes[j][1]), `${what}: «${boxes[i][0]}» and «${boxes[j][0]}» do not overlap`);
};

test('the split labels: inside their ribbons, never over each other, the share kept, the week added', () => {
  const t = eventTreeView(ETREE)!;
  for (const [W, H] of [[760, 340], [420, 200], [300, 120]]) {
    const L = layoutEventTree(t, { width: W, height: H, left: 10, right: 60, top: 6, bottom: 22, minH: 7 });
    const ends = new Map(t.nodes.filter(n => !n.children.length).map(n => [n.id, `$${(n.outcomeEnd! / 1e6).toFixed(2)}M`]));
    const placed = placeLabels(t, L, ends);
    for (const p of placed) if (p.left) { const b = L.boxes.get(p.id)!; assert.ok(textWidth(p.left) <= b.x1 - b.x0 - 8 + 1e-9, `${p.left} fits its ribbon`); assert.match(p.left, /\d+%$/, 'the share is kept'); }
    noOverlap(labelBoxes(L, placed), `${W}x${H}`);
    if (W === 760) {
      assert.equal(placed.find(p => p.id === 'n1')!.left, 'GPU capacity shortage at Nimbus Cloud · w6 · 22%', 'the week of the split');
      assert.equal(placed.find(p => p.id === 'n2')!.left, 'No such event · 74%');
    }
  }
});

/** A root and two ribbons after week 10: «Deal won» on top (`top` of the height), «No such event» below. */
const twoRibbons = (top: number): EventTreeView => eventTreeView({ option: 'x', n: 100, weeks: 52, metrics: ['cash'], nodes: [
  { id: 'r', parent: null, from: 0, to: 10, share: 1, split: null, bands: bandsOf(0, 10, 2e6, 0) },
  { id: 'a', parent: 'r', from: 10, to: 52, share: top, split: { week: 10, type: 'client', text: 'Deal won' }, bands: bandsOf(10, 52, 2.9e6, 1e3) },
  { id: 'b', parent: 'r', from: 10, to: 52, share: 1 - top, split: { week: 10, type: 'none', text: '', rest: true, of: 'Deal won' }, bands: bandsOf(10, 52, 2e6, -1e3) },
] })!;
const lineThrough = (t: EventTreeView, L: TreeLayout, node: string, room = 0) => lineBoxes(pathPoints(t, L, { id: 'p', leaf: node, nodes: ['r', node], events: [], weeks: [] }, { labelRoom: room }), 2);

test('the labels keep off the path lines: another row of a tall ribbon, a thin ribbon\'s label on the line', () => {
  // a tall ribbon (about 60 px): the path runs through its middle; the label takes the top row, clear of it
  const t = twoRibbons(0.3), L = layoutEventTree(t, { width: 600, height: 222, left: 0, right: 0, top: 0, bottom: 22, minH: 7 });
  const a = L.boxes.get('a')!;
  assert.ok(a.y1 - a.y0 > 50);
  const tall = placeLabels(t, L, new Map([['a', '$2.90M']]), { lines: lineThrough(t, L, 'a') }).find(p => p.id === 'a')!;
  assert.equal(tall.left, 'Deal won · w10 · 30%'); assert.equal(tall.leftOnLine, false);
  assert.equal(tall.leftBox![1], a.y0 + 1, 'the top row');
  // the line moved into the top row: the label goes to a row below it
  const top = lineBoxes([[a.x0, a.y0 + 7], [a.x1, a.y0 + 7]]);
  const moved = placeLabels(t, L, new Map(), { lines: top }).find(p => p.id === 'a')!;
  assert.equal(moved.leftOnLine, false); assert.equal(moved.leftBox![1], a.y0 + 1 + LABEL_ROW, 'the second row');
  // a thin ribbon (one row): the path crosses every row, so the label stays and stands on the line (on its plate)
  const u = twoRibbons(0.07), M = layoutEventTree(u, { width: 600, height: 222, left: 0, right: 0, top: 0, bottom: 22, minH: 7 });
  const ua = M.boxes.get('a')!;
  assert.ok(ua.y1 - ua.y0 < 2 * LABEL_ROW);
  const thin = placeLabels(u, M, new Map([['a', '$2.90M']]), { lines: lineThrough(u, M, 'a', 14) }).find(p => p.id === 'a')!;
  assert.equal(thin.left, 'Deal won · w10 · 7%'); assert.equal(thin.leftOnLine, true); assert.equal(thin.end, '$2.90M'); assert.equal(thin.endOnLine, true);
  // with room kept for the label, a path through a tall ribbon runs below its label row: no label on a line
  const kept = placeLabels(t, L, new Map([['a', '$2.90M'], ['b', '$2.00M']]), { lines: [...lineThrough(t, L, 'a', 14), ...lineThrough(t, L, 'b', 14)] });
  assert.ok(kept.every(p => !p.leftOnLine && !p.endOnLine), 'paths below the labels');
});

test('the value at the end: beside the label, else in the row under it, else shorter; never over the label', () => {
  const t = twoRibbons(0.3), wide = layoutEventTree(t, { width: 600, height: 222, left: 0, right: 0, top: 0, bottom: 22, minH: 7 });
  const at = (L: TreeLayout, ends: string[], id = 'a') => placeLabels(t, L, new Map([[id, ends]])).find(p => p.id === id)!;
  const beside = at(wide, ['$2.90M', '$2.9M']);
  assert.equal(beside.end, '$2.90M'); assert.equal(beside.endBox![1], beside.leftBox![1], 'the same row');
  assert.ok(beside.leftBox![2] + 8 <= beside.endBox![0]);
  // a narrow ribbon: the label keeps its words and the value goes to the row under it (stacked)
  const narrow = layoutEventTree(t, { width: 10 + 52 / 42 * 150, height: 222, left: 0, right: 0, top: 0, bottom: 22, minH: 7 });
  const a = narrow.boxes.get('a')!, w = a.x1 - a.x0 - 8;
  assert.ok(textWidth('Deal won · w10 · 30%') <= w && textWidth('Deal won · w10 · 30%') + 8 + textWidth('$2.90M') + 2 > w);
  const stacked = at(narrow, ['$2.90M', '$2.9M']);
  assert.equal(stacked.left, 'Deal won · w10 · 30%'); assert.equal(stacked.endBox![1], stacked.leftBox![1] + LABEL_ROW, 'under the label');
  // one row only and no room beside the share: the short amount
  const u = twoRibbons(0.07), thin = layoutEventTree(u, { width: 10 + 52 / 42 * 64, height: 222, left: 0, right: 0, top: 0, bottom: 22, minH: 7 });
  const ua = thin.boxes.get('a')!, uw = ua.x1 - ua.x0 - 8;
  const one = placeLabels(u, thin, new Map([['a', ['$2.90M', '$2.9M']]]), { measure: textWidth, measureEnd: x => textWidth(x) + 2 }).find(p => p.id === 'a')!;
  assert.equal(one.left, '7%');
  if (textWidth('7%') + 8 + textWidth('$2.90M') + 2 > uw && textWidth('7%') + 8 + textWidth('$2.9M') + 2 <= uw) assert.equal(one.end, '$2.9M', 'the short amount beside the share');
  assert.ok(!one.end || one.leftBox![2] + 8 <= one.endBox![0], 'never over the label');
});

test('a polyline\'s boxes: one per segment, padded; a zero-length step is none', () => {
  assert.deepEqual(lineBoxes([[0, 10], [5, 10], [5, 10], [5, 30]], 2), [[-2, 8, 7, 12], [3, 8, 7, 32]]);
  assert.deepEqual(lineBoxes([[1, 1]]), []);
});

test('money where room is short: one decimal for millions', () => {
  assert.equal(money(2_904_000), '$2.90M'); assert.equal(moneyShort(2_904_000), '$2.9M');
  assert.equal(moneyShort(-1_250_000), '−$1.3M'); assert.equal(moneyShort(250_000), '$250k'); assert.equal(moneyShort(NaN), '—');
});

// ------------------------------------------------------------------------------------------------ the week gate's spread

test('the score spread of the week gate: fixed anchors and ties, a fork\'s score independent of the others', () => {
  // two criteria: cash (higher, anchors 0..2M, tie 100k) and churn (lower, anchors 0..0.4, tie 0.005); 4 scenarios
  const run = (forks: number[][][]) => {
    const O = forks.length, n = forks[0].length, K = 2, X = new Float64Array(n * O * K);
    forks.forEach((sc, j) => sc.forEach(([cash, churn], s) => { X[(s * O + j) * K] = cash; X[(s * O + j) * K + 1] = churn; }));
    return { n, options: forks.map((_f, j) => `f${j}`), criteria: ['cash', 'churn'], X, higher: [true, false], ties: [1e5, 0.005], anchors: [{ lo: 0, span: 2e6 }, { lo: 0, span: 0.4 }] };
  };
  const A = [[2.0e6, 0.2], [1.5e6, 0.2], [1.0e6, 0.1], [1.8e6, 0.3]], B = [[1.0e6, 0.4], [1.0e6, 0.4], [1.0e6, 0.4], [1.0e6, 0.4]];
  const w = [0.5, 0.5];
  const two = formulaScoreSpread(run([A, B]), w);
  // B: cash 1.0M -> 0.5, churn 0.4 -> 0: score 0.25 in every scenario, whatever the other fork does
  assert.deepEqual(two[1].map(v => +v.toFixed(6)), [0.25, 0.25, 0.25]);
  // adding a third fork does not move the first two (fixed anchors, not 0..1 across the forks)
  const three = formulaScoreSpread(run([A, B, [[0, 0], [0, 0], [0, 0], [0, 0]]]), w);
  assert.deepEqual(three[0].map(v => +v.toFixed(9)), two[0].map(v => +v.toFixed(9)));
  // within the meaningful difference forks tie in a scenario: 1.05M and 1.0M of cash score the same
  const tied = formulaScoreSpread(run([[[1.05e6, 0.2]], [[1.0e6, 0.2]]]), w);
  assert.equal(tied[0][1], tied[1][1]);
  // without anchors (an older run) the scale is 0..1 over the forks of the scenario
  const old = formulaScoreSpread({ ...run([A, B]), anchors: undefined }, w);
  assert.ok(old[0][2] <= 1 && old[1][0] >= 0);
});

test('the actor rivals of the week gate: the engine\'s pairwise total, and the tie', () => {
  const m = month();
  (m.ranking as unknown as { pairwise: unknown[] }).pairwise = [
    { a: 'reserve_gpu', b: 'pay_tech_debt', crit: 'total', bWins: 0.42, aWins: 0.5, meanDiff: 0, medianMargin: 0, scoreDiff: 0, tie: 0 },
    { a: 'reserve_gpu', b: 'hire_5', crit: 'total', bWins: 0.08, aWins: 0.9, meanDiff: 0, medianMargin: 0, scoreDiff: 0, tie: 0 },
    { a: 'reserve_gpu', b: 'status_quo', crit: 'total', bWins: 0.11, aWins: 0.8, meanDiff: 0, medianMargin: 0, scoreDiff: 0, tie: 0 },
    { a: 'reserve_gpu', b: 'pay_tech_debt', crit: 'cash', bWins: 0.9, aWins: 0, meanDiff: 0, medianMargin: 0, scoreDiff: 0, tie: 0 },
  ];
  const all = forks;
  assert.deepEqual([...actorRivals(m, 'reserve_gpu', all)].sort(), ['pay_tech_debt', 'status_quo']);
  assert.deepEqual([...actorRivals(m, 'reserve_gpu', all, 'hire_5')].sort(), ['hire_5', 'pay_tech_debt', 'status_quo'], 'a full tie is always a rival');
  assert.deepEqual([...actorRivals(m, 'reserve_gpu', ['reserve_gpu', 'hire_5'])], [], 'forks switched off are left out');
  // without pairwise rows: P(best) of 1 in 10
  assert.deepEqual([...actorRivals(month(), 'reserve_gpu', all)].sort(), ['hire_5', 'pay_tech_debt']);
});

test('short readable names of the splits', () => {
  assert.equal(shortEvent('GPU shortage at the cloud provider', 'shock'), 'GPU shortage');
  assert.equal(shortEvent('Team over capacity for 3 weeks in a row', 'people'), 'Overload');
  assert.equal(shortEvent('Rhys Llewellyn hands in notice', 'people'), 'Rhys leaves');
  assert.equal(shortEvent('Quillon Insurance Services gives notice', 'client'), 'Quillon leaves');
  assert.equal(shortEvent('A big new deal won (ACV $150k or more)', 'client'), 'Big deal won');
  assert.equal(shortEvent('Major SEV1 outage (4 h or more)', 'incident'), 'SEV1 outage');
  assert.equal(shortEvent('Kaveri Tech Services rotates the team', 'vendor'), 'Kaveri rotation');
  assert.equal(shortEvent('The business adds features (scope creep)', 'client'), 'Scope creep');
  assert.equal(shortEvent('Something rare happens: details', 'shock'), 'Something rare happens');
  assert.equal(splitShort({ week: 23, type: 'shock', text: 'No such event', rest: true, of: 'GPU shortage at the cloud provider' }), 'No GPU shortage');
  assert.equal(splitShort({ week: 44, type: 'people', text: 'No such event', rest: true, of: 'Rhys Llewellyn hands in notice' }), 'Rhys stays');
  assert.equal(splitShort({ week: 46, type: 'other', text: 'Other events', other: true }), 'Other events');
});

// the precomputed month file of case B (a copy of the stack's artifact; CASE_B_MONTH names another copy)
const REAL_B = process.env.CASE_B_MONTH ?? '/tmp/variator-1000/mvp-artifacts/future/company_best_year/month.json';
test('the real case B trees, desktop and phone: no label over another or a mark, off the path lines where a row allows, never cut', { skip: !existsSync(REAL_B) && 'no artifact here' }, () => {
  const m = JSON.parse(readFileSync(REAL_B, 'utf8')) as ActorMonth & { eventTrees?: Record<string, EventTreeRaw> };
  assert.ok(m.eventTrees && Object.keys(m.eventTrees).length > 0, 'event trees in month.json');
  for (const [fork, raw] of Object.entries(m.eventTrees!)) {
    const t = eventTreeView(raw)!, leaves = t.nodes.filter(n => !n.children.length).length;
    // the tree card's chart (EventTreeChart: width of the card, height by the leaves, margins of the marks) at 1920,
    // 1440 and 1280 px, and on a phone
    for (const W of [1000, 680, 600, 340]) {
      const narrow = W < 520, H = Math.max(narrow ? 300 : 340, Math.min(narrow ? 460 : 560, 60 + leaves * (narrow ? 16 : 22)));
      const L = layoutEventTree(t, { width: W, height: H, left: narrow ? 6 : 10, right: narrow ? 26 : 40, top: 6, bottom: 22, minH: narrow ? 5 : 7 });
      const ends = new Map(t.nodes.filter(n => !n.children.length && n.outcomeEnd != null).map(n => [n.id, [money(n.outcomeEnd!), moneyShort(n.outcomeEnd!)]]));
      const offset: Record<string, number> = { pessimistic: 3, median: 0, optimistic: -3 };
      const lines = examplePaths(t).flatMap(p => lineBoxes(pathPoints(t, L, p, { labelRoom: 14 }).map(([x, y]) => [x, y + offset[p.kind!]] as [number, number])));
      // the end marks of the paths, right of the plot
      const marks: Box[] = examplePaths(t).map(p => { const pts = pathPoints(t, L, p, { labelRoom: 14 }), [x, y] = pts[pts.length - 1]; return [x + 1, y - 8, x + 17, y + 8]; });
      const placed = placeLabels(t, L, ends, { lines, taken: marks, maxRows: narrow ? 2 : 4 });
      const boxes = labelBoxes(L, placed), at = `${fork} at ${W} px`;
      noOverlap(boxes, at);
      for (const [text, b] of boxes) assert.ok(marks.every(m => !overlaps(b, m)), `${at}: «${text}» clear of the end marks`);
      for (const p of placed) {
        if (p.left) { assert.ok(!p.left.includes('…'), `${at} ${p.left}: words are not cut`); assert.match(p.left, /\d+%$/, 'the share is kept'); }
        assert.equal(p.leftOnLine, !!p.leftBox && lines.some(l => overlaps(p.leftBox!, l)), `${at}: the flag says where the label is`);
        assert.equal(p.endOnLine, !!p.endBox && lines.some(l => overlaps(p.endBox!, l)), `${at}: the flag says where the value is`);
        // a label alone on a path line only when no row of its ribbon is free of lines and of the other texts
        if (p.leftBox && p.leftOnLine && !p.end) {
          const b = L.boxes.get(p.id)!, rows = Math.max(1, Math.min(narrow ? 2 : 4, Math.floor((b.y1 - b.y0 - 1) / LABEL_ROW)));
          const others = [...boxes.filter(([, x]) => x !== p.leftBox).map(([, x]) => x), ...marks];
          const clear = Array.from({ length: rows }, (_, k): Box => [p.leftBox![0], b.y0 + 1 + k * LABEL_ROW, p.leftBox![2], b.y0 + LABEL_ROW * (k + 1) - 1])
            .some(r => !lines.some(l => overlaps(r, l)) && !others.some(o => overlaps(r, o)));
          assert.ok(!clear, `${at}: «${p.left}» on a line only for want of a free row`);
        }
      }
      if (W >= 680) assert.ok(placed.some(p => p.left && / · w\d+ · \d+%$/.test(p.left)), `${at}: some split shows its week`);
      else assert.ok(placed.filter(p => p.left).length >= 3, `${at}: the phone shows labels too`);
    }
  }
});

test('a fork\'s week tree key: its own inputs and the world; an unrelated edit keeps the tree', () => {
  const lever = cfg.interventions.find(i => i.forks?.includes('hire_5') && !i.forks.includes('status_quo'))!;
  const size = lever.values[0]?.path ?? lever.monthPath;
  // turning another fork off, priorities, caution and the view: the defaults of Keep the plan
  const unrelated: Record<string, number | boolean | string | string[]>[] = [{ 'fork|reserve_gpu': false }, { 'w|cash': 0.6, caution: 'worst', priority: 'custom' }, { week: ['status_quo'], dayExample: 'median' }];
  for (const e of unrelated) assert.equal(forkKey(cfg, e, 'status_quo'), DEFAULT_KEY, JSON.stringify(e));
  // another fork's lever: only that fork's tree changes
  const other = { [size]: { kind: 'fixed' as const, value: 2 } };
  assert.equal(forkKey(cfg, other, 'status_quo'), DEFAULT_KEY);
  assert.notEqual(forkKey(cfg, other, 'hire_5'), DEFAULT_KEY);
  // the world (a hypothesis, a shock, a placed event) changes every fork's tree
  const event = cfg.interventions.find(i => i.placePath)!.placePath!;
  const world: Record<string, number | boolean>[] = [{ 'hyp|price_war': true }, { [`r|${cfg.shocks[0].id}|p`]: 0.5 }, { [event]: true }];
  for (const e of world) {
    assert.notEqual(forkKey(cfg, e, 'status_quo'), DEFAULT_KEY, JSON.stringify(e));
    assert.equal(forkKey(cfg, e, 'status_quo'), forkKey(cfg, e, 'hire_5'));
  }
  // an override of one fork is that fork's own
  assert.equal(forkKey(cfg, { 'o|hire_5|x': { kind: 'fixed' as const, value: 1 } }, 'status_quo'), DEFAULT_KEY);
});

test('short names never stand an article for the subject', () => {
  assert.equal(shortEvent('A new client gives notice', 'client'), 'New client leaves');
  assert.equal(splitShort({ week: 20, type: 'client', text: 'No such event', rest: true, of: 'A new client gives notice' }), 'New client stays');
  assert.equal(shortEvent('The lead engineer hands in notice', 'people'), 'Lead engineer leaves');
  assert.equal(shortEvent('Quillon Insurance Services gives notice', 'client'), 'Quillon leaves');
});

test('«Rerun on actors»: the reason in words for each state it cannot run in', () => {
  const at = (x: Partial<Parameters<typeof rerunWhy>[0]>) => rerunWhy({ rerunAvailable: true, pending: false, running: false, stale: false, ...x });
  assert.deepEqual(at({}), { state: 'current', off: true, why: 'Up to date: the actors already answered for this page. Edit a fork, an event or a priority to rerun.' });
  assert.deepEqual(at({ rerunAvailable: false }), { state: 'unavailable', off: true, why: RERUN_WHY.unavailable });
  assert.match(RERUN_WHY.unavailable, /does not answer on this server/);
  // no engine wins over a stale result: the button could not run anyway
  assert.equal(at({ rerunAvailable: false, stale: true }).state, 'unavailable');
  assert.equal(at({ rerunAvailable: null }).state, 'checking');
  assert.equal(at({ pending: true, stale: true }).state, 'running');
  assert.equal(at({ running: true }).why, RERUN_WHY.running);
  // a failed rerun for this page: the button retries, without a reason beside it
  assert.deepEqual(at({ stale: true }), { state: 'ready', off: false, why: '' });
  for (const w of Object.values(RERUN_WHY)) assert.ok(w.length <= 120 && /^[A-Z]/.test(w), `short and capitalised: ${w}`);
});

test('the scope of a P(best): the forks of the ranking it is computed over, from the data', () => {
  assert.equal(pBestScope(5), 'of 5 forks'); assert.equal(pBestScope(1), 'of 1 fork');
  // on actors: every option of the month file's ranking, also while a fork turned off waits for its rerun
  const M = month(), n = M.ranking!.options.length;
  const actors = { month: M, verdict: verdictOf(cfg, M), rankState: 'current', pending: false } as unknown as Parameters<typeof numbersOf>[1];
  const page = (forksOn: string[]) => ({ forksOn, run: null, ranking: null } as unknown as Parameters<typeof numbersOf>[0]);
  const all = numbersOf(page(M.ranking!.options.map(o => o.option)), actors)!;
  assert.equal(all.source, 'actors'); assert.equal(all.pBestOf, n);
  const fewer = numbersOf(page(M.ranking!.options.slice(0, 3).map(o => o.option)), actors)!;
  assert.equal(fewer.order.length, 3); assert.equal(fewer.pBestOf, n, 'the shares shown are still out of all the ranked forks');
  // the formula model: the forks of its run
  const formula = numbersOf({ forksOn: ['a', 'b'], run: { feasible: {}, stats: {}, n: 10 }, ranking: { order: ['a', 'b'], pBest: { a: 0.7, b: 0.3 }, key: {}, overall: {}, weights: {} } } as unknown as Parameters<typeof numbersOf>[0],
    { month: null, verdict: null, rankState: 'defaults' } as unknown as Parameters<typeof numbersOf>[1])!;
  assert.equal(formula.source, 'formula'); assert.equal(formula.pBestOf, 2);
});

test('dates read as in the rest of the app: day first, three-letter months, never the browser\'s locale', () => {
  assert.equal(dayMonth('2026-10-26'), '26 Oct'); assert.equal(dayMonth('2026-10-01T07:14:29+01:00'), '1 Oct');
  assert.equal(longDate('2027-04-26'), '26 Apr 2027'); assert.equal(longDate('2026-09-05'), '5 Sep 2026', 'Sep, not Sept');
  assert.equal(weekdayDate('2027-04-26'), 'Mon, 26 Apr 2027');
  assert.equal(dateTime('2026-10-30T21:05:00Z', 'America/Los_Angeles'), '30 Oct 2026, 14:05');
  assert.equal(dateTime('2026-10-30T00:05:00Z', 'UTC'), '30 Oct 2026, 00:05', '24 hours');
  assert.equal(dateTime('not a date'), '—'); assert.equal(longDate(''), '—');
  for (const t of [dayMonth('2026-10-26'), longDate('2027-04-26'), dateTime('2026-10-30T21:05:00Z')]) assert.doesNotMatch(t, /\d+\/\d+\/\d+|\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}\b/, `no US order: ${t}`);
});
