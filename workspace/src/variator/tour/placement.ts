// Where the tour card goes, as a pure function (tested in tests/tour-template.test.ts): beside, under or over the
// highlighted region, never on it, unless there is no room anywhere (then the lower right corner).
export interface Box { top: number; left: number; width: number; height: number }
export type Side = 'below' | 'right' | 'left' | 'above' | 'corner' | 'sheet';
/** narrow: a card beside or under a small region; wide: a short card under or over a wide region (text | controls). */
export type Mode = 'narrow' | 'wide';

const GAP = 14, MARGIN = 16, NARROW = 340, WIDE_FROM = 640, WIDE_MAX = 980;

export interface Placement { top: number; left: number; width: number | null; side: Side; mode: Mode }

/** Where the card goes. A wide region gets the wide card under or over it, else the narrow card beside it; a small
 *  region gets the narrow card under, beside or over it; with no room anywhere, the lower right corner. `h`: the
 *  heights of the two layouts (the one on screen is measured, the other remembered). `prefer`: the side and layout of
 *  the frame before, kept while it still fits (no flip-flop while a region grows or the page scrolls). */
export function placeCard(r: Box | null, h: Record<Mode, number>, vw: number, vh: number, top0: number, reserveRight = 0, prefer?: { side: Side; mode: Mode }): Placement {
  const narrow = Math.min(NARROW, vw - 2 * MARGIN);
  const cx = (x: number, w: number) => Math.max(MARGIN, Math.min(vw - w - MARGIN, x)), cy = (y: number, hh: number) => Math.max(top0 + MARGIN, Math.min(vh - hh - MARGIN, y));
  const corner: Placement = { top: vh - h.narrow - MARGIN, left: vw - narrow - MARGIN, width: null, side: 'corner', mode: 'narrow' };
  if (!r) return corner;
  const bottom = r.top + r.height, right = r.left + r.width, onScreen = bottom > top0 && r.top < vh;
  const below = (hh: number) => bottom + GAP >= top0 && bottom + GAP + hh <= vh - MARGIN;
  const above = (hh: number) => r.top - GAP - hh >= top0 + MARGIN && r.top <= vh;
  // the wide card keeps clear of a column the page reserves on the right (its notices), when it can stay wide
  const free = vw - reserveRight - Math.max(MARGIN, r.left);
  const ww = Math.min(r.width, WIDE_MAX, vw - 2 * MARGIN, free >= WIDE_FROM ? free : Infinity);
  const tries: Record<string, () => Placement | null> = {
    'below:wide': () => below(h.wide) ? { top: bottom + GAP, left: cx(r.left, ww), width: ww, side: 'below', mode: 'wide' } : null,
    'above:wide': () => above(h.wide) ? { top: r.top - GAP - h.wide, left: cx(r.left, ww), width: ww, side: 'above', mode: 'wide' } : null,
    'below:narrow': () => below(h.narrow) ? { top: bottom + GAP, left: cx(r.left, narrow), width: null, side: 'below', mode: 'narrow' } : null,
    'above:narrow': () => above(h.narrow) ? { top: r.top - GAP - h.narrow, left: cx(r.left, narrow), width: null, side: 'above', mode: 'narrow' } : null,
    'right:narrow': () => right + GAP + narrow <= vw - MARGIN && onScreen ? { top: cy(r.top, h.narrow), left: right + GAP, width: null, side: 'right', mode: 'narrow' } : null,
    'left:narrow': () => r.left - GAP - narrow >= MARGIN && onScreen ? { top: cy(r.top, h.narrow), left: r.left - GAP - narrow, width: null, side: 'left', mode: 'narrow' } : null,
  };
  const order = r.width >= WIDE_FROM ? ['below:wide', 'above:wide', 'right:narrow', 'left:narrow'] : ['below:narrow', 'right:narrow', 'left:narrow', 'above:narrow'];
  const first = prefer && `${prefer.side}:${prefer.mode}`;
  for (const key of first && order.includes(first) ? [first, ...order] : order) { const p = tries[key](); if (p) return p; }
  return corner;
}
