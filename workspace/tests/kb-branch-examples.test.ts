// Example questions of a simulated future: found in the day artifact set of its case, in any of the expected forms,
// and none (never the company's) when the branch has no examples.
import test from 'node:test';
import assert from 'node:assert/strict';
import { exampleFiles, examplesOf, loadBranchExamples, loadBranchPosition, placeText, positionOf } from '../src/kb/branchExamples.js';

const T = 'company-b-pay_tech_debt-p50';
const day = (files: string[], tenants = [T, 'company-b-status_quo-p50']) => ({ id: 'day-company_best_year', manifest: { schema: 'variator.day/1', tenants, files: files.map(path => ({ path })) } });

test('the example files of a branch: in a day set that lists the branch, its own file first', () => {
  assert.deepEqual(exampleFiles(day(['day_examples.json', 'examples.json', `${T}/examples.json`, 'company-b-status_quo-p50/examples.json']), T),
    [`${T}/examples.json`, 'examples.json', 'company-b-status_quo-p50/examples.json']);
  assert.deepEqual(exampleFiles(day(['examples.json'], ['company-b-status_quo-p50']), T), []); // the set has not this branch
  assert.deepEqual(exampleFiles({ id: 'history-metrics-ceo', manifest: { schema: 'history-metrics' } }, T), []);
  assert.deepEqual(exampleFiles(day(['day_examples.json']), T), []); // day examples are days, not questions
});

test('the questions of a branch in every expected form, at most four, never another branch', () => {
  const qs = ['Why did incidents rise in March?', 'Why did the cash fall in April?', 'Why did Acme give notice?', 'Why did we hire?', 'Why did costs grow?'];
  assert.deepEqual(examplesOf(qs, T), qs.slice(0, 4));
  assert.deepEqual(examplesOf({ tenant: T, questions: qs.slice(0, 2) }, T), qs.slice(0, 2));
  assert.deepEqual(examplesOf({ questions: qs.slice(0, 2).map(question => ({ question, why: 'validated' })) }, T), qs.slice(0, 2));
  assert.deepEqual(examplesOf({ [T]: qs.slice(0, 3), other: ['x'] }, T), qs.slice(0, 3));
  assert.deepEqual(examplesOf({ branches: { [T]: { questions: qs.slice(1, 3) } } }, T), qs.slice(1, 3));
  assert.deepEqual(examplesOf({ tenant: 'company-b-status_quo-p50', questions: qs }, T), []); // another branch's file
  // WP19's two files (scripts/mvp/branch_examples.py): the delivery's and the day set's
  const checked = (question: string) => ({ question, entity: 'sim.SIG-0101', chain: 3 });
  assert.deepEqual(examplesOf({ tenant: T, checkedAt: '2026-10-28T02:50:00+00:00', examples: [checked(qs[0]), checked(qs[1])] }, T), qs.slice(0, 2));
  assert.deepEqual(examplesOf({ [T]: [checked(qs[2])], 'company-b-status_quo-p50': [checked(qs[3])] }, T), [qs[2]]);
  assert.deepEqual(examplesOf({ [T]: [] }, T), []);
  assert.deepEqual(examplesOf({ branches: { other: qs } }, T), []);
  assert.deepEqual(examplesOf(null, T), []);
});

test('loading: through the artifact API; nothing found or an API that is down gives no chips', async () => {
  const files: Record<string, unknown> = {
    '/api/variator/artifacts': { sets: [{ id: 'history-metrics-ceo', manifest: { kind: 'history-metrics' } }, day([`${T}/examples.json`])] },
    [`/api/variator/artifacts/day-company_best_year/${T}/examples.json`]: { questions: ['Why did incidents rise in March?'] },
  };
  const get = async (url: string) => { if (!(url in files)) throw new Error('404'); return files[url]; };
  assert.deepEqual(await loadBranchExamples(T, get), ['Why did incidents rise in March?']);
  assert.deepEqual(await loadBranchExamples('company-b-hire_5-p50', get), []);
  assert.deepEqual(await loadBranchExamples(T, async () => { throw new Error('502'); }), []);
});

test('the place of a branch: its true percentile from the day set, else only its order among the three (M7)', async () => {
  const ex = (tenant: string, extra: Record<string, unknown> = {}) => ({ kind: 'median', tenant, percentile: 50, horizonEnd: '2027-04-26', ...extra });
  const doc = { dayExamples: { pay_tech_debt: [ex('company-b-pay_tech_debt-p10'), ex(T, { percentileAtWindowEnd: 52, windowEndFan: { n: 1000, date: '2027-04-26' } })], hire_5: [ex(null as unknown as string)] } };
  assert.deepEqual(positionOf(doc, T), { percentile: 52, scenarios: 1000, date: '2027-04-26' });
  assert.equal(positionOf(doc, 'company-b-pay_tech_debt-p10'), null); // the nominal «percentile» of the tenant name is not used
  assert.equal(placeText('middle', positionOf(doc, T)), '≈ 52nd percentile of cash among 1,000 scenarios on 26 Apr 2027');
  assert.equal(placeText('lowest', null), 'Lowest of the three example days');
  assert.equal(placeText('highest', { percentile: 11.4, scenarios: null, date: null }), '≈ 11th percentile of cash');
  const files: Record<string, unknown> = { '/api/variator/artifacts': { sets: [day(['day_examples.json', 'examples.json'])] }, '/api/variator/artifacts/day-company_best_year/day_examples.json': doc };
  const get = async (url: string) => { if (!(url in files)) throw new Error('404'); return files[url]; };
  assert.deepEqual(await loadBranchPosition(T, get), { percentile: 52, scenarios: 1000, date: '2027-04-26' });
  assert.equal(await loadBranchPosition('company-b-pay_tech_debt-p10', get), null);
  assert.equal(await loadBranchPosition(T, async () => { throw new Error('502'); }), null);
});
