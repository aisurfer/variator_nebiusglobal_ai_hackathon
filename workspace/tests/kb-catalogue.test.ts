// The CEO brief from the VARIATOR metric catalogue: which artifact set the brief reads, the fallback note, the
// drill-down built from a metric file, and the artifact routes through the workspace proxy.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { createApp } from '../server/app.js';
import { asBrief, catalogueSets, drilldownOf, fallbackNote, lineageSteps, onBranch, parameterRows, pickSet, type CatalogueMetric } from '../src/kb/catalogue.js';
import { formatDelta, formatValue as formatGenValue, STATUS_LABEL } from '../src/genui/primitives/format.js';
import { formatValue } from '../src/kb/api.js';
import { statusAgrees, tileOf } from '../src/kb/tiles.js';
import { METRICS, statusOf as kbStatusOf } from '../server/kb/metrics.js';
import type { KbBranch, KbBrief, KbMetricResult } from '../src/kb/types.js';
import { ORG } from './kbFixture.js';

const manifest = (asOf: string, builtAt: string, extra: Record<string, unknown> = {}) => ({
  kind: 'history-metrics', schema: 2, asOf, builtAt,
  brief: { role: 'ceo', file: 'briefs/ceo.json', top: ['rev.arr'], areas: [] },
  metrics: { 'rev.arr': { file: 'metrics/rev.arr.json' }, 'ops.incidents': { file: 'metrics/ops.incidents.json' } },
  ...extra,
});
const LIST = {
  sets: [
    { id: 'actors', manifest: { kind: 'variator.actors/1' } },
    { id: 'history-metrics', manifest: { kind: 'history-metrics', schema: 1, asOf: '2026-10-27', roles: { ceo: {}, finance: {}, delivery: {} } } },
    { id: 'ceo-old', manifest: manifest('2026-10-26', '2026-10-27T10:00:00+00:00') },
    { id: 'ceo-new', manifest: manifest('2026-10-26', '2026-10-27T15:00:00+00:00') },
    { id: 'ceo-week', manifest: manifest('2026-10-19', '2026-10-27T15:00:00+00:00') },
    { id: 'broken', error: 'manifest.json is not valid JSON' },
  ],
};

test('the catalogue sets: only sets with a CEO brief (schema 2); the set of the view date, newest build first', () => {
  const sets = catalogueSets(LIST);
  assert.deepEqual(sets.map(s => s.id), ['ceo-old', 'ceo-new', 'ceo-week']);
  assert.deepEqual(sets[0].metrics, { 'rev.arr': 'metrics/rev.arr.json', 'ops.incidents': 'metrics/ops.incidents.json' });
  assert.equal(pickSet(sets, '2026-10-26')?.id, 'ceo-new');
  assert.equal(pickSet(sets, '2026-10-19')?.id, 'ceo-week');
  // nothing after the view date may count: a set of another date is never used
  assert.equal(pickSet(sets, '2026-10-25'), null);
  assert.equal(pickSet(sets, '2026-10-27'), null);
  assert.equal(pickSet(sets, undefined), null);
  assert.deepEqual(catalogueSets({}), []);
  assert.deepEqual(catalogueSets(null), []);
});

test('a simulated future reads only its own branch set; the company never reads a branch set (M8)', () => {
  const BR = 'company-b-status_quo-p50';
  const branchSet = (tenant: string, id = `branch-metrics-${tenant}`) => ({ id, manifest: manifest('2027-04-26', '2026-10-28T01:35:14+00:00', { kind: 'branch-metrics', tenant, history: { tenant } }) });
  const list = { sets: [...LIST.sets, branchSet(BR), branchSet('company-b-status_quo-p10'), { id: 'no-tenant', manifest: manifest('2027-04-26', '2026-10-28T01:00:00+00:00', { kind: 'branch-metrics' }) }] };
  assert.deepEqual(catalogueSets(list).map(s => s.id), ['ceo-old', 'ceo-new', 'ceo-week']);
  const own = catalogueSets(list, BR);
  assert.deepEqual(own.map(s => [s.id, s.tenant, s.asOf]), [[`branch-metrics-${BR}`, BR, '2027-04-26']]);
  assert.equal(pickSet(own, '2027-04-26')?.id, `branch-metrics-${BR}`); // the branch's last day
  assert.equal(pickSet(own, '2027-04-19'), null); // another week of the future: its own knowledge-base brief
  assert.deepEqual(catalogueSets(list, 'company-b-hire_5-p50'), []);
});

test('a brief of a simulated future says so first, keeps metrics with data and weeks from the fork date', () => {
  const branch: KbBranch = { tenant: 'company-b-status_quo-p50', caseId: 'company_best_year', fork: 'status_quo', kind: 'median', place: 'middle', label: 'Keep the plan · middle example', from: '2026-10-26' };
  const m = (id: string, value: number | null) => ({ id, value }) as KbMetricResult;
  const b = { asOf: '2027-04-26', headline: ['Money: cash $2.47M.'], metrics: [m('fin.cash', 2.4e6), m('ops.team_load', null), m('rev.arr', 1.2e7)],
    top: ['fin.cash', 'ops.team_load'], areas: [{ id: 'delivery', title: 'Delivery', question: '?', metrics: ['ops.team_load'] }, { id: 'clients', title: 'Clients', question: '?', metrics: ['rev.arr'] }],
    weeks: ['2026-10-19', '2026-10-26', '2027-04-26'] } as unknown as KbBrief;
  const org = { ...ORG, branches: [branch] };
  const shown = onBranch(b, org, branch.tenant);
  assert.match(shown.headline[0], /^Simulated future: Keep the plan · middle example, simulated from 26 Oct 2026\. Not real history\.$/);
  assert.equal(shown.headline[1], 'Money: cash $2.47M.');
  assert.deepEqual(shown.metrics.map(x => x.id), ['fin.cash', 'rev.arr']);
  assert.deepEqual(shown.top, ['fin.cash']);
  assert.deepEqual(shown.areas.map(a => a.id), ['clients']);
  assert.deepEqual(shown.weeks, ['2026-10-26', '2027-04-26']);
  assert.equal(onBranch(shown, org, branch.tenant).headline.filter(h => h.startsWith('Simulated future')).length, 1); // said once
  assert.equal(onBranch(b, org, undefined), b); // the company: as it is
});

test('the fallback note says quietly why the brief comes from the knowledge base', () => {
  const sets = catalogueSets(LIST);
  assert.equal(fallbackNote(sets, '2026-10-26'), null);
  assert.equal(fallbackNote(null, '2026-10-26'), 'The VARIATOR metric catalogue is not available, so this brief is computed from the knowledge base.');
  assert.equal(fallbackNote([], '2026-10-26'), 'The VARIATOR metric catalogue is not available, so this brief is computed from the knowledge base.');
  assert.equal(fallbackNote(sets, '2026-10-12'), 'The VARIATOR metric catalogue is computed as of 19 Oct, 26 Oct; for this date this brief is computed from the knowledge base.');
  assert.match(fallbackNote(sets, '2026-10-26', 'HTTP 404')!, /could not be read \(HTTP 404\)/);
});

const METRIC: CatalogueMetric = {
  metric: {
    id: 'ops.incidents', roles: ['ceo'], area: 'delivery', title: 'Incidents · 30 days', question: 'How often does the platform break?',
    definition: 'Incidents that started in the last 30 days.', howCalculated: ['Incident records known by the date.', 'Count the ones of the last 30 days.'],
    unit: 'count', direction: 'lower', target: 4, bands: { good: 4, warn: 6 }, view: 'kpi', sources: ['Incidents (sim Incident)'], itemsLabel: 'Incidents, newest first',
    asOf: '2026-10-26', value: 1, status: 'good', previous: 1, series: [{ date: '2026-10-19', value: 0 }, { date: '2026-10-26', value: 1 }],
    breakdown: [{ key: 'SEV3', label: 'SEV3', value: 1 }], items: [], itemsTotal: 1, restrictedTotal: 0, sample: 1, caveats: ['Since the history start: 42 incidents.'],
  },
  result: {
    metricId: 'ops.incidents', specVersion: 1, asOf: '2026-10-26', value: 1, unit: 'count', status: 'good', previous: { day: 1, week: 0 },
    series: [{ date: '2026-10-19', value: 0 }, { date: '2026-10-26', value: 1 }], breakdown: [{ key: 'SEV3', label: 'SEV3', value: 1, detail: null }],
    highlight: [{ dataset: 'incidents', id: 'sim.INC-0042', label: 'api: stalled data loads', detail: { kind: 'kbEntity', id: 'sim.INC-0042' }, fields: { severity: 'SEV3' }, evidenceIds: ['sim.INC-0042'] }],
    changes: { comparedTo: '2026-10-19', added: [], removed: [], addedTotal: 0, removedTotal: 0 },
    lineage: { id: 'root', label: 'Incidents · 30 days', kind: 'metric', value: 1, unit: 'count', explanation: 'Incidents as of Oct 26: 1', formulaText: 'count(incidents started in 30 days) × 30 / 30', children: [
      { id: 'root.0', label: 'Incidents started in 30 days', kind: 'aggregate', value: 1, unit: 'count', explanation: 'Incidents with a start from Sep 27 to Oct 26', formulaText: null, children: [], records: [], recordsTotal: 1 },
    ] },
    coverage: { records: 1, unknownComponents: [], historyAvailable: true }, computedAt: '2026-10-27T19:17:00+00:00', engineVersion: 'variator.metrics.catalog/1.0',
  },
  params: { target: 4, bands: { good: 4, warn: 6 }, window_days: 30, options: { severity: 'any' } },
};

test('a metric file of the catalogue becomes the spec and result of the drill-down, with parameters and steps', () => {
  const { spec, result } = drilldownOf(METRIC, 'company');
  assert.equal(result, METRIC.result);
  assert.deepEqual([spec.id, spec.title, spec.businessQuestion, spec.unit, spec.direction, spec.role], ['ops.incidents', 'Incidents · 30 days', 'How often does the platform break?', 'count', 'lower', 'ceo']);
  assert.deepEqual(spec.formula, { op: 'metric', metricId: 'ops.incidents', label: 'count(incidents started in 30 days) × 30 / 30' });
  assert.deepEqual(spec.target, { value: 4, basis: 'parameter of the metric catalogue' });
  assert.deepEqual(spec.window, { kind: 'rolling', days: 30 });
  assert.equal(spec.highlight?.label, 'Incidents, newest first');
  assert.equal(spec.cadence, 'weekly');
  assert.deepEqual(spec.limitations, ['Since the history start: 42 incidents.']);
  assert.deepEqual(parameterRows(METRIC.params, v => formatGenValue(v, 'count')), [
    { label: 'Target', value: '4' }, { label: 'Bands', value: 'good 4 · watch 6' }, { label: 'Window', value: '30 days' }, { label: 'severity', value: 'any' },
  ]);
  assert.deepEqual(lineageSteps(METRIC.result), ['Incidents started in 30 days: Incidents with a start from Sep 27 to Oct 26']);
  // a record of the result opens the knowledge-base entity
  assert.deepEqual(result.highlight[0].detail, { kind: 'kbEntity', id: 'sim.INC-0042' });
});

test('a brief file must have the parts the page reads; the new units read as people expect', () => {
  const brief = { asOf: '2026-10-26', headline: [], metrics: [], top: [], areas: [] };
  assert.equal(asBrief(brief), brief);
  assert.throws(() => asBrief({ ...brief, areas: undefined }), /unknown shape/);
  assert.throws(() => asBrief(null), /unknown shape/);
  assert.equal(formatValue(60, 'months'), '60 mo');
  assert.equal(formatValue(181.8, 'ms'), '182 ms');
  assert.equal(formatValue(99.991, 'percent'), '99.991%');
  assert.equal(formatValue(-48208.33, 'money'), '−$48,208');
  // the numbers of the catalogue headline (core.tile_value, tests/metrics/catalog/test_catalog_artifacts.py): same
  // numbers, a true minus sign
  assert.deepEqual([formatValue(12_001_200, 'money'), formatValue(478_800, 'money'), formatValue(-5.51, 'percent'), formatValue(62, 'days'), formatValue(9.75, 'count'), formatValue(1.81, 'hours'), formatValue(30.1, 'index')],
    ['$12.0M', '$478,800', '−5.5%', '62 d', '9.75', '1.8 h', '30']);
  assert.equal(formatGenValue(60, 'months'), '60 months');
  assert.equal(formatGenValue(181.8, 'ms'), '182 ms');
  assert.equal(formatGenValue(99.991, 'percent'), '99.991%');
});

test('tiles: a runway at its cap reads 60+ mo, a negative burn reads as net cash flow, NPS is a plain index', () => {
  const card = (over: Partial<KbMetricResult>): KbMetricResult => ({ ...METRIC.metric, ...over });
  const runway = tileOf(card({ id: 'fin.runway', title: 'Runway', unit: 'months', value: 60, previous: 60, target: 12, direction: 'higher',
    display: { value: '60+ mo', note: 'The company is profitable: cash grows, so the runway is capped at 60 months (5 years).' } }));
  assert.deepEqual([runway.title, runway.value, runway.target, runway.delta], ['Runway', '60+ mo', '12 mo', 'no change vs last week']);
  assert.match(runway.note!, /profitable.*capped at 60 months/);

  const burn = tileOf(card({ id: 'fin.burn', title: 'Net burn per month', unit: 'money', value: -48208.33, previous: -41831.33, target: 0, direction: 'lower',
    series: [{ date: '2026-10-19', value: -41831.33 }, { date: '2026-10-26', value: -48208.33 }],
    display: { label: 'Net cash flow per month', negate: true, note: 'The company earns cash.' } }));
  assert.deepEqual([burn.title, burn.value, burn.delta, burn.target], ['Net cash flow per month', '+$48,208', '+$6,377 vs last week', null]);
  assert.deepEqual(burn.series.map(p => p.value), [41831.33, 48208.33]); // the spark rises as the inflow grows
  assert.ok(!burn.value.includes('−') && !burn.value.includes('-'), 'never a negative burn');

  const nps = tileOf(card({ id: 'cust.nps', title: 'NPS', unit: 'index', value: 30.1, previous: 37.5, target: 40, direction: 'higher', status: 'warn',
    display: { note: 'NPS runs from −100 to 100: the share of promoters (score 9–10) minus the share of detractors (0–6).' } }));
  assert.deepEqual([nps.value, nps.target, nps.delta], ['30', '40', '−7.4 pts vs last week']);
  assert.ok(!nps.value.includes('/ 100') && nps.note!.includes('−100 to 100'));

  // a metric without a hint shows as before
  const plain = tileOf(METRIC.metric);
  assert.deepEqual([plain.title, plain.value, plain.target, plain.note], ['Incidents · 30 days', '1', '4', null]);
  assert.equal(formatGenValue(30.1, 'index'), '30');
});

test('a badge never contradicts its target: «On track · target X» only when the value meets X', () => {
  const m = (value: number, target: number | null, direction: 'higher' | 'lower' | 'context', status: KbMetricResult['status']) => ({ value, target, direction, status });
  assert.equal(statusAgrees(m(30.1, 40, 'higher', 'good')), false); // the old NPS tile
  assert.equal(statusAgrees(m(30.1, 40, 'higher', 'warn')), true);
  assert.equal(statusAgrees(m(1, 0, 'lower', 'good')), false); // one letter waiting against a target of 0
  assert.equal(statusAgrees(m(1, 0, 'lower', 'warn')), true);
  assert.equal(statusAgrees(m(0, 0, 'lower', 'good')), true);
  assert.equal(statusAgrees(m(5, null, 'lower', 'good')), true);
  assert.equal(statusAgrees(m(5, 3, 'context', 'neutral')), true);
  // a status set another way (pace towards a dated goal) keeps its badge but drops the target from it
  const star = tileOf({ ...METRIC.metric, value: 12001, target: 14400, direction: 'higher', status: 'good', display: { note: 'Goal 14,400 by Dec 31, 2027.' } });
  assert.equal(star.target, null);
  assert.equal(star.note, 'Goal 14,400 by Dec 31, 2027.');
});

test('the brief metrics of the knowledge base: status by target and bands, never On track below the target', () => {
  const def = (target: number | null, good: number, warn: number, direction: 'higher' | 'lower' = 'lower') => ({ ...METRIC.metric, target, bands: { good, warn }, direction });
  assert.equal(kbStatusOf(def(0, 1, 3), 1), 'warn'); // client letters awaiting: target 0, good band 1
  assert.equal(kbStatusOf(def(0, 1, 3), 0), 'good');
  assert.equal(kbStatusOf(def(0, 1, 3), 4), 'bad');
  assert.equal(kbStatusOf(def(40, 30, 0, 'higher'), 35), 'warn');
  assert.equal(kbStatusOf(def(40, 30, 0, 'higher'), 41), 'good');
  assert.equal(kbStatusOf(def(null, 1, 3), 1), 'good'); // no target: the bands alone
  for (const metric of METRICS) {
    for (const value of [0, 1, 2, 5, 10, 100]) {
      const status = kbStatusOf(metric, value);
      assert.ok(statusAgrees({ value, target: metric.target, direction: metric.direction, status }), `${metric.id} at ${value}: ${status}`);
    }
  }
});

test('the artifact routes reach the VARIATOR API through the workspace proxy', async () => {
  const files: Record<string, unknown> = { '/api/variator/artifacts': LIST, '/api/variator/artifacts/ceo-new/metrics/ops.incidents.json': METRIC };
  const api: Server = createServer((req, res) => {
    const body = files[req.url ?? ''];
    res.writeHead(body ? 200 : 404, { 'content-type': 'application/json' });
    res.end(JSON.stringify(body ?? { error: { status: 404, message: `no such artifact: ${req.url}` } }));
  }).listen(0, '127.0.0.1');
  await new Promise<void>(r => api.once('listening', r));
  const server = createApp({ kb: { organizations: [ORG] }, variator: { baseUrl: `http://127.0.0.1:${(api.address() as AddressInfo).port}` } }).listen(0, '127.0.0.1');
  await new Promise<void>(r => server.once('listening', r));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  try {
    const sets = catalogueSets(await (await fetch(`${origin}/api/variator/artifacts`)).json());
    assert.equal(pickSet(sets, '2026-10-26')?.id, 'ceo-new');
    const metric = await (await fetch(`${origin}/api/variator/artifacts/ceo-new/metrics/ops.incidents.json`)).json() as CatalogueMetric;
    assert.equal(drilldownOf(metric, 'company').spec.title, 'Incidents · 30 days');
    const missing = await fetch(`${origin}/api/variator/artifacts/ceo-new/briefs/ceo.json`);
    assert.equal(missing.status, 404);
  } finally { server.close(); api.close(); }
  // the VARIATOR API down: the proxy answers 502, the brief falls back to the knowledge base
  const down = createApp({ kb: { organizations: [ORG] }, variator: { baseUrl: 'http://127.0.0.1:9' } }).listen(0, '127.0.0.1');
  await new Promise<void>(r => down.once('listening', r));
  try {
    const res = await fetch(`http://127.0.0.1:${(down.address() as AddressInfo).port}/api/variator/artifacts`);
    assert.equal(res.status, 502);
    assert.deepEqual(catalogueSets(await res.json()), []);
  } finally { down.close(); }
});

test('the churn tile and its sheet: one status word, one rounding, no «−0 pts»', () => {
  const card = (over: Partial<KbMetricResult>): KbMetricResult => ({ ...METRIC.metric, ...over });
  const churn = card({ id: 'rev.churn', title: 'Churn (gross ARR)', unit: 'percent', value: 12.34, previous: 12.58, target: 12, direction: 'lower', status: 'warn', bands: { good: 12, warn: 18 } });
  const tile = tileOf(churn);
  // the sheet (MetricDrilldown) writes the value, the status and the change with the genui helpers
  assert.equal(tile.value, '12.3%');
  assert.equal(formatGenValue(churn.value, 'percent'), tile.value);
  assert.equal(STATUS_LABEL[churn.status], 'Watch');
  assert.equal(tile.delta, '−0.2 pts vs last week');
  assert.equal(`${formatDelta(12.34, 12.58, 'percent', 'lower').text} vs last week`, tile.delta);
  // a change too small to show is no change on both
  assert.equal(tileOf(card({ ...churn, previous: 12.3400001 })).delta, 'no change vs last week');
  assert.equal(tileOf(card({ ...churn, unit: 'money', value: 100.2, previous: 100 })).delta, 'no change vs last week');
  assert.equal(formatDelta(12.34, 12.3400001, 'percent', 'lower').text, 'No change');
});
