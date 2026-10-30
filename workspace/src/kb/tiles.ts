// What a brief tile shows: its title, value text, series, weekly change, target and note. Pure functions (no React,
// no stylesheets), unit-tested in tests/kb-catalogue.test.ts.
import type { KbMetricResult } from './types';
import { formatValue } from './api';
import { formatPoints } from '../genui/primitives/format';

function delta(value: number | null, previous: number | null | undefined, unit: KbMetricResult['unit']): string | null {
  if (value === null || previous === null || previous === undefined) return null;
  const d = value - previous;
  // a change of a score, an index or a share is in points, by the rule of the metric sheet (genui formatPoints)
  const points = unit === 'score' || unit === 'index' || unit === 'percent';
  const text = points ? formatPoints(d) : `${d > 0 ? '+' : '−'}${formatValue(Math.abs(d), unit)}`;
  // a change that rounds to zero («0 pts», «$0») is no change, never «−0 pts»
  if (Math.abs(d) < 1e-9 || !/[1-9]/.test(text)) return 'no change vs last week';
  return `${text} vs last week`;
}

/** What a tile shows, after the metric's display hint (KbDisplay): a runway at its cap reads «60+ mo», a negative
 * burn reads as a positive net cash flow (number, series and change negated, no target), NPS gets its range as a note. */
export interface Tile { title: string; value: string; series: { date: string; value: number | null }[]; delta: string | null; target: string | null; note: string | null }
export function tileOf(m: KbMetricResult): Tile {
  const d = m.display ?? {};
  const k = d.negate ? -1 : 1;
  const flip = (x: number | null | undefined) => (x === null || x === undefined ? null : x * k);
  const value = flip(m.value);
  const shown = d.value ?? (d.negate && value !== null && value > 0 ? `+${formatValue(value, m.unit)}` : formatValue(value, m.unit));
  return {
    title: d.label ?? m.title, value: shown,
    series: d.negate ? m.series.map(p => ({ date: p.date, value: flip(p.value) })) : m.series,
    delta: delta(value, flip(m.previous), m.unit),
    target: !d.negate && m.target !== null && m.direction !== 'context' && statusAgrees(m) ? formatValue(m.target, m.unit) : null,
    note: d.note ?? null,
  };
}

/** The badge never contradicts its target: «On track · target X» only when the value meets X, «Watch» or «Needs
 * attention» only when it misses X. A status set another way (the pace of the North Star towards a dated goal) shows
 * without the target; the note names the goal. */
export function statusAgrees(m: Pick<KbMetricResult, 'value' | 'target' | 'direction' | 'status'>): boolean {
  if (m.value === null || m.target === null || m.direction === 'context' || !['good', 'warn', 'bad'].includes(m.status)) return true;
  const meets = m.direction === 'lower' ? m.value <= m.target : m.value >= m.target;
  return meets === (m.status === 'good');
}
