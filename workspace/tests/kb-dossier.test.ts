// Dossiers list the objects related to an entity, not the entity again: a person's tasks, incidents on call, merge
// requests and meetings, grouped, deduplicated and newest first; a task keeps its people. Same on a branch tenant.
import test from 'node:test';
import assert from 'node:assert/strict';
import type { AddressInfo } from 'node:net';
import { createApp } from '../server/app.js';
import { relatedGroups, sinceOf } from '../server/kb/router.js';
import { FakeKb, ORG } from './kbFixture.js';

const P = 'person.dev';
const at = (d: number) => `2026-09-${String(d).padStart(2, '0')}T08:00:00Z`;
const about = (subject: string, kind: string, title: string, predicate: string, day: number, extra: Record<string, unknown> = {}) =>
  ({ subject_id: subject, subject_kind: kind, subject_title: title, predicate_id: predicate, object_id: P, object_kind: 'Person', object_title: 'Riley Grant', valid_from: at(day), ...extra });
const STATEMENTS = [
  about('jira.S-1', 'Bug', 'Android crash', 'assignee', 2),
  about('jira.S-1', 'Bug', 'Android crash', 'assignee', 8), // the same bug again: listed once, at its newest date
  about('jira.S-2', 'Bug', 'Refund stuck', 'assignee', 3),
  about('sim.INC-0042', 'Incident', 'api: stalled data loads', 'assignee', 20),
  about('gitlab.mr-1', 'MergeRequest', 'Fix OOM', 'author', 10),
  ...Array.from({ length: 7 }, (_, i) => about(`conf.m${i}`, 'Meeting', `Sync ${i}`, 'participant', 1 + i)),
  about('mail.m9', 'Email', 'Old letter', 'addressed_to', 5, { retracted_at: '2026-09-06T00:00:00Z' }), // retracted: not shown
  { subject_id: P, predicate_id: 'works_at', object_id: 'org.a', object_kind: 'Organization', object_title: 'Alpha Foods Ltd', valid_from: at(1) },
];

class PersonKb extends FakeKb {
  override async entity(t: string, p: string, id: string): Promise<Record<string, unknown>> {
    if (id !== P) return super.entity(t, p, id);
    return { entity_id: P, kind: 'Person', title: 'Riley Grant', source_id: 'directory-company',
      head: { version_id: `${P}@1`, fields: { role: 'Backend engineer', department: 'Engineering', email: 'r@larchwood.io' }, body: '', effective_from: at(1) }, statements: STATEMENTS };
  }
}

test('related groups: one per relation and kind, each object once, newest first, the entity never listed again', () => {
  const groups = relatedGroups(STATEMENTS.filter(x => !('retracted_at' in x)), P, null);
  assert.deepEqual(groups.map(g => [g.label, g.total]), [
    ['Assigned: bugs', 2], ['On call for: incidents', 1], ['Authored: merge requests', 1], ['Meetings', 7], ['Linked to', 1],
  ]);
  const bugs = groups[0].items;
  assert.deepEqual(bugs.map(b => [b.id, b.date]), [['jira.S-1', at(8)], ['jira.S-2', at(3)]]);
  assert.ok(groups.every(g => g.items.every(x => x.id !== P)), 'never the person again');
  assert.deepEqual(groups[3].items.map(x => x.title).slice(0, 2), ['Sync 6', 'Sync 5']);
  assert.deepEqual(groups.at(-1)!.items.map(x => [x.title, x.via]), [['Alpha Foods Ltd', 'works at']]);
});

test('a person dossier: its own facts and related objects, no «Assignee: <the person>» cards; a task keeps its people', async () => {
  const server = createApp({ kb: { client: new PersonKb(), organizations: [ORG] } }).listen(0, '127.0.0.1');
  await new Promise<void>(r => server.once('listening', r));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  try {
    const d = await (await fetch(`${origin}/api/kb/company/entity/${P}`)).json();
    assert.deepEqual(d.people, []); // the bug: dozens of «Assignee Riley Grant» cards
    assert.deepEqual(d.facts.map((f: { label: string }) => f.label), ['role', 'department', 'email']);
    const groups = d.related as { label: string; total: number; items: { id: string; status: string | null }[] }[];
    assert.deepEqual(groups.map(g => g.label), ['Assigned: bugs', 'On call for: incidents', 'Authored: merge requests', 'Meetings', 'Linked to']);
    assert.equal(groups[0].items[0].status, 'Done'); // the bug's status from the read model
    assert.ok(!groups.some(g => g.items.some(x => x.id === 'mail.m9')), 'a retracted statement is not shown');
    // a task-like object keeps its people (its own statements), deduplicated
    const mail = await (await fetch(`${origin}/api/kb/company/entity/mail.m1`)).json();
    assert.deepEqual(mail.people, [{ role: 'Author', name: 'Paul Shaw' }]);
    assert.deepEqual(mail.related, []);
  } finally { server.close(); }
});

test('a client dossier says since when it is a client: the earliest contract start, never the directory load day (m6)', async () => {
  const item = (id: string, kind: string, date: string) => ({ id, title: id, kind, date, status: null, via: 'client' });
  const related = [
    { key: 'client:Invoice', label: 'Invoices', kind: 'Invoice', total: 1, items: [item('sim.INV-1', 'Invoice', '2025-11-01T06:00:00Z')] },
    { key: 'client:Contract', label: 'Contracts', kind: 'Contract', total: 2, items: [item('sim.CTR-1', 'Contract', '2026-07-01T06:44:38Z'), item('sim.CTR-2', 'Contract', '2025-10-27T06:00:00Z')] },
  ];
  const contracts: Record<string, Record<string, unknown>> = { 'sim.CTR-1': { start_date: '2025-04-01', signed_at: '2025-03-21' }, 'sim.CTR-2': { signed_at: '2025-06-02' } };
  const entity = async (id: string) => ({ head: { fields: contracts[id] } });
  assert.deepEqual(await sinceOf({ kind: 'Organization', related }, { kind: 'client' }, entity), { label: 'Client since', date: '2025-04-01' });
  // no contract: its earliest related record
  assert.deepEqual(await sinceOf({ kind: 'Organization', related: [related[0]] }, { kind: 'vendor' }, entity), { label: 'Vendor since', date: '2025-11-01' });
  assert.equal(await sinceOf({ kind: 'Person', related }, { kind: 'client' }, entity), null);
  assert.equal(await sinceOf({ kind: 'Organization', related }, { kind: 'partner' }, entity), null);
});
