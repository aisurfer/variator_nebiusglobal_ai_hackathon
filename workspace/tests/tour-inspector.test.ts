// The inspector of the timeline (src/variator/case/model/inspector.ts): a decision says whose it is and that it changes
// only its fork (never «the settings of the best fork»); an event says it is in the world and hits every fork alike,
// names its actor and the risk of the register that already happened in the history.
import test from 'node:test';
import assert from 'node:assert/strict';
import { actorLine, cardOf, groupLabels, historyLine, inspectorLine, INSPECTOR_HINT, INSPECTOR_TITLE, riskOf } from '../src/variator/case/model/inspector.js';
import type { CaseConfig, Intervention } from '../src/variator/case/model/types.js';
import type { ActorCard, Risk } from '../src/variator/actors/types.js';

const item = (over: Partial<Intervention>): Intervention => ({ id: 'x', lane: 'decisions', kind: 'lever', label: 'X', description: '', monthPath: 'v|m', values: [], source: '', affects: [], ...over });
const cfgB = {
  id: 'company_best_year',
  forks: [{ id: 'status_quo', label: 'Keep the plan', baseline: true }, { id: 'hire_5', label: 'Hire 5 engineers' }, { id: 'pay_tech_debt', label: 'Reduce tech debt' }],
  interventions: [item({ id: 'hire_start', forks: ['hire_5'] }), item({ id: 'debt_start', forks: ['pay_tech_debt'] }), item({ id: 'gpu_event', kind: 'event', lane: 'factors' })],
} as unknown as CaseConfig;
const cfgA = { id: 'delivery_best_way', forks: [{ id: 'in_house', label: 'In-house team' }], interventions: [item({ id: 'env_work' }), item({ id: 'key_person_leaves', kind: 'event' })] } as unknown as CaseConfig;
const habit = (key: string, point: number) => ({ key, point }) as unknown as ActorCard['habits'][number];
const card = (id: string, kind: ActorCard['kind'], name: string, title: string, arr?: number) => ({ id, kind, name, title, habits: arr ? [habit('arr', arr)] : [] }) as unknown as ActorCard;
const cards = [card('VN-03', 'vendor', 'Nimbus Cloud', 'Cloud provider · terms 10 days'), card('LW-E06', 'person', 'Daniel Kowalski', 'Lead Backend Engineer'),
  card('CL-001', 'client', 'Meridian Freight Group', 'Key client · logistics', 1_022_400), card('CL-002', 'client', 'Halcyon Health Partners', 'Key client · health', 700_000), card('CL-003', 'client', 'Small Co', 'Client', 300_000),
  { id: 'company', kind: 'company', name: 'Larchwood Systems', title: 'Company', habits: [habit('arr_at_t', 12_001_200)] } as unknown as ActorCard];
const risk = (id: string, title: string, count: number, shocks: [string, string][]) => ({ id, title, count, shocks: shocks.map(([caseId, sid]) => ({ caseId, id: sid })) }) as unknown as Risk;
const risks = [risk('R4', 'Cloud provider capacity shortage', 7, [['company_best_year', 'gpu_shortage_event']]), risk('R5', 'Client gives notice (churn)', 19, [['company_best_year', 'key_account_event']]),
  risk('R10', 'Employee resigns', 3, [['company_best_year', 'key_person_event'], ['delivery_best_way', 'key_person_loss']]), risk('R11', 'A senior seat is empty', 1, [['company_best_year', 'key_person_event']])];

test('the header and the hint never read like the settings of the best fork', () => {
  assert.equal(INSPECTOR_TITLE, 'Selected on the timeline');
  assert.equal(INSPECTOR_HINT, 'Click any marker to see and change it.');
  const lever = inspectorLine(cfgB, item({ forks: ['hire_5'] }));
  assert.equal(lever, 'Your decision in the fork Hire 5 engineers. Its date and size change only that fork.');
  assert.doesNotMatch(lever, /best|leader/i);
  assert.equal(inspectorLine(cfgB, item({ kind: 'event' })), 'An event in the world. Placed events hit every fork the same way.');
  assert.equal(inspectorLine(cfgA, item({})), 'Your decision, the same in every fork. Its date and size change every fork alike.');
});

test('the two groups of the timeline', () => {
  assert.deepEqual(groupLabels(cfgB), { decisions: 'Your decisions · one per fork', events: 'Events you can place · the same for every fork' });
  assert.equal(groupLabels(cfgA).decisions, 'Your decisions · the same in every fork');
});

test('an event names its actor from the cards', () => {
  const gpu = item({ kind: 'event', entityId: 'org.VN-03' }), acct = item({ kind: 'event', entityId: 'org.CL-001' }), kp = item({ kind: 'event', person: 'LW-E06', entityId: 'person.lw-e06' });
  assert.equal(actorLine(cardOf(gpu, cards), cards), 'Nimbus Cloud is our cloud provider.');
  assert.equal(actorLine(cardOf(acct, cards), cards), 'Meridian Freight Group is our largest client, about 9% of ARR.');
  assert.equal(actorLine(cardOf(kp, cards), cards), 'Daniel Kowalski is the lead backend engineer.');
  assert.equal(actorLine(cardOf(item({ kind: 'event' }), cards), cards), null);
  // without the company card: the share of the clients that have a card
  const noCompany = cards.filter(c => c.kind !== 'company');
  assert.equal(actorLine(cardOf(acct, noCompany), noCompany), 'Meridian Freight Group is our largest client, about 51% of ARR.');
});

test('an event names the risk of the register that already happened (the first one that models its shock)', () => {
  const r = riskOf(cfgB, item({ kind: 'event', shock: 'gpu_shortage_event' }), risks);
  assert.deepEqual(r, { id: 'R4', title: 'Cloud provider capacity shortage', count: 7 });
  assert.equal(historyLine(r!), 'Already happened in the history: 7 times (R4)');
  assert.equal(riskOf(cfgB, item({ kind: 'event', shock: 'key_person_event' }), risks)?.id, 'R10');
  assert.equal(riskOf(cfgA, item({ kind: 'event', shock: 'key_person_loss' }), risks)?.id, 'R10');
  assert.equal(riskOf(cfgA, item({ kind: 'event', shock: 'scope_creep' }), risks), null, 'no risk of the register models it');
  assert.equal(historyLine({ id: 'R11', count: 1 }), 'Already happened in the history: 1 time (R11)');
});
