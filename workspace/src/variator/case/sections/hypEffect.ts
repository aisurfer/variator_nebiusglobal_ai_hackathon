// The effect of the last change of the world hypotheses on the timeline: the months where the median monthly
// curves of the leading fork moved most, between the run before the change and the run after it.
import { useEffect, useRef, useState } from 'react';
import type { CaseRun } from '../model/types';
import { useCase } from '../state';

export interface HypMonth { month: number; share: number; lines: string[] }

/** The run before the last change of the hypotheses (null until the hypotheses change on this page). */
function useRunBeforeHyp(): CaseRun | null {
  const c = useCase(), key = c.hypotheses.join(',');
  const last = useRef({ key, run: c.run });
  const [before, setBefore] = useState<CaseRun | null>(null);
  useEffect(() => {
    if (last.current.key !== key) setBefore(last.current.run);
    if (!c.stale) last.current = { key, run: c.run };
    else last.current = { ...last.current, key };
  }, [c.run, c.stale, key]);
  return before;
}

/** Up to 3 months with the largest change; a change under 2% of the curve's range does not count. */
export function useHypEffect(): HypMonth[] {
  const c = useCase(), before = useRunBeforeHyp();
  const after = c.run, fork = c.best?.leader;
  if (!before || !after || c.stale || !fork || before === after) return [];
  const months = new Map<number, HypMonth>();
  for (const def of after.preview.monthMetrics) {
    const a = after.preview.fans[fork]?.[def.id], b = before.preview.fans[fork]?.[def.id];
    if (!a || !b) continue;
    const range = Math.max(...b.map(p => p.p50), ...a.map(p => p.p50)) - Math.min(...b.map(p => p.p50), ...a.map(p => p.p50)) || 1;
    for (const p of a) {
      const q = b.find(x => x.month === p.month);
      if (!q) continue;
      const share = Math.abs(p.p50 - q.p50) / range;
      if (share < 0.02) continue;
      const m = months.get(p.month) ?? { month: p.month, share: 0, lines: [] };
      m.share = Math.max(m.share, share);
      m.lines.push(`${after.preview.metricLabels[def.id] ?? def.label}: ${p.p50 > q.p50 ? 'up' : 'down'} ${Math.round(share * 100)}% of its range`);
      months.set(p.month, m);
    }
  }
  return [...months.values()].sort((x, y) => y.share - x.share).slice(0, 3).sort((x, y) => x.month - y.month);
}
