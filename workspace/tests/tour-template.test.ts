// The generic tour: its text templates and where its card goes (beside, under or over the region, never on it).
import test from 'node:test';
import assert from 'node:assert/strict';
import { fillTemplate, literalText, placeholders } from '../src/variator/tour/template.js';
import { placeCard } from '../src/variator/tour/placement.js';

test('placeholders are found and filled from the live state; unknown values read «…»', () => {
  const t = '{leader} is best in {pBest} of the scenarios; {stat.cash.p50} in the median, {missing}.';
  assert.deepEqual(placeholders(t), ['leader', 'pBest', 'stat.cash.p50', 'missing']);
  const v: Record<string, string> = { leader: 'Reduce tech debt', pBest: '82%', 'stat.cash.p50': '$2.45M' };
  assert.equal(fillTemplate(t, k => v[k]), 'Reduce tech debt is best in 82% of the scenarios; $2.45M in the median, ….');
  assert.equal(literalText('From {from} to {to}.'), 'From   to  .');
  assert.doesNotMatch(literalText('Best in {pBest}, risk {risk.1.head}'), /\d/);
});

const overlap = (a: { top: number; left: number; width: number; height: number }, b: { top: number; left: number; width: number; height: number }) =>
  a.left < b.left + b.width && b.left < a.left + a.width && a.top < b.top + b.height && b.top < a.top + a.height;
const H = { narrow: 400, wide: 200 };

test('a small region gets the narrow card under it, else beside it; the card never covers the region', () => {
  const r = { top: 90, left: 1130, width: 140, height: 44 };
  const p = placeCard(r, H, 1440, 1000, 72);
  assert.equal(p.side, 'below'); assert.equal(p.mode, 'narrow');
  assert.ok(!overlap(r, { top: p.top, left: p.left, width: 340, height: H.narrow }));
  const tall = { top: 90, left: 380, width: 600, height: 700 };
  const q = placeCard(tall, H, 1440, 1000, 72);
  assert.equal(q.side, 'right');
  assert.ok(!overlap(tall, { top: q.top, left: q.left, width: 340, height: H.narrow }));
});

test('a wide region gets the wide card under it, clear of the reserved column on the right', () => {
  const r = { top: 84, left: 294, width: 1090, height: 330 };
  const p = placeCard(r, H, 1440, 1000, 72, 420);
  assert.equal(p.mode, 'wide'); assert.equal(p.side, 'below');
  assert.ok(p.width! <= 1440 - 420 - 294, 'clear of the notices');
  assert.ok(!overlap(r, { top: p.top, left: p.left, width: p.width!, height: H.wide }));
});

test('no room anywhere: the lower right corner', () => {
  const r = { top: 84, left: 20, width: 1400, height: 900 };
  const p = placeCard(r, H, 1440, 1000, 72);
  assert.equal(p.side, 'corner');
});
