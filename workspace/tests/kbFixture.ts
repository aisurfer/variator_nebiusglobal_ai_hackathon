// Synthetic knowledge-base tenant shared by the KB tests: a fake /api/v1 client over a handful of versions.
import { KbClient, KbError, type KbVersion } from '../server/kb/client.js';
import type { KbOrganization } from '../src/kb/types.js';

export const TZ = 'Asia/Bangkok'; // UTC+7 all year: working hours are easy to read off the instants below
export const ORG: KbOrganization = { id: 'company', tenant: 'company', name: 'Larchwood Systems', sector: 'IT services', headcount: 20, timezone: TZ, ownDomain: 'larchwood.io',
  currency: 'USD', engineeringDepartment: 'Engineering',
  northStar: { title: 'Weekly active clients', reportLabel: 'report', property: 'Active clients', period: 'Period', unit: 'clients', target: 400, by: '2026-12-31',
    secondary: [{ property: 'Paying clients', label: 'paying' }] },
  roles: [
  { id: 'ceo', label: 'CEO', title: 'Chief Executive Officer', name: 'Avery Collins', personId: 'person.ceo', principal: 'user:ceo', metadata: true },
] };

let seq = 0;
export function v(entity: string, kind: string, title: string, at: string, fields: Record<string, unknown>, n = 1): KbVersion {
  return { version_id: `${entity}@${n}`, entity_id: entity, seq: n, op: n === 1 ? 'create' : 'update', kind, title, fields, effective_from: at, recorded_at: at, superseded_at: null, is_current: true, actor_id: null };
}
const mail = (id: string, from: string, to: string[], subject: string, at: string, thread: string, inReplyTo: string | null = null) =>
  v(`mail.${id}`, 'Email', subject, at, { from, to, sent_at: at, subject, message_id: `<${id}>`, in_reply_to: inReplyTo, thread_id: thread });

export const VERSIONS: KbVersion[] = [
  v('org.a', 'Organization', 'Alpha Foods Ltd', '2026-06-01T00:00:00Z', { org_id: 'org-a', kind: 'client', domain: 'alpha.com' }),
  v('org.p', 'Organization', 'Perspective Retail', '2026-06-01T00:00:00Z', { org_id: 'org-p', kind: 'prospect', domain: 'persp.com' }),
  v('person.dev', 'Person', 'Riley Grant', '2026-06-01T00:00:00Z', { email: 'r@larchwood.io', department: 'Engineering', role: 'Backend engineer' }),
  v('person.boss', 'Person', 'Paul Shaw', '2026-06-01T00:00:00Z', { email: 'boss@alpha.com', role: 'Chief Executive Officer', org: 'org-a', external: true }),
  // answered within the thread two working hours later (Mon 10:00 -> 12:00 local = 03:00Z -> 05:00Z)
  mail('m1', 'boss@alpha.com', ['sales@larchwood.io'], 'Invoice for September', '2026-09-14T03:00:00Z', 't1'),
  mail('m2', 'sales@larchwood.io', ['boss@alpha.com'], 'Re: Invoice for September', '2026-09-14T05:00:00Z', 't1', '<m1>'),
  // answered as a new letter with the same subject
  mail('m3', 'boss@alpha.com', ['sales@larchwood.io'], 'Pilot: dates', '2026-09-15T03:00:00Z', 't2'),
  mail('m4', 'sales@larchwood.io', ['boss@alpha.com'], 'Re: Pilot: dates', '2026-09-15T07:00:00Z', 't9'),
  // unanswered for days
  mail('m5', 'boss@alpha.com', ['sales@larchwood.io'], 'URGENT: refunds', '2026-09-16T03:00:00Z', 't3'),
  // a prospect that went quiet
  mail('m6', 'sales@larchwood.io', ['ceo@persp.com'], 'Proposal', '2026-09-01T03:00:00Z', 't4'),
  v('mail.m7.att-1', 'Invoice', 'Invoice #1 of 2026-09-10', '2026-09-10T03:00:00Z', { amount: 150000, doc_date: '2026-09-10', counterparty: 'org-a', number: '1' }),
  v('jira.S-1', 'Bug', 'Android crash', '2026-09-01T03:00:00Z', { status: 'To Do', priority: 'High', assignee: 'Riley Grant' }),
  v('jira.S-1', 'Bug', 'Android crash', '2026-09-02T03:00:00Z', { status: 'In Progress', priority: 'High', assignee: 'Riley Grant' }, 2),
  v('jira.S-1', 'Bug', 'Android crash', '2026-09-08T03:00:00Z', { status: 'Done', priority: 'High', assignee: 'Riley Grant' }, 3),
  v('jira.S-2', 'Bug', 'Refund stuck', '2026-09-03T03:00:00Z', { status: 'In Progress', priority: 'Highest', assignee: 'Riley Grant' }),
  v('gitlab.mr-1', 'MergeRequest', 'Fix OOM', '2026-09-10T03:00:00Z', { state: 'opened', created: '2026-09-10T03:00:00Z', author: 'rg' }),
  v('conf.d1', 'Decision', 'Release on July 1', '2026-06-15T03:00:00Z', { statement: 'Release on July 1' }),
  v('conf.d2', 'Decision', 'New release date: July 15', '2026-09-10T03:00:00Z', { statement: 'New release date: July 15' }),
].map(x => ({ ...x, version_id: `${x.version_id}#${seq++}` }));

export class FakeKb extends KbClient {
  down = false;
  visible = new Set(VERSIONS.map(x => x.entity_id));
  constructor() { super('http://fake.invalid'); }
  private check() { if (this.down) throw new KbError(502, 'Knowledge base is unreachable'); }
  override async timeline(): Promise<KbVersion[]> { this.check(); return VERSIONS; }
  override async stale(): Promise<{ items?: unknown[] }> { return { items: [] }; }
  override async visibleIds() { this.check(); return this.visible; }
  override async watermarks(_tenant?: string): Promise<Record<string, unknown>> { this.check(); return { journal: 42, search: 42 }; }
  /** Counts as known at `knownAt`, like the base: entities whose first version was recorded by then. */
  override async stats(_t: string, _p: string, knownAt?: string) {
    this.calls.push({ name: 'stats', at: knownAt });
    const first = new Map<string, KbVersion>();
    for (const v of VERSIONS) if (!first.has(v.entity_id) || v.seq < first.get(v.entity_id)!.seq) first.set(v.entity_id, v);
    const byKind: Record<string, number> = {};
    for (const v of first.values()) if (!knownAt || Date.parse(v.recorded_at) <= Date.parse(knownAt)) byKind[v.kind] = (byKind[v.kind] ?? 0) + 1;
    return { entities_by_kind: byKind, causal_links_by_status: { CURRENT: 3 } };
  }
  override async sources() { return { items: [{ source_id: 'mail-company', provider: 'email', name: 'Mail' }] }; }
  /** Calls with their date arguments: the view date must reach every call of one request. */
  calls: { name: string; at?: string; now?: string }[] = [];
  override async entity(_t: string, _p: string, id: string): Promise<Record<string, unknown>> { this.check(); if (!this.visible.has(id)) throw new KbError(404, 'not found'); return { entity_id: id, kind: 'Email', title: 'Invoice for September', head: { version_id: `${id}@2`, fields: { from: 'boss@alpha.com' }, body: '# Invoice, corrected', effective_from: '2026-09-15T03:00:00Z' }, statements: [{ subject_id: id, predicate_id: 'author', object_id: 'person.boss', object_title: 'Paul Shaw', object_kind: 'Person' }] }; }
  // two versions: recorded on Sep 14 and on Sep 15 (03:00Z)
  private versionsOf(id: string) { return [{ version_id: `${id}@1`, seq: 1, effective_from: '2026-09-14T03:00:00Z', recorded_at: '2026-09-14T03:00:00Z', actor_id: 'person.boss', changed_fields: ['body'] },
    { version_id: `${id}@2`, seq: 2, effective_from: '2026-09-15T03:00:00Z', recorded_at: '2026-09-15T03:00:00Z', actor_id: 'person.boss', changed_fields: ['body'] }]; }
  override async history(_t: string, _p: string, id: string, knownAt?: string) { this.calls.push({ name: 'history', at: knownAt }); return { versions: this.versionsOf(id).filter(x => !knownAt || x.recorded_at < knownAt) }; }
  override async entityAt(_t: string, _p: string, id: string, knownAt: string) {
    this.calls.push({ name: 'entityAt', at: knownAt });
    const known = this.versionsOf(id).filter(x => x.recorded_at <= knownAt);
    if (!known.length) throw new KbError(404, 'not found');
    const last = known[known.length - 1];
    return { version: { ...last, fields: { from: 'boss@alpha.com' }, kind: 'Email', title: 'Invoice for September' } };
  }
  override async version(_t: string, _p: string, versionId: string) { this.calls.push({ name: 'version' }); return { version_id: versionId, body: versionId.endsWith('@1') ? '# Invoice' : '# Invoice, corrected' }; }
  override async statements(_t: string, _p: string, id: string, knownAt?: string) { this.calls.push({ name: 'statements', at: knownAt }); return { statements: [{ subject_id: id, predicate_id: 'author', object_id: 'person.boss', object_title: 'Paul Shaw', object_kind: 'Person' }] }; }
  override async why(_t: string, _p: string, _id: string, knownAt?: string) { this.calls.push({ name: 'why', at: knownAt }); return { nodes: {}, links: [], chains: [] }; }
  override async effects(_t: string, _p: string, _id: string, knownAt?: string) { this.calls.push({ name: 'effects', at: knownAt }); return { nodes: {}, links: [] }; }
  override async ask(_t: string, _p: string, question: string, _mode = 'auto', at: { now?: string; knownAt?: string } = {}) {
    this.calls.push({ name: 'ask', at: at.knownAt, now: at.now });
    return { answer: `Answer to «${question}»`, answer_mode: 'template', intent: 'list', citations: [], unknowns: [], evidence: [],
      plan: { planner: 'llm', operation: 'list', kinds: ['Meeting'], understood: 'looking for: Meeting', text: '', notes: [],
        nodes: [{ id: 'g1', kinds: ['Person'], text: null, key: null, group: 'internal', entities: [] }],
        edges: [{ src: 'x', predicate: 'participant', dst: 'g1', negate: false }],
        time: { start: '2026-09-04T00:00:00+07:00', end: '2026-09-11T00:00:00+07:00', event: 'occurred', known_at: null, interpretation: '«last week»: Sep 4 to Sep 10' } },
      results: { list: { total: 3, shown: 3 } } };
  }
}
