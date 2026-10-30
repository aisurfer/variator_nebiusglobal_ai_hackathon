// Case A (delivery_best_way) through the TS case engine equals the page engine bit for bit, as for case B in
// variator-engine-page.test.ts: the script of docs/playground/model-simulator.html runs in node:vm on the delivery
// model of the case config (same n and seed). Statistics, ranking, timeline, best path, risk texts, strategy and the
// plan of levers of the in-house team (a mode lever: hire or outsource) must be the same. Skipped without the page.
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import vm from 'node:vm';
import { analyseRun, defaultState, loadCase, simulateCase, solvePlan, type CaseConfig, type CaseState } from '../src/variator/engine/index.js';

const PAGE = new URL('../../docs/playground/model-simulator.html', import.meta.url);
const config = JSON.parse(readFileSync(new URL('../config/cases/delivery_best_way.json', import.meta.url), 'utf8')) as CaseConfig;
const MODEL_KEYS = ['question', 'variables', 'derived', 'options', 'criteria', 'risks', 'rules', 'assumptions', 'page', 'scenarios', 'timeline'] as const;

interface PageOut {
  means: number[][]; p50: number[][]; cvar: number[][]; feasible: number[]; score: number[]; order: number[]; best: number[];
  overall: number[][]; fire: Record<string, number[]>; pmet: Record<string, (number | null)[]>; conflicts: [string, number, string, number][];
  breaks: string[][]; risks: string[]; strategy: string; tradeoff: string; plan: { met: boolean; p: number[]; steps: unknown[][] };
}

function stub(init: Record<string, unknown> = {}): Record<string, unknown> {
  const store: Record<string | symbol, unknown> = {
    value: '', checked: false, clientWidth: 1000, clientHeight: 200, children: [], dataset: {}, style: {}, hidden: false,
    classList: { toggle() {}, add() {}, remove() {} }, ...init,
  };
  return new Proxy(store, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'querySelectorAll') return () => [];
      if (typeof k === 'symbol') return undefined;
      return () => stub();
    },
    set(t, k, v) { t[k] = v; return true; },
  }) as Record<string, unknown>;
}

function runPage(n: number, seed: number, active: string[], planOption: number, planN: number): PageOut {
  const html = readFileSync(PAGE, 'utf8');
  const model = Object.fromEntries(MODEL_KEYS.map(k => [k, config[k]]));
  const src = html.slice(html.lastIndexOf('<script>') + 8, html.lastIndexOf('</script>')).replace('/*__MODEL__*/null', JSON.stringify(model));
  const els: Record<string, unknown> = { n: stub({ value: String(n) }), seed: stub({ value: String(seed) }), whisk: stub({ value: '5' }) };
  const sandbox: Record<string, unknown> = {
    document: stub({ getElementById: (id: string) => (els[id] = els[id] || stub()), querySelectorAll: () => [], addEventListener() {}, createElement: () => stub(), documentElement: stub(), body: stub() }),
    window: stub({ devicePixelRatio: 1, innerWidth: 1440, innerHeight: 900, addEventListener() {} }),
    localStorage: { getItem: () => 'advanced', setItem() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    performance, setTimeout: () => 0, clearTimeout: () => {}, console, __ACTIVE: active,
  };
  sandbox.globalThis = sandbox;
  const read = `(() => { const bp = bestPath(); const pl = solvePlan(${planOption}, ${planN}, ${seed}); return {
    means: C.opts.map((o, j) => M.criteria.map((c, k) => R.st[j][k].mean)), p50: C.opts.map((o, j) => M.criteria.map((c, k) => R.st[j][k].p50)),
    cvar: C.opts.map((o, j) => M.criteria.map((c, k) => R.st[j][k].cvar)), feasible: C.opts.map((o, j) => R.st[j].feasible),
    score: R.rk.score, order: R.rk.order, best: Array.from(R.rk.best), overall: R.rk.overall, fire: R.fire,
    pmet: Object.fromEntries(R.tl.goals.map(g => [g.name, g.p])), conflicts: R.tl.conflicts.map(c => [c.goal.name, c.j, c.v, c.r]),
    breaks: bp.breaks, risks: bp.risks, strategy: strategy(bp, SCN.filter(s => ACTIVE.has(s.name))), tradeoff: $("tradeoff").textContent,
    plan: {met: pl.met, p: pl.p, steps: pl.steps.map(s => [s.lever.variable, s.mode, s.value, s.current])} }; })()`;
  const out = vm.runInNewContext(src + '\n;' + read, sandbox, { filename: 'model-simulator.js' });
  return JSON.parse(JSON.stringify(out)) as PageOut; // objects of this realm (deepStrictEqual checks prototypes)
}

const plain = (html: string) => html.replace(/<[^>]+>/g, '').replace(' · 👁 Watch:', ' · Watch:').replace(/^\p{Extended_Pictographic}\S* /u, ''); // case A headlines have no emoji

for (const [seed, active] of [[7, []], [42, ['requirements_change', 'fixed_deadline']]] as [number, string[]][]) {
  test(`the engine equals the page engine (n 1000, seed ${seed}${active.length ? ', ' + active.join(' + ') : ''})`, { skip: !existsSync(PAGE) && 'docs/playground/model-simulator.html is not here' }, () => {
    const n = 1000, c = loadCase(config);
    const state: CaseState = { ...defaultState(c), n, seed, activePresets: active };
    const run = simulateCase(c, state), a = analyseRun(c, state, run);
    const hire = 0; // the plan of the in-house team (index 0)
    const page = runPage(n, seed, active, hire, 300);
    assert.deepEqual(run.stats.map(r => r.map(s => s.mean)), page.means);
    assert.deepEqual(run.stats.map(r => r.map(s => s.p50)), page.p50);
    assert.deepEqual(run.stats.map(r => r.map(s => s.cvar)), page.cvar);
    assert.deepEqual(run.feasible, page.feasible);
    assert.deepEqual(run.fire, page.fire);
    assert.deepEqual(a.ranking.score, page.score);
    assert.deepEqual(a.ranking.order, page.order);
    assert.deepEqual(a.ranking.best, page.best);
    assert.deepEqual(a.ranking.overall, page.overall);
    assert.deepEqual(Object.fromEntries(a.timeline.goals.map(g => [g.name, g.p])), page.pmet);
    assert.deepEqual(a.timeline.conflicts.map(x => [x.goal, x.j, x.variable, x.r]), page.conflicts);
    assert.deepEqual(a.path.breaks.map(b => [b.change, b.winner, b.label, b.winnerLabel]), page.breaks);
    assert.deepEqual(a.path.risks.map(r => r.text), page.risks.map(plain));
    assert.equal(a.path.strategy, page.strategy.replace(/<[^>]+>/g, ''));
    assert.equal(a.ranking.tradeoff.text, page.tradeoff);
    const plan = solvePlan(c, state, hire, { n: 300 });
    assert.deepEqual({ met: plan.met, p: plan.p, steps: plan.steps.map(s => [s.variable, s.mode, s.value, s.current]) }, page.plan);
  });
}
