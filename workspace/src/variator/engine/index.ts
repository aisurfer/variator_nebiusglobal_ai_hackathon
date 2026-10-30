// Public API of the case engine (pure TS, no DOM). The contract types are in types.ts.
//
//   const c = loadCase(config);                    // config = config/cases/company_best_year.json
//   let state = loadState(c);                      // the user layer (localStorage) or the defaults
//   const run = simulateCase(c, state);            // heavy: use EngineClient (Web Worker) in the UI
//   const a = analyseRun(c, state, run);           // ranking, timeline, best path, toast snapshot (instant)
//   const rk = rankRun(run, effectivePrefs(c, state2, run.model));   // a priority change: no new simulation
import { diffReports, snapshot } from './diff.js';
import { bestPath } from './path.js';
import { rankRun } from './rank.js';
import { simulateModel } from './simulate.js';
import { effectiveModel, effectivePrefs, type LoadedCase } from './state.js';
import { evaluateTimeline, solvePlan as solvePlanModel, timelineOptionIndex } from './timeline.js';
import type { BestPath, CaseSnapshot, CaseState, Plan, Prefs, Ranking, SimRun, TimelineResult } from './types.js';

export * from './types.js';
export {
  applyMoves, applyOp, applyPlan, applyPriority, clearState, defaultState, effectiveModel, effectivePrefs, fromPreset,
  isModified, loadCase, loadState, modifiedPaths, moveMarker, resetAll, resetParts, resetPath, resetSection, saveState,
  setEdit, setItemOff, setPath, setRiskAversion, setWeight, stateKey, toggleEvent, togglePreset,
  type LoadedCase, type StorageLike,
} from './state.js';
export { computeStats, q, runBuffers, shareOf, simulateModel, type SimulateOptions } from './simulate.js';
export { DEFAULT_RISK_AVERSION, normalisedWeights, rankRun, resolvePrefs, snapTies, tiedOn, type PrefsInput } from './rank.js';
export { activeScenarios, bestPath, CLEAR_LEAD, corr, keysFor, riskList, strategy, TARGET, whatBreaks } from './path.js';
export {
  applies, evaluateTimeline, isPlaced, itemMonth, MIN_R, needsPlan, PLAN_N, snapMonth, suggestDates, timelineGoals,
  timelineOptionIndex, TOP_C, varVal, type SolveOptions,
} from './timeline.js';
export { solvePlanModel };
export { diffReports, requiredFromPlan, snapshot, withPlan } from './diff.js';
export { compile, compileModel, tokenize, withDerived, type CompiledFn, type CompiledModel } from './compile.js';
export { mulberry32, ndtri, pertQ, quant } from './dist.js';
export {
  breakText, cautionWords, criterionLabel, fmt, fmt1, lbl, money, noEmoji, odds, optionLabel, pct, peopleText, peopleValue, ppTxt, scenarios,
  splitEmoji, variableLabel, vfmt,
} from './format.js';

/** Simulate the case for the state: defaults -> hypotheses -> edits -> timeline, with state.n and state.seed. */
export function simulateCase(c: LoadedCase, state: CaseState): SimRun {
  const { model } = effectiveModel(c, state);
  return simulateModel(model, { n: state.n, seed: state.seed, caseId: c.id });
}

/** Plan of levers for option j under the state (the page: 1000 scenarios, the state's seed). */
export function solvePlan(c: LoadedCase, state: CaseState, j: number, opts: { n?: number; seed?: number } = {}): Plan {
  const { model } = effectiveModel(c, state);
  return solvePlanModel(model, j, { n: opts.n, seed: opts.seed ?? state.seed, off: state.timeline.off });
}

export interface CaseAnalysis {
  prefs: Prefs;
  ranking: Ranking;
  timeline: TimelineResult;
  path: BestPath;
  /** The option the timeline shows. */
  j: number;
  snapshot: CaseSnapshot;
}

/** Everything instant over a run for a state: ranking, timeline, best path and the toast snapshot. */
export function analyseRun(c: LoadedCase, state: CaseState, run: SimRun): CaseAnalysis {
  const prefs = effectivePrefs(c, state, run.model);
  const ranking = rankRun(run, prefs);
  const timeline = evaluateTimeline(run, state.timeline.off);
  const j = timelineOptionIndex(run.model, state.timelineOption, ranking);
  const path = bestPath(run, prefs, state.activePresets, { ranking, timeline });
  return { prefs, ranking, timeline, path, j, snapshot: snapshot(run, ranking, timeline, j) };
}

/** The state of the comparison run: "template" = no hypotheses (same edits and priorities); "defaults" = nothing changed. */
export function baseState(c: LoadedCase, state: CaseState, mode: 'template' | 'defaults'): CaseState {
  if (mode === 'template') return { ...state, activePresets: [] };
  const d = c.config.defaults;
  return {
    ...state,
    activePresets: [],
    edits: {},
    prefs: { weights: {}, riskAversion: null, priority: null },
    timeline: { moves: {}, events: [], off: [] },
    n: d.n,
    seed: d.seed,
  };
}

export interface RunComparison {
  /** [option][criterion]: mean of the run minus mean of the base. */
  delta: number[][];
  /** [option][criterion]: delta / |base mean| (null when the base mean is about 0). */
  relative: (number | null)[][];
  /** [option][criterion]: true when the change is good for that criterion. */
  better: boolean[][];
  /** Per option: its place (1-based) in the base ranking. */
  basePlace: number[];
}

/** Differences to a comparison run (the page's "vs template" column and delta tags). */
export function compareRuns(run: SimRun, rk: Ranking, base: SimRun, baseRk: Ranking): RunComparison {
  const delta = run.stats.map((row, j) => row.map((s, k) => s.mean - base.stats[j][k].mean));
  return {
    delta,
    relative: delta.map((row, j) => row.map((d, k) => {
      const b = base.stats[j][k].mean;
      return Math.abs(b) > 1e-9 ? d / Math.abs(b) : null;
    })),
    better: delta.map(row => row.map((d, k) => (run.higher[k] ? d > 0 : d < 0))),
    basePlace: run.options.map((_o, j) => baseRk.order.indexOf(j) + 1),
  };
}

/** A toast for a change: snapshots before and after (same timeline option), the change text and an optional plan. */
export function changeReport(before: CaseSnapshot, after: CaseAnalysis, change: string, plan?: Plan | null) {
  return diffReports(before, after.snapshot, change, plan);
}
