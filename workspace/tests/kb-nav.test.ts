// The navigation links: only the view date and the branch go with them (WP25 polish: «/actors?metric=…&entity=…»).
import test from 'node:test';
import assert from 'node:assert/strict';
import { navHref } from '../src/kb/nav.js';

test('a navigation link keeps the view date and the branch, never the parameters of the page it leaves', () => {
  assert.equal(navHref('actors', '?asOf=2026-10-19&metric=fin.cash&entity=sim.INV-1&role=finance', false), '/actors?asOf=2026-10-19');
  assert.equal(navHref('brief', '?actor=CL-001&tab=risks', true), '/brief');
  assert.equal(navHref('ask', '?branch=company-b-status_quo-p50&asOf=2027-04-19&entity=person.lw-e13&q=Why', true), '/ask?branch=company-b-status_quo-p50&asOf=2027-04-19');
  // a page outside the simulated future: the branch and its dates stay behind
  assert.equal(navHref('cases', '?branch=company-b-status_quo-p50&asOf=2027-04-19', false), '/cases');
  assert.equal(navHref('brief', '', true), '/brief');
});
