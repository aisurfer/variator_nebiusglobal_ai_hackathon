// The change notice on the actors (src/variator/case/model/actorNotice.ts, WP25 R1): the edits rerun on the actors and
// the notice compares the previous actor result with the new one for the same fork, never with the formula model; a
// line that says «X → X» is dropped (QA p2).
import test from 'node:test';
import assert from 'node:assert/strict';
import { actorNotice, dropNoops, isNoop } from '../src/variator/case/model/actorNotice.js';
import type { ActorMonth } from '../src/variator/case/actors/types.js';
import type { CaseConfig } from '../src/variator/case/model/types.js';

const cfg = {
  criteria: [{ id: 'cash', label: 'Cash at year end', unit: 'USD', higher: true }, { id: 'churn', label: 'Client churn', unit: 'share', higher: false }],
  driverCriterion: 'cash',
  forks: [{ id: 'keep', label: 'Keep the plan', baseline: true }, { id: 'hire', label: 'Hire 5 engineers' }, { id: 'raise', label: 'Raise prices 8%' }],
  rules: [{ id: 'over_budget', label: 'Cost above budget' }],
} as unknown as CaseConfig;

function month(rank: Record<string, [number, number]>, hire: { ready: number; cash: number; budget: number }): ActorMonth {
  return {
    ranking: { options: Object.entries(rank).map(([option, [r, pBest]]) => ({ option, rank: r, score: 0.5, pBest, pFeasible: 1, stats: { cash: { mean: 0, p5: 0, p50: option === 'hire' ? hire.cash : 2_700_000, p95: 0 } } })) },
    options: {
      hire: { milestones: [{ id: 'hires_ready', label: 'New hires productive', pMet: hire.ready }], goals: [{ id: 'cash_goal', label: 'Cash at year end at least the goal', pMet: 0.83 }], checks: [{ id: 'over_budget', rule: 'over_budget', pEver: hire.budget }] },
      keep: { goals: [{ id: 'cash_goal', label: 'Cash at year end at least the goal', pMet: 0.98 }], checks: [] },
      raise: { goals: [], checks: [] },
    },
  } as unknown as ActorMonth;
}

test('the hiring move: the notice reads the actors for the fork it is about (QA R1: no formula 92% → 0%)', () => {
  const before = month({ keep: [1, 0.15], raise: [2, 0.64], hire: [3, 0.0] }, { ready: 0.21, cash: 2_500_000, budget: 0.3 });
  const after = month({ keep: [1, 0.15], raise: [2, 0.64], hire: [3, 0.0] }, { ready: 0.62, cash: 2_420_000, budget: 0.41 });
  const n = actorNotice(cfg, before, after, ['keep', 'hire', 'raise'], 'hire');
  assert.equal(n.fork, 'hire');
  assert.ok(n.comparable);
  assert.deepEqual(n.report.effect, ['New hires productive: 21% → 62%', 'Median cash at year end: $2.50M → $2.42M']);
  assert.deepEqual(n.report.risks, ['Cost above budget: 30% → 41%']);
  assert.ok(!n.report.effect.some(isNoop), 'the unchanged cash goal (83% → 83%) is not a line');
});

test('a preset that changes the answer: the leader line, then the fork that led before (by id, not by rank)', () => {
  const before = month({ keep: [1, 0.15], raise: [2, 0.64], hire: [3, 0] }, { ready: 0.2, cash: 2_500_000, budget: 0.3 });
  const after = month({ raise: [1, 0.87], keep: [2, 0.08], hire: [3, 0] }, { ready: 0.2, cash: 2_500_000, budget: 0.3 });
  const n = actorNotice(cfg, before, after, ['keep', 'hire', 'raise'], null);
  assert.equal(n.fork, 'keep');
  assert.equal(n.report.effect[0], 'Answer: Keep the plan → Raise prices 8%');
  assert.ok(n.report.effect.includes('P(best) of Keep the plan (of 3 forks): 15% → 8%'), 'with the forks it is computed over');
  assert.deepEqual(n.report.recommended, ['Read the Answer again: it now names Raise prices 8%']);
});

test('no-op lines are dropped: «Best option: Raise prices 8% → Raise prices 8%» (QA p2)', () => {
  assert.ok(isNoop('Best option: Raise prices 8% → Raise prices 8%'));
  assert.ok(!isNoop('Best option: Keep the plan → Raise prices 8%'));
  const r = dropNoops({ effect: ['Best option: Raise prices 8% → Raise prices 8%', 'Churn: 18% → 21%'], required: [], recommended: [], supporting: [], risks: [], tone: 'neutral' });
  assert.deepEqual(r.effect, ['Churn: 18% → 21%']);
});
