import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { AddressInfo } from 'node:net';
import { createApp } from '../server/app.js';
import { buildSnapshot } from '../server/kb/snapshot.js';
import { BRIEF, METRICS, OFF_BRIEF, briefMetricsFor, businessHours, endOfDay, evaluate, metricById, metricsFor } from '../server/kb/metrics.js';
import { linkQuote, loadKbOrganizations, whyOf } from '../server/kb/router.js';
import { markHighlights, peopleAnswer, withCitations } from '../src/kb/KbAsk.js';
import { FakeKb, ORG, TZ, VERSIONS, v } from './kbFixture.js';
import { FakeKbEn, ORG_EN, TZ_EN, VERSIONS_EN } from './kbFixtureEn.js';

test('business hours skip nights and weekends; end of day follows the tenant time zone', () => {
  assert.equal(businessHours(Date.parse('2026-09-18T11:00:00Z'), Date.parse('2026-09-21T03:00:00Z'), TZ), 2); // Fri 18:00 -> Mon 10:00
  assert.equal(new Date(endOfDay('2026-09-22', TZ)).toISOString(), '2026-09-22T16:59:59.999Z');
});

test('the brief metrics are computed from versions as of a date, with the records behind them', async () => {
  const s = await buildSnapshot(new FakeKb(), ORG, 42);
  const all = new Set(VERSIONS.map(x => x.entity_id));
  const get = (id: string, asOf = '2026-09-22', visible = all) => evaluate(metricById(id)!, s, asOf, TZ, visible, 50);
  const awaiting = get('clients.awaiting');
  assert.equal(awaiting.value, 1);
  assert.equal(awaiting.items[0].title, 'URGENT: refunds');
  assert.equal(get('clients.firstReply').value, 3); // replies after 2 h and 4 h
  assert.equal(get('clients.escalations').value, 3); // senior sender (3 letters), one also urgent
  assert.equal(get('money.invoiced').value, 150000);
  assert.equal(get('deals.silent').value, 1);
  assert.equal(get('eng.wip').value, 1);
  assert.equal(get('eng.bugs').value, 1);
  assert.equal(get('eng.cycle').value, 6);
  assert.equal(get('eng.reviewQueue').value, 1);
  // as of an earlier date the later artefacts do not exist yet
  assert.equal(get('eng.bugs', '2026-09-02').value, 1);
  assert.equal(get('clients.awaiting', '2026-09-13').value, 0);
  assert.ok(get('money.invoiced').series.length >= 2);
  // one role: the CEO brief holds every metric, the finance and delivery metrics included
  assert.ok(['deals.proposals', 'eng.wip', 'company.northStar'].every(id => metricsFor('ceo').some(m => m.id === id)));
});

test('the CEO brief layout: every metric once, a top row of 4–6 key numbers, then three areas', () => {
  const all = METRICS.map(m => m.id);
  const placed = [...BRIEF.top, ...BRIEF.areas.flatMap(a => a.metrics)];
  // every metric is placed once, on the brief or off it (a metric that would always show 0 here is off it)
  assert.deepEqual([...placed, ...OFF_BRIEF.map(x => x.id)].sort(), [...all].sort());
  assert.equal(new Set(placed).size, placed.length);
  assert.deepEqual(OFF_BRIEF.map(x => x.id), ['deals.discounts']);
  assert.ok(BRIEF.top.length >= 4 && BRIEF.top.length <= 6, `top row of ${BRIEF.top.length}`);
  assert.deepEqual(BRIEF.areas.map(a => [a.id, a.title]), [['money', 'Money'], ['clients', 'Clients and growth'], ['delivery', 'Delivery and service']]);
  assert.deepEqual(briefMetricsFor('ceo').map(m => m.id), placed, 'the brief order: the top row, then the areas');
  assert.deepEqual(metricsFor('ceo').map(m => m.id), all, 'every metric still opens by id');
  assert.ok(METRICS.every(m => m.area && m.roles.length === 1 && m.roles[0] === 'ceo'), 'every metric has an area and the one role');
});

test('open proposals read sales deals of a simulated history: proposal and negotiation stages as known on the date', async () => {
  const deal = (id: string, n: number, at: string, status: string, amount: number) => v(`sim.${id}`, 'Deal', `New business: ${id}`, at, { amount, client: 'org-a', client_name: 'Alpha Foods Ltd', status, owner_name: 'Riley Grant', expected_close: '2026-10-01' }, n);
  const sim = [
    deal('DEAL-1', 1, '2026-09-01T03:00:00Z', 'qualified', 50000), deal('DEAL-1', 2, '2026-09-10T03:00:00Z', 'proposal', 50000), deal('DEAL-1', 3, '2026-09-20T03:00:00Z', 'won', 50000),
    deal('DEAL-2', 1, '2026-09-05T03:00:00Z', 'proposal', 20000), deal('DEAL-2', 2, '2026-09-12T03:00:00Z', 'negotiation', 20000),
    deal('DEAL-3', 1, '2026-09-05T03:00:00Z', 'lead', 99000),
  ];
  class DealKb extends FakeKb { override async timeline() { return [...VERSIONS, ...sim]; } }
  const s = await buildSnapshot(new DealKb(), ORG, 44);
  const all = new Set([...VERSIONS, ...sim].map(x => x.entity_id));
  const get = (asOf: string) => evaluate(metricById('deals.proposals')!, s, asOf, TZ, all, 10);
  assert.equal(get('2026-09-08').value, 20000); // DEAL-2 at proposal; DEAL-1 still qualified
  assert.equal(get('2026-09-15').value, 70000); // DEAL-1 at proposal, DEAL-2 at negotiation
  const after = get('2026-09-22'); // DEAL-1 won: no longer open
  assert.equal(after.value, 20000);
  assert.deepEqual(after.items.map(i => [i.id, i.facts.stage, i.facts.amount]), [['sim.DEAL-2', 'negotiation', 20000]]);
});

test('dossier links: a cause-type quote of a simulated history becomes a title, never «process: CTR-0102»', async () => {
  const s = await buildSnapshot(new FakeKb(), ORG, 45);
  assert.equal(linkQuote('process: DEP-0480', 'sim.DEP-0480', s), null); // the linked object itself: its title shows
  assert.equal(linkQuote('habit: confluence.page-2000014', 'confluence.page-2000014', s), null);
  assert.equal(linkQuote('decision: conf.d2', 'mail.m1', s), 'New release date: July 15'); // another object: its title
  assert.equal(linkQuote('rule: RULE-7', 'mail.m1', s), 'RULE-7'); // unknown here: the bare id
  assert.equal(linkQuote('Status: resolved', 'mail.m1', s), 'Status: resolved'); // words of a document stay
  assert.equal(linkQuote('“Fix the regression PLAT-891”', 'x', s), '“Fix the regression PLAT-891”');
  assert.equal(linkQuote(null, 'x', s), null);
});

test('Ask «why»: the subject, its chains (effect first) and root causes from results.why, for people', () => {
  const node = (id: string, title: string, kind: string, date: string, extra: Record<string, unknown> = {}) =>
    ({ version_id: `${id}@1`, entity_id: id, title, kind_label: kind, effective_from: date, ...extra });
  const why = whyOf({ why: {
    target: node('sim.SIG-0002', 'Incidents fell in February 2027', 'life event', '2027-02-26T09:00:00+00:00'),
    nodes: {
      'sim.SIG-0002@1': node('sim.SIG-0002', 'Incidents fell in February 2027', 'life event', '2027-02-26T09:00:00+00:00'),
      'sim.LE-0087@1': node('sim.LE-0087', 'Debt work shows an effect', 'life event', '2027-02-20T09:00:00+00:00'),
      'sim.DEC-0004@1': node('sim.DEC-0004', 'Use 15% of engineering capacity for technical debt', 'decision', '2026-10-27T10:08:00+00:00',
        { actor: 'Avery Collins', key_fields: { statement: 'Spend 15% of capacity on debt.' } }),
    },
    chains: [
      { versions: ['sim.SIG-0002@1', 'sim.LE-0087@1', 'sim.DEC-0004@1'], steps: [{ predicate_label: 'caused by' }, { predicate: 'implements' }] },
      { versions: ['sim.SIG-0002@1'], steps: [] }, // no cause: left out
    ],
    roots: ['sim.DEC-0004@1'],
  } });
  assert.ok(why);
  assert.deepEqual([why.subject.id, why.subject.title, why.subject.kind], ['sim.SIG-0002', 'Incidents fell in February 2027', 'life event']);
  assert.equal(why.chains.length, 1);
  assert.deepEqual(why.chains[0].map(n => [n.title, n.via]), [['Incidents fell in February 2027', null], ['Debt work shows an effect', 'caused by'], ['Use 15% of engineering capacity for technical debt', 'implements']]);
  assert.deepEqual([why.roots[0].id, why.roots[0].actor, why.roots[0].statement], ['sim.DEC-0004', 'Avery Collins', 'Spend 15% of capacity on debt.']);
  assert.equal(whyOf({ list: [] }), null);
  assert.equal(whyOf(null), null);
  // the specific cause first; standing processes and policies after it, and not a root cause when a specific one exists
  const contract = whyOf({ why: {
    target: node('sim.CTR-0089', 'Brightwell: platform services agreement', 'contract', '2026-10-13T14:27:00Z'),
    nodes: {
      'sim.CTR-0089@1': node('sim.CTR-0089', 'Brightwell: platform services agreement', 'contract', '2026-10-13T14:27:00Z'),
      'sim.PROC-OPENING@1': node('sim.PROC-OPENING', 'Opening balances', 'process', '2025-10-27T05:07:00Z'),
      'sim.RULE-CLIENT-NOTICE@1': node('sim.RULE-CLIENT-NOTICE', 'Client notice', 'policy', '2025-10-27T05:31:00Z'),
      'sim.LE-0062@1': node('sim.LE-0062', 'Brightwell decides to leave', 'life event', '2026-10-13T07:00:00Z'),
      'sim.INC-0040@1': node('sim.INC-0040', 'ml-inference: high latency', 'incident', '2026-10-01T07:00:00Z'),
    },
    chains: [
      { versions: ['sim.CTR-0089@1', 'sim.PROC-OPENING@1'], steps: [{ predicate_label: 'caused by' }] },
      { versions: ['sim.CTR-0089@1', 'sim.RULE-CLIENT-NOTICE@1'], steps: [{ predicate_label: 'governed by' }] },
      { versions: ['sim.CTR-0089@1', 'sim.LE-0062@1', 'sim.INC-0040@1'], steps: [{ predicate_label: 'caused by' }, { predicate_label: 'caused by' }] },
    ],
    roots: ['sim.PROC-OPENING@1', 'sim.RULE-CLIENT-NOTICE@1', 'sim.INC-0040@1'],
  } });
  assert.deepEqual(contract?.chains.map(c => c[c.length - 1].title), ['ml-inference: high latency', 'Opening balances', 'Client notice']);
  assert.deepEqual(contract?.roots.map(r => r.title), ['ml-inference: high latency']);
});

test('Ask: search highlights render as marks, never as raw HTML tags', () => {
  assert.equal(markHighlights('<mark>Hiring</mark>: <mark>DevOps</mark> <mark>Engineer</mark>.'), '[Hiring](#mark): [DevOps](#mark) [Engineer](#mark).');
  assert.equal(markHighlights('a <mark>[x]</mark> b'), 'a [\\[x\\]](#mark) b');
  assert.equal(markHighlights('stray </mark> tag <mark>'), 'stray  tag ');
  assert.equal(markHighlights('<b>bold</b>'), '<b>bold</b>'); // other HTML is shown as text by the markdown renderer, not run
  const { text, order } = withCitations({ answer: '“Hiring” [jira.BIZ-197@6]: <mark>DevOps</mark>' });
  assert.equal(text, '“Hiring”  [1](#cite-jira.BIZ-197%406): [DevOps](#mark)');
  assert.deepEqual(order, ['jira.BIZ-197@6']);
});

// the raw «why» answer of the branch company-b-pay_tech_debt-p50 (kb-api, 26 Oct 2026), the demo question
const RAW_WHY = "Subject: “Incidents fell in February 2027” (LifeEvent, sim.SIG-0002; matched: ['title', \"alias 'Incidents fell in February 2027'\", 'semantic']).\nTime: “in February 2027” read as February 2027 [2027-02-01, 2027-03-01).\nKnowledge cut: as known at 2027-04-26 14:37 (UTC+01).\nChains from the effect to the root cause (declared links):\n• “Incidents fell in February 2027” (sim.SIG-0002@1) —implements→ “Use 15% of engineering capacity for technical debt” (sim.DEC-0004@1)\n• “Incidents fell in February 2027” (sim.SIG-0002@1) —caused by→ “Debt work shows an effect” (sim.LE-0087@1) —implements→ “Use 15% of engineering capacity for technical debt” (sim.DEC-0004@1)\nRoot causes:\n• “Use 15% of engineering capacity for technical debt” (Decision, [sim.DEC-0004@1]; effective 2026-10-27 10:08 (UTC+00), recorded 2026-10-27 10:09 (UTC+00); statement: From 27 October 2026 the delivery team spends 15% of its capacity on technical debt: the services with the most incidents first.; owner: LW-E01; decided_at: 2026-10-27T10:08:22+00:00)\nInterpretation: “why” means the grounds relied upon when the decision was made (transaction axis); the status shows whether they changed since.\nFragments found:\n• “Incidents fell in February 2027” [sim.SIG-0002@1]: Life event SIG-0002 · signal_observed (operations) · <mark>2027</mark>-02-26 17:30\n• “Payroll February 2027” [sim.PR-2027-02@1]: Payroll PR-<mark>2027</mark>-02 · status: paid · 199,824.99 USD · payroll_paid (finance) · <mark>2027</mark>-02-25 08:46\n• “Leadership review: January 2027” [sim.DEC-0007@1]: - caused by: life event SIG-0001 - caused by: life event LE-0077 - caused by: life event LE-0080 - caused by: life event LE-0081 - implements: decision DEC-0004\n• “PAYE and NIC for January 2027” [sim.TAX-PAYE-2027-01@2]: - caused by: process PR-2027-01 - governed by: rule RULE-PAYE\n• “Leadership review: February 2027” [sim.DEC-0008@1]: <mark>February</mark> <mark>2027</mark> showed 0 <mark>incidents</mark>. January <mark>2027</mark> had 3, and the three months before ran at 4.0 a month. There were 0 <mark>incidents</mark> after deploys, 0 during a GPU capacity shortage and 0 from outside causes.\n• “PAYE and NIC for February 2027” [sim.TAX-PAYE-2027-02@2]: - caused by: process PR-<mark>2027</mark>-02\n- governed by: rule RULE-PAYE";

test('Ask: an answer for people; the match, ids, times, the cut and the interpretation go to the disclosure (M5)', () => {
  const { text, technical } = peopleAnswer(RAW_WHY);
  assert.match(text, /^Subject: “Incidents fell in February 2027”\.\n/);
  assert.match(text, /“Incidents fell in February 2027” —caused by→ “Debt work shows an effect” —implements→ “Use 15% of engineering capacity for technical debt”/);
  for (const raw of [/@\d/, /\bsim\.[A-Z]/, /\d{4}-\d\d-\d\dT\d\d/, /\bUSD\b/, /\[\s*'/, /\b[a-z]+_[a-z0-9]+\b/, /Knowledge cut|Interpretation|Fragments found|UTC\+/]) assert.doesNotMatch(text, raw);
  assert.match(technical, /^Time: .*\nKnowledge cut: .*\nInterpretation: .*\nFragments found:/s);
  // the search fragments keep their citations for the disclosure; a date split by a highlight stays a date
  assert.match(technical, /\[sim\.SIG-0002@1\]/);
  assert.equal(peopleAnswer('Found 2 invoices:\n• “INV-1” [sim.INV-1@1]: 1,200.00 USD · <mark>2026</mark>-08-19 11:22').text, 'Found 2 invoices:\n• “INV-1” [sim.INV-1@1]: $1,200 · 19 Aug 2026, 11:22');
});

test('simulated invoices (provider sim) count by direction, client or vendor and issue date', async () => {
  const sim = [
    v('org.vn', 'Organization', 'Vistula Code Works', '2026-06-01T00:00:00Z', { org_id: 'VN-01', kind: 'vendor', domain: 'vistula.example' }),
    v('sim.INV-1', 'Invoice', 'Invoice INV-1: Alpha Foods, August 2026', '2026-09-01T03:00:00Z',
      { amount: 5400, client: 'org-a', direction: 'receivable', issued_at: '2026-09-01', number: 'INV-1' }),
    v('sim.BILL-1', 'Invoice', 'Vistula Code Works bill for August 2026', '2026-09-02T03:00:00Z',
      { amount: 1200, client: 'LW', vendor: 'VN-01', direction: 'payable', issued_at: '2026-09-02', number: 'BILL-1' }),
  ];
  class SimKb extends FakeKb { override async timeline() { return [...VERSIONS, ...sim]; } }
  const s = await buildSnapshot(new SimKb(), ORG, 42);
  const all = new Set([...VERSIONS, ...sim].map(x => x.entity_id));
  const get = (id: string) => evaluate(metricById(id)!, s, '2026-09-22', TZ, all, 50);
  assert.equal(get('money.invoiced').value, 150000 + 5400);
  assert.equal(get('money.payables').value, 1200);
});

test('records the viewer may not open are anonymised, or shown as metadata to management roles', async () => {
  const s = await buildSnapshot(new FakeKb(), ORG, 42);
  const none = new Set<string>();
  const hidden = evaluate(metricById('clients.awaiting')!, s, '2026-09-22', TZ, none, 10, false);
  assert.equal(hidden.value, 1);
  assert.deepEqual({ id: hidden.items[0].id, title: hidden.items[0].title, restricted: hidden.items[0].restricted }, { id: '', title: 'Restricted email', restricted: true });
  assert.equal(hidden.restrictedTotal, 1);
  const meta = evaluate(metricById('clients.awaiting')!, s, '2026-09-22', TZ, none, 10, true);
  assert.equal(meta.items[0].title, 'URGENT: refunds');
  assert.equal(meta.items[0].restricted, true);
  const bugs = evaluate(metricById('eng.bugs')!, s, '2026-09-22', TZ, none, 10, true);
  assert.equal(bugs.items[0].title, 'Restricted bug'); // internal material never shows as metadata
});

test('KB API: company, brief, metric, dossier, ACL and errors', async () => {
  const kb = new FakeKb();
  const server = createApp({ kb: { client: kb, organizations: [ORG] } }).listen(0, '127.0.0.1');
  await new Promise<void>(resolve => server.once('listening', resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const json = async (path: string, init?: RequestInit) => { const r = await fetch(origin + path, init); return { status: r.status, body: await r.json() }; };
  try {
    assert.deepEqual((await json('/api/health')).body, { status: 'ok', company: { id: 'company', name: 'Larchwood Systems', tenant: 'company' } });
    const orgs = await json('/api/kb/organizations');
    assert.deepEqual(orgs.body.map((o: { id: string; name: string }) => [o.id, o.name]), [['company', 'Larchwood Systems']]);
    // one role: without `role` the brief is the CEO brief with every metric, in a top row and three areas
    const brief = await json('/api/kb/company/brief');
    assert.equal(brief.status, 200);
    assert.equal(brief.body.asOf, '2026-09-16');
    assert.equal(brief.body.role.id, 'ceo');
    assert.deepEqual(brief.body.metrics.map((m: { id: string }) => m.id), briefMetricsFor('ceo').map(m => m.id));
    assert.ok(!brief.body.metrics.some((m: { id: string }) => m.id === 'deals.discounts'), 'a metric off the brief');
    assert.deepEqual(brief.body.top, BRIEF.top);
    assert.deepEqual(brief.body.areas.map((a: { id: string; title: string }) => a.title), ['Money', 'Clients and growth', 'Delivery and service']);
    assert.ok(brief.body.metrics.every((m: { area?: string; roles: string[] }) => m.area && m.roles.join() === 'ceo'));
    // one CEO headline: a sentence per area (the fixture has no weekly report, so no North Star line)
    const lines: string[] = brief.body.headline;
    assert.deepEqual(lines.map(h => h.split(':')[0]), ['Money', 'Clients', 'Delivery'], lines.join('\n'));
    // no proposal documents or deals in this base: open proposals are unknown, so their clause is left out
    assert.equal(lines[0], 'Money: invoiced $150,000 to clients and received $0 of supplier bills in 30 days.');
    const proposals = brief.body.metrics.find((m: { id: string }) => m.id === 'deals.proposals');
    assert.deepEqual([proposals.value, proposals.status], [null, 'unknown']);
    assert.match(proposals.caveats[0], /no Deal, CommercialProposal records by this date: the number is unknown, not zero/);
    assert.ok(lines[1].startsWith('Clients: 1 client letter waits for a reply'), lines[1]);
    // the team index is unknown here (no usual week yet), so its clause is left out
    assert.equal(lines[2], 'Delivery: 0 tickets done this week; 1 high-priority bug open; 0 items rest on grounds that have since changed.');
    // an old request with role=ceo is the same brief; any other role is refused
    assert.deepEqual((await json('/api/kb/company/brief?role=ceo')).body.headline, lines);
    assert.equal((await json('/api/kb/company/brief?role=finance')).status, 400);
    assert.equal((await json('/api/kb/company/brief?role=sales')).status, 400);
    const eng = await json('/api/kb/company/metrics/eng.wip');
    assert.equal(eng.body.value, 1);
    assert.equal(eng.body.area, 'delivery');
    assert.equal((await json('/api/kb/company/metrics/eng.wip?role=ceo')).body.value, 1);
    assert.equal((await json('/api/kb/company/metrics/eng.wip?role=delivery')).status, 400);
    assert.equal((await json('/api/kb/company/metrics/nope')).status, 404);
    assert.equal((await json('/api/kb/company/brief?asOf=2025-01-01')).status, 400);
    assert.equal((await json('/api/kb/nope/brief')).status, 404);
    const dossier = await json('/api/kb/company/entity/mail.m1');
    assert.equal(dossier.body.title, 'Invoice for September');
    assert.deepEqual(dossier.body.people, [{ role: 'Author', name: 'Paul Shaw' }]);
    kb.visible.delete('mail.m5');
    assert.equal((await json('/api/kb/company/entity/mail.m5')).status, 404);
    assert.equal((await json('/api/kb/company/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status, 400);
    assert.equal((await json('/api/kb/company/status')).body.reachable, true);
    assert.equal((await json('/api/nope')).status, 404);
    kb.down = true;
    const down = await json('/api/kb/company/entity/mail.m1?role=ceo');
    assert.equal(down.status, 502);
    assert.match(down.body.error, /unreachable/);
    assert.equal((await json('/api/kb/company/status')).body.reachable, false);
  } finally { server.close(); }
});

test('one view date for the workspace: brief, ask and dossier follow asOf together', async () => {
  const kb = new FakeKb();
  const server = createApp({ kb: { client: kb, organizations: [ORG, { ...ORG, id: 'company-cut', dataEnd: '2026-09-12' }] } }).listen(0, '127.0.0.1');
  await new Promise<void>(resolve => server.once('listening', resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const json = async (path: string, init?: RequestInit) => { const r = await fetch(origin + path, init); return { status: r.status, body: await r.json() }; };
  const ask = (body: Record<string, unknown>) => json('/api/kb/company/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  try {
    const dates = (await json('/api/kb/company/dates')).body;
    assert.deepEqual({ start: dates.start, end: dates.end, default: dates.default }, { start: '2026-06-01', end: '2026-09-16', default: '2026-09-16' });
    assert.ok(dates.max >= dates.end); // up to today: the real calendar may be chosen
    assert.equal((await json('/api/kb/company-cut/dates')).body.default, '2026-09-12'); // the configured dataEnd overrides the end

    // without a date every page uses the last day of data
    const at16 = new Date(endOfDay('2026-09-16', TZ)).toISOString();
    const a0 = await ask({ question: 'which meetings did the team hold internally last week?' });
    assert.equal(a0.body.asOf, '2026-09-16');
    // on the last day of data the knowledge cut stops at the last recorded version (the base refuses a later cut),
    // while «now» of the question stays the end of the chosen day
    assert.ok(a0.body.knownAt <= at16 && a0.body.now === at16, JSON.stringify(a0.body));
    assert.deepEqual(kb.calls.at(-1), { name: 'ask', at: a0.body.knownAt, now: at16 });
    const statusAt = await json('/api/kb/company/status?asOf=2026-09-14');
    assert.equal(statusAt.body.causalAsOf, '2026-09-14');
    assert.deepEqual(kb.calls.at(-1), { name: 'stats', at: new Date(endOfDay('2026-09-14', TZ)).toISOString() });

    // the same date reaches the brief, the question and the dossier
    const day = '2026-09-14', k = new Date(endOfDay(day, TZ)).toISOString();
    const brief = await json(`/api/kb/company/brief?role=ceo&asOf=${day}`);
    assert.equal(brief.body.asOf, day);
    kb.calls = [];
    const a = await ask({ question: 'which meetings did the team hold internally last week?', asOf: day });
    assert.equal(a.status, 200);
    assert.deepEqual({ asOf: a.body.asOf, now: a.body.now, knownAt: a.body.knownAt }, { asOf: day, now: k, knownAt: k });
    assert.deepEqual(kb.calls, [{ name: 'ask', at: k, now: k }]);
    assert.equal(a.body.plan.operation, 'list');
    assert.deepEqual(a.body.plan.relations, [{ from: 'Meeting', predicate: 'participant', to: 'staff only', negate: false }]);
    assert.equal(a.body.plan.time.interpretation, '«last week»: Sep 4 to Sep 10');
    assert.equal(a.body.total, 3);
    kb.calls = [];
    const d = await json(`/api/kb/company/entity/mail.m1?role=ceo&asOf=${day}`);
    assert.equal(d.status, 200);
    assert.equal(d.body.asOf, day);
    assert.ok(kb.calls.filter(c => c.name !== 'version').every(c => c.at === k || (c.name === 'history' && c.at === undefined)), JSON.stringify(kb.calls));
    assert.deepEqual(d.body.history.map((h: { seq: number }) => h.seq), [1]); // the version of Sep 15 was recorded later
    assert.equal(d.body.versionsLater, 1);
    assert.equal(d.body.excerpt, '# Invoice'); // the text known then
    // a day later the dossier moves on with the date
    const d2 = await json('/api/kb/company/entity/mail.m1?role=ceo&asOf=2026-09-15');
    assert.deepEqual(d2.body.history.map((h: { seq: number }) => h.seq), [2, 1]);
    assert.equal(d2.body.excerpt, '# Invoice, corrected');
    // before the first version: not known yet
    const d0 = await json('/api/kb/company/entity/mail.m1?role=ceo&asOf=2026-09-13');
    assert.equal(d0.status, 404);
    assert.match(d0.body.error, /Not known yet on 2026-09-13/);
    // record counts on the view date
    const st = await json(`/api/kb/company/status?asOf=${day}`);
    assert.equal(st.body.asOf, day);
    assert.equal(st.body.entitiesByKind.Email, 3); // m6 (Sep 1), m1 and m2 (Sep 14); m3–m5 came later
    assert.equal(st.body.causalAsOf, day); // link statuses come from the base as known on the view date
    assert.equal((await json('/api/kb/company/brief?asOf=2099-01-01')).status, 400);
  } finally { server.close(); }
});

test('CEO: the North Star follows the weekly metrics report; the team index compares each person with their usual week', async () => {
  const DAY = 86_400_000, t = Date.parse('2026-09-22T16:59:59.999Z');
  const report = (n: number, at: string, active: number) => v(`confluence.page-90${n}`, 'Report', `Company metrics: week ${n}`, at, { space: 'PROD', labels: ['report'], properties: { 'Period': `week ${n}`, 'Active clients': String(active), 'Paying clients': '338' } });
  const action = (id: string, at: number) => ({ ...v(`confluence.page-${id}`, 'Document', id, new Date(at).toISOString(), { space: 'PROD' }), actor_id: 'person.dev' });
  const extra = [report(1, '2026-09-07T04:30:00Z', 290), report(2, '2026-09-14T04:30:00Z', 293), report(3, '2026-09-21T04:30:00Z', 298)];
  for (let k = 1; k <= 4; k++) for (let i = 0; i < 10; i++) extra.push(action(`w${k}-${i}`, t - k * 7 * DAY - (i + 1) * 3_600_000));
  for (let i = 0; i < 3; i++) extra.push(action(`now-${i}`, t - (i + 1) * 3_600_000));
  class TeamKb extends FakeKb { override async timeline() { return [...VERSIONS, ...extra]; } }
  const s = await buildSnapshot(new TeamKb(), ORG, 43);
  const all = new Set([...VERSIONS, ...extra].map(x => x.entity_id));
  const ns = evaluate(metricById('company.northStar')!, s, '2026-09-22', TZ, all, 10);
  assert.equal(ns.value, 298);
  assert.equal(ns.previous, 293);
  assert.equal(ns.status, 'warn'); // +4 a week: more than half of the ~7 a week still needed for 400 by 31.12
  assert.match(ns.caveats[0], /Needs \+7\.\d clients a week/);
  assert.equal(ns.items[0].title, 'Company metrics: week 3');
  const team = evaluate(metricById('team.activity')!, s, '2026-09-22', TZ, all, 10);
  assert.equal(team.value, 22); // 3 actions against a usual 10 (30) minus one ticket idle for a week (8)
  assert.equal(team.baseline, 96); // previous weeks: 92 (the ticket was idle already) and 100
  assert.equal(team.items[0].title, 'Riley Grant');
  assert.match(String(team.items[0].facts.why), /activity 30% of usual \(3 vs 10 a week\); 1 ticket without movement 7\+ days/);
  assert.deepEqual(team.breakdown, [{ key: 'Engineering', label: 'Engineering', value: 22 }]);
});

test('a second company: every built-in metric of the CEO brief is computed from its data, in dollars', async () => {
  const s = await buildSnapshot(new FakeKbEn(), ORG_EN, 7);
  const all = new Set(VERSIONS_EN.map(x => x.entity_id));
  const get = (id: string) => evaluate(metricById(id, ORG_EN)!, s, '2026-09-22', TZ_EN, all, 50);
  const empty = metricsFor('ceo', ORG_EN).map(m => [m.id, get(m.id).value] as const).filter(([, value]) => !value);
  assert.deepEqual(empty, []);
  const esc = get('clients.escalations');
  assert.equal(esc.value, 3); // «URGENT: …», «Complaint about …» and a letter from the client's CEO
  assert.deepEqual(esc.items.map(i => i.facts.why), ['escalation wording', 'senior sender', 'escalation wording']);
  assert.equal(get('clients.awaiting').items[0].title, 'URGENT: refunds failing at checkout');
  const fr = get('clients.firstReply');
  assert.deepEqual([fr.value, fr.sample], [3.5, 4]); // in the thread, «Re:» and «FW:» letters with the same subject
  const invoiced = get('money.invoiced');
  assert.deepEqual([invoiced.value, invoiced.unit], [12500, 'money']);
  assert.match(invoiced.howCalculated.join(' '), /Sum of amounts \(\$\)/);
  assert.equal(get('money.payables').value, 3400);
  assert.equal(get('deals.proposals').value, 48000);
  assert.equal(get('deals.discounts').value, 2); // the amendment and «New pricing and a 10% discount»
  assert.deepEqual([get('eng.throughput').value, get('eng.cycle').value, get('eng.reopened').value, get('eng.bugs').value], [2, 11.5, 1, 1]);
  assert.match(get('eng.throughput').definition, /Engineering department/);
  const ns = get('company.northStar');
  assert.deepEqual([ns.title, ns.value, ns.previous, ns.target], ['North Star · weekly active customers', 298, 293, 400]);
  assert.match(ns.caveats[0], /^Needs \+\d+\.\d customers a week to reach 400 by Dec 31, 2026/);
  assert.equal(ns.caveats[1], 'Latest report: week 39 · paying 203.');
  assert.deepEqual(ns.items[0].facts, { 'active customers': 298, paying: 203, 'MRR, $k': 123 });
  assert.ok(!/Larchwood|active clients/i.test(JSON.stringify(metricsFor('ceo', ORG_EN).map(({ compute: _c, ...d }) => d))), 'no text of another company');
  assert.deepEqual(get('team.activity').breakdown.map(b => b.key), ['Engineering']);
});

test('a second company through the API: dollar headline, North Star from its configuration, a public company card', async () => {
  const server = createApp({ kb: { client: new FakeKbEn(), organizations: [ORG_EN] } }).listen(0, '127.0.0.1');
  await new Promise<void>(resolve => server.once('listening', resolve));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const json = async (path: string) => (await fetch(origin + path)).json();
  try {
    const ceo = await json('/api/kb/harbor/brief?asOf=2026-09-22');
    assert.equal(ceo.headline[0], 'North Star: 298 weekly active customers (goal 400 by Dec 31), +5 vs the week before.', ceo.headline.join('\n'));
    // the finance and delivery sentences of the old role briefs are now clauses of the one CEO headline
    assert.equal(ceo.headline[1], 'Money: invoiced $12,500 to clients and received $3,400 of supplier bills in 30 days; open proposals worth $48,000.');
    assert.match(ceo.headline[2], /^Clients: \d+ client letters? waits? for a reply.*; 3 escalations in 14 days; \d+ prospects? ha(s|ve) gone quiet for over a week\.$/);
    assert.match(ceo.headline[3], /^Delivery: team activity index .*; 2 tickets done this week; 1 high-priority bug open; .*\.$/);
    assert.equal(ceo.headline.length, 4);
    assert.deepEqual(Object.keys(ceo.organization), ['id', 'tenant', 'name', 'sector', 'headcount', 'timezone', 'ownDomain', 'roles']);
    const orgs = await json('/api/kb/organizations');
    assert.deepEqual([orgs[0].currency, orgs[0].northStar.property], ['USD', 'Active customers']);
  } finally { server.close(); }
});

test('the company loads from one object or from a list', () => {
  const dir = mkdtempSync(join(tmpdir(), 'workspace-kb-orgs-'));
  const one = { ...ORG_EN, id: 'redwood', tenant: 'redwood' };
  writeFileSync(join(dir, 'dashboard.json'), JSON.stringify(one));
  writeFileSync(join(dir, 'list.json'), JSON.stringify([ORG, one]));
  assert.deepEqual(loadKbOrganizations(join(dir, 'dashboard.json')).map(o => [o.id, o.currency, o.northStar?.title]), [['redwood', 'USD', 'Weekly active customers']]);
  assert.deepEqual(loadKbOrganizations(join(dir, 'list.json')).map(o => o.id), ['company', 'redwood']);
  assert.deepEqual(loadKbOrganizations(join(dir, 'missing.json')), []);
});

test('the configured company: Larchwood Systems on tenant company, one user role (the CEO) with a full-access principal', () => {
  const [company, ...rest] = loadKbOrganizations();
  assert.equal(rest.length, 0);
  assert.deepEqual([company.id, company.tenant, company.name, company.currency], ['company', 'company', 'Larchwood Systems', 'USD']);
  assert.deepEqual(company.roles.map(r => [r.id, r.title, r.name, r.personId]), [['ceo', 'Chief Executive Officer', 'Avery Collins', 'person.lw-e01']]);
  assert.equal(company.roles[0].principal, 'user:admin');
  assert.equal(metricsFor('ceo', company).length, METRICS.length, 'the CEO brief has every metric');
  assert.equal(metricsFor('ceo', company).find(m => m.id === 'company.northStar')?.title, 'North Star · annual recurring revenue');
  // one list of example questions, for the CEO, across money, clients and delivery
  const asks = company.askExamples?.ceo ?? [];
  assert.ok(asks.length >= 6 && asks.length <= 8, `${asks.length} questions`);
  assert.deepEqual(Object.keys(company.askExamples ?? {}), ['ceo']);
});
