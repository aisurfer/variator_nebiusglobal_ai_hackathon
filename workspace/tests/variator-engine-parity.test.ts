// Parity: the TS case engine vs the Python reference (simulate_tensor + best_path + timeline.evaluate) for every
// case: B company_best_year (company_model) and A delivery_best_way (delivery_model), at n = 4096, seeds 7 and 42,
// and one layered run (two hypotheses). The VARIATOR parity rule: means per option x criterion
// z = |diff| / (std * sqrt(2 / n)) < 4; shares (feasible, P(met)) z < 4; the same best-path decision and runner-up.
// Write the references with: .venv/bin/python scripts/mvp/export_case.py --reference
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { analyseRun, defaultState, loadCase, simulateCase, type CaseConfig, type CaseState } from '../src/variator/engine/index.js';

interface RefRun {
  seed: number;
  n: number;
  options: string[];
  criteria: string[];
  mean: Record<string, Record<string, number>>;
  std: Record<string, Record<string, number>>;
  feasible: Record<string, number>;
  bestPath: { decision: string; runnerUp: string | null; breaks: string[]; pBest: number };
  timeline: { pMet: Record<string, Record<string, number>> };
  activePresets?: string[];
}
const CASES = ['company_best_year', 'delivery_best_way'];
const read = <T,>(path: string) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8')) as T;
const Z_MAX = 4;
// two proportions: the pooled share gives the variance (with only the Python share, 0 failures of 4096 against 4 gave
// z 4.4 for pure noise); the floor keeps shares of exactly 0 or 1 on both sides testable
const shareZ = (a: number, b: number, n: number) => {
  const p = (a + b) / 2;
  return Math.abs(a - b) / Math.max(Math.sqrt(Math.max(p * (1 - p), 1e-4) * 2 / n), 1e-12);
};

function compare(config: CaseConfig, r: RefRun) {
  const c = loadCase(config);
  const state: CaseState = { ...defaultState(c), n: r.n, seed: r.seed, activePresets: r.activePresets ?? [] };
  const run = simulateCase(c, state);
  const a = analyseRun(c, state, run);
  let worstMean = 0, worstShare = 0, worstPMet = 0;
  const where: string[] = [];
  r.options.forEach(o => {
    const j = run.options.indexOf(o);
    r.criteria.forEach(name => {
      const k = run.criteria.indexOf(name), std = r.std[o][name];
      const z = Math.abs(run.stats[j][k].mean - r.mean[o][name]) / Math.max(std * Math.sqrt(2 / r.n), 1e-12);
      if (z > worstMean) where.push(`${o}/${name} z=${z.toFixed(2)}`);
      worstMean = Math.max(worstMean, z);
    });
    worstShare = Math.max(worstShare, shareZ(run.feasible[j], r.feasible[o], r.n));
  });
  for (const [item, per] of Object.entries(r.timeline.pMet)) {
    const g = a.timeline.goals.find(x => x.name === item);
    assert.ok(g, `timeline item ${item} missing in the TS result`);
    for (const [o, p] of Object.entries(per)) {
      const ts = g.p[run.options.indexOf(o)];
      assert.ok(ts != null, `P(met) of ${item} for ${o} missing`);
      worstPMet = Math.max(worstPMet, shareZ(ts, p, r.n));
    }
  }
  return { run, a, worstMean, worstShare, worstPMet, where };
}

for (const id of CASES) {
  const config = read<CaseConfig>(`../config/cases/${id}.json`);
  const ref = read<{ runs: RefRun[]; presetRuns: RefRun[] }>(`./fixtures/${id}.reference.json`);
  for (const r of [...ref.runs, ...ref.presetRuns]) {
    const label = `${id}, seed ${r.seed}${r.activePresets ? ' + ' + r.activePresets.join(', ') : ''}`;
    test(`parity with Python, ${label}: means, feasible, best path, P(met)`, () => {
      const { a, worstMean, worstShare, worstPMet, where } = compare(config, r);
      console.log(`[parity ${label}] largest z: means ${worstMean.toFixed(2)}, feasible ${worstShare.toFixed(2)}, P(met) ${worstPMet.toFixed(2)};`
        + ` decision TS ${a.path.decision} / Python ${r.bestPath.decision}; breaks TS [${a.path.breaks.map(b => `${b.change} -> ${b.winner}`).join('; ')}]`
        + ` / Python [${r.bestPath.breaks.join('; ')}]`);
      assert.ok(worstMean < Z_MAX, `largest mean z ${worstMean.toFixed(2)} (${where.slice(-1)[0]})`);
      assert.ok(worstShare < Z_MAX, `largest feasible z ${worstShare.toFixed(2)}`);
      assert.ok(worstPMet < Z_MAX, `largest P(met) z ${worstPMet.toFixed(2)}`);
      assert.equal(a.path.decision, r.bestPath.decision);
      assert.equal(a.path.runner, r.bestPath.runnerUp);
    });
  }
}
