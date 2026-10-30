// The actor engine's files as the case page reads them (contract: docs/knowledge/future-engine.md). `month.json` has the
// schema of the preview (engine/types.ts `PreviewFuture`) with `preview: false`, `source: "actors"` and extra blocks;
// the day file of WP19 (`day_examples.json`, schema variator.day/1) adds a knowledge-base branch tenant per example.
// Every extra field is optional here: the same adapter also reads a preview file.
import type { CaseState, FanPoint, PreviewMetric, RiskCalendarRow, WeekBand, WeekNode as EngineWeekNode } from '../../engine';
import type { DayExample, FanPoint as ViewFanPoint, Preview, RiskCalendarRow as ViewRow } from '../model/types';

export interface ActorFanPoint extends FanPoint { p5?: number; p95?: number }

export interface ActorRow extends RiskCalendarRow {
  /** P(the first failure is in month m), m = 1..horizon (pByMonth is its running sum). */
  pFirstByMonth?: number[];
  /** A milestone: the month it is due, and the median month it is done. */
  markerMonth?: number; doneMonthP50?: number;
  /** `risk`: a failure to watch; `progress`: it counts something good (case A «Live by the month»). */
  rowType?: 'risk' | 'progress';
}

export interface ActorOption { preview?: boolean; option: string; label: string; fans: Record<string, ActorFanPoint[]>; riskCalendar: ActorRow[] }

export interface ActorStat { mean: number; p5: number; p50: number; p95: number; cvar?: number }
export interface ActorRankedOption {
  option: string; rank: number; score: number; scoreTimesFeasible?: number; pBest: number; pFeasible: number;
  robustness?: number; rag?: { green: number; amber: number; red: number }; stats?: Record<string, ActorStat>;
}
export interface ActorRanking {
  weights?: Record<string, number>; riskAversion?: number;
  options: ActorRankedOption[];
  /** «a is better than b on: x, y» lines of the engine (ids). */
  tradeoffs?: string[];
  /** Criteria whose difference between the leader and the runner-up is below its meaningful difference (WP16). */
  ties?: string[];
  reversals?: string[]; warnings?: string[];
  bestPath?: { decision: string; runner_up?: string | null; prob_best?: number; p_feasible?: number; robustness?: number; stays_best?: string[]; breaks_if?: string[]; ties?: string[] };
}

export interface TreeEvent { week: number; type: string; text: string; entityId?: string; severity?: string }
export type ExampleKind = 'pessimistic' | 'median' | 'optimistic';
export interface TreeExample {
  kind: ExampleKind; label?: string; fork: string; leaf: string;
  /** Node ids of the path, week 0 to the leaf. */
  nodes: string[]; keys?: string[]; seed?: number;
  events: TreeEvent[];
  weeks: Record<string, number>[];
}
export interface ActorWeekTree {
  preview: boolean; source?: string; option: string; label: string; k: number; maxNodes: number; weeks: number;
  levels: EngineWeekNode[][];
  bands: Record<string, WeekBand[]>;
  examples?: TreeExample[];
}

export interface ActorDayEvent { date: string; type: string; text: string; entityId?: string; person?: string; severity?: string }
export interface ActorDayExample {
  preview?: boolean; source?: string; option: string; kind: ExampleKind; label: string; summary: string;
  yearEnd: Record<string, number | null>;
  days: { date: string; values: Record<string, number> }[];
  events: ActorDayEvent[];
  /** The knowledge-base branch of this future (WP19): its own tenant. */
  tenant?: string; scenario?: number; percentile?: number; leaf?: string | null; horizonEnd?: string;
}

export interface ActorMonth {
  preview: boolean; source: string; caseId: string; engine?: string; note: string;
  provenance?: { seed?: number; n?: number; habits?: { as_of?: string }; history?: { months?: number; run_id?: string } | null; code?: { commit?: string };
    /** A live run (WP18's route): served from its cache; edits of formula inputs that the actors learn instead. */
    live?: boolean; cached?: boolean; ignoredEdits?: string[]; stateHash?: string };
  seed: number; n: number; anchor: string; horizonMonths: number; months?: string[];
  metrics: PreviewMetric[]; weekMetrics: PreviewMetric[]; dayMetrics: PreviewMetric[];
  options: Record<string, ActorOption>;
  ranking?: ActorRanking;
  layers?: Record<string, { label: string; winner: string }>;
  parameters?: { name: string; point: number; low: number; high: number; source: string }[];
  weekTrees?: Record<string, ActorWeekTree>;
  /** The week event trees by fork (WP18c; see eventTree.ts). */
  eventTrees?: Record<string, import('./eventTreeModel').EventTreeRaw>;
  dayExamples?: Record<string, ActorDayExample[]>;
  timings?: Record<string, number | string>;
}

export interface DayFile {
  preview: boolean; caseId: string; source: string; note: string; seed: number; anchor: string; horizonMonths: number;
  dayMetrics: PreviewMetric[];
  dayExamples: Record<string, ActorDayExample[]>;
}

// ------------------------------------------------------------------------------------------------ the page's view

export interface FanView extends ViewFanPoint { p5?: number; p95?: number }
export interface CalendarRowView extends ViewRow {
  /** P(first failure in month m). */
  firstByMonth?: number[];
  markerMonth?: number;
  rowType?: 'risk' | 'progress';
}
export interface MarkedLeaf { kind: ExampleKind; leaf: string; nodes: string[] }
export interface DayExampleView extends DayExample {
  /** The knowledge-base branch tenant of the example (WP19), when there is one. */
  tenant?: string;
}

/** The future sections' data from one source, in the page's view types (a superset of `Preview`). */
export interface FutureView extends Omit<Preview, 'preview' | 'fans' | 'calendar' | 'days'> {
  preview: boolean;
  source: 'actors' | 'preview';
  /** Scenarios behind the fans and the calendar (0: unknown). */
  n: number;
  fans: Record<string, Record<string, FanView[]>>;
  calendar: Record<string, CalendarRowView[]>;
  days: Record<string, DayExampleView[]>;
  /** Week tree: the leaves of the day examples per fork. */
  marked: Record<string, MarkedLeaf[]>;
  /** Where the day examples come from: WP19's day file, the bridge of the tree leaves, or the preview; the forks whose
   *  examples are from the day file. */
  daySource: 'day-file' | 'bridge' | 'preview';
  dayFileForks?: string[];
  /** The ids of the criteria among the monthly metrics (the rest are links of the chain). */
  criteria: string[];
}

export interface VerdictView {
  leader: string; runnerUp: string | null;
  pBest: number; runnerPBest: number | null; feasible: number;
  /** The number of forks the P(best) is computed over (all the forks of the actor ranking). */
  pBestOf: number;
  /** Forks in rank order. */
  order: { fork: string; pBest: number; feasible: number; score: number }[];
  /** «a is better on x and y; b on z», with labels, the tied criteria left out; empty without a runner-up. */
  tradeoff: string;
  /** Criteria where the two do not differ meaningfully (labels), and the line that says so. */
  ties: string[]; tieLine: string;
  /** The leader and the runner-up are equal on every criterion. */
  allTied: boolean;
}

/** The body of a rerun on actors (POST /api/variator/cases/{case}/month, WP18): the page's engine CaseState and the
 *  forks that are on. */
export type MonthRequest = CaseState & { forks: string[]; include?: string[]; weekForks?: string[]; weekN?: number };
