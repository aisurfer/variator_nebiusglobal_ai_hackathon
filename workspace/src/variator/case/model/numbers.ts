// One source of numbers per page state (QA M1/M2), the pure part: which source the page is in and its numbers (the
// ranking, the milestones and goals, the checks). The hooks and the badges are in ../numbers.tsx.
import type { ActorsApi } from '../actors/useActors';
import type { CaseApi } from '../state';

/** `actors`: the actor result is of the page state; `pending`: the page reruns on the actors after an edit, the previous
 *  actor result stays (dimmed); `draft`: the actors cannot answer for this state (no route, or the rerun failed), the
 *  formula model gives a quick estimate; `formula`: no actor data for the case. */
export type NumbersSource = 'actors' | 'pending' | 'draft' | 'formula';
/** The actor result is what the page shows (current, or the previous one while the rerun runs). */
export const onActors = (s: NumbersSource | undefined) => s === 'actors' || s === 'pending';
export interface StatView { mean: number; p5: number; p50: number; p95: number }
export interface Numbers {
  source: NumbersSource;
  /** The forks by the decision rule (score × feasible), the decision first. */
  order: string[];
  pBest: Record<string, number>;
  /** The number of forks the P(best) is computed over: the forks of the ranking (a fork is best among all of them). */
  pBestOf: number;
  feasible: Record<string, number>;
  /** Score × P(feasible). */
  key: Record<string, number>;
  /** Green, amber, red shares over the scenarios. */
  rag: Record<string, [number, number, number]>;
  stat: (fork: string, crit: string) => StatView | undefined;
  /** Shares of the decision per criterion. */
  weights: Record<string, number>;
  n: number;
}

/** The state of the page's numbers: see the header. */
export function sourceOf(a: Pick<ActorsApi, 'month' | 'verdict' | 'rankState'> & { pending?: boolean }): NumbersSource {
  return !a.month || !a.verdict ? 'formula' : a.rankState === 'stale' ? 'draft' : a.pending ? 'pending' : 'actors';
}

/** The numbers of the page in the state it is in (null while the first run computes). */
export function numbersOf(c: CaseApi, a: ActorsApi): Numbers | null {
  const source = sourceOf(a);
  if (onActors(source) && a.month?.ranking) {
    const opts = a.month.ranking.options.filter(o => c.forksOn.includes(o.option)).sort((x, y) => x.rank - y.rank);
    const by = <T,>(f: (o: (typeof opts)[number]) => T) => Object.fromEntries(opts.map(o => [o.option, f(o)]));
    const w = a.month.ranking.weights ?? {}, tot = Object.values(w).reduce((s, v) => s + v, 0) || 1;
    return {
      source, order: opts.map(o => o.option), pBest: by(o => o.pBest), pBestOf: a.month.ranking.options.length, feasible: by(o => o.pFeasible), key: by(o => o.scoreTimesFeasible ?? o.score),
      rag: by(o => [o.rag?.green ?? 0, o.rag?.amber ?? 0, o.rag?.red ?? 0] as [number, number, number]),
      stat: (f, k) => opts.find(o => o.option === f)?.stats?.[k], weights: Object.fromEntries(Object.entries(w).map(([k, v]) => [k, v / tot])), n: a.month.n,
    };
  }
  const run = c.run, rk = c.ranking;
  if (!run || !rk) return null;
  return {
    source: onActors(source) ? 'formula' : source, order: rk.order, pBest: rk.pBest, pBestOf: rk.order.length, feasible: run.feasible, key: rk.key, rag: rk.overall,
    stat: (f, k) => run.stats[f]?.[k], weights: rk.weights, n: run.n,
  };
}

// ---------------------------------------------------------------- goals, milestones and checks (WP18, month.json)
/** A goal or milestone row of a fork in month.json: P(met) at the target, P(by month m) for m = 1..H. */
interface ActorGoalRow { id: string; kind?: string; pMet: number; pByMonth?: number[]; cumulative?: boolean; markerMonth?: number }
/** A check of a fork in month.json: `rule` is the rule id of the case config where one matches. */
interface ActorCheckRow { id: string; rule: string | null; pEver: number }
interface ActorOptionRows { goals?: ActorGoalRow[]; milestones?: ActorGoalRow[]; checks?: ActorCheckRow[] }
const rowsOf = (a: Pick<ActorsApi, 'month'>, fork: string) => a.month?.options?.[fork] as unknown as ActorOptionRows | undefined;

/** The month by which a share q of the scenarios is done, from the cumulative P(done by month m) (linear in a month). */
export function monthAt(byMonth: number[], q: number): number | null {
  let prev = 0;
  for (let m = 1; m <= byMonth.length; m++) {
    const p = byMonth[m - 1];
    if (p >= q) return p > prev ? m - 1 + (q - prev) / (p - prev) : m;
    prev = p;
  }
  return null;
}
/** [p10, p50, p90, p80] of a forecast month from the cumulative P(done by month m); null if half are not done. */
export function spreadOf(byMonth: number[] | undefined): [number, number, number, number] | null {
  if (!byMonth?.length) return null;
  const [p10, p50, p90, p80] = [0.1, 0.5, 0.9, 0.8].map(q => monthAt(byMonth, q));
  if (p10 == null || p50 == null) return null;
  const H = byMonth.length;
  return [p10, p50, p90 ?? H, p80 ?? H];
}

export interface GoalView {
  id: string; label: string; kind: 'milestone' | 'goal'; month: number;
  /** P(met) for the fork in the page's source; undefined: that source has no number for the item. */
  p: number | undefined;
  /** [p10, p50, p90, p80] of the month it is reached (milestones). */
  spread: [number, number, number, number] | null;
}

/** The milestones and goals of a fork as the page shows them: P(met) and the forecast month of the actors while their
 *  result is current, else of the formula model (the list and the targets are the case's either way). */
export function goalViews(c: Pick<CaseApi, 'run'>, a: Pick<ActorsApi, 'month' | 'verdict' | 'rankState'> & { pending?: boolean }, fork: string): GoalView[] {
  const goals = (c.run?.goals ?? []).filter(g => g.p[fork] != null);
  if (!onActors(sourceOf(a))) return goals.map(g => ({ id: g.id, label: g.label, kind: g.kind, month: g.month, p: g.p[fork] ?? undefined, spread: g.spread[fork] ?? null }));
  const o = rowsOf(a, fork);
  return goals.map(g => {
    const r = o?.milestones?.find(x => x.id === g.id) ?? o?.goals?.find(x => x.id === g.id);
    const timed = !!r && (r.markerMonth != null || r.cumulative === true);
    return { id: g.id, label: g.label, kind: g.kind, month: g.month, p: r?.pMet, spread: timed ? spreadOf(r!.pByMonth) : null };
  });
}

/** How often a rule fires for a fork in the page's source (the actor checks by rule id while they are current). */
export function fireRate(c: Pick<CaseApi, 'run'>, a: Pick<ActorsApi, 'month' | 'verdict' | 'rankState'> & { pending?: boolean }, rule: string, fork: string): number | undefined {
  if (!onActors(sourceOf(a))) return c.run?.fire[rule]?.[fork] ?? 0;
  return rowsOf(a, fork)?.checks?.find(x => x.rule === rule)?.pEver;
}

/** The fork the Plan shows: the one the user picked, else the leader of the page's source (the actor verdict's while
 *  it is current), so the Plan and the answer name the same fork. */
export function planFork(c: Pick<CaseApi, 'timelineFork' | 'timelineChosen' | 'run'>, nums: Pick<Numbers, 'order'> | null): string {
  if (c.timelineChosen || !nums) return c.timelineFork;
  return nums.order.find(f => c.run?.forks.includes(f)) ?? c.timelineFork;
}
