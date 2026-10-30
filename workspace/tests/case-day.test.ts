// WP24: the Day section's rules (case/day/model.ts): each example its own data (never the identical week-tree bridge),
// the selected day and its events, people's words for the events, the metric choice and the knowledge-base links. A
// synthetic day file in the schema variator.day/1; WP19's real file is checked too when it is on this machine.
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import {
  askOrder, bandIndex, cleanText, coveredForks, dayMetrics, exampleCount, dayTones, dayValue, defaultMetricIds, distinct, examplesFor, eventView, fromDayFile, itemsOn, kbTarget,
  peopleEventText, pickMetrics, positionLabel, seriesDate, startDay, stepDay, toggleMetric, truePosition, type DayPoint, type RawDayExample,
} from '../src/variator/case/day/model.js';
import { listsFile } from '../src/variator/case/day/load.js';
import type { ActorDayExample, DayFile } from '../src/variator/case/actors/types.js';

const FORK_DATE = '2026-10-26';
const WORKDAYS = ['2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30', '2026-11-02', '2026-11-03'];
const METRICS = ([
  ['cash', 'Cash', 'USD', true], ['load', 'Team load', 'ratio', false], ['incidents', 'Incidents', 'count', false],
  ['arr', 'ARR', 'USD', true], ['headcount', 'Headcount', 'people', true], ['availability', 'Availability', 'share', true],
] as const).map(([id, label, unit, higher]) => ({ id, label, unit, higher, description: `${label} on the day` }));
function example(kind: 'pessimistic' | 'median' | 'optimistic', cash: number, events: ActorDayExample['events']): ActorDayExample {
  return {
    option: 'status_quo', kind, label: kind.charAt(0).toUpperCase() + kind.slice(1), summary: `${kind} example`, tenant: `company-b-status_quo-p${kind === 'pessimistic' ? 10 : kind === 'median' ? 50 : 90}`,
    percentile: kind === 'pessimistic' ? 10 : kind === 'median' ? 50 : 90, horizonEnd: WORKDAYS[WORKDAYS.length - 1], yearEnd: { cash: cash + 5e4 },
    days: WORKDAYS.map((date, i) => ({ date, values: { cash: cash + i * 1e4, load: 0.9 + i / 100, incidents: events.filter(e => e.date === date && e.type === 'incident').length, arr: 12e6, headcount: 20, availability: 0.999 } })),
    events,
  };
}
const EVENTS_MEDIAN: ActorDayExample['events'] = [
  { date: '2026-10-27', type: 'client', text: 'Orchard Media gives notice ($29k a year): budget cuts', entityId: 'sim.LE-0063' },
  { date: '2026-10-28', type: 'incident', text: 'SEV3 incident: ml-inference failed logins (5 clients, 15 min)', entityId: 'sim.INC-0043', person: 'LW-E13' },
  { date: '2026-10-28', type: 'shock', text: 'GPU capacity shortage at Nimbus Cloud', entityId: 'sim.LE-0064' },
  { date: '2026-11-02', type: 'people', text: 'Natalie Brooks (Senior Account Manager) resigns; last day 27 Nov', entityId: 'sim.LE-0070', person: 'LW-E18' },
];
const file = (exs: Record<string, ActorDayExample[]>): DayFile => ({ preview: false, caseId: 'company_best_year', source: 'actors', note: '', seed: 7, anchor: '2026-10-27', horizonMonths: 6, dayMetrics: METRICS, dayExamples: exs });
const DAY = file({
  status_quo: [
    example('optimistic', 2.1e6, [{ date: '2026-10-30', type: 'client', text: 'Crestline Academy buys an extra region: +$44k a year', entityId: 'sim.DEAL-0049' }]),
    example('pessimistic', 1.9e6, [{ date: '2026-10-28', type: 'incident', text: 'SEV2 incident: integrations failed logins (6 clients, 94 min)', entityId: 'sim.INC-0040', person: 'LW-E13' }]),
    example('median', 2.0e6, EVENTS_MEDIAN),
  ],
  pay_tech_debt: [],
});
const PEOPLE = { 'LW-E13': { name: 'Rhys Llewellyn', entity: 'person.lw-e13' } };

// ------------------------------------------------------------------------------------------------ examples
test('each example of the day file brings its own days, events and header, in pessimistic-median-optimistic order', () => {
  const r = examplesFor('status_quo', { file: DAY, onActors: true, forkDate: FORK_DATE, people: PEOPLE });
  assert.equal(r.source, 'day-file');
  assert.deepEqual(r.examples.map(e => e.id), ['pessimistic', 'median', 'optimistic']);
  assert.ok(distinct(r.examples));
  assert.deepEqual(r.examples.map(e => e.points[0].values.cash), [1.9e6, 2.0e6, 2.1e6]);
  assert.deepEqual(r.examples.map(e => e.headline.value), [1.95e6, 2.05e6, 2.15e6]);
  assert.equal(r.examples[0].headline.label, 'cash on 3 Nov 2026');
  assert.deepEqual(r.examples.map(e => e.items.length), [1, 4, 1]);
  assert.deepEqual(r.examples.map(e => e.tenant), ['company-b-status_quo-p10', 'company-b-status_quo-p50', 'company-b-status_quo-p90']);
  assert.equal(r.examples[1].outcome, '1 incident · 1 client notice · 1 exit');
  // days count from the fork date (26 Oct): the first working day after it is day 1
  assert.equal(r.examples[1].points[0].day, 1);
  assert.deepEqual(r.metrics.map(m => m.id), ['cash', 'load', 'incidents', 'arr', 'headcount', 'availability']);
  assert.equal(r.metrics.find(m => m.id === 'incidents')!.bars, true);
  assert.equal(r.metrics.find(m => m.id === 'cash')!.bars, false);
});

test('identical examples (the week-tree bridge: three leaves on one path) count as no example days', () => {
  const same = example('median', 2e6, EVENTS_MEDIAN);
  const bridge = file({ reserve_gpu: [{ ...same, kind: 'pessimistic', yearEnd: { cash: 676e3 } }, { ...same, yearEnd: { cash: 763e3 } }, { ...same, kind: 'optimistic', yearEnd: { cash: 874e3 } }] });
  const r = examplesFor('reserve_gpu', { file: bridge, onActors: true, forkDate: FORK_DATE });
  assert.equal(r.source, 'none');
  assert.equal(r.examples.length, 0);
});

test('a fork without day-file examples has none on actors; the preview only while the page has no actor set', () => {
  const preview = {
    days: { reserve_gpu: (['pessimistic', 'median', 'optimistic'] as const).map((id, k) => ({ id, label: id, outcome: '', headline: { value: k, unit: 'USD' as const, label: 'cash' }, daily: [{ day: 1, cash: k }, { day: 2, cash: k + 1 }], events: [{ day: 2, title: 'Shock', text: 'A competitor cuts prices: size 0.127', tone: 'bad' as const }] })) },
    dayMetrics: [{ id: 'cash', label: 'Cash', unit: 'USD' }],
  };
  assert.equal(examplesFor('reserve_gpu', { file: DAY, onActors: true, forkDate: FORK_DATE, preview }).source, 'none');
  assert.equal(examplesFor('pay_tech_debt', { file: DAY, onActors: true, forkDate: FORK_DATE, preview }).source, 'none');
  const pv = examplesFor('reserve_gpu', { file: null, onActors: false, forkDate: FORK_DATE, preview });
  assert.equal(pv.source, 'preview');
  assert.equal(pv.examples[0].points[0].date, '2026-10-27');
  assert.equal(pv.examples[0].items[0].text, 'A competitor cuts prices by about 13%');
  assert.deepEqual(coveredForks(DAY), ['status_quo']);
  // the tour's «{examples}»: the day file's count, none for a fork it lacks or without a file
  assert.equal(exampleCount(DAY, 'status_quo'), 3); assert.equal(exampleCount(DAY, 'reserve_gpu'), 0); assert.equal(exampleCount(null, 'status_quo'), 0);
});

// ------------------------------------------------------------------------------------------------ people's words
test('event texts in people\'s words, by event type', () => {
  assert.equal(peopleEventText('shock', 'A competitor cuts prices: size 0.127'), 'A competitor cuts prices by about 13%');
  assert.equal(peopleEventText('shock', 'Currency move raises offshore cost: size 0.08'), 'Currency move raises offshore cost by about 8%');
  assert.equal(peopleEventText('shock', 'GPU shortage at the cloud provider: size 0.4'), 'GPU shortage at the cloud provider: about 40% of the GPU capacity is lost');
  assert.equal(peopleEventText('shock', 'New regulation needs compliance work: size 2.5e+05'), 'New regulation needs compliance work: about $250k');
  assert.equal(peopleEventText('client', 'Orchard Media gives notice: ACV $29k'), 'Orchard Media gives notice: $29k a year');
  assert.equal(peopleEventText('client', 'Crestline Academy buys more: +$44k ACV'), 'Crestline Academy buys more: +$44k a year');
  assert.equal(peopleEventText('client', 'Deal won: Summit Health, ACV $120k'), 'New client Summit Health signs: $120k a year');
  assert.equal(peopleEventText('shock', 'Engineering load over capacity in Nov 2026: 1.07'), 'Engineering load over capacity in Nov 2026: 107% of capacity');
  assert.equal(peopleEventText('client', 'Quarry Lending leaves: service ended'), 'Quarry Lending leaves: service ended');
});

test('an incident: severity, service and fault, clients, duration and the person on call', () => {
  const e = eventView(EVENTS_MEDIAN[1], 1, FORK_DATE, PEOPLE);
  assert.equal(e.group, 'incident');
  assert.equal(e.title, 'SEV3 incident');
  assert.equal(e.text, 'ml-inference: failed logins');
  assert.equal(e.severity, 'SEV3'); assert.equal(e.clients, 5); assert.equal(e.minutes, 15); assert.equal(e.service, 'ml-inference');
  assert.equal(e.person, 'Rhys Llewellyn'); assert.equal(e.personEntity, 'person.lw-e13');
  assert.equal(e.tone, 'bad'); assert.equal(e.day, 2);
  // the bare form of the month engine; fields of the data win over the text
  const bare = eventView({ date: '2026-10-28', type: 'incident', text: 'SEV2 incident' }, 0, FORK_DATE);
  assert.equal(bare.title, 'SEV2 incident'); assert.equal(bare.text, '');
  const rich = eventView({ date: '2026-10-28', type: 'incident', text: 'SEV1 incident', severity: 'sev1', minutes: 200, clients: 30, personName: 'Ana Ruiz', person: 'LW-E99' } as never, 0, FORK_DATE);
  assert.equal(rich.severity, 'SEV1'); assert.equal(rich.minutes, 200); assert.equal(rich.person, 'Ana Ruiz'); assert.equal(rich.personEntity, 'person.lw-e99');
  // a decision loses its «Decision:» lead (the group says it)
  const d = eventView({ date: '2026-10-27', type: 'decision', text: 'Decision: 15% of engineering capacity goes to technical debt' }, 0, FORK_DATE);
  assert.equal(d.title, 'Decision'); assert.equal(d.text, '15% of engineering capacity goes to technical debt');
  assert.equal(eventView({ date: '2026-10-27', type: 'client', text: 'New client Pebble Media signs: $40k a year' }, 0, FORK_DATE).tone, 'good');
  assert.equal(eventView(EVENTS_MEDIAN[3], 0, FORK_DATE).group, 'people');
});

// ------------------------------------------------------------------------------------------------ the selected day
test('a day: its events (a weekend\'s with the next working day), the next and the previous day, the start of the keys', () => {
  const ex = examplesFor('status_quo', { file: DAY, onActors: true, forkDate: FORK_DATE }).examples[1];
  assert.deepEqual(itemsOn(ex, '2026-10-28').map(e => e.i), [1, 2]);
  assert.deepEqual(itemsOn(ex, '2026-10-29'), []);
  assert.deepEqual(itemsOn(ex, null), []);
  // a Saturday event falls on Monday
  const weekend = { ...ex, items: [...ex.items, { ...ex.items[0], i: 9, date: '2026-10-31' }] };
  assert.equal(seriesDate(ex.points, '2026-10-31'), '2026-11-02');
  assert.deepEqual(itemsOn(weekend, '2026-11-02').map(e => e.i), [3, 9]);
  const pts: DayPoint[] = ex.points;
  assert.equal(stepDay(pts, '2026-10-30', 1), '2026-11-02');
  assert.equal(stepDay(pts, '2026-10-27', -1), '2026-10-27');
  assert.equal(stepDay(pts, '2026-11-03', 5), '2026-11-03');
  assert.equal(stepDay(pts, null, 1), '2026-10-27');
  assert.equal(stepDay(pts, null, -1), '2026-11-03');
  assert.equal(startDay(ex, null, null), '2026-10-27');
  assert.equal(startDay(ex, '2026-10-30', '2026-10-28'), '2026-10-30');
  assert.equal(startDay(ex, null, '2026-10-28'), '2026-10-28');
  assert.deepEqual([...dayTones(ex)], [['2026-10-27', 'bad'], ['2026-10-28', 'bad'], ['2026-11-02', 'bad']]);
});

test('a click on a chart: the day of the band under it, none beside the plot', () => {
  const rect = { left: 100, width: 58 + 12 + 600 }; // 600 px of plot
  assert.equal(bandIndex(100 + 58 + 1, rect, 120), 0);
  assert.equal(bandIndex(100 + 58 + 302, rect, 120), 60);
  assert.equal(bandIndex(100 + 58 + 599, rect, 120), 119);
  assert.equal(bandIndex(100 + 10, rect, 120), null);
  assert.equal(bandIndex(100 + 58 + 640, rect, 120), null);
});

// ------------------------------------------------------------------------------------------------ the metric choice
test('the charted metrics: cash, ARR, load and incidents by default; the viewer picks up to four', () => {
  const all = ['cash', 'load', 'incidents', 'arr', 'headcount', 'availability', 'p95_ms', 'deploys'];
  assert.deepEqual(defaultMetricIds(all), ['cash', 'arr', 'load', 'incidents']);
  assert.deepEqual(defaultMetricIds(['cash', 'load', 'incidents']), ['cash', 'load', 'incidents']);
  assert.deepEqual(defaultMetricIds(['p95_ms', 'zeta', 'availability', 'cash', 'headcount']), ['cash', 'availability', 'headcount', 'p95_ms']);
  assert.deepEqual(pickMetrics(null, all), ['cash', 'arr', 'load', 'incidents']);
  assert.deepEqual(pickMetrics(['availability', 'cash', 'gone', 'cash'], all), ['cash', 'availability']);
  assert.deepEqual(pickMetrics(['gone', 7], all), ['cash', 'arr', 'load', 'incidents']);
  assert.deepEqual(pickMetrics(['p95_ms', 'deploys', 'headcount', 'availability', 'cash'], all).length, 4);
  assert.deepEqual(toggleMetric(['cash', 'arr', 'load', 'incidents'], 'availability', all), ['cash', 'arr', 'load', 'incidents']);
  assert.deepEqual(toggleMetric(['cash', 'arr', 'load', 'incidents'], 'load', all), ['cash', 'arr', 'incidents']);
  assert.deepEqual(toggleMetric(['cash', 'arr', 'incidents'], 'availability', all), ['cash', 'arr', 'incidents', 'availability']);
  assert.deepEqual(toggleMetric(['cash'], 'cash', all), ['cash']);
  assert.deepEqual(toggleMetric(['cash'], 'nope', all), ['cash']);
});

test('day values for people', () => {
  assert.equal(dayValue({ id: 'cash', unit: 'USD' }, 2456358), '$2.46M');
  assert.equal(dayValue({ id: 'load', unit: 'ratio' }, 0.966), '97%');
  assert.equal(dayValue({ id: 'x', unit: 'ratio' }, 1.234), '1.23');
  assert.equal(dayValue({ id: 'availability', unit: 'share' }, 0.9995), '99.95%');
  assert.equal(dayValue({ id: 'incidents', unit: 'count' }, 2), '2');
  assert.equal(dayValue({ id: 'headcount', unit: 'people' }, 19), '19');
  assert.equal(dayValue({ id: 'p95_ms', unit: 'ms' }, 178.4), '178 ms');
  assert.equal(dayValue({ id: 'cash', unit: 'USD' }, NaN), '—');
});

// ------------------------------------------------------------------------------------------------ knowledge-base links
test('entity links: the dossier on the example\'s branch once listed, else the company history (directory entities only)', () => {
  const org = { branches: [{ tenant: 'company-b-status_quo-p50' }] };
  const on = kbTarget('sim.INC-0043', 'company-b-status_quo-p50', org, '/cases/company_best_year')!;
  assert.equal(on.path, '/brief');
  assert.equal(on.branch, 'company-b-status_quo-p50');
  assert.deepEqual(on.params, { branch: 'company-b-status_quo-p50', entity: 'sim.INC-0043', metric: null });
  assert.equal(kbTarget('person.lw-e13', 'company-b-status_quo-p50', org, '/x')!.params.branch, 'company-b-status_quo-p50');
  // not listed (still loading) or no tenant: the history, for people and organisations only
  const off = kbTarget('person.lw-e13', 'company-b-status_quo-p90', org, '/cases/company_best_year')!;
  assert.deepEqual(off, { path: '/cases/company_best_year', params: { entity: 'person.lw-e13' }, branch: null });
  assert.equal(kbTarget('sim.INC-0043', 'company-b-status_quo-p90', org, '/x'), null);
  assert.equal(kbTarget('org.CL-001', undefined, null, '/x')!.branch, null);
  assert.equal(kbTarget(undefined, 'company-b-status_quo-p50', org, '/x'), null);
});

// ------------------------------------------------------------------------------------------------ WP19's second day file
test('catalogue metrics: no duplicates of the run series, clean labels, levels as lines, their units for people', () => {
  const defs = [
    ...METRICS, { id: 'p95_ms', label: 'p95 latency', unit: 'ms', higher: false, description: '' },
    { id: 'fin.cash', label: 'Cash', unit: 'money', higher: true, description: 'Balance', source: 'variator.metrics.catalog' },
    { id: 'rev.arr', label: 'ARR', unit: 'money', higher: true, description: '', source: 'variator.metrics.catalog' },
    { id: 'ppl.headcount', label: 'Headcount', unit: 'count', higher: false, description: '', source: 'variator.metrics.catalog' },
    { id: 'ops.incidents', label: 'Incidents · {window_days} days', unit: 'count', higher: false, description: 'Incidents that started in the last {window_days} days, per 30 days.', source: 'variator.metrics.catalog' },
    { id: 'ops.latency', label: 'p95 latency', unit: 'ms', higher: false, description: 'objective {opt.slo_ms} ms', source: 'variator.metrics.catalog' },
    { id: 'fin.runway', label: 'Runway', unit: 'months', higher: true, description: '', source: 'variator.metrics.catalog' },
  ];
  const ms = dayMetrics(defs);
  assert.deepEqual(ms.map(m => m.id), ['cash', 'load', 'incidents', 'arr', 'headcount', 'availability', 'p95_ms', 'ops.incidents', 'ops.latency', 'fin.runway']);
  const by = Object.fromEntries(ms.map(m => [m.id, m]));
  assert.equal(by['ops.incidents'].label, 'Incidents · rolling');
  assert.equal(by['ops.incidents'].description, 'Incidents that started in a rolling window, per 30 days.');
  assert.equal(by['ops.latency'].label, 'p95 latency · rolling');
  assert.equal(by['ops.latency'].description, 'objective … ms');
  assert.equal(by['ops.incidents'].bars, false); assert.equal(by.incidents.bars, true);
  assert.equal(by['fin.runway'].catalogue, true); assert.equal(by.cash.catalogue, false);
  assert.equal(cleanText('Taxes paid · {window_days} days', true), 'Taxes paid · rolling');
  assert.deepEqual(defaultMetricIds(ms.map(m => m.id)), ['cash', 'arr', 'load', 'incidents']);
  assert.equal(dayValue({ id: 'fin.burn', unit: 'money' }, -50527), '−$51k');
  assert.equal(dayValue({ id: 'rev.churn', unit: 'percent' }, 18.89), '19%');
  assert.equal(dayValue({ id: 'fin.budget_variance', unit: 'percent' }, -4.79), '−4.8%');
  assert.equal(dayValue({ id: 'ops.sla', unit: 'percent' }, 99.856), '99.86%');
  assert.equal(dayValue({ id: 'fin.runway', unit: 'months' }, 60), '60+ mo');
  assert.equal(dayValue({ id: 'cust.nps', unit: 'index' }, 30.6), '31');
  assert.equal(dayValue({ id: 'ops.mttr', unit: 'hours' }, 0.72), '43 min');
  assert.equal(dayValue({ id: 'fin.dso', unit: 'days' }, 59.9), '60 d');
});

test('WP19\'s incident facts (raw), records-only futures and the branch questions', () => {
  const ev = { date: '2026-10-28', type: 'incident', text: 'SEV3 incident on ML inference, restored in 0.3 h (on call: Rhys Llewellyn)', entityId: 'sim.INC-0043', person: 'LW-E13',
    raw: { severity: 'SEV3', component: 'ml-inference', symptom: 'failed logins', clients: 5, restore_minutes: 15, cause: 'cloud provider capacity' } };
  const e = eventView(ev, 0, FORK_DATE);
  assert.equal(e.title, 'SEV3 incident'); assert.equal(e.text, 'ML inference: failed logins');
  assert.equal(e.clients, 5); assert.equal(e.minutes, 15); assert.equal(e.cause, 'cloud provider capacity');
  assert.equal(e.person, 'Rhys Llewellyn'); assert.equal(e.personEntity, 'person.lw-e13');
  assert.equal(eventView({ ...ev, text: 'SEV2 incident on the web app, restored in 1.3 h (on call: Rhys Llewellyn)', raw: { severity: 'SEV2', symptom: 'missing data in reports' } }, 0, FORK_DATE).text, 'web app: missing data in reports');
  assert.deepEqual(askOrder(['Why did cash fall in January?', 'Why did incidents fall in February?', 'Why is the Deltawood Travel contract in notice?', 'Why is the Crestline Travel contract in notice?', 'What caused incident INC-0043?']),
    ['Why did incidents fall in February?', 'What caused incident INC-0043?', 'Why did cash fall in January?', 'Why is the Deltawood Travel contract in notice?']);
  assert.deepEqual(askOrder(null), []);
  const base = example('median', 2e6, EVENTS_MEDIAN);
  const [kb] = fromDayFile([{ ...base, kb: true, askExamples: ['Why did cash fall in January?', 'Why did incidents fall in February?'] } as RawDayExample], FORK_DATE);
  assert.deepEqual(kb.ask, ['Why did incidents fall in February?', 'Why did cash fall in January?']);
  assert.equal(kb.kb, true);
  const [rec] = fromDayFile([{ ...base, kb: false, tenant: null, askExamples: ['Why?'] } as RawDayExample], FORK_DATE);
  assert.deepEqual(rec.ask, []); assert.equal(rec.kb, false); assert.equal(rec.tenant, undefined);
});

// ------------------------------------------------------------------------------------------------ release fixes (QA WP25)
test('M7: an example says its place among the three and its true percentile, never a p10 it is not', () => {
  const base = example('pessimistic', 2.46e6, EVENTS_MEDIAN);
  // nominal p10/p50/p90 only: the place among the three, no percentile
  const [plain] = fromDayFile([base as RawDayExample], FORK_DATE);
  assert.equal(plain.truePercentile, undefined);
  assert.equal(positionLabel(plain, 1000), 'Lowest of the three · cash on 3 Nov 2026');
  // the true percentile among the fork's scenarios at the window end (WP19)
  const [real] = fromDayFile([{ ...base, percentileAtWindowEnd: 52.4, ensembleN: 1000 } as RawDayExample], FORK_DATE);
  assert.equal(positionLabel(real), 'Lowest of the three · ≈ 52nd percentile of cash among 1,000 scenarios on 3 Nov 2026');
  const [mid] = fromDayFile([{ ...base, kind: 'median', percentileAtWindowEnd: { percentile: 61 } } as RawDayExample], FORK_DATE);
  assert.equal(positionLabel(mid, 1000), 'Middle of the three · ≈ 61st percentile of cash among 1,000 scenarios on 3 Nov 2026');
  const [high] = fromDayFile([{ ...base, kind: 'optimistic', percentileAtWindowEnd: 0.913 } as RawDayExample], FORK_DATE);
  assert.equal(positionLabel(high), 'Highest of the three · ≈ 91st percentile of cash on 3 Nov 2026');
  assert.deepEqual(truePosition({ percentileAtWindowEnd: null }), { p: undefined, n: undefined });
  assert.deepEqual(truePosition({ percentileAtWindowEnd: { value: 12, n: 1000 } }), { p: 12, n: 1000 });
  for (const label of [plain, real, mid, high].map(e => positionLabel(e, 1000))) assert.doesNotMatch(label, /\bp\d+\b|percentile path/);
});

test('display hints: the burn reads as a net cash flow, its values turned, a gain with «+»', () => {
  const burn = { id: 'fin.burn', label: 'Net burn per month', unit: 'money', higher: false, description: 'Average monthly net outflow.', source: 'variator.metrics.catalog' };
  const [fallback] = dayMetrics([burn]);
  assert.equal(fallback.label, 'Net cash flow per month'); assert.equal(fallback.negate, true); assert.equal(fallback.higher, true);
  assert.match(fallback.description!, /sign turned/);
  const [hinted] = dayMetrics([{ ...burn, display: { label: 'Net cash flow', negate: true } }]);
  assert.equal(hinted.label, 'Net cash flow');
  const [plainCost] = dayMetrics([{ id: 'fin.costs', label: 'Costs', unit: 'money', higher: false, description: '', source: 'x' }]);
  assert.equal(plainCost.negate, undefined);
  assert.equal(dayValue(fallback, 50527), '+$51k'); assert.equal(dayValue(fallback, -26551), '−$27k');
  // the values turn in the examples (charts, card, tooltip read the same numbers)
  const ex = (cash: number) => { const e = example('median', cash, []); e.days = e.days.map(d => ({ ...d, values: { ...d.values, 'fin.burn': -50527 } })); return e; };
  const f = file({ status_quo: [{ ...ex(1.9e6), kind: 'pessimistic' }, ex(2e6), { ...ex(2.1e6), kind: 'optimistic' }] });
  f.dayMetrics = [...f.dayMetrics, burn];
  const r = examplesFor('status_quo', { file: f, onActors: true, forkDate: FORK_DATE });
  assert.equal(r.examples[1].points[0].values['fin.burn'], 50527);
  assert.equal(r.examples[1].points[0].values.cash, 2e6);
});

test('m2: a file is asked for only when its set lists it', () => {
  assert.equal(listsFile({ files: [{ path: 'day_examples.json' }, { path: 'examples.json' }] }, 'day_examples.json'), true);
  assert.equal(listsFile({ files: { 'month.json': {}, 'week-tree.json': {} } }, 'day/day_examples.json'), false);
  assert.equal(listsFile({ files: { 'month.json': {}, 'day/day_examples.json': {} } }, 'day/day_examples.json'), true);
  assert.equal(listsFile({ files: ['actors.json'] }, 'actors.json'), true);
  assert.equal(listsFile(undefined, 'actors.json'), false);
});

// ------------------------------------------------------------------------------------------------ WP19's real file
const REAL = ['/tmp/variator-1000/mvp-artifacts/future/company_best_year/day/day_examples.json', '/tmp/variator-1000/mvp-release/artifacts/day-company_best_year/day_examples.json'].find(existsSync);
test('WP19\'s day file: three different examples per fork, every event in people\'s words', { skip: !REAL && 'no day file on this machine' }, () => {
  const real = JSON.parse(readFileSync(REAL!, 'utf8')) as DayFile;
  for (const fork of coveredForks(real)) {
    const r = examplesFor(fork, { file: real, onActors: true, forkDate: FORK_DATE });
    assert.equal(r.source, 'day-file', fork);
    assert.equal(r.examples.length, 3);
    assert.equal(new Set(r.examples.map(e => JSON.stringify(e.points.map(p => p.values.cash)))).size, 3, `${fork}: three cash series`);
    for (const e of r.examples) {
      for (const i of e.items) assert.doesNotMatch(i.text, /size \d|ACV|\bLW-E\d/, i.text);
      for (const i of e.items.filter(x => x.group === 'incident')) assert.match(i.title, /^SEV\d incident$/, i.text);
      assert.ok(e.ask.length <= 4);
      if (e.kb === false) assert.deepEqual(e.ask, []);
    }
    for (const m of r.metrics) assert.doesNotMatch(`${m.label} ${m.description ?? ''}`, /[{}]/, m.id);
    assert.equal(new Set(r.metrics.map(m => m.label)).size, r.metrics.length, `${fork}: one label per metric`);
    const ids = r.metrics.map(m => m.id);
    if (ids.includes('arr')) assert.deepEqual(pickMetrics(null, ids), ['cash', 'arr', 'load', 'incidents']);
  }
  // demo step six: the median future of «Reduce tech debt» asks first why incidents fell in February
  const debt = examplesFor('pay_tech_debt', { file: real, onActors: true, forkDate: FORK_DATE }).examples.find(e => e.id === 'median');
  if (debt?.ask.some(q => /incidents fall in February/.test(q))) assert.equal(debt.ask[0], 'Why did incidents fall in February?');
});
