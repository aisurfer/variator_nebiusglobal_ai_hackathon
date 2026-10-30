// View model of the case page. The page renders only these types; `source.ts` builds them from the engine
// (the WP13a engine, workspace/src/variator/engine). Ids stay in the logic; people see labels.

export type Unit = 'USD' | 'USD/year' | 'USD/month' | 'months' | 'month' | 'share' | 'points' | 'ms' | 'count' | 'ratio'
  | 'index' | 'hours' | 'days' | 'score' | 'x' | '0/1' | 'people' | 'FTE' | (string & {});

export type Dist =
  | { kind: 'fixed'; value: number }
  | { kind: 'pert'; low: number; mode: number; high: number };

/** How a fork is recomputed when it changes: an overlay on the same paths, a rerun from its date, or a new structure. */
export type RecomputeClass = 'baseline' | 'overlay' | 'from_date' | 'structural';
export type Caution = 'average' | 'margin' | 'worst';
export type StepId = 'forks' | 'month' | 'week' | 'day';

export interface HelpEntry { title: string; short: string; full: string; metric?: string }

export interface Criterion { id: string; label: string; unit: Unit; higher: boolean; description: string; help: string }

export interface Fork {
  id: string; label: string; line: string; recompute: RecomputeClass; icon?: string;
  /** The fork every delta is measured against; always compared. */
  baseline?: boolean;
  /** Index of the categorical colour slot (0..4). */
  color: number;
}

export interface Hypothesis { id: string; label: string; line: string; changes: string[]; icon?: string }

export interface PriorityPreset { id: string; label: string; line: string; weights: Record<string, number>; caution?: Caution }
export interface CautionLevel { id: Caution; label: string; description: string; value: number }

export type Lane = 'decisions' | 'people' | 'clients' | 'factors' | (string & {});
/** An editable value of an intervention: a number (slider), a choice between named values (segmented), or an
 *  uncertain range (low / likely / high with the PERT curve). */
export type InterventionValue =
  | { kind: 'number'; path: string; label: string; unit: Unit; min: number; max: number; step: number }
  | { kind: 'choice'; path: string; label: string; unit: Unit; options: { value: number; label: string }[] }
  | { kind: 'range'; path: string; label: string; unit: Unit };
export interface Intervention {
  id: string; lane: Lane; kind: 'lever' | 'event'; label: string; description: string;
  /** Edit path of its month (a model variable). */
  monthPath: string;
  /** Edit path of the placement switch of an event (true = placed). */
  placePath?: string;
  /** Forks it belongs to (a lever of these forks); none: it applies to every fork. */
  forks?: string[];
  /** Values the inspector edits, in order (a size, a count, a choice, a lead time). */
  values: InterventionValue[];
  /** The value whose PERT range draws the «ready» band after the marker (a lead time in months). */
  lead?: string;
  source: string;
  affects: string[];
  /** Knowledge-base entity behind it, when one exists. */
  entityId?: string;
  /** Person id (`people`) an event is about (the engineer who leaves). */
  person?: string;
  /** Person id (`people`) who decides or handles it. */
  owner?: string;
  /** The random shock of the model that stands for the same event (its yearly chance). */
  shock?: string;
}

export interface InputDef {
  path: string; label: string; unit: Unit; block: string; source: string;
  /** False: a fork, a hypothesis or an intervention touches it (not shown under «What stays as before»). */
  untouched: boolean;
}
export interface ShockDef { id: string; label: string; probabilityPath: string; magnitudePath: string; unit: Unit; lane?: string; person?: string; owner?: string; entityId?: string; affects: string[]; event?: string }

export interface ChainNode { id: string; label: string; unit: Unit; definition: string; dependsOn: string[]; higher: boolean | null }
/** A line on the charts of a criterion: a goal, a check or a red limit. */
export interface GoalLine { value: number; label: string }
export interface RuleDef { id: string; label: string; kind: 'effect' | 'check' | 'flag'; text: string }

export interface Milestone {
  id: string; label: string; kind: 'milestone' | 'goal';
  /** Edit path of its month (a movable date), or null for a fixed date (goals at the horizon). */
  monthPath: string | null; fixedMonth?: number;
  forks?: string[];
  /** Plan item that must finish first (finish-to-start), by intervention id. */
  dependsOn: string[];
  condition: string;
}

export interface SectionDef { id: string; step: StepId; label: string; purpose: string; help: string }
export interface StepDef { id: StepId; label: string; number: number; help: string }

export interface CaseConfig {
  id: string; letter: string; title: string; question: string; lead: string;
  /** The fork date T (ISO). Nothing after T is known; months count from T. */
  forkDate: string; horizonMonths: number;
  criteria: Criterion[];
  forks: Fork[];
  hypotheses: Hypothesis[];
  priorityPresets: PriorityPreset[];
  caution: CautionLevel[];
  interventions: Intervention[];
  milestones: Milestone[];
  levers: { path: string; label: string; low: number; high: number; step: number; forks: string[] }[];
  inputs: InputDef[];
  shocks: ShockDef[];
  blocks: { id: string; label: string }[];
  chain: ChainNode[];
  /** The causal chain shown on «Why»: columns left to right, one or more values per column. */
  chainPath: string[][];
  rules: RuleDef[];
  steps: StepDef[];
  sections: SectionDef[];
  help: Record<string, HelpEntry>;
  /** The defaults: the only source of «as it was». */
  defaults: Record<string, EditValue>;
  people: { id: string; name: string; title: string; entityId?: string }[];
  /** Goal values per criterion (dashed lines on the charts). */
  goals: Record<string, GoalLine>;
  /** The criterion whose drivers «Why» shows (the first criterion). */
  driverCriterion: string;
  /** Best-path risk ref (criterion, rule or timeline id, or «fragile») → risk-calendar row id. */
  riskRows: Record<string, string>;
  company?: string;
  /** Person id of the case owner. */
  owner?: string;
  /** Where the forks come from: the author's request, what was drafted from it, the review (null: no origin file). */
  origin: CaseOrigin | null;
}

export interface CaseOrigin {
  request: string;
  /** The clarifying questions of the intake and the answers. */
  qa: { q: string; a: string }[];
  author: { id: string; name: string; title: string; entityId?: string };
  /** ISO dates. */
  requestedOn: string; frozenOn: string;
  drafted: { forks: string[]; hypotheses: string[]; criteria: string[] };
  reviewEdits: string[];
}

export type EditValue = number | boolean | string | string[] | Dist | Record<string, number>;
/** User edits as a layer over the defaults and the presets: edit path → value. */
export type Edits = Record<string, EditValue>;

export interface CaseSummary {
  id: string; letter: string; title: string; question: string; forks: number; hypotheses: number; criteria: string[];
  status: string; lastRun: string | null; ready: boolean; owner: string;
  /** Levels with live results, and levels with preview data. */
  live: StepId[]; preview: StepId[];
  /** The request the case was drafted from (its origin), one line on the list. */
  request?: string;
}

// ------------------------------------------------------------------------------------------------
// Run results
// ------------------------------------------------------------------------------------------------
export interface Stat { mean: number; p1: number; p5: number; p10: number; p25: number; p50: number; p75: number; p90: number; p95: number; p99: number; min: number; max: number; cvar: number }

export interface GoalEval {
  id: string; label: string; kind: 'milestone' | 'goal';
  month: number; movable: boolean;
  /** P(met) per fork; null: does not apply to the fork. */
  p: Record<string, number | null>;
  /** Forecast month per fork: [p10, p50, p90, p80]. */
  spread: Record<string, [number, number, number, number] | null>;
}
export interface Conflict { goal: string; fork: string; input: string; inputLabel: string; r: number }
export interface PlanStep {
  path: string; label: string; unit: Unit; value: number; current: number;
  /** A lever with modes (hire or vendor): the mode the plan picks, its variable and value. */
  mode?: { label: string; path: string; value: number; current: number } | null;
}
export interface Plan { fork: string; steps: PlanStep[]; p: Record<string, number>; met: boolean; raw?: unknown }

export interface FanPoint { month: number; p10: number; p25: number; p50: number; p75: number; p90: number }
export interface RiskCalendarRow {
  id: string; label: string; consequence: string; byMonth: number[]; medianMonth: number | null; total: number;
  /** Criterion, check, goal or milestone the row is about (dates a risk of the best path). */
  ref?: string;
  /** check, flag, threshold, milestone or goal. */
  kind?: string;
  /** A row from the live run (not the preview). */
  live?: boolean;
}
export interface WeekNode { id: string; week: number; label: string; share: number; parent: string | null; tone: 'good' | 'neutral' | 'bad'; summary: string; events: string[]; numbers: { label: string; value: string }[]; end: string }
export interface WeekDist { week: number; metric: string; p10: number; p50: number; p90: number }
/** A metric of the week or day level: its label, unit and how it is drawn. */
export interface PreviewMetricDef { id: string; label: string; unit: Unit; higher: boolean | null; bars?: boolean }
export interface DayEvent { day: number; title: string; text: string; tone: 'event' | 'good' | 'bad'; entityId?: string; person?: string }
export interface DayExample {
  id: 'pessimistic' | 'median' | 'optimistic'; label: string; outcome: string;
  /** The value the examples are ordered by (10th, 50th, 90th percentile) and its unit. */
  headline: { value: number; unit: Unit; label: string };
  daily: ({ day: number } & Record<string, number>)[]; events: DayEvent[];
}
export interface Preview {
  preview: true;
  /** What made the data and what it covers (shown with the «Preview data» badge). */
  note: string;
  /** Forks that have week and day data. */
  covers: string[];
  /** Months of the fans and the risk calendar. */
  horizon: number;
  /** Labels of the monthly metrics (they can differ from the criteria: «Churn, year to date»). */
  metricLabels: Record<string, string>;
  /** Metrics of the monthly fans; of the week tree and its small multiples; of the day charts. */
  monthMetrics: PreviewMetricDef[]; weekMetrics: PreviewMetricDef[]; dayMetrics: PreviewMetricDef[];
  /** Paths behind a week tree (status line of step 3). */
  paths: number;
  fans: Record<string, Record<string, FanPoint[]>>; // fork → criterion → months
  calendar: Record<string, RiskCalendarRow[]>; // fork → checks
  tree: Record<string, WeekNode[]>; // fork → nodes
  weekly: Record<string, WeekDist[]>; // fork → per week and metric
  days: Record<string, DayExample[]>; // fork → examples
}

export interface CaseRun {
  n: number; seed: number; ms: number;
  forks: string[];
  stats: Record<string, Record<string, Stat>>;
  feasible: Record<string, number>;
  /** RAG limits per criterion; `fixed`: set by the case (variables), else taken from the baseline fork. */
  limits: Record<string, { green: number; red: number; fixed?: boolean }>;
  /** Share of scenarios where a rule fires, per fork. */
  fire: Record<string, Record<string, number>>;
  /** Mean of each chain value per fork. */
  chain: Record<string, Record<string, number>>;
  goals: GoalEval[];
  conflicts: Conflict[];
  drivers: Record<string, { input: string; label: string; r: number }[]>;
  preview: Preview;
  /** Opaque engine data for the instant ranking (per-scenario values). */
  raw: unknown;
}

export interface Ranking {
  /** Opaque engine ranking (for the best path and the toast snapshot). */
  raw?: unknown;
  weights: Record<string, number>; caution: Caution;
  score: Record<string, number>;
  /** score × P(feasible): the sort key. */
  key: Record<string, number>;
  pBest: Record<string, number>;
  order: string[];
  rag: Record<string, Record<string, [number, number, number]>>;
  overall: Record<string, [number, number, number]>;
  /** Spread of the per-scenario score: [p10, p50, p90]. */
  scoreSpread: Record<string, [number, number, number]>;
}

export interface Risk {
  id: string; head: string; p: number; odds: string; detail: string;
  choice: { fork: string; odds: string } | null; watch: string | null;
  month: { median: number; label: string } | null;
  kind: 'check' | 'criterion' | 'goal' | 'red';
}
export interface BestPath {
  leader: string; runnerUp: string | null; pBest: number; feasible: number;
  breaks: { change: string; winner: string }[];
  risks: Risk[];
  /** The strategy sentence in parts; strong parts are the decision. */
  strategy: { text: string; strong?: boolean }[];
  tradeoff: string;
}

/** A snapshot taken before a change, compared after the recompute to write the toast (opaque engine data). */
export type Snapshot = object;
export interface ChangeReport {
  effect: string[]; required: string[]; recommended: string[]; supporting: string[]; risks: string[];
  tone: 'good' | 'bad' | 'neutral';
  /** Goals are missed and the plan is still being searched. */
  pending?: boolean;
}
