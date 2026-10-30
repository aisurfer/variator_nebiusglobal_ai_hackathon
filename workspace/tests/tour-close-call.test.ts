// How firm the decision is (src/variator/case/model/closeCall.ts): «Equal choices» when the decision and the runner-up
// tie on every criterion within the meaningful difference; «Close call» only when the fork that wins most scenarios is
// outside the decision's tie group and wins more than that whole group. P(best) splits between tied forks, so a tie
// alone (0.34 / 0.33 / 0.33) never makes a close call.
import test from 'node:test';
import assert from 'node:assert/strict';
import { closeCallOf, equalChoicesOf, type CloseCallInput, type Pairwise } from '../src/variator/case/model/closeCall.js';
import type { CaseConfig } from '../src/variator/case/model/types.js';

const crit = (id: string, higher = true) => ({ id, label: id.toUpperCase(), unit: id === 'cash' ? 'USD' : 'share', higher, description: '', help: '' });
const cfg = {
  criteria: [crit('cash'), crit('churn', false), crit('nps')],
  forks: [{ id: 'keep', label: 'Keep the plan', baseline: true }, { id: 'debt', label: 'Reduce tech debt' }, { id: 'hire', label: 'Hire 5 engineers' }],
  priorityPresets: [{ id: 'cash_first', label: 'Cash first', line: '', weights: { cash: 2, churn: 0.5, nps: 0.25 } }, { id: 'clients_first', label: 'Clients first', line: '', weights: { cash: 0.5, churn: 2, nps: 1.5 } }],
} as unknown as CaseConfig;
const pw = (rows: Partial<Pairwise>[]): Pairwise[] => cfg.criteria.map((c, i) => ({ crit: c.id, bWins: 0, meanDiff: 0, medianMargin: 0, scoreDiff: 0, ...rows[i] }));
const input = (over: Partial<CloseCallInput>): CloseCallInput => ({
  order: ['keep', 'debt', 'hire'], pBest: { keep: 0.34, debt: 0.33, hire: 0.33 }, weights: { cash: 0.4, churn: 0.4, nps: 0.2 },
  spread: () => 1, pairwise: () => pw([]), equal: () => [], feasible: { keep: 1, debt: 1, hire: 1 }, ...over,
});

test('a full tie of the decision and the runner-up: equal choices, the baseline wins it, no close call', () => {
  const i = input({ equal: (a, b) => (a === 'keep' && b === 'debt') || (a === 'debt' && b === 'keep') ? ['cash', 'churn', 'nps'] : [] });
  const eq = equalChoicesOf(cfg, i);
  assert.ok(eq);
  assert.equal(eq!.why, 'baseline');
  assert.equal(eq!.runnerUp, 'debt');
  assert.equal(closeCallOf(cfg, i), null, 'P(best) split between tied forks is not a close call');
});

test('P(best) below a half because of ties is not a close call', () => {
  const i = input({ pBest: { keep: 0.4, debt: 0.4, hire: 0.2 }, equal: (a, b) => [a, b].sort().join() === 'debt,keep' ? ['cash', 'churn', 'nps'] : [] });
  assert.equal(closeCallOf(cfg, i), null);
});

test('a rival outside the tie group that wins more scenarios than the group: a close call, said from the result', () => {
  const i = input({
    pBest: { keep: 0.2, debt: 0.1, hire: 0.7 },
    pairwise: () => pw([{ meanDiff: 500_000, scoreDiff: 0.6 }, { bWins: 0.8, meanDiff: 0.01, medianMargin: 0.001 }, { bWins: 0.7, meanDiff: -2, medianMargin: 0.1 }]),
  });
  const cc = closeCallOf(cfg, i);
  assert.ok(cc);
  assert.equal(cc!.rival, 'hire');
  assert.equal(cc!.strong[0].crit.id, 'cash');
  assert.deepEqual(cc!.rivalWins.map(w => w.crit.id), ['churn', 'nps']);
  assert.ok(cc!.small);
  assert.deepEqual(cc!.presets.map(p => p.id), ['cash_first', 'clients_first']);
  assert.equal(equalChoicesOf(cfg, i), null);
});

test('no ties known (a source without the meaningful difference): equal choices never, the close call rule unchanged', () => {
  const i = input({ equal: undefined, pBest: { keep: 0.6, debt: 0.3, hire: 0.1 } });
  assert.equal(equalChoicesOf(cfg, i), null);
  assert.equal(closeCallOf(cfg, i), null);
});

test('a decision tied with its runner-up while a third fork wins most scenarios: the close call, not equal choices', () => {
  // the live case B after the fixed anchors: Keep the plan = Reduce tech debt, Raise prices (here «hire») best in 66%
  const rows: Record<string, Partial<Pairwise>[]> = {
    'keep>hire': [{ bWins: 0.985, meanDiff: -533_690, medianMargin: 520_000, scoreDiff: -0.19 }, { bWins: 0.001, meanDiff: -0.065, scoreDiff: 0.18 }, { bWins: 0, meanDiff: 3.36, scoreDiff: 0.05 }],
    'hire>keep': [{ bWins: 0.004 }, { bWins: 0.991 }, { bWins: 1 }],
  };
  const i = input({
    pBest: { keep: 0.145, debt: 0.157, hire: 0.663 },
    equal: (a, b) => [a, b].sort().join() === 'debt,keep' ? ['cash', 'churn', 'nps'] : [],
    pairwise: (a, b) => pw(rows[`${a}>${b}`] ?? []),
    key: { keep: 0.4868, debt: 0.4868, hire: 0.4846 },
  });
  assert.equal(equalChoicesOf(cfg, i), null, 'the close call is the message');
  const cc = closeCallOf(cfg, i);
  assert.ok(cc);
  assert.equal(cc!.rival, 'hire');
  assert.equal(cc!.pBestOf, 3, 'its P(best) is out of the 3 forks of the ranking');
  assert.deepEqual(cc!.tied, ['debt']);
  // the decision keeps churn (largest part) and NPS (it wins it in every scenario, though its part is small)
  assert.deepEqual(cc!.strong.map(s => s.crit.id), ['churn', 'nps']);
  assert.deepEqual(cc!.rivalWins.map(w => w.crit.id), ['cash']);
  assert.ok(cc!.even, 'a score gap of 0.002 is «about the same on average»');
  assert.ok(Math.abs(cc!.gap - 0.0022) < 1e-9);
});
