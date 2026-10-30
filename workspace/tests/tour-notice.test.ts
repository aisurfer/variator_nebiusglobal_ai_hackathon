// A change notice compares the same fork before and after (src/variator/case/model/notice.ts): when a change makes
// another fork the leader, the notice still reads the fork it started with (QA M4: «For Keep the plan: median cash
// $2.92M → $2.19M» mixed Raise prices' $2.92M with Keep the plan's $2.19M).
import test from 'node:test';
import assert from 'node:assert/strict';
import { noticeFork } from '../src/variator/case/model/notice.js';

test('the leader changes with the edit: the notice keeps the fork of its «before»', () => {
  assert.deepEqual(noticeFork('raise_prices_8', ['status_quo', 'raise_prices_8', 'hire_5'], 'status_quo'), { fork: 'raise_prices_8', comparable: true });
});

test('the fork of the «before» is no longer compared: no per-fork effect, the new leader is named', () => {
  assert.deepEqual(noticeFork('hire_5', ['status_quo', 'raise_prices_8'], 'status_quo'), { fork: 'status_quo', comparable: false });
  assert.deepEqual(noticeFork(null, ['status_quo'], 'status_quo'), { fork: 'status_quo', comparable: false });
});

test('notices never cover an open side sheet: beside it with room, else held until it closes (QA m4)', async () => {
  const { noticePlace } = await import('../src/variator/case/ui/Toasts.js');
  assert.equal(noticePlace(false, true), 'corner');
  assert.equal(noticePlace(false, false), 'corner');
  assert.equal(noticePlace(true, true), 'aside');
  assert.equal(noticePlace(true, false), 'held');
});

test('notices never cover a primary control: the other edge, else compact, else held (WP25 R2)', async () => {
  const { cornerEdge } = await import('../src/variator/case/ui/Toasts.js');
  const stack = { width: 340, height: 220, compactHeight: 58, right: 1420 }, view = { height: 1000 }, inset = { top: 76, bottom: 20 };
  const box = (top: number, h = 36) => ({ top, bottom: top + h, left: 1200, right: 1350 });
  assert.deepEqual(cornerEdge('bottom', stack, view, inset, []), { edge: 'bottom', compact: false });
  assert.deepEqual(cornerEdge('bottom', stack, view, inset, [box(900)]), { edge: 'top', compact: false }, '«Rerun on actors» near the bottom: the notices go up');
  assert.deepEqual(cornerEdge('bottom', stack, view, inset, [box(100)]), { edge: 'bottom', compact: false }, 'near the top: they stay down');
  assert.deepEqual(cornerEdge('bottom', stack, view, inset, [box(200), box(800)]), { edge: 'bottom', compact: true }, 'both edges taken in full: compact');
  assert.equal(cornerEdge('bottom', stack, view, inset, [box(80), box(940)]), 'held', 'no room even compact: held');
  assert.deepEqual(cornerEdge('top', stack, view, inset, [box(100)]), { edge: 'bottom', compact: false }, 'on a phone: down when the control is at the top');
});

test('notices keep to the top right: full, else compact, and down only when even the heads would cover «Rerun on actors» (WP25 R2)', async () => {
  const { cornerEdge } = await import('../src/variator/case/ui/Toasts.js');
  const stack = { width: 510, height: 260, compactHeight: 58, right: 1420 }, view = { height: 1000 }, inset = { top: 76, bottom: 20 };
  const box = (top: number, h = 36) => ({ top, bottom: top + h, left: 1000, right: 1350 });
  assert.deepEqual(cornerEdge('top', stack, view, inset, [], true), { edge: 'top', compact: false });
  assert.deepEqual(cornerEdge('top', stack, view, inset, [box(930)], true), { edge: 'top', compact: false }, 'the control near the bottom: the notices stay up in full');
  assert.deepEqual(cornerEdge('top', stack, view, inset, [box(200)], true), { edge: 'top', compact: true }, 'the control under the full stack: up, its heads only');
  assert.deepEqual(cornerEdge('top', stack, view, inset, [box(90)], true), { edge: 'bottom', compact: false }, 'the control under the heads too: down');
  assert.equal(cornerEdge('top', stack, view, inset, [box(90), box(900)], true), 'held', 'both edges taken even compact: held');
});
