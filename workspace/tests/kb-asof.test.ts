// The as-of guarantee of the dashboard metrics (the as-of rule, README.md): a sheet of a date depends only on what the knowledge
// base knew by the end of that date. Checked against a reference: the same metrics over a knowledge base that holds only
// the data recorded by then must give the same numbers, series and records.
import test from 'node:test';
import assert from 'node:assert/strict';
import { KbClient, type KbStaleHistoryItem, type KbVersion } from '../server/kb/client.js';
import { endOfDay, evaluate, metricsOf } from '../server/kb/metrics.js';
import { buildSnapshot } from '../server/kb/snapshot.js';
import { ORG, TZ, VERSIONS, v } from './kbFixture.js';

const late = (x: KbVersion, recorded: string): KbVersion => ({ ...x, recorded_at: recorded });
// data recorded after the business time it describes: a status change entered late, a corrected weekly report, a letter
// imported days later
const EXTRA: KbVersion[] = [
  late(v('jira.S-2', 'Bug', 'Refund stuck', '2026-09-09T03:00:00Z', { status: 'Done', priority: 'Highest', assignee: 'Riley Grant' }, 2), '2026-09-19T03:00:00Z'),
  v('conf.r1', 'Report', 'Weekly metrics', '2026-09-07T03:00:00Z', { labels: ['report'], properties: { 'Active clients': '300', 'Period': '2026-08-31 — 2026-09-06', 'Paying clients': '310' } }),
  late(v('conf.r1', 'Report', 'Weekly metrics', '2026-09-07T03:00:00Z', { labels: ['report'], properties: { 'Active clients': '280', 'Period': '2026-08-31 — 2026-09-06', 'Paying clients': '305' } }, 2), '2026-09-21T03:00:00Z'),
  late(v('mail.m8', 'Email', 'Claim', '2026-09-11T03:00:00Z', { from: 'boss@alpha.com', to: ['sales@larchwood.io'], sent_at: '2026-09-11T03:00:00Z', subject: 'Claim', message_id: '<m8>', thread_id: 't8' }), '2026-09-18T03:00:00Z'),
].map((x, i) => ({ ...x, version_id: `${x.version_id}#x${i}` }));
const ALL = [...VERSIONS, ...EXTRA];
const HISTORY: KbStaleHistoryItem[] = [{
  link_id: 'cl-9', effect_entity_id: 'jira.S-1', cause_entity_id: 'conf.d1', predicate: 'implements', predicate_label: 'implements',
  segments: [
    { from: '2026-09-05T03:00:00Z', to: '2026-09-12T03:00:00Z', status: 'STALE', changed_at: '2026-09-05T03:00:00Z', effect_kind: 'Bug', effect_title: 'Android crash', cause_kind: 'Decision', cause_title: 'Release on July 1' },
    { from: '2026-09-16T03:00:00Z', to: null, status: 'BROKEN', changed_at: '2026-09-16T03:00:00Z', effect_kind: 'Bug', effect_title: 'Android crash', cause_kind: 'Decision', cause_title: 'Release on July 1' },
  ],
}];

/** A knowledge base, optionally loaded only up to instant `t`: what it holds then is what its API returns. */
class AsOfKb extends KbClient {
  constructor(readonly t: number | null) { super('http://fake.invalid'); }
  private known = (at: string) => this.t === null || Date.parse(at) <= this.t;
  override async timeline() { return ALL.filter(x => this.known(x.recorded_at)); }
  override async stale() { return { items: [] }; }
  override async staleHistory() {
    if (this.t === null) return HISTORY;
    return HISTORY.map(x => ({ ...x, segments: x.segments.filter(g => this.known(g.from)).map(g => (g.to && Date.parse(g.to) > this.t! ? { ...g, to: null } : g)) }))
      .filter(x => x.segments.length);
  }
  override async visibleIds() { return new Set(ALL.map(x => x.entity_id)); }
  override async watermarks() { return { journal: 1 }; }
}

test('a metric sheet of a date equals the sheet of a knowledge base loaded only up to that date', async () => {
  const full = await buildSnapshot(new AsOfKb(null), ORG, 1);
  const visible = new Set(ALL.map(x => x.entity_id));
  let compared = 0, changed = 0;
  for (const date of ['2026-09-08', '2026-09-12', '2026-09-16', '2026-09-20', '2026-09-22']) {
    const cut = await buildSnapshot(new AsOfKb(endOfDay(date, TZ)), ORG, 1);
    for (const m of metricsOf(ORG)) {
      const a = evaluate(m, full, date, TZ, visible, 200, true), b = evaluate(m, cut, date, TZ, visible, 200, true);
      assert.deepEqual(a, b, `${m.id} on ${date}`);
      compared++;
      // the old cut-off rule (all data, business time up to the date) would have read the late data
      const old = m.compute({ s: full, t: endOfDay(date, TZ), tz: TZ, visible, metadata: true });
      if (JSON.stringify(old) !== JSON.stringify(m.compute({ s: full.view(endOfDay(date, TZ)), t: endOfDay(date, TZ), tz: TZ, visible, metadata: true }))) changed++;
    }
  }
  assert.ok(compared > 50 && changed > 0, `${compared} sheets compared, ${changed} would have leaked`);
});

test('the view of a date: late corrections and stale intervals are not known before they were', async () => {
  const s = await buildSnapshot(new AsOfKb(null), ORG, 1);
  const t20 = endOfDay('2026-09-20', TZ), t22 = endOfDay('2026-09-22', TZ);
  const report = (t: number) => s.view(t).items.get('conf.r1')!.versions.at(-1)!.fields.properties as Record<string, string>;
  assert.equal(report(t20)['Active clients'], '300');  // the correction was recorded on 21 September
  assert.equal(report(t22)['Active clients'], '280');
  assert.deepEqual(s.view(endOfDay('2026-09-08', TZ)).stale.map(x => x.changedAt), ['2026-09-05T03:00:00Z']);
  assert.deepEqual(s.view(endOfDay('2026-09-14', TZ)).stale, []);  // revalidated on the 12th
  assert.deepEqual(s.view(t22).stale.map(x => x.changedAt), ['2026-09-16T03:00:00Z']);
  assert.equal(s.view(t22), s.view(t22));  // memoised per instant
});
