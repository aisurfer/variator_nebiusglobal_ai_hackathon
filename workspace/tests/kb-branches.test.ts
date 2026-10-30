// Simulated futures as knowledge-base branches (G6): the list of loaded branches, every route with ?branch=<tenant>,
// an unknown branch refused, the read model cached per tenant, and the dates of a branch after T.
import test from 'node:test';
import assert from 'node:assert/strict';
import type { AddressInfo } from 'node:net';
import { createApp } from '../server/app.js';
import { branchCandidates, branchReady } from '../server/kb/branches.js';
import { KbError, type KbVersion } from '../server/kb/client.js';
import { FakeKb, ORG, VERSIONS, v } from './kbFixture.js';

const BR = 'company-b-pay_tech_debt-p50';
const HALF = 'company-b-pay_tech_debt-p90'; // loaded, its search not caught up yet
// the branch: the state at T (a client and a person) and what the simulation made after the fork date
const BRANCH: KbVersion[] = [
  v('org.a', 'Organization', 'Alpha Foods Ltd', '2026-10-26T03:00:00Z', { org_id: 'org-a', kind: 'client', domain: 'alpha.com' }),
  v('person.dev', 'Person', 'Riley Grant', '2026-10-26T03:00:00Z', { email: 'r@larchwood.io', department: 'Engineering', role: 'Backend engineer' }),
  v('sim.INV-9001', 'Invoice', 'Invoice INV-9001: Alpha Foods, November 2026', '2026-11-02T03:00:00Z', { amount: 77000, client: 'org-a', direction: 'receivable', issued_at: '2026-11-02', number: 'INV-9001' }),
  v('sim.INC-0101', 'Incident', 'api: stalled data loads', '2026-11-20T03:00:00Z', { severity: 'SEV2', status: 'resolved' }),
];

class BranchKb extends FakeKb {
  tenants: string[] = [];
  override async watermarks(tenant = 'company'): Promise<Record<string, unknown>> {
    if (tenant === 'company') return { journal: 42, search: 42, graph: 42, descriptors: 42 };
    if (tenant === BR) return { journal: 10, graph: 10, search: 10, descriptors: 10 };
    if (tenant === HALF) return { journal: 10, graph: 10, descriptors: 4 };
    throw new KbError(404, 'unknown tenant');
  }
  override async timeline(tenant = 'company') { this.tenants.push(`timeline:${tenant}`); return tenant === BR ? BRANCH : VERSIONS; }
  visibleCalls = 0;
  override async visibleIds(tenant = 'company') { this.visibleCalls++; return new Set((tenant === BR ? BRANCH : VERSIONS).map(x => x.entity_id)); }
  override async entity(tenant: string, p: string, id: string) { this.tenants.push(`entity:${tenant}`); return super.entity(tenant, p, id); }
  override async ask(tenant: string, p: string, q: string, mode = 'auto', at: { now?: string; knownAt?: string } = {}) { this.tenants.push(`ask:${tenant}`); return super.ask(tenant, p, q, mode, at); }
}

test('branch candidates come from the case files: tenant, fork, kind and a label people read', () => {
  const all = branchCandidates(ORG);
  assert.equal(all.length, 5 * 3 + 3 * 3); // case B: 5 forks, case A: 3 forks; 3 example futures each
  assert.deepEqual(all.find(b => b.tenant === BR), {
    tenant: BR, caseId: 'company_best_year', fork: 'pay_tech_debt', kind: 'median', place: 'middle',
    label: 'Reduce tech debt · middle example', from: '2026-10-26',
  });
  assert.ok(all.some(b => b.tenant === 'company-a-in_house-p10' && b.kind === 'pessimistic'));
  assert.deepEqual(branchCandidates(ORG, '/nonexistent'), []);
  assert.equal(branchReady({ journal: 10, graph: 10, search: 10, descriptors: 10 }), true);
  assert.equal(branchReady({ journal: 10, graph: 10, descriptors: 4 }), false);
  assert.equal(branchReady({ journal: 0, graph: 0, search: 0, descriptors: 0 }), false);
});

test('the KB API with branches: the list, every route with ?branch, unknown branches refused, a read model per tenant', async () => {
  const kb = new BranchKb();
  const server = createApp({ kb: { client: kb, organizations: [ORG] } }).listen(0, '127.0.0.1');
  await new Promise<void>(r => server.once('listening', r));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const json = async (path: string, init?: RequestInit) => { const r = await fetch(origin + path, init); return { status: r.status, body: await r.json() }; };
  try {
    // only the loaded and fully processed branch is listed
    const orgs = (await json('/api/kb/organizations')).body;
    assert.deepEqual(orgs[0].branches.map((b: { tenant: string; label: string }) => [b.tenant, b.label]), [[BR, 'Reduce tech debt · middle example']]);

    // the brief of the branch: its own tenant, its own numbers, and it says it is a simulated future
    const company = (await json('/api/kb/company/brief')).body;
    const brief = await json(`/api/kb/company/brief?branch=${BR}`);
    assert.equal(brief.status, 200);
    assert.equal(brief.body.branch.tenant, BR);
    assert.equal(brief.body.headline[0], 'Simulated future: Reduce tech debt · middle example, simulated from Oct 26, 2026. Not real history.');
    assert.ok(!company.headline.some((h: string) => h.startsWith('Simulated future')));
    // records written once by the simulation: no «items rest on grounds that have since changed» (M8)
    assert.ok(!brief.body.headline.some((h: string) => /grounds that have since changed/.test(h)), brief.body.headline.join(' | '));
    const invoiced = (b: { metrics: { id: string; value: number | null }[] }) => b.metrics.find(m => m.id === 'money.invoiced')?.value;
    assert.deepEqual([invoiced(company), invoiced(brief.body)], [150000, 77000]);
    assert.equal(brief.body.organization.tenant, BR);
    // the branch has no mail and no Jira: those numbers are unknown, not zero, and the headline leaves them out
    const awaiting = brief.body.metrics.find((m: { id: string }) => m.id === 'clients.awaiting');
    assert.deepEqual([awaiting.value, awaiting.status], [null, 'unknown']);
    assert.ok(!brief.body.headline.some((h: string) => h.startsWith('Clients:')), brief.body.headline.join('\n'));

    // the view dates of the branch run from its fork date to its last simulated day
    const dates = (await json(`/api/kb/company/dates?branch=${BR}`)).body;
    assert.deepEqual([dates.start, dates.end, dates.max, dates.default], ['2026-10-26', '2026-11-20', '2026-11-20', '2026-11-20']);
    assert.ok(brief.body.weeks.every((w: string) => w >= '2026-10-26'));
    assert.equal((await json(`/api/kb/company/brief?branch=${BR}&asOf=2026-10-01`)).status, 400); // before the fork

    // every route reads the branch tenant
    assert.equal((await json(`/api/kb/company/metrics/money.invoiced?branch=${BR}`)).body.value, 77000);
    assert.equal((await json(`/api/kb/company/entity/mail.m1?branch=${BR}`)).status, 200);
    const ask = await json(`/api/kb/company/ask?branch=${BR}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: 'Why did incidents rise?' }) });
    assert.equal(ask.status, 200);
    assert.ok(ask.body.asOf >= '2026-10-26', ask.body.asOf);
    assert.equal((await json(`/api/kb/company/status?branch=${BR}`)).body.tenant, BR);
    assert.ok(kb.tenants.includes(`entity:${BR}`) && kb.tenants.includes(`ask:${BR}`));

    // unknown, half-loaded or company tenants are refused
    for (const bad of ['company-b-nope-p50', HALF, 'company']) {
      const r = await json(`/api/kb/company/brief?branch=${bad}`);
      assert.equal(r.status, 400, bad);
      assert.match(r.body.error, new RegExp(`Unknown branch; loaded: ${BR}`));
    }

    // one read model per tenant: built once each, the company one untouched by the branch
    const builds = (t: string) => kb.tenants.filter(x => x === `timeline:${t}`).length;
    await json(`/api/kb/company/brief?branch=${BR}`);
    await json('/api/kb/company/brief');
    assert.deepEqual([builds('company'), builds(BR)], [1, 1]);

    // the brief of a tenant, date and role is built once; the state of the sources is kept 60 s
    const visibleBefore = kb.visibleCalls;
    const again = await json(`/api/kb/company/brief?branch=${BR}`);
    assert.deepEqual(again.body, brief.body);
    assert.equal(kb.visibleCalls, visibleBefore);
    const statsBefore = kb.calls.filter(c => c.name === 'stats').length;
    await json(`/api/kb/company/status?branch=${BR}`);
    await json(`/api/kb/company/status?branch=${BR}`);
    assert.equal(kb.calls.filter(c => c.name === 'stats').length, statsBefore);
  } finally { server.close(); }
});
