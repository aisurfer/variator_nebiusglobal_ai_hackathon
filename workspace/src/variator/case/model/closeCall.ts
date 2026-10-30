// How firm is the decision? Two honest cards, built from the result, never from hand-written numbers:
// - «Equal choices»: the decision and the runner-up are equal on every criterion within its meaningful difference
//   (the engine's ties); the decision wins the tie (the baseline changes nothing, or it passes the checks more often).
// - «Close call»: the fork that wins most scenarios is not the decision nor tied with it, and it wins more scenarios than
//   the decision and the forks tied with it together (P(best) splits between tied forks, so ties alone never make one).
//   It comes before «Equal choices»: a decision tied with its runner-up while a third fork wins most scenarios is a close
//   call with that fork.
// The logic reads a small input, so it works on the formula run (`formulaInput`) and on an actor ranking alike.
import { snapTies, type Ranking as EngineRanking, type SimRun } from '../../engine';
import { anchored } from '../../engine/rank';
import type { CaseConfig, CaseRun, Criterion, PriorityPreset, Ranking } from './types';

/** Two forks a and b scenario by scenario, per criterion: how often b beats a by more than the meaningful difference,
 *  the mean difference a − b, the median margin of b's wins (in the criterion's unit), and `scoreDiff`: the mean
 *  difference a − b of the criterion's part of the score (each scenario after the ties, on the criterion's fixed
 *  anchors as the ranking does), before the weight. */
export interface Pairwise { crit: string; bWins: number; meanDiff: number; medianMargin: number; scoreDiff: number }

/** What the cards need from a ranking, whatever computed it. */
export interface CloseCallInput {
  /** The forks by the decision rule, the decision first. */
  order: string[];
  pBest: Record<string, number>;
  /** Weights of the criteria (shares of the decision). */
  weights: Record<string, number>;
  /** The typical spread of a criterion for a fork (its middle half: p75 − p25). */
  spread: (fork: string, crit: string) => number;
  pairwise: (a: string, b: string) => Pairwise[];
  /** Criteria on which a and b are equal within the meaningful difference (none when the source has no ties). */
  equal?: (a: string, b: string) => string[];
  /** The checks pass share per fork (for «it passes the checks more often»). */
  feasible?: Record<string, number>;
  /** The ranking key per fork (score × P(feasible)), when the source gives it: the gap of the close call. */
  key?: Record<string, number>;
}

export interface CloseCall {
  decision: string;
  /** The forks equal to the decision on every criterion (its tie group without it). */
  tied: string[];
  /** The fork that wins most scenarios outside the decision's tie group, and its P(best). */
  rival: string; pRival: number;
  /** The number of forks the P(best) is computed over (the forks of the ranking). */
  pBestOf: number;
  /** The criteria that give the decision its higher score (largest part first, at most two: the second one when its part
   *  is large or the decision wins it in most scenarios), with their mean advantage (decision − rival) in their own unit. */
  strong: { crit: Criterion; diff: number }[];
  /** The criteria the rival wins in most scenarios, most often first, with the mean difference (decision − rival);
   *  `small`: its typical win is small. */
  rivalWins: { crit: Criterion; share: number; diff: number }[];
  small: boolean;
  /** The decision's score minus the rival's (0..1 scale); `even`: within EVEN_SCORE, «about the same on average». */
  gap: number; even: boolean;
  /** The presets that settle it: the one for the decision's side, the one for the rival's. */
  presets: PriorityPreset[];
}

export interface EqualChoices {
  decision: string; runnerUp: string;
  /** Every criterion (they are equal on all of them). */
  on: Criterion[];
  /** Why the decision wins the tie. */
  why: 'baseline' | 'feasible' | 'score';
  feasible?: [number, number];
}

/** Two scores closer than this (on the 0..1 scale of the score) are «about the same on average». */
export const EVEN_SCORE = 0.01;

const share = (p: PriorityPreset, ids: string[]) => { const tot = Object.values(p.weights).reduce((a, b) => a + b, 0) || 1; return ids.reduce((a, id) => a + (p.weights[id] ?? 0), 0) / tot; };
const tieGroup = (input: CloseCallInput, cfg: CaseConfig, f: string) => {
  const K = cfg.criteria.length;
  return input.order.filter(g => g === f || (input.equal?.(f, g).length ?? 0) >= K);
};

/** The fork outside the decision's tie group that wins more scenarios than the whole group (a close call), if any. */
function rivalOf(cfg: CaseConfig, input: CloseCallInput) {
  const { order, pBest } = input, group = tieGroup(input, cfg, order[0]), pGroup = group.reduce((a, f) => a + (pBest[f] ?? 0), 0);
  const rival = order.filter(f => !group.includes(f)).sort((a, b) => (pBest[b] ?? 0) - (pBest[a] ?? 0))[0];
  return { group, rival: rival && (pBest[rival] ?? 0) > pGroup ? rival : null };
}

/** The decision and the runner-up equal on every criterion: an «Equal choices» card instead of a close call, unless a
 *  third fork wins more scenarios than the two together (then that close call is the honest message). */
export function equalChoicesOf(cfg: CaseConfig, input: CloseCallInput | null): EqualChoices | null {
  if (!input?.equal || input.order.length < 2) return null;
  const [d, r] = input.order, eq = input.equal(d, r);
  if (eq.length < cfg.criteria.length) return null;
  if (rivalOf(cfg, input).rival && closeCallOf(cfg, input)) return null;
  const base = cfg.forks.find(f => f.baseline)?.id, fd = input.feasible?.[d], fr = input.feasible?.[r];
  const why: EqualChoices['why'] = d === base ? 'baseline' : fd != null && fr != null && fd - fr >= 0.01 ? 'feasible' : 'score';
  return { decision: d, runnerUp: r, on: cfg.criteria, why, feasible: fd != null && fr != null ? [fd, fr] : undefined };
}

export function closeCallOf(cfg: CaseConfig, input: CloseCallInput | null): CloseCall | null {
  if (!input || input.order.length < 2) return null;
  const { order, pBest } = input, decision = order[0];
  // P(best) splits between forks that are equal on every criterion: the decision's group counts as one
  const { group, rival } = rivalOf(cfg, input);
  if (!rival) return null;
  const pw = input.pairwise(decision, rival), back = input.pairwise(rival, decision);
  const crit = (id: string) => cfg.criteria.find(c => c.id === id);
  const iqr = (id: string) => Math.max(1e-9, input.spread(decision, id));
  // the decision's higher score, criterion by criterion: the weighted part of the score it gains over the rival
  const part = (x: Pairwise) => (input.weights[x.crit] ?? 0) * x.scoreDiff;
  const gains = pw.filter(x => crit(x.crit) && part(x) > 0 && (crit(x.crit)!.higher ? x.meanDiff > 0 : x.meanDiff < 0)).sort((a, b) => part(b) - part(a));
  if (!gains.length) return null;
  // the second criterion: a large part of the score, or one the decision wins in most scenarios
  const decisionWins = (id: string) => back.find(y => y.crit === id)?.bWins ?? 0;
  const top = part(gains[0]), better = [gains[0], gains.slice(1).find(x => part(x) >= 0.4 * top || decisionWins(x.crit) >= 0.5)].filter((x): x is Pairwise => !!x);
  const wins = pw.filter(x => crit(x.crit) && x.bWins >= 0.5 && !better.some(b => b.crit === x.crit)).sort((a, b) => b.bWins - a.bWins).slice(0, 3);
  const gap = input.key && input.key[decision] != null && input.key[rival] != null ? input.key[decision] - input.key[rival] : pw.reduce((a, x) => a + part(x), 0);
  const small = wins.length > 0 && wins.every(x => x.medianMargin < 0.5 * iqr(x.crit));
  const pick = (ids: string[]) => ids.length ? [...cfg.priorityPresets].sort((a, b) => share(b, ids) - share(a, ids))[0] : undefined;
  const presets = [pick(better.map(x => x.crit)), pick(wins.map(x => x.crit))].filter((p, i, xs): p is PriorityPreset => !!p && xs.findIndex(q => q?.id === p.id) === i);
  return {
    decision, tied: group.filter(f => f !== decision), rival, pRival: pBest[rival] ?? 0, pBestOf: order.length,
    strong: better.map(x => ({ crit: crit(x.crit)!, diff: x.meanDiff })), rivalWins: wins.map(x => ({ crit: crit(x.crit)!, share: x.bWins, diff: x.meanDiff })), small, presets,
    gap, even: Math.abs(gap) < EVEN_SCORE,
  };
}

// ---------------------------------------------------------------- the formula model's input
interface RawRun { run: SimRun }
const rawOf = (run: CaseRun) => (run.raw as RawRun | undefined)?.run;
const engineOf = (rk: Ranking) => (rk.raw as { rk?: EngineRanking } | undefined)?.rk;

/** Two forks of the formula run scenario by scenario, with the engine's ties (snapTies with the meaningful differences). */
export function formulaPairwise(run: CaseRun, a: string, b: string): Pairwise[] {
  const R = rawOf(run);
  if (!R) return [];
  const O = R.options.length, K = R.criteria.length, ja = R.options.indexOf(a), jb = R.options.indexOf(b);
  if (ja < 0 || jb < 0) return [];
  const col = new Array<number>(O);
  return R.criteria.map((crit, k) => {
    let wins = 0, sum = 0, part = 0;
    const margins: number[] = [];
    for (let s = 0; s < R.n; s++) {
      for (let j = 0; j < O; j++) col[j] = R.X[(s * O + j) * K + k];
      const sk = snapTies(col, R.higher[k], R.ties?.[k] ?? 0);
      const xa = sk[ja], xb = sk[jb];
      // the score's own scale: the fixed anchors of the criterion (the ranking's), else 0..1 over the forks
      if (R.anchors?.[k]) part += anchored(R, R.higher[k], xa, k) - anchored(R, R.higher[k], xb, k);
      else {
        let lo = Infinity, hi = -Infinity;
        for (let j = 0; j < O; j++) { lo = Math.min(lo, sk[j]); hi = Math.max(hi, sk[j]); }
        if (hi > lo) part += (R.higher[k] ? 1 : -1) * (xa - xb) / (hi - lo);
      }
      sum += col[ja] - col[jb];
      if (R.higher[k] ? xb > xa : xb < xa) { wins++; margins.push(Math.abs(col[jb] - col[ja])); }
    }
    margins.sort((x, y) => x - y);
    return { crit, bWins: wins / R.n, meanDiff: sum / R.n, medianMargin: margins.length ? margins[Math.floor(margins.length / 2)] : 0, scoreDiff: part / R.n };
  });
}

/** The input from the formula run of the page and its ranking (the engine's means Mx and meaningful differences tell
 *  what is equal, as its trade-off does; the pairwise parts use its fixed anchors). */
export function formulaInput(run: CaseRun | null, rk: Ranking | null): CloseCallInput | null {
  if (!run || !rk) return null;
  const R = rawOf(run), E = engineOf(rk);
  const idx = (f: string) => R?.options.indexOf(f) ?? -1;
  return {
    order: rk.order, pBest: rk.pBest, weights: rk.weights, feasible: run.feasible, key: rk.key,
    spread: (f, k) => (run.stats[f]?.[k]?.p75 ?? 0) - (run.stats[f]?.[k]?.p25 ?? 0),
    pairwise: (a, b) => formulaPairwise(run, a, b),
    // the engine's own tie (its trade-off's `equal`): the means differ by less than the meaningful difference
    equal: R && E?.Mx ? (a, b) => {
      const ja = idx(a), jb = idx(b);
      return ja < 0 || jb < 0 ? [] : R.criteria.filter((_c, k) => { const d = Math.abs(E.Mx[ja][k] - E.Mx[jb][k]); return d === 0 || d < (R.ties?.[k] ?? 0); });
    } : undefined,
  };
}

/** The meaningful difference per criterion id, from the formula run (the model's md_* variables, as resolved there). */
export function tiesOf(run: CaseRun | null): Record<string, number> {
  const R = run && rawOf(run);
  return R ? Object.fromEntries(R.criteria.map((c, k) => [c, R.ties?.[k] ?? 0])) : {};
}

export interface Versus { fork: string; better: { crit: Criterion; diff: number }[]; worse: { crit: Criterion; diff: number }[]; noise: Criterion[] }

/** Per fork against the leader (order[0]), from mean values and the meaningful differences: where it is meaningfully
 *  better or worse (after the ties, as the engine snaps them), and where it is better only within the noise. Works on
 *  the formula run and on the actor result alike. */
export function versusByMeans(cfg: CaseConfig, order: string[], mean: (fork: string, crit: string) => number | undefined, ties: Record<string, number>): Versus[] {
  const d = order[0];
  const snapped = Object.fromEntries(cfg.criteria.map(c => {
    const vals = order.map(f => mean(f, c.id) ?? NaN);
    return [c.id, vals.some(v => !Number.isFinite(v)) ? vals : snapTies(vals, c.higher, ties[c.id] ?? 0)];
  }));
  return order.slice(1).map((f, i) => {
    const better: Versus['better'] = [], worse: Versus['worse'] = [], noise: Criterion[] = [];
    for (const c of cfg.criteria) {
      const s = snapped[c.id][i + 1] - snapped[c.id][0], m = (mean(f, c.id) ?? NaN) - (mean(d, c.id) ?? NaN);
      if (!Number.isFinite(s) || !Number.isFinite(m)) continue;
      if (s !== 0) (c.higher === s > 0 ? better : worse).push({ crit: c, diff: m });
      else if (m !== 0 && c.higher === m > 0) noise.push(c);
    }
    return { fork: f, better, worse, noise };
  });
}

/** The same for the formula run of the page (its own ranking order and means). */
export function versusDecision(cfg: CaseConfig, run: CaseRun | null, rk: Ranking | null): Versus[] {
  if (!run || !rk) return [];
  return versusByMeans(cfg, rk.order, (f, k) => run.stats[f]?.[k]?.mean, tiesOf(run));
}
