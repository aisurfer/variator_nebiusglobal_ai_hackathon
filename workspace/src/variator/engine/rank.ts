// Ranking and RAG over a run (the page's "rank"): instant, no new simulation.
// Score = sum of weight x criterion scaled 0..1 across the options; order key = score x feasible share.
import { criterionLabel, fmt, optionLabel, pct } from './format.js';
import type { ExportedModel, Prefs, RagCounts, Ranking, SimRun, Tradeoff } from './types.js';

export const DEFAULT_RISK_AVERSION = 0.25;

/** Partial preferences: missing weights come from the run's model (after the presets), missing risk aversion = 0.25. */
export interface PrefsInput { weights?: Record<string, number>; riskAversion?: number | null }

export function resolvePrefs(M: ExportedModel, prefs?: PrefsInput | null): Prefs {
  const weights: Record<string, number> = {};
  for (const c of M.criteria) weights[c.name] = prefs?.weights?.[c.name] ?? c.weight;
  return { weights, riskAversion: prefs?.riskAversion ?? DEFAULT_RISK_AVERSION };
}

/** Weights in criterion order, normalised to sum 1 (sum 0 -> divide by 1). */
export function normalisedWeights(M: ExportedModel, prefs: Prefs): number[] {
  const w = M.criteria.map(c => prefs.weights[c.name]);
  const t = w.reduce((a, b) => a + b, 0) || 1;
  return w.map(x => x / t);
}

export function rankRun(R: SimRun, prefsIn?: PrefsInput | null): Ranking {
  const M = R.model, prefs = resolvePrefs(M, prefsIn);
  const O = R.options.length, K = M.criteria.length, w = normalisedWeights(M, prefs), a = prefs.riskAversion;
  const Mx = R.options.map((_o, j) => M.criteria.map((_c, k) => (1 - a) * R.stats[j][k].mean + a * R.stats[j][k].cvar));
  // fixed anchors: the score of a fork depends on itself and the baseline only (Sx: the gap beyond the meaningful
  // difference); in each scenario (P(best)) forks within the meaningful difference of each other tie (snapTies)
  const norm = Mx.map(r => r.slice()), Sx = Mx.map(r => r.slice()), col = new Array<number>(O);
  for (let k = 0; k < K; k++) {
    for (let j = 0; j < O; j++) {
      Sx[j][k] = beyondTie(Mx[j][k], Mx[0][k], R.ties[k]);
      norm[j][k] = anchored(R, M.criteria[k].higher, Sx[j][k], k);
    }
  }
  const score = norm.map(r => r.reduce((s, v, k) => s + v * w[k], 0));
  const best = new Float64Array(O), sc = new Float64Array(O);
  let valid = 0;
  for (let s = 0; s < R.n; s++) {
    sc.fill(0);
    for (let k = 0; k < K; k++) {
      for (let j = 0; j < O; j++) col[j] = R.X[(s * O + j) * K + k];
      const sk = snapTies(col, M.criteria[k].higher, R.ties[k]);
      for (let j = 0; j < O; j++) sc[j] += anchored(R, M.criteria[k].higher, sk[j], k) * w[k];
    }
    let m = -Infinity;
    for (let j = 0; j < O; j++) if (!R.BAD[s * O + j]) m = Math.max(m, sc[j]);
    if (m === -Infinity) continue;
    valid++;
    let ties = 0;
    for (let j = 0; j < O; j++) if (!R.BAD[s * O + j] && sc[j] === m) ties++;
    for (let j = 0; j < O; j++) if (!R.BAD[s * O + j] && sc[j] === m) best[j] += 1 / ties;
  }
  const rag: RagCounts[][] = R.options.map(() => M.criteria.map(() => [0, 0, 0] as RagCounts));
  const overall: RagCounts[] = R.options.map(() => [0, 0, 0] as RagCounts);
  for (let s = 0; s < R.n; s++) {
    for (let j = 0; j < O; j++) {
      let worst = 0;
      for (let k = 0; k < K; k++) {
        const idx = (s * O + j) * K + k, v = R.X[idx], L = R.limits[k], hb = M.criteria[k].higher;
        let c = hb ? (v >= L.green ? 0 : v <= L.red ? 2 : 1) : (v <= L.green ? 0 : v >= L.red ? 2 : 1);
        if (R.FLAG[idx] > c) c = R.FLAG[idx];
        if (R.BAD[s * O + j]) c = 2;
        rag[j][k][c]++;
        if (w[k] > 0) worst = Math.max(worst, c);
      }
      overall[j][worst]++;
    }
  }
  const FK = feasibleKey(R), key = score.map((x, j) => x * FK[j]);
  const order = R.options.map((_o, j) => j).sort((x, y) => score[y] * FK[y] - score[x] * FK[x]);
  const bestArr = Array.from(best);
  return {
    w,
    rawWeights: M.criteria.map(c => prefs.weights[c.name]),
    riskAversion: a,
    Mx,
    Sx,
    norm,
    score,
    key,
    best: bestArr,
    pBest: bestArr.map(b => b / Math.max(valid, 1)),
    valid,
    overall,
    rag,
    order,
    feasibleKey: FK,
    tradeoff: tradeoff(R, Mx, FK, order),
  };
}

/**
 * Ties within the meaningful difference q (as Python `mcda.snap` and the page's snapTies): sort the values; neighbours
 * closer than q join one group; each value takes the best value of its group. q = 0 returns the values as they are.
 */
export function snapTies(vals: number[], higher: boolean, q: number): number[] {
  if (!(q > 0) || vals.length < 2) return vals;
  const n = vals.length, idx = vals.map((_v, i) => i).sort((x, y) => vals[x] - vals[y]), out = vals.slice();
  let start = 0;
  for (let i = 1; i <= n; i++) {
    if (i < n && vals[idx[i]] - vals[idx[i - 1]] < q) continue;
    const rep = higher ? vals[idx[i - 1]] : vals[idx[start]];
    for (let t = start; t < i; t++) out[idx[t]] = rep;
    start = i;
  }
  return out;
}

/** A value on the fixed anchors of criterion k, clamped to 0..1, 1 = best (Python `mcda.scaled`, the page's anchored). */
export function anchored(R: SimRun, higher: boolean, x: number, k: number): number {
  const A = R.anchors[k];
  let v = (x - A.lo) / A.span;
  v = v < 0 ? 0 : v > 1 ? 1 : v;
  return higher ? v : 1 - v;
}

/** The score's value of a fork: only the part of its gap to the baseline value b beyond the meaningful difference q counts. */
export const beyondTie = (x: number, b: number, q: number): number => (q > 0 ? b + Math.sign(x - b) * Math.max(Math.abs(x - b) - q, 0) : x);

/** A feasible share within 1 point of the baseline's counts as the baseline's in the order key (with meaningful differences). */
export const FEASIBLE_TIE = 0.01;

/** Feasible shares of the order key (score x feasible), as Python `mcda.feasible_key` and the page's feasKey. */
export function feasibleKey(R: SimRun): number[] {
  const f = R.feasible.slice();
  return R.ties.some(q => q > 0) ? f.map(v => (Math.abs(v - f[0]) < FEASIBLE_TIE ? f[0] : v)) : f;
}

/** Criteria (indices) where options x and y differ, but by less than the meaningful difference (Python `tied_criteria`). */
export function tiedOn(R: SimRun, Mx: number[][], x: number, y: number): number[] {
  return Mx[x].map((_v, k) => k).filter(k => { const d = Math.abs(Mx[x][k] - Mx[y][k]); return d > 0 && d < (R.ties[k] ?? 0); });
}

/** x is better than y on criterion k by at least the meaningful difference (Python `mcda._better`, the page's betterOn). */
export function betterOn(R: SimRun, Mx: number[][], x: number, y: number, k: number): boolean {
  const g = R.model.criteria[k].higher ? Mx[x][k] - Mx[y][k] : Mx[y][k] - Mx[x][k];
  return g > 0 && g >= (R.ties[k] ?? 0);
}

/** A difference for people: shares in percentage points, other units after the number (the page's dfmt). */
const dfmt = (d: number, unit: string) => (unit === 'share' ? `${(100 * d).toFixed(d < 0.01 ? 2 : 1)} pp` : `${fmt(d)} ${unit}`);

function tradeoff(R: SimRun, Mx: number[][], FK: number[], order: number[]): Tradeoff {
  const M = R.model, a1 = order[0], a2 = order[1];
  if (a2 === undefined) return { leader: M.options[a1].name, runner: null, leaderBetter: [], runnerBetter: [], equal: [], text: '' };
  const wins = (x: number, y: number) => M.criteria.map((c, k) => [c, k] as const).filter(([, k]) => betterOn(R, Mx, x, y, k));
  const w1 = wins(a1, a2), w2 = wins(a2, a1), eq = tiedOn(R, Mx, a1, a2).map(k => criterionLabel(M, k));
  const by = (x: number, y: number) => ([c, k]: readonly [typeof M.criteria[number], number]) =>
    `${criterionLabel(M, k)} (by ${dfmt(Math.abs(R.stats[x][k].mean - R.stats[y][k].mean), c.unit)})`;
  const on = (j: number) => optionLabel(M, j);
  // equal on every criterion: say what orders them (the feasible share, or the baseline wins a tie)
  const text = !w1.length && !w2.length && eq.length
    ? `${on(a1)} and ${on(a2)} are equal within the meaningful difference on: ${eq.join(', ')}.`
      + (FK[a1] > FK[a2] ? ` ${on(a1)} passes the checks more often (${pct(R.feasible[a1])} vs ${pct(R.feasible[a2])}).` : a1 === 0 ? ` ${on(a1)} is the baseline: it wins a tie.` : '')
    : `${on(a1)} is better than ${on(a2)} on: ${w1.map(by(a1, a2)).join(', ') || 'none'}.` + (w2.length ? ` ${on(a2)} is better on: ${w2.map(by(a2, a1)).join(', ')}.` : '')
      + (eq.length ? ` Equal within the meaningful difference on: ${eq.join(', ')}.` : '');
  return {
    leader: M.options[a1].name, runner: M.options[a2].name,
    leaderBetter: w1.map(([, k]) => criterionLabel(M, k)), runnerBetter: w2.map(([, k]) => criterionLabel(M, k)), equal: eq, text,
  };
}
