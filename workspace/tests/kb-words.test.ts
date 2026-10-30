// People's words: the raw texts QA found on the pages (WP25 m1, m6, M5) read as a person would write them.
import test from 'node:test';
import assert from 'node:assert/strict';
import { peopleWords, sourceLabel, splitAnswer } from '../src/kb/words.js';

test('amounts in dollars, dates a person reads', () => {
  assert.equal(peopleWords('Bank entry BNK-00918, 255,600.00 USD — Meridian Freight Group'), 'Bank entry BNK-00918, $255,600 — Meridian Freight Group');
  assert.equal(peopleWords('ARR now stands at USD 12,001,200 and cash at USD 2,000,000'), 'ARR now stands at $12.0M and cash at $2.00M');
  assert.equal(peopleWords('Reminded at: 2026-08-07T11:32:17+01:00'), 'Reminded at: 7 Aug 2026, 11:32');
  assert.equal(peopleWords('effective 2026-10-27 10:08 (UTC+00), recorded 2026-10-27 10:09 (UTC+00)'), 'effective 27 Oct 2026, 10:08, recorded 27 Oct 2026, 10:09');
  assert.equal(peopleWords('read as February 2027 [2027-02-01, 2027-03-01)'), 'read as February 2027 [1 Feb 2027, 1 Mar 2027)');
});

test('no machine ids, version suffixes, Python lists or snake_case', () => {
  assert.equal(peopleWords('Subject: “Incidents fell in February 2027” (LifeEvent, sim.SIG-0002; matched: [\'title\', "alias \'x\'", \'semantic\']).'),
    'Subject: “Incidents fell in February 2027”.');
  assert.equal(peopleWords('Invoice INV-01090 · status: paid · 255,600.00 USD · invoice_paid (finance) · 2026-08-19 11:22'),
    'Invoice INV-01090 · status: paid · $255,600 · invoice paid (finance) · 19 Aug 2026, 11:22');
  assert.equal(peopleWords('- caused by: process CTR-0001'), '- caused by: CTR-0001');
  assert.equal(peopleWords('Other objects may be meant: “Hire a DevOps engineer” (confluence.page-2000016.dec-1); “Approved” (mail.msg-000847).'),
    'Other objects may be meant: “Hire a DevOps engineer”; “Approved”.');
  // a citation stays for the numbered links of the answer
  assert.equal(peopleWords('“Incidents fell” [sim.SIG-0002@1]: life event'), '“Incidents fell” [sim.SIG-0002@1]: life event');
  const raw = peopleWords('owner: LW-E01; decided_at: 2026-10-27T10:08:22+00:00 (sim.DEC-0004@1) via sim.LE-0087');
  assert.ok(!/\b[a-z]+_[a-z]+\b|\bsim\.|\d{4}-\d\d-\d\dT|@\d/.test(raw), raw);
  assert.equal(peopleWords(null), '');
  // the invoice dossier QA found (m6): its source text in people's words
  const excerpt = peopleWords('Invoice INV-01090 · status: paid · 255,600.00 USD · invoice_paid (finance) · 2026-08-19 11:22\n- Currency: USD\n- Reminded at: 2026-08-07T11:32:17+01:00\n- caused by: process CTR-0001');
  assert.equal(excerpt, 'Invoice INV-01090 · status: paid · $255,600 · invoice paid (finance) · 19 Aug 2026, 11:22\n- Currency: US dollars\n- Reminded at: 7 Aug 2026, 11:32\n- caused by: CTR-0001');
});

test('sources for people and an answer split into what a person reads and the technical part', () => {
  assert.deepEqual([sourceLabel('sim'), sourceLabel('sim', true), sourceLabel('directory-company'), sourceLabel('gitlab-company'), sourceLabel('mail-company')],
    ['Company records', 'Simulation', 'Directory', 'GitLab', 'Mail']);
  const { main, technical } = splitAnswer(['Subject: “X” (LifeEvent, sim.SIG-1).', 'Time: “in February” read as February 2027.', 'Knowledge cut: as known at 2027-04-26 14:37 (UTC+01).',
    'Chains from the effect to the root cause (declared links):', '• “X” —implements→ “Y”', 'Interpretation: “why” means …', 'Fragments found:', '• “X” [sim.SIG-1@1]: …'].join('\n'));
  assert.equal(main, 'Subject: “X” (LifeEvent, sim.SIG-1).\nChains from the effect to the root cause (declared links):\n• “X” —implements→ “Y”');
  assert.match(technical, /^Time:.*\nKnowledge cut:.*\nInterpretation:.*\nFragments found:\n• “X”/s);
});
