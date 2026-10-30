// Timeline (mirrors variator.timeline): P(met) and forecast bands per option, inputs in conflict, the plan of levers
// (what, how much, when) and suggested dates. A port of the page's tlEvaluate, solvePlan and the "Suggest dates" button.
import { compileModel } from './compile.js';
import { centre } from './dist.js';
import { variableLabel } from './format.js';
import { corr, TARGET } from './path.js';
import { q, shareOf, simulateModel } from './simulate.js';
import type { ExportedModel, Plan, PlanStep, Ranking, SimRun, Spread, TimelineConflict, TimelineGoal, TimelineItem, TimelineResult } from './types.js';

export const MIN_R = 0.1;
export const TOP_C = 3;
/** Plan trials: scenarios per trial (the page uses 1000). */
export const PLAN_N = 1000;

/** Value of a model variable: value, else mode, else mean, else 0 (the page's varVal). */
export function varVal(M: ExportedModel, name: string): number {
  const v = M.variables.find(x => x.name === name);
  return v ? centre(v.dist) : 0;
}
export const itemMonth = (M: ExportedModel, it: TimelineItem): number => (typeof it.month === 'string' ? varVal(M, it.month) : it.month);
export const applies = (M: ExportedModel, it: Pick<TimelineItem, 'options'>, j: number): boolean => !it.options || it.options.includes(M.options[j].name);
/** An event is placed when all its "place" values hold in the model. */
export const isPlaced = (M: ExportedModel, it: TimelineItem): boolean => Object.entries(it.place || {}).every(([k, v]) => varVal(M, k) === v);
/** Milestones and goals still evaluated (not deleted). */
export const timelineGoals = (M: ExportedModel, off: Iterable<string> = []): TimelineItem[] => {
  const o = new Set(off);
  return (M.timeline ? M.timeline.items : []).filter(it => it.condition && !o.has(it.name));
};
/** Round to half a month and keep inside [0, horizon] (the page's setMonth). */
export const snapMonth = (v: number, horizon: number): number => Math.min(Math.max(Math.round(v * 2) / 2, 0), horizon);

/** P(met), spreads and inputs in conflict (the page's tlEvaluate). `off` = deleted milestones and goals. */
export function evaluateTimeline(R: SimRun, off: Iterable<string> = []): TimelineResult {
  const M = R.model;
  if (!M.timeline) return { target: TARGET, horizon: 0, goals: [], spreads: {}, conflicts: [] };
  const O = R.options.length, n = R.n, conflicts: TimelineConflict[] = [], spreads: Record<string, Spread[]> = {};
  for (const it of M.timeline.items) {
    if (it.show) {
      spreads[it.name] = R.options.map((_o, j) => {
        const a = new Float64Array(n);
        for (let s = 0; s < n; s++) a[s] = R.TS[it.name][s * O + j];
        a.sort();
        return [q(a, 0.1), q(a, 0.5), q(a, 0.9), q(a, 0.8)] as Spread;
      });
    }
  }
  const goals: TimelineGoal[] = timelineGoals(M, off).map(it => ({
    name: it.name,
    label: it.label,
    emoji: it.emoji,
    kind: it.kind,
    month: itemMonth(M, it),
    monthVar: typeof it.month === 'string' ? it.month : null,
    depends: it.depends || [],
    options: it.options,
    p: R.options.map((_o, j) => (applies(M, it, j) ? shareOf(R, R.TM[it.name], j) : null)),
    spread: spreads[it.name] || [],
  }));
  for (const g of goals) {
    for (let j = 0; j < O; j++) {
      const pj = g.p[j];
      if (pj == null || pj >= TARGET) continue;
      const y = new Float64Array(n), x = new Float64Array(n);
      for (let s = 0; s < n; s++) y[s] = R.TM[g.name][s * O + j];
      if (pj === 0 && R.TS[g.name]) for (let s = 0; s < n; s++) y[s] = -R.TS[g.name][s * O + j]; // never met: a later month is worse
      const found: TimelineConflict[] = [];
      for (const [name, arr] of Object.entries(R.IN || {})) {
        for (let s = 0; s < n; s++) x[s] = arr[s * O + j];
        const r = corr(x, y);
        if (r != null && Math.abs(r) >= MIN_R) {
          found.push({ goal: g.name, goalLabel: g.label, j, option: R.options[j], variable: name, variableLabel: variableLabel(M, name), r, direction: r < 0 ? 'too high' : 'too low' });
        }
      }
      conflicts.push(...found.sort((a, b) => Math.abs(b.r) - Math.abs(a.r)).slice(0, TOP_C));
    }
  }
  return { target: TARGET, horizon: M.timeline.horizon, goals, spreads, conflicts };
}

/** True when a goal of option j is below the target (the page then searches a plan). */
export function needsPlan(tl: TimelineResult, j: number): boolean {
  return tl.goals.some(g => g.p[j] != null && (g.p[j] as number) < TARGET);
}

/** The option the timeline shows: "best" = the leader of the ranking, else the named option. */
export function timelineOptionIndex(M: ExportedModel, option: string, rk: Ranking | null): number {
  if (option === 'best') return rk ? rk.order[0] : 0;
  const j = M.options.findIndex(o => o.name === option);
  return j < 0 ? (rk ? rk.order[0] : 0) : j;
}

export interface SolveOptions { n?: number; seed: number; off?: Iterable<string> }

/**
 * Plan (mirrors variator.timeline.solve_plan): the levers of option j in order; each takes the grid value with the
 * most goals met, then the highest capped P; among equal results its preferred end. Each trial is the same engine
 * with the same seed (`M` is the effective model; the lever variables are fixed per trial).
 */
export function solvePlan(M: ExportedModel, j: number, opts: SolveOptions): Plan {
  const tl = M.timeline;
  const n = opts.n ?? PLAN_N;
  const lev = (tl?.levers || []).filter(lv => !lv.options || lv.options.includes(M.options[j].name));
  const goals = timelineGoals(M, opts.off).filter(g => applies(M, g, j));
  const C = compileModel(M);
  const cache = new Map<string, { met: number; soft: number; p: number[] }>();
  const state: Record<string, number> = {}, steps: PlanStep[] = [];
  const score = (values: Record<string, number>) => {
    const key = JSON.stringify(Object.entries(values).sort());
    if (!cache.has(key)) {
      const m: ExportedModel = JSON.parse(JSON.stringify(M));
      for (const [k, v] of Object.entries(values)) m.variables.find(x => x.name === k)!.dist = { kind: 'fixed', value: v };
      const R = simulateModel(m, { n, seed: opts.seed, recordInputs: false, withStats: false, compiled: C });
      const p = goals.map(g => shareOf(R, R.TM[g.name], j));
      cache.set(key, { met: p.filter(x => x >= TARGET).length, soft: p.reduce((s, x) => s + Math.min(x, TARGET), 0), p });
    }
    return cache.get(key)!;
  };
  for (const lv of lev) {
    if (lv.needs && !(state[lv.needs] ?? varVal(M, lv.needs))) continue;
    const grid: number[] = [];
    for (let i = 0; i <= Math.round((lv.high - lv.low) / lv.step); i++) grid.push(+(lv.low + i * lv.step).toFixed(10));
    if (lv.prefer === 'high') grid.reverse();
    let best: { met: number; soft: number; v: number; mode: string | null; mv: number | null } | null = null;
    const modes: [string | null, number | null][] = lv.mode ? Object.entries(lv.modes) : [[null, null]];
    for (const [mode, mv] of modes) {
      for (const v of grid) {
        const sc = score({ ...state, [lv.variable]: v, ...(lv.mode ? { [lv.mode]: mv as number } : {}) });
        if (!best || sc.met > best.met || (sc.met === best.met && (sc.soft > best.soft || (sc.soft === best.soft && lv.prefer === 'low' && v < best.v)))) {
          best = { met: sc.met, soft: sc.soft, v, mode, mv };
        }
      }
    }
    state[lv.variable] = best!.v;
    if (lv.mode) state[lv.mode] = best!.mv as number;
    steps.push({ variable: lv.variable, label: lv.label, unit: lv.unit, mode: best!.mode, modeValue: best!.mv, value: best!.v, current: varVal(M, lv.variable) });
  }
  const fin = score(state);
  return { option: M.options[j].name, j, steps, p: fin.p, met: fin.met === goals.length, names: goals.map(g => g.name) };
}

/** Suggested dates: each movable milestone of option j goes to the month that 8 of 10 futures meet (rounded up to half). */
export function suggestDates(tl: TimelineResult, M: ExportedModel, j: number): Record<string, number> {
  const out: Record<string, number> = {};
  for (const g of tl.goals) {
    const sp = g.spread[j];
    if (sp && g.monthVar && applies(M, g, j)) out[g.monthVar] = snapMonth(Math.ceil(sp[3] * 2) / 2, tl.horizon);
  }
  return out;
}
