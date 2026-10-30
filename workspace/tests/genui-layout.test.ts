// Pure helpers of the GenUI primitives: grid spans, unit and delta formatting with direction, status derivation,
// deterministic change ranking and the morning-brief arrangement.
import test from 'node:test';
import assert from 'node:assert/strict';
import { barGeometry, breakdownUnit, constantFields, eventAmount, eventLine, sharedPhrases, formatDelta, formatDiff, formatPoints, formatValue, longDay, monthLabel, plainDates, plainEngineText, plainFormula, plainLabel, recordPhrases, shortDay, spanFor, stamp, statusFor, STATUS_LABEL, valueParts, weekday, weightShare } from '../src/genui/primitives/format.js';
import { ATTENTION_LIMIT, arrangeBrief, comparedMorning, orderBreakdown, morningLabel, rankChanges, stepDate, summaryLine, surpriseCounts } from '../src/genui/primitives/brief.js';
import type { DashboardSpec, LineageNode, MetricResult, MetricSpec, WidgetSpec } from '../src/genui/contracts.js';

test('widget sizes map to 3/4/6/12 of 12 columns', () => {
  assert.equal(spanFor('sm'), 3);
  assert.equal(spanFor('md'), 4);
  assert.equal(spanFor('lg'), 6);
  assert.equal(spanFor('xl'), 12);
});

test('values are formatted by unit, compact for money, unknown is a dash', () => {
  assert.equal(formatValue(4_812_300, 'usd'), '$4.8M');
  assert.equal(formatValue(312_000, 'usd'), '$312K');
  assert.equal(formatValue(950, 'usd'), '$950');
  assert.equal(formatValue(-120_000, 'usd'), '−$120K');
  assert.equal(formatValue(4_812_300, 'usd', { compact: false }), '$4,812,300');
  assert.equal(formatValue(64.28, 'percent'), '64.3%'); // one decimal, as on the brief tile
  assert.equal(formatValue(4.46, 'percent'), '4.5%');
  assert.equal(formatValue(1, 'days'), '1 day');
  assert.equal(formatValue(3.46, 'days'), '3.5 days');
  assert.equal(formatValue(12, 'hours'), '12 h');
  assert.equal(formatValue(1, 'weeks'), '1 week');
  assert.equal(formatValue(2.5, 'weeks'), '2.5 weeks');
  assert.equal(formatDelta(3, 1, 'weeks', 'lower').text, '+2 weeks');
  assert.equal(formatValue(72.4, 'score'), '72');
  assert.equal(formatValue(2.43, 'multiple'), '2.4×');
  assert.equal(formatValue(1284, 'count'), '1,284');
  assert.equal(formatValue(0.643, 'ratio'), '0.64');
  assert.equal(formatValue(null, 'percent'), '—');
  assert.equal(formatValue(Number.NaN, 'count'), '—');
});

test('value parts separate the number from a small unit suffix', () => {
  assert.deepEqual(valueParts(64.2, 'percent'), { main: '64.2', suffix: '%' });
  assert.deepEqual(valueParts(72, 'score'), { main: '72', suffix: '/100' });
  assert.deepEqual(valueParts(3.5, 'days'), { main: '3.5', suffix: 'days' });
  assert.deepEqual(valueParts(4_800_000, 'usd'), { main: '$4.8M', suffix: '' });
  assert.deepEqual(valueParts(null, 'usd'), { main: '—', suffix: '' });
});

test('delta colour follows direction, not sign; unknown and context stay neutral', () => {
  assert.deepEqual(formatDelta(67, 64, 'percent', 'higher'), { text: '+3 pts', tone: 'good', diff: 3 });
  assert.deepEqual(formatDelta(61, 64, 'score', 'higher'), { text: '−3 pts', tone: 'bad', diff: -3 });
  assert.deepEqual(formatDelta(5, 3, 'count', 'lower'), { text: '+2', tone: 'bad', diff: 2 });
  assert.deepEqual(formatDelta(2, 3, 'count', 'lower'), { text: '−1', tone: 'good', diff: -1 });
  assert.deepEqual(formatDelta(4_920_000, 4_800_000, 'usd', 'context'), { text: '+$120K', tone: 'neutral', diff: 120_000 });
  assert.deepEqual(formatDelta(3, 3, 'count', 'higher'), { text: 'No change', tone: 'neutral', diff: 0 });
  assert.deepEqual(formatDelta(3, null, 'count', 'higher'), { text: 'No comparison', tone: 'unknown', diff: null });
  assert.deepEqual(formatDelta(null, 3, 'count', 'higher'), { text: 'No comparison', tone: 'unknown', diff: null });
  assert.equal(formatDelta(2.5, 1, 'days', 'lower').text, '+1.5 days');
});

test('status follows bands with direction, falls back to target, unknown for null', () => {
  const higher = { direction: 'higher' as const, bands: { good: 80, warn: 60 }, target: null };
  assert.equal(statusFor(higher, 85), 'good');
  assert.equal(statusFor(higher, 70), 'warn');
  assert.equal(statusFor(higher, 50), 'bad');
  const lower = { direction: 'lower' as const, bands: { good: 2, warn: 5 }, target: null };
  assert.equal(statusFor(lower, 1), 'good');
  assert.equal(statusFor(lower, 4), 'warn');
  assert.equal(statusFor(lower, 6), 'bad');
  assert.equal(statusFor({ direction: 'higher', bands: null, target: { value: 90, basis: 'plan' } }, 91), 'good');
  assert.equal(statusFor({ direction: 'higher', bands: null, target: { value: 90, basis: 'plan' } }, 80), 'warn');
  assert.equal(statusFor({ direction: 'context', bands: { good: 1, warn: 2 }, target: null }, 5), 'neutral');
  assert.equal(statusFor({ direction: 'higher', bands: null, target: null }, 5), 'neutral');
  assert.equal(statusFor(higher, null), 'unknown');
});

// ----------------------------------------------------------------------------------------------
const lineage = (records: LineageNode['records'] = []): LineageNode => ({ id: 'root', label: 'root', kind: 'aggregate', value: null, unit: null, explanation: '', formulaText: null, children: [], records });
function spec(id: string, over: Partial<MetricSpec> = {}): MetricSpec {
  return {
    id, title: id, businessQuestion: '', plainDefinition: '', howCalculated: ['x'], formula: { op: 'const', value: 1, label: 'x' }, unit: 'count', direction: 'higher', target: null, bands: null,
    window: { kind: 'all' }, scope: { kind: 'organization', employeeId: null, projectIds: [] }, breakdown: null, highlight: null, cadence: 'daily', comparison: 'both',
    visualization: { preferred: 'kpi', alternatives: [], rationale: '' }, dataSources: [], assumptions: [], openQuestions: [], limitations: [], confidence: 'high',
    contractVersion: 1, version: 1, organizationId: 'company', role: 'ceo', status: 'active', origin: { kind: 'seed', messageIds: [] }, createdAt: '', updatedAt: '', ...over,
  };
}
function result(metricId: string, value: number | null, previous: { day: number | null; week: number | null }, over: Partial<MetricResult> = {}): MetricResult {
  return {
    metricId, specVersion: 1, asOf: '2026-06-18', value, unit: 'count', status: 'neutral', previous, series: [], breakdown: [], highlight: [],
    changes: { comparedTo: '2026-06-17', added: [], removed: [], addedTotal: 0, removedTotal: 0 }, lineage: lineage(), coverage: { records: 1, unknownComponents: [], historyAvailable: true }, computedAt: '', engineVersion: 't', ...over,
  };
}
const ref = (id: string, fields: Record<string, string | number | boolean | null> = {}) => ({ dataset: 'issues' as const, id, label: id, detail: null, fields, evidenceIds: [] });

const specs = [
  spec('calm', { unit: 'score', bands: { good: 75, warn: 60 } }),
  spec('revenue', { unit: 'usd', direction: 'higher' }),
  spec('overdue', { direction: 'lower', bands: { good: 1, warn: 3 } }),
  spec('flat'),
  spec('records'),
  spec('nohist'),
];
const results: Record<string, MetricResult> = {
  calm: result('calm', 58, { day: 66, week: 78 }, { unit: 'score', status: 'bad' }), // good/warn → bad: status change
  revenue: result('revenue', 4_920_000, { day: 4_800_000, week: 4_500_000 }, { unit: 'usd' }), // +2.5 % day
  overdue: result('overdue', 4, { day: 3, week: 1 }, { status: 'bad' }), // warn → bad: status change, +33 %
  flat: result('flat', 10, { day: 10, week: 10 }),
  records: result('records', 7, { day: 7, week: 7 }, { changes: { comparedTo: '2026-06-17', added: [ref('a'), ref('b')], removed: [ref('c')], addedTotal: 2, removedTotal: 1 } }),
  nohist: result('nohist', 5, { day: null, week: null }, { coverage: { records: 5, unknownComponents: [], historyAvailable: false } }),
};

test('changes rank status changes first, then relative delta, then record churn; unknown is skipped', () => {
  const ranked = rankChanges(specs, results, 'day');
  // Both worsened to "bad"; the larger relative move (3 → 4 overdue) comes first.
  assert.deepEqual(ranked.map(c => c.metricId), ['overdue', 'calm', 'revenue', 'records']);
  const calm = ranked[1];
  assert.equal(calm.kind, 'status');
  assert.equal(calm.tone, 'bad');
  assert.equal(calm.statusFrom, 'warn');
  assert.equal(calm.statusTo, 'bad');
  assert.equal(calm.deltaText, '−8 pts');
  assert.equal(ranked[2].kind, 'delta');
  assert.equal(ranked[2].deltaText, '+$120K');
  assert.equal(ranked[3].kind, 'records');
  assert.equal(ranked[3].added, 2);
  assert.equal(ranked[3].removed, 1);
  assert.equal(rankChanges(specs, results, 'day', 2).length, 2);
  // Week comparison: calm was good a week ago, overdue was good.
  const week = rankChanges(specs, results, 'week');
  assert.equal(week.find(c => c.metricId === 'calm')?.statusFrom, 'good');
  assert.equal(week.find(c => c.metricId === 'overdue')?.statusFrom, 'good');
});

test('status change is only claimed when our derivation agrees with the engine status', () => {
  const disagree = { ...results, calm: { ...results.calm, status: 'warn' as const } };
  const item = rankChanges(specs, disagree, 'day').find(c => c.metricId === 'calm');
  assert.equal(item?.kind, 'delta');
});

const widget = (id: string, primitive: WidgetSpec['primitive'], metricIds: string[]): WidgetSpec => ({ id, primitive, metricIds, size: 'sm', title: null, options: { seriesDays: null, showTarget: null, limit: null, noteKind: null }, rationale: '' });
const board = (sections: WidgetSpec[][]): DashboardSpec => ({
  id: 'd', contractVersion: 1, organizationId: 'company', role: 'ceo', version: 1, generatedBy: { kind: 'seed', model: null, at: '' }, title: 'Morning', subtitle: '',
  sections: sections.map((widgets, i) => ({ id: `s${i}`, title: i ? `Section ${i}` : 'Today', description: null, widgets })),
});
const unknownResult = { ...results.nohist, value: null, status: 'unknown' as const };

test('morning brief: headline kept, rest bucketed by attention, change, stability and unknown', () => {
  const withHighlight = { ...results, flat: { ...results.flat, highlight: [ref('x')] }, nohist: unknownResult };
  const brief = arrangeBrief(board([
    [widget('w1', 'kpi', ['calm']), widget('w2', 'kpi', ['flat'])],
    [widget('w3', 'exceptionList', ['flat']), widget('w4', 'trend', ['revenue']), widget('w5', 'kpi', ['records']), widget('w6', 'kpi', ['overdue']), widget('w7', 'kpi', ['missing']), widget('w8', 'kpi', ['nohist'])],
  ]), specs, withHighlight, 'day');
  assert.deepEqual(brief.headline.map(w => w.id), ['w1', 'w2']);
  // Compact tiles lead their bucket so they share rows with tiles, then composer order.
  assert.deepEqual(brief.attention.map(w => w.id), ['w6', 'w3']);
  assert.deepEqual(brief.changed.map(w => w.id), ['w4']);
  assert.deepEqual(brief.stable.map(w => w.id), ['w5'], 'unknown and missing metrics are never "stable"');
  assert.deepEqual(brief.unknown, ['missing', 'nohist']);
  // An empty exception list is calm, not attention.
  assert.deepEqual(arrangeBrief(board([[widget('w1', 'kpi', ['calm'])], [widget('w3', 'exceptionList', ['flat'])]]), specs, results, 'day').attention.map(w => w.id), []);
});

test('morning brief shows a metric once as a tile and once in detail; an index keeps its component breakdown', () => {
  const index = spec('idx', { unit: 'score', bands: { good: 75, warn: 60 }, formula: { op: 'index', label: 'i', minCoverage: 0.6, components: [{ label: 'a', weight: 1, worst: 0, best: 1, minSample: 0, expr: { op: 'const', value: 1, label: 'a' } }] } });
  const rs = { ...results, idx: result('idx', 50, { day: 55, week: 60 }, { unit: 'score', status: 'bad', highlight: [ref('h')] }) };
  const brief = arrangeBrief(board([
    [widget('t1', 'kpi', ['idx'])],
    [widget('x1', 'exceptionList', ['idx']), widget('t2', 'scoreGauge', ['idx']), widget('b1', 'indexBreakdown', ['idx']), widget('r1', 'trend', ['idx']), widget('c1', 'comparison', ['idx', 'calm'])],
  ]), [...specs, index], rs, 'day');
  assert.deepEqual([...brief.attention, ...brief.attentionMore].map(w => w.id), ['b1', 'c1']);
  assert.deepEqual(brief.repeated.map(w => w.id), ['x1', 't2', 'r1']);
});

test('needs attention is capped; the rest folds behind "+N more"', () => {
  const many = Array.from({ length: 7 }, (_, i) => spec(`bad${i}`, { direction: 'lower', bands: { good: 1, warn: 3 } }));
  const rs = Object.fromEntries(many.map(s => [s.id, result(s.id, 9, { day: 9, week: 9 }, { status: 'bad' })]));
  const brief = arrangeBrief(board([[], many.map(s => widget(`w-${s.id}`, 'kpi', [s.id]))]), many, rs, 'day');
  assert.equal(brief.attention.length, ATTENTION_LIMIT);
  // The index breakdown and at-risk widgets are never the ones folded away.
  const index = spec('idx', { unit: 'score', bands: { good: 75, warn: 60 }, formula: { op: 'index', label: 'i', minCoverage: 0.6, components: [{ label: 'a', weight: 1, worst: 0, best: 1, minSample: 0, expr: { op: 'const', value: 1, label: 'a' } }] } });
  const warnSpecs = Array.from({ length: 5 }, (_, i) => spec(`warn${i}`, { direction: 'lower', bands: { good: 1, warn: 5 } }));
  const rs2 = { ...Object.fromEntries(warnSpecs.map(s => [s.id, result(s.id, 3, { day: 3, week: 3 }, { status: 'warn' })])), idx: result('idx', 50, { day: 50, week: 50 }, { unit: 'score', status: 'bad' }) };
  const mixed = arrangeBrief(board([[widget('t', 'kpi', ['idx'])], [...warnSpecs.map(s => widget(`w-${s.id}`, 'kpi', [s.id])), widget('b', 'indexBreakdown', ['idx'])]]), [...warnSpecs, index], rs2, 'day');
  assert.equal(mixed.attention[0].id, 'b');
  assert.equal(brief.attentionMore.length, 7 - ATTENTION_LIMIT);
});

test('summary line counts every active metric, separates "within range" from "no band", and names the biggest move', () => {
  const line = summaryLine(specs, results, 'day');
  assert.match(line, /^2 of 6 measures need attention, 3 moved since yesterday \(1 more with record changes only\)\./);
  assert.match(line, /Biggest move: overdue \+1 to 4\./);
  assert.match(line, /4 have no band set\.$/);
  assert.match(summaryLine(specs, results, 'day', new Set(['calm', 'overdue'])), /4 are not on this layout\.$/);
  assert.equal(summaryLine([spec('flat')], { flat: results.flat }, 'week'), 'The measure needs no attention, and nothing moved materially since last week. 1 has no band set.');
  const good = spec('g', { bands: { good: 5, warn: 2 } });
  assert.equal(summaryLine([good], { g: result('g', 10, { day: 10, week: 10 }, { status: 'good' }) }, 'day'), 'The measure is within range, and nothing moved materially since yesterday.');
  const two = summaryLine([spec('flat'), spec('nohist')], { flat: results.flat, nohist: unknownResult }, 'day');
  assert.equal(two, 'None of 2 measures needs attention, and nothing moved materially since yesterday. 1 has no band set. 1 cannot be computed for this morning.');
});

test('surprise counts: from records when the engine includes how it reached you, else from a companion metric, never an invented zero', () => {
  const tree: LineageNode = { ...lineage(), kind: 'metric', children: [
    { ...lineage([ref('i1', { toldFirstScore: 0, execAwareVia: 'client' }), ref('i2', { toldFirstScore: 1, execAwareVia: 'owner-report' }), ref('i3', { toldFirstScore: 0, execAwareVia: 'own-discovery' })]), id: 's' },
    { ...lineage([ref('i1', { toldFirstScore: 0, execAwareVia: 'client' })]), id: 'w' },
  ] };
  assert.deepEqual(surpriseCounts({ ...results.calm, lineage: tree }), { surprises: 2, issues: 3, outside: 1, outsideLabel: 'clients or outside parties' });
  assert.equal(surpriseCounts(results.calm), null);
  // Only toldFirstScore, as the real engine returns: no "from clients" unless a companion metric says so.
  const bare: LineageNode = { ...lineage(), kind: 'metric', children: [{ ...lineage([ref('i1', { toldFirstScore: 0 }), ref('i2', { toldFirstScore: 1 })]), id: 's' }] };
  assert.deepEqual(surpriseCounts({ ...results.calm, lineage: bare }), { surprises: 1, issues: 2, outside: null, outsideLabel: null });
  const ext = spec('ext', { formula: { op: 'count', dataset: 'issues', field: null, filter: [{ field: 'execAwareVia', op: 'in', value: ['client', 'external'] }], window: null, dateField: 'execAwareAt', label: 'x' } });
  assert.deepEqual(surpriseCounts({ ...results.calm, lineage: bare }, { specs: [ext], results: { ext: result('ext', 4, { day: 3, week: 1 }) } }), { surprises: 1, issues: 2, outside: 4, outsideLabel: 'clients or outside parties' });
  assert.equal(surpriseCounts({ ...results.calm, lineage: bare }, { specs: [ext], results: { ext: { ...result('ext', null, { day: null, week: null }) } } })?.outside, null);
  // A truncated dashboard view must not report partial counts: the caller loads the full result.
  const truncated: LineageNode = { ...tree, children: [{ ...tree.children[0], recordsTotal: 12 }] };
  assert.equal(surpriseCounts({ ...results.calm, lineage: truncated }), null);
});

test('record facts read as phrases; filter fields shared by every record are dropped', () => {
  const recs = [{ fields: { execAwareAt: '2026-06-18', knownToExec: true, toldFirstScore: 0, execAwareVia: 'client' } }, { fields: { execAwareAt: '2026-06-11', knownToExec: true, toldFirstScore: 1, execAwareVia: 'owner-report' } }];
  const omit = constantFields(recs);
  assert.deepEqual([...omit], ['knownToExec']);
  assert.deepEqual(recordPhrases(recs[0].fields, omit), ['A client told you first', 'You learned 18 Jun 2026']);
  assert.deepEqual(recordPhrases({ dueAt: '2026-05-30', statusAsOf: 'overdue', daysLate: 19, remindersAsOf: 2 }), ['Overdue', '19 days late', 'Chased twice', 'Due 30 May 2026']);
  assert.deepEqual(recordPhrases({ firstSignalAt: '2026-06-03', resolvedAsOf: false, openAgeDays: 15 }), ['Open 15 days', 'First signal 3 Jun 2026', 'Still open']);
  assert.ok(!recordPhrases({ toldFirstScore: 0, knownToExec: true }).some(p => /score|known/i.test(p)), 'implicit filter flags are not repeated');
  assert.deepEqual(sharedPhrases([{ fields: { dueDate: '2026-06-12', statusAsOf: 'Overdue' } }, { fields: { dueDate: '2026-06-12', statusAsOf: 'Overdue' } }]), ['Overdue', 'Due 12 Jun 2026']);
});

test('engine explanations are rewritten without catalog field names', () => {
  assert.equal(plainEngineText('Average toldFirstScore of 14 issues with execAwareAt in the last 28 days (2026-05-22 – 2026-06-18) where knownToExec is true.'),
    'Average told-first score of 14 issues that reached you in the last 28 days (22 May – 18 Jun 2026) where known to you.');
  assert.equal(plainEngineText('Count of 4 issues where knownToExec is true and execAwareVia is one of client, external.'), 'Count of 4 issues where known to you and how it reached you is client or external.');
  assert.equal(plainEngineText('excluded by "statusAsOf is doneOnTime"'), 'outside the filter (status is done on time)');
  assert.equal(plainEngineText('Problems where resolvedAsOf is false and openAgeDays > 14'), 'Problems where still open and days open > 14');
});

test('bars handle negative values from a shared zero; index weights are shares of the total', () => {
  const g = barGeometry([7, 2, -1]);
  assert.deepEqual(g(7), { left: 12.5, width: 87.5, negative: false });
  assert.deepEqual(g(-1), { left: 0, width: 12.5, negative: true });
  assert.equal(weightShare(20, 100), '20%');
  assert.equal(weightShare(25, 0), '—');
});

test('person breakdowns are listed alphabetically, never ranked by value', () => {
  const rows = [{ label: 'Marcus Reed', value: 2 }, { label: 'Isabel Torres', value: 7 }, { label: 'Amara Johnson', value: 0 }];
  assert.deepEqual(orderBreakdown(spec('p', { breakdown: { dimension: 'employee', limit: 5 } }), rows).rows.map(r => r.label), ['Amara Johnson', 'Isabel Torres', 'Marcus Reed']);
  assert.equal(orderBreakdown(spec('p', { breakdown: { dimension: 'employee', limit: 5 } }), rows).person, true);
  assert.deepEqual(orderBreakdown(spec('q', { breakdown: { dimension: 'project', limit: 5 } }), rows).rows, rows);
});

test('the comparison morning exists in the result history (no request for a day before history)', () => {
  const r = result('calm', 58, { day: null, week: null }, { asOf: '2026-03-30', series: [{ date: '2026-03-30', value: 58 }] });
  assert.equal(comparedMorning(r, 'day'), null);
  const r2 = result('calm', 58, { day: 1, week: 1 }, { series: [{ date: '2026-06-11', value: 1 }, { date: '2026-06-17', value: 1 }, { date: '2026-06-18', value: 58 }] });
  assert.equal(comparedMorning(r2, 'day'), '2026-06-17');
  assert.equal(comparedMorning(r2, 'week'), '2026-06-11');
});

test('change totals beat the length of truncated record samples', () => {
  const big = { ...results.records, changes: { comparedTo: '2026-06-17', added: [ref('a')], removed: [], addedTotal: 9, removedTotal: 4 } };
  const item = rankChanges(specs, { ...results, records: big }, 'day').find(c => c.metricId === 'records');
  assert.equal(item?.added, 9);
  assert.equal(item?.removed, 4);
});

test('morning label and date stepping over available mornings', () => {
  assert.equal(morningLabel('2026-06-18'), 'Thu 18 Jun 2026');
  const dates = ['2026-06-16', '2026-06-17', '2026-06-18'];
  assert.equal(stepDate(dates, '2026-06-18', -1), '2026-06-17');
  assert.equal(stepDate(dates, '2026-06-18', 1), null);
  assert.equal(stepDate(dates, '2026-06-16', -1), null);
  assert.equal(stepDate(dates, '2026-06-15', 1), '2026-06-16');
});

// ----------------------------------------------------------------------------------------------
// The brief tile and its sheet say the same thing (F3); the sheet's engine texts read as words and app dates (F1, F2).
test('one status vocabulary: the words of the brief tile', () => {
  assert.deepEqual(STATUS_LABEL, { good: 'On track', warn: 'Watch', bad: 'Needs attention', neutral: 'Context', unknown: 'Unknown' });
});

test('the sheet rounds as the tile: a share keeps one decimal, money as the brief writes it, no space before %', () => {
  assert.equal(formatValue(12.34, 'percent'), '12.3%');
  assert.equal(formatValue(99.991, 'percent'), '99.991%');
  assert.deepEqual(valueParts(12.34, 'percent'), { main: '12.3', suffix: '%' });
  assert.equal(formatValue(2_000_000, 'usd', { brief: true }), '$2.00M');
  assert.equal(formatValue(12_001_200, 'usd', { brief: true }), '$12.0M');
  assert.equal(formatValue(871_303.33, 'usd', { brief: true }), '$871,303');
  assert.equal(formatValue(59.94, 'days'), '59.9 days');
  assert.equal(formatValue(9.75, 'count'), '9.75');
});

test('a change that rounds to zero is no change, never −0', () => {
  assert.equal(formatPoints(-0.24), '−0.2 pts');
  assert.equal(formatPoints(-0.03), '−0.03 pts'); // below 0.1: up to three decimals, as on the tile
  assert.equal(formatPoints(-0.00001), '0 pts');
  assert.equal(formatPoints(0), '0 pts');
  assert.deepEqual(formatDelta(12.34, 12.37, 'percent', 'lower'), { text: '−0.03 pts', tone: 'good', diff: -0.03 });
  assert.deepEqual(formatDelta(12.34, 12.340001, 'percent', 'lower'), { text: 'No change', tone: 'neutral', diff: 0 });
  assert.deepEqual(formatDelta(100.2, 100, 'usd', 'higher'), { text: 'No change', tone: 'neutral', diff: 0 }); // «$0»
  assert.equal(formatDiff(-0.04, 'days'), '0 days');
  assert.equal(formatValue(-0.04, 'percent'), '0%');
  for (const t of [formatPoints(-0.0002), formatDiff(-0.3, 'usd'), formatDiff(-0.01, 'count'), formatValue(-0.001, 'score')]) assert.ok(!/[−-]0\b/.test(t), t);
});

test('dates read day before month, as in the rest of the app', () => {
  assert.equal(shortDay('2026-10-27'), '27 Oct');
  assert.equal(longDay('2026-10-01'), '1 Oct 2026');
  assert.equal(longDay('2026-10-01T00:30:00+01:00'), '1 Oct 2026'); // a local timestamp counts by its own day
  assert.equal(weekday('2026-10-27'), 'Tue 27 Oct');
  assert.match(stamp('2026-06-18T07:00:00Z') ?? '', /^18 Jun, \d\d:\d\d$/);
  assert.equal(longDay('2026-09-01'), '1 Sep 2026'); // as the case pages write it, never Chrome's en-GB «Sept»
  assert.equal(monthLabel('2026-09'), 'Sep 2026');
  for (const t of [shortDay('2026-10-27'), longDay('2026-10-27'), weekday('2026-10-27'), stamp('2026-10-27T07:00:00Z') ?? '']) assert.ok(!/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec) \d{1,2}\b/.test(t), `no month-first date: ${t}`);
});

test('engine dates: a year-long window keeps both years, so it never reads inverted', () => {
  // the churn window is the 365 days that end on the view date: without the years it read «Oct 27 to Oct 26»
  assert.equal(plainEngineText('Effective from 2025-10-27 to 2026-10-26'), 'Effective from 27 Oct 2025 to 26 Oct 2026');
  assert.equal(plainDates('PLAT Sprint 64 (2026-10-08 to 2026-10-22)'), 'PLAT Sprint 64 (8 Oct to 22 Oct 2026)');
  assert.equal(plainDates('Costs for 2027-01…2027-03'), `Costs for Jan–${longDay('2027-03-15').split(' ').slice(1).join(' ')}`);
  assert.equal(plainLabel('ARR at 2025-10-27'), 'ARR at 27 Oct 2025');
  assert.equal(plainLabel('a lower price from a competitor'), 'A lower price from a competitor');
  assert.equal(plainEngineText('Lost $1.51M of $12.22M ARR at 2025-10-27; 14 contracts ended.'), 'Lost $1.51M of $12.22M ARR at 27 Oct 2025; 14 contracts ended.');
});

const churnRecord = { dataset: 'contracts' as const, id: 'sim.CTR-0016', label: 'Maple Energy: platform services agreement', detail: { kind: 'kbEntity' as const, id: 'sim.CTR-0016' }, evidenceIds: [],
  fields: { client: 'Maple Energy', ACV: 81600, status: 'terminated', 'term end': '2026-10-01', billing: 'quarterly', 'lost ARR': 81600, ended: '2026-10-01', reason: 'a merger with another company', 'notice given': '2026-07-27' } };
const churnEvent: LineageNode = { id: 'root.0.0.0', label: 'Maple Energy: platform services agreement: contract terminated', kind: 'event', value: null, unit: null,
  explanation: 'Event contract_terminated of sim.CTR-0016, known at 2026-10-01T07:14:29+01:00', formulaText: null, children: [], records: [churnRecord], recordsTotal: 1 };

test('an event of the breakdown reads as who, what and when, with the amount it moved', () => {
  assert.equal(eventLine(churnEvent), 'Maple Energy · Contract terminated · 1 Oct 2026');
  assert.deepEqual(eventAmount(churnEvent), { text: '$81,600', caption: 'ARR lost' });
  // without its record the line still names no id and no code
  assert.equal(plainEngineText(churnEvent.explanation), 'Contract terminated · 1 Oct 2026');
  assert.equal(eventAmount({ ...churnEvent, records: [{ ...churnRecord, fields: { severity: 'SEV2' } }] }), null);
  assert.equal(plainEngineText('Declared cause (life_event) of Maple Energy: platform services agreement'), 'Recorded cause of Maple Energy: platform services agreement');
  assert.equal(plainEngineText('Implementation completed after its promised date (go_live_late_days > 0)'), 'Implementation completed after its promised date (go live late days > 0)');
  assert.equal(plainEngineText('Gross pay × (1 + employer tax rate 0.150)'), 'Gross pay × (1 + employer tax rate 0.15)');
  assert.equal(plainEngineText('Balance after BNK-01655 booked 2027-04-26: $2.46M'), 'Balance after BNK-01655 booked 26 Apr 2027: $2.46M');
  for (const t of [eventLine(churnEvent)!, plainEngineText(churnEvent.explanation)]) assert.ok(!/sim\.|_|T\d\d:/.test(t), t);
  // the catalogue's own wording of later sets (core.py chains) reads the same
  assert.equal(plainEngineText('Maple Energy · Contract terminated · 2026-10-01'), 'Maple Energy · Contract terminated · 1 Oct 2026');
  assert.equal(plainEngineText('Recorded cause of Maple Energy: platform services agreement'), 'Recorded cause of Maple Energy: platform services agreement');
});

test('record facts: amounts as money, dates with their year', () => {
  assert.deepEqual(recordPhrases(churnRecord.fields, new Set(), 6, 'contracts'),
    ['Maple Energy', 'Lost ARR $81,600', 'Ended 1 Oct 2026', 'A merger with another company', 'ACV $81,600', 'Status: terminated']);
});

test('a worked formula shows money and shares, and its result', () => {
  const step = (label: string, value: number | null, unit: LineageNode['unit'], formulaText: string | null = null, children: LineageNode[] = []): LineageNode =>
    ({ id: label, label, kind: 'operation', value, unit, explanation: '', formulaText, children });
  const churn = step('Gross ARR churn', 12.34, 'percent', '1507200.0 / 12217200.0 × 100', [step('ARR lost', 1507200, 'usd'), step('ARR at 2025-10-27', 12217200, 'usd')]);
  assert.equal(plainFormula(churn), '$1,507,200 / $12,217,200 × 100 = 12.3%');
  const runway = step('Runway', 60, 'months', 'min(2000000.0 / max(-48208.33, 1), 60)', [step('Cash', 2_000_000, 'usd'), step('Monthly net burn', -48208.33, 'usd')]);
  assert.equal(plainFormula(runway), 'min($2,000,000 / max(−$48,208, 1), 60) = 60 months');
  const variance = step('Budget variance', -5.51, 'percent', '(2645776.92 − 2800000.0) / 2800000.0 × 100', [step('Costs of 2026-07', 877000.28, 'usd')]);
  assert.equal(plainFormula(variance), '($2,645,777 − $2,800,000) / $2,800,000 × 100');
  assert.equal(plainFormula(step('NPS', 30.1, 'index', '(28 − 6) / 73 × 100', [step('Promoters', 28, 'count'), step('Detractors', 6, 'count')])), '(28 − 6) / 73 × 100 = 30');
  assert.equal(plainFormula(step('Revenue', 1008800, 'usd', 'sum(invoice.amount / period months) for 2026-09', [])), `sum(invoice.amount / period months) for ${plainDates('2026-09')}`);
  assert.equal(plainFormula(step('Cash', 2_000_000, 'usd', 'balance_after(BNK-01128)', [step('x', 2_000_000, 'usd')])), 'balance after(BNK-01128)');
  assert.equal(plainFormula(step('Budget', -5.51, 'percent', '(actual − annual_budget / 12 × months) / budget × 100', [])), '(actual − annual budget / 12 × months) / budget × 100');
  assert.equal(plainFormula(step('Latency', 181.8, 'ms', 'mean_d(sum_s w_s × p95_ms)', [])), 'mean_d(sum_s w_s × p95 ms)'); // math subscripts stay
});

test('a breakdown that adds up to a money step is shown in money, with that step named once', () => {
  const node = (label: string, value: number, unit: LineageNode['unit'], children: LineageNode[] = []): LineageNode => ({ id: label, label, kind: 'aggregate', value, unit, explanation: '', formulaText: null, children });
  const churn = node('Churn (gross ARR)', 12.34, 'percent', [node('Gross ARR churn', 12.34, 'percent', [node('ARR lost', 1_507_200, 'usd'), node('ARR at 2025-10-27', 12_217_200, 'usd')])]);
  const byReason = [{ value: 408_000 }, { value: 370_800 }, { value: 254_400 }, { value: 181_200 }, { value: 151_200 }, { value: 122_400 }, { value: 19_200 }];
  assert.deepEqual(breakdownUnit(byReason, churn, 'percent'), { unit: 'usd', of: 'ARR lost' });
  assert.equal(formatValue(byReason[0].value, breakdownUnit(byReason, churn, 'percent').unit), '$408K'); // not «408,000%»
  // shares that do not add up to a step keep the metric's unit
  assert.deepEqual(breakdownUnit([{ value: 13.02 }, { value: 16.39 }], churn, 'percent'), { unit: 'percent', of: null });
  assert.deepEqual(breakdownUnit([], churn, 'percent'), { unit: 'percent', of: null });
});
