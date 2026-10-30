// The spread of each fork's score over the scenarios (the week gate's «overlaps the leader»), on the same scale as the
// ranking: per scenario, the forks within the meaningful difference of a criterion tie (snapTies), and each value sits
// on the criterion's fixed anchors (0 at lo, 1 at lo + span), so a fork's score does not depend on the other forks.
// And the rivals of the leader on the actor result: from the engine's exact pairwise «total» rows. Pure (unit-tested).
import { snapTies, type SimRun } from '../../engine';
import { anchored } from '../../engine/rank';
import type { ActorMonth } from './types';

const quant = (srt: ArrayLike<number>, p: number) => { const i = (srt.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i); return srt[lo] + (srt[hi] - srt[lo]) * (i - lo); };

/** Per option of the run: the 10th, 50th and 90th percentile of its weighted score over the scenarios. `w`: the
 *  ranking's weights per criterion (normalised). Without anchors (an older run) the scale is 0..1 over the forks. */
export function formulaScoreSpread(R: Pick<SimRun, 'n' | 'options' | 'criteria' | 'X' | 'higher'> & { ties?: number[]; anchors?: { lo: number; span: number }[] }, w: ArrayLike<number>): [number, number, number][] {
  const O = R.options.length, K = R.criteria.length, n = R.n;
  const per = R.options.map(() => new Float64Array(n)), col = new Array<number>(O);
  for (let s = 0; s < n; s++) {
    for (let k = 0; k < K; k++) {
      for (let j = 0; j < O; j++) col[j] = R.X[(s * O + j) * K + k];
      const sk = snapTies(col, R.higher[k], R.ties?.[k] ?? 0);
      if (R.anchors?.[k]) for (let j = 0; j < O; j++) per[j][s] += anchored(R as SimRun, R.higher[k], sk[j], k) * w[k];
      else {
        let lo = Infinity, hi = -Infinity;
        for (let j = 0; j < O; j++) { lo = Math.min(lo, sk[j]); hi = Math.max(hi, sk[j]); }
        for (let j = 0; j < O; j++) { let x = hi > lo ? (sk[j] - lo) / (hi - lo) : 1; if (!R.higher[k] && hi > lo) x = 1 - x; per[j][s] += x * w[k]; }
      }
    }
  }
  return per.map(a => { const srt = Float64Array.from(a).sort(); return [quant(srt, 0.1), quant(srt, 0.5), quant(srt, 0.9)]; });
}

/** The rivals of the actor leader the week review should keep: the forks equal to it on every criterion (the engine's
 *  ties), and the forks whose weighted score beats the leader's in at least 1 scenario in 10 (`ranking.pairwise`,
 *  the «total» rows: the leader against each rival, ties respected, a failed check a loss). */
export function actorRivals(M: ActorMonth, leader: string, forks: string[], tied: string | null = null, share = 0.1): Set<string> {
  const rows = ((M.ranking as { pairwise?: { a: string; b: string; crit: string; bWins: number }[] } | undefined)?.pairwise ?? []).filter(x => x.crit === 'total' && x.a === leader);
  const out = new Set<string>();
  if (tied && forks.includes(tied)) out.add(tied);
  if (rows.length) for (const x of rows) { if (forks.includes(x.b) && x.b !== leader && x.bWins >= share) out.add(x.b); }
  else for (const o of M.ranking?.options ?? []) if (forks.includes(o.option) && o.option !== leader && o.pBest >= share) out.add(o.option);
  return out;
}
