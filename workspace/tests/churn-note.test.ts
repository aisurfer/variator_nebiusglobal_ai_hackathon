// What churn counts on the case page against the brief (src/variator/case/model/churn.ts, QA m2): one line from WP18's
// churnBreakdown, the definitions and the clients who had already given notice behind «?».
import test from 'node:test';
import assert from 'node:assert/strict';
import { churnLine, churnMore, type ChurnBreakdown } from '../src/variator/case/model/churn.js';

const b: ChurnBreakdown = {
  alreadyNoticed: { arrShare: 0.0364, clients: [
    { id: 'CL-027', name: 'Quarry Lending', arr: 70800, endDate: '2026-11-01' }, { id: 'CL-059', name: 'Northgate Care Group', arr: 51600, endDate: '2027-01-01' },
    { id: 'CL-061', name: 'Sterling Travel', arr: 134400, endDate: '2026-12-01' }, { id: 'CL-089', name: 'Brightwell Council Services', arr: 80400, endDate: '2027-01-01' },
    { id: 'CL-098', name: 'Vale Journeys', arr: 99600, endDate: '2027-01-01' }] },
  criterion: { p50: 0.18154 }, terminations: { p50: 0.17157 },
};

test('one line: notices after T as the case counts them, contracts ending as the brief does, the clients already gone', () => {
  assert.equal(churnLine(b, '2026-10-26', 12, 'Keep the plan'),
    'Churn here counts notices given after 26 Oct (18% for Keep the plan). Counted like the brief (contracts ending): about 17% in year 1, including 3.6% from 5 clients who had already given notice.');
  assert.match(churnLine({ ...b, alreadyNoticed: undefined }, '2026-10-26', 24), /about 17% over 24 months\.$/);
});

test('behind «?»: plain definitions and the clients with the dates their contracts end', () => {
  const more = churnMore(b, '2026-10-26', 12);
  assert.equal(more.length, 3);
  assert.match(more[0], /^Churn here: the share of yearly revenue on 26 Oct whose clients give notice after 26 Oct, plus downgrades/);
  assert.match(more[2], /^Five clients had already given notice: Quarry Lending \(ends 1 Nov 2026\), .*Vale Journeys \(ends 1 Jan 2027\)\.$/);
});
