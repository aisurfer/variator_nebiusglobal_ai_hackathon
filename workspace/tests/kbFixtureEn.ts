// A second synthetic tenant: a US company in dollars on Pacific time with its own North Star. Every built-in metric of
// the CEO brief has data here as of 2026-09-22.
import { KbError, type KbVersion } from '../server/kb/client.js';
import type { KbOrganization } from '../src/kb/types.js';
import { FakeKb, v } from './kbFixture.js';

export const TZ_EN = 'America/Los_Angeles';
export const ORG_EN: KbOrganization = {
  id: 'harbor', tenant: 'harbor', name: 'Harbor Analytics Inc.', sector: 'B2B analytics', headcount: 30, timezone: TZ_EN, ownDomain: 'harbor.io',
  currency: 'USD', engineeringDepartment: 'Engineering',
  roles: [
    { id: 'ceo', label: 'CEO', title: 'Chief Executive Officer', name: 'Morgan Hale', personId: 'person.ceo', principal: 'user:ceo', metadata: true },
  ],
  northStar: { title: 'Weekly active customers', reportLabel: 'report', property: 'Active customers', period: 'Period', unit: 'customers',
    secondary: [{ property: 'Paying customers', label: 'paying' }, { property: 'MRR, $k', label: 'MRR, $k', summary: false }], target: 400, by: '2026-12-31' },
};

const mail = (id: string, from: string, to: string[], subject: string, at: string, thread: string, inReplyTo: string | null = null) =>
  v(`mail.${id}`, 'Email', subject, at, { from, to, sent_at: at, subject, message_id: `<${id}>`, in_reply_to: inReplyTo, thread_id: thread });
const doc = (id: string, kind: string, title: string, at: string, fields: Record<string, unknown>) => v(id, kind, title, at, { currency: 'USD', doc_date: at.slice(0, 10), ...fields });
const ticket = (key: string, kind: string, title: string, priority: string, steps: [string, string][]) =>
  steps.map(([at, status], i) => v(`jira.${key}`, kind, title, at, { key, status, priority, assignee: 'Alex Kim' }, i + 1));
const report = (n: number, at: string, active: number) => v(`confluence.page-90${n}`, 'Report', `Weekly metrics: week ${36 + n}`, at,
  { space: 'PROD', labels: ['report'], properties: { Period: `week ${36 + n}`, 'Active customers': String(active), 'Paying customers': String(200 + n), 'MRR, $k': String(120 + n) } });
const DAY = 86_400_000, END = Date.parse('2026-09-23T06:59:59.999Z'); // end of 2026-09-22 in Los Angeles
const action = (who: string, id: string, at: number) => ({ ...v(`confluence.page-${id}`, 'Document', id, new Date(at).toISOString(), { space: 'PROD' }), actor_id: who });

let seq = 0;
export const VERSIONS_EN: KbVersion[] = [
  v('org.acme', 'Organization', 'Acme Retail', '2026-06-01T00:00:00Z', { org_id: 'org-acme', kind: 'client', domain: 'acme.com' }),
  v('org.globex', 'Organization', 'Globex Logistics', '2026-06-01T00:00:00Z', { org_id: 'org-globex', kind: 'prospect', domain: 'globex.com' }),
  v('org.cloud', 'Organization', 'CloudHost LLC', '2026-06-01T00:00:00Z', { org_id: 'org-cloud', kind: 'vendor', domain: 'cloudhost.com' }),
  v('person.alex', 'Person', 'Alex Kim', '2026-06-01T00:00:00Z', { email: 'alex@harbor.io', department: 'Engineering', role: 'Backend engineer' }),
  v('person.sales', 'Person', 'Jordan Blake', '2026-06-01T00:00:00Z', { email: 'jordan@harbor.io', department: 'Sales', role: 'VP of Sales' }),
  v('person.pat', 'Person', 'Pat Morgan', '2026-06-01T00:00:00Z', { email: 'pat@acme.com', role: 'Chief Executive Officer', org: 'org-acme', external: true }),
  v('person.robin', 'Person', 'Robin Diaz', '2026-06-01T00:00:00Z', { email: 'robin@acme.com', role: 'Procurement manager', org: 'org-acme', external: true }),
  // answered in the thread two working hours later (Mon 09:00 -> 11:00 local)
  mail('e1', 'robin@acme.com', ['jordan@harbor.io'], 'Invoice for September', '2026-09-14T16:00:00Z', 't1'),
  mail('e2', 'jordan@harbor.io', ['robin@acme.com'], 'RE: Invoice for September', '2026-09-14T18:00:00Z', 't1', '<e1>'),
  // answered as a new letter with the same subject
  mail('e3', 'robin@acme.com', ['jordan@harbor.io'], 'Pilot dates', '2026-09-15T16:00:00Z', 't2'),
  mail('e4', 'jordan@harbor.io', ['robin@acme.com'], 'Re: Pilot dates', '2026-09-15T20:00:00Z', 't9'),
  // escalation wording, never answered
  mail('e5', 'robin@acme.com', ['jordan@harbor.io'], 'URGENT: refunds failing at checkout', '2026-09-16T16:00:00Z', 't3'),
  // the client's CEO, answered with a forward prefix in a new thread
  mail('e6', 'pat@acme.com', ['jordan@harbor.io'], 'Contract renewal', '2026-09-17T16:00:00Z', 't5'),
  mail('e7', 'jordan@harbor.io', ['pat@acme.com'], 'FW: Contract renewal', '2026-09-17T19:00:00Z', 't10'),
  mail('e8', 'robin@acme.com', ['jordan@harbor.io'], 'Complaint about the dashboard export', '2026-09-18T16:00:00Z', 't6'),
  mail('e9', 'jordan@harbor.io', ['robin@acme.com'], 'Re: Complaint about the dashboard export', '2026-09-21T17:00:00Z', 't6', '<e8>'),
  // a prospect that went quiet; a discount offered to the client
  mail('e10', 'jordan@harbor.io', ['ceo@globex.com'], 'Proposal for Globex', '2026-09-01T16:00:00Z', 't4'),
  mail('e11', 'jordan@harbor.io', ['robin@acme.com'], 'New pricing and a 10% discount for 2027', '2026-09-10T16:00:00Z', 't7'),
  doc('mail.e12.att-1', 'Invoice', 'Invoice #1042', '2026-09-10T16:00:00Z', { amount: 12500, counterparty: 'org-acme', number: '1042' }),
  doc('mail.e13.att-1', 'Invoice', 'CloudHost invoice 2026-09', '2026-09-08T16:00:00Z', { amount: 3400, counterparty: 'org-cloud', number: 'CH-0912' }),
  doc('mail.e14.att-1', 'CommercialProposal', 'Proposal: Acme analytics expansion', '2026-09-05T16:00:00Z', { amount: 48000, counterparty: 'org-acme', number: 'P-77' }),
  doc('mail.e15.att-1', 'Contract', 'Master services agreement — Globex', '2026-09-12T16:00:00Z', { amount: 60000, counterparty: 'org-globex', number: 'MSA-3' }),
  doc('mail.e16.att-1', 'Amendment', 'Amendment No. 2 — Acme seats', '2026-09-11T16:00:00Z', { counterparty: 'org-acme', number: 'A-2' }),
  // done this week after a bounce back from review; in progress for weeks; done the week before
  ...ticket('H-1', 'Bug', 'Export crashes on large tables', 'High', [['2026-09-01T16:00:00Z', 'To Do'], ['2026-09-02T16:00:00Z', 'In Progress'], ['2026-09-05T16:00:00Z', 'Review'], ['2026-09-06T16:00:00Z', 'In Progress'], ['2026-09-18T16:00:00Z', 'Done']]),
  ...ticket('H-2', 'Bug', 'Login loop on Safari', 'Highest', [['2026-09-03T16:00:00Z', 'In Progress']]),
  ...ticket('H-3', 'Task', 'Add an audit log', 'Medium', [['2026-09-08T16:00:00Z', 'To Do'], ['2026-09-09T16:00:00Z', 'In Progress'], ['2026-09-16T16:00:00Z', 'Done']]),
  v('gitlab.mr-1', 'MergeRequest', 'Fix export memory use', '2026-09-10T16:00:00Z', { state: 'opened', created: '2026-09-10T16:00:00Z', author: 'akim' }),
  v('conf.d1', 'Decision', 'Release 2.0 ships on July 1', '2026-06-15T16:00:00Z', { statement: 'Release 2.0 ships on July 1' }),
  v('conf.d2', 'Decision', 'New release date for 2.0: July 15', '2026-09-10T16:00:00Z', { statement: 'New release date for 2.0: July 15' }),
  report(1, '2026-09-07T15:30:00Z', 290), report(2, '2026-09-14T15:30:00Z', 293), report(3, '2026-09-21T15:30:00Z', 298),
  ...[1, 2, 3, 4].flatMap(k => Array.from({ length: 10 }, (_, i) => action('person.alex', `w${k}-${i}`, END - k * 7 * DAY - (i + 1) * 3_600_000))),
  ...Array.from({ length: 3 }, (_, i) => action('person.alex', `now-${i}`, END - (i + 1) * 3_600_000)),
].map(x => ({ ...x, version_id: `${x.version_id}#${seq++}` }));

export class FakeKbEn extends FakeKb {
  override visible = new Set(VERSIONS_EN.map(x => x.entity_id));
  override async timeline(): Promise<KbVersion[]> { if (this.down) throw new KbError(502, 'Knowledge base is unreachable'); return VERSIONS_EN; }
  override async stale(): Promise<{ items?: unknown[] }> {
    return { items: [{ effect_entity_id: 'jira.H-3', effect_title: 'Add an audit log', effect_kind: 'Task', cause_entity_id: 'conf.d1', cause_title: 'Release 2.0 ships on July 1', cause_kind: 'Decision', changed_at: '2026-09-10T16:00:00Z', predicate: 'implements', predicate_label: 'implements' }] };
  }
}
