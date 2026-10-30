// The typed contract of the case engine (WP13a). The UI (WP13b) builds on these types only.
//
// Flow: config JSON (scripts/mvp/export_case.py) -> loadCase -> effectiveModel(case, state)
//   -> simulateCase (heavy, Web Worker) -> SimRun -> rankRun / bestPath / evaluateTimeline (instant)
//   -> snapshot + diffReports (change toasts).
// The maths is a port of docs/playground/model-simulator.html (same seeds, same random stream layout).

// ---------------------------------------------------------------- the exported model (variator.report.playground)

export type DistKind = 'fixed' | 'pert' | 'triangular' | 'uniform' | 'normal' | 'lognormal' | 'bernoulli';

/** A distribution. Only the keys of its kind are present (fixed: value; pert: low, mode, high; ...). */
export interface Dist {
  kind: DistKind;
  value?: number;
  low?: number;
  mode?: number;
  high?: number;
  mean?: number;
  std?: number;
  p?: number;
}

export interface ModelVariable {
  name: string;
  desc: string;
  unit: string;
  /** Input block: finance, clients, product, operations, people, market, external, link, timeline, lever. */
  block: string;
  /** Data source tag, for example "data: CRM", "assumption: link", "decision". */
  source: string;
  dist: Dist;
}

export interface ModelDerived { name: string; desc: string; unit: string; formula: string }

export interface ModelOption {
  name: string;
  desc: string;
  /** Formula of the first criterion. */
  formula: string;
  /** Criterion name -> formula, for the other criteria. */
  metrics: Record<string, string>;
  /** Lever variables this option sets (variable -> distribution). */
  overrides: Record<string, Dist>;
}

/**
 * `rag`: fixed RAG limits (only when the template sets them). Each limit is a number or a variable name; a name
 * gives the median of that variable after the edits and presets (as Python `simulation.tensor.resolved_criteria`).
 */
export interface ModelCriterion {
  name: string; desc: string; unit: string; higher: boolean; weight: number; rag?: { green: number | string; red: number | string };
  /** Meaningful difference (a number or a variable name, as `rag`): smaller differences between options are ties. */
  tie?: number | string;
}

export interface ModelRisk {
  name: string;
  desc: string;
  p: number;
  variable: string;
  effect: 'add' | 'multiply';
  options: string[] | null;
  magnitude: Dist;
}

export interface RuleAction {
  set: Record<string, string | number>;
  fail: string | null;
  flag: 'green' | 'amber' | 'red' | null;
  criterion: string | null;
}

export interface ModelRule {
  id: string;
  pack: string;
  /** effect: changes inputs; check: a failure makes the scenario infeasible; flag: sets a RAG colour. */
  kind: 'effect' | 'check' | 'flag';
  desc: string;
  branches: { if: string; then: RuleAction }[];
  else: RuleAction | null;
  options: string[] | null;
}

export interface ScenarioOp {
  kind?: 'variable' | 'risk' | 'weight';
  target: string;
  scale?: number;
  shift?: number;
  probability?: number;
  options?: string[];
}

/** A scenario preset of the template (a world hypothesis on the case page). */
export interface ScenarioPreset { name: string; label: string; emoji: string; description: string; ops: ScenarioOp[] }

export type TimelineItemKind = 'start' | 'plan' | 'event' | 'milestone' | 'goal';

export interface TimelineItem {
  name: string;
  label: string;
  kind: TimelineItemKind;
  emoji: string;
  description: string;
  /** A fixed month (number) or the name of a month variable (movable marker). */
  month: number | string;
  /** Milestones and goals: met when true. */
  condition: string | null;
  target: number;
  /** Events: variable -> value when the event is placed. */
  place: Record<string, number>;
  /** Forecast formula (a month) for the band. */
  show: string | null;
  options: string[] | null;
  depends: string[];
}

export interface Lever {
  variable: string;
  label: string;
  low: number;
  high: number;
  step: number;
  unit: string;
  mode: string | null;
  modes: Record<string, number>;
  prefer: 'low' | 'high';
  needs: string | null;
  options: string[] | null;
}

export interface ModelTimeline { horizon: number; items: TimelineItem[]; levers: Lever[] }

export interface PageText {
  title: string;
  eyebrow: string;
  lead: string;
  scope?: string;
  change?: string[];
  real?: string[];
  metrics?: string[];
  /** Names for people: id -> label (options, criteria, rules). */
  labels?: Record<string, string>;
  /** Risk headline per criterion or check id. */
  risks?: Record<string, string>;
  /** Derived values shown as the cause-and-effect chain. */
  chain?: string[];
}

/** variator.report.playground.export_model(model, packs, PAGE, scenarios, timeline). */
export interface ExportedModel {
  question: string;
  variables: ModelVariable[];
  derived: ModelDerived[];
  options: ModelOption[];
  criteria: ModelCriterion[];
  risks: ModelRisk[];
  rules: ModelRule[];
  assumptions: string[];
  page: PageText;
  scenarios: ScenarioPreset[];
  timeline: ModelTimeline | null;
}

// ---------------------------------------------------------------- the case config (workspace/config/cases/<id>.json)

export const CASE_SCHEMA = 'variator-case/1' as const;

/** Defaults of every setting: the only source of "as it was at the start". */
export interface CaseDefaults {
  /** Criterion -> weight (the model weights). */
  weights: Record<string, number>;
  /** 0 = rank by the mean, 1 = rank by the worst 5% of scenarios. */
  riskAversion: number;
  n: number;
  seed: number;
  activePresets: string[];
  edits: Record<string, EditValue>;
}

export interface PriorityPreset {
  id: string;
  label: string;
  description: string;
  /** Criterion -> weight (complete). "balanced" = the model defaults. */
  weights: Record<string, number>;
}

/** The segmented control "How careful?" (riskAversion values). */
export interface CautionLevel { id: string; label: string; description: string; value: number }

/** A world hypothesis: a scenario preset of the model, shown as a chip layer. */
export interface Hypothesis {
  id: string;
  kind: 'hypothesis';
  label: string;
  description: string;
  emoji: string;
  /** Lucide icon name (kebab case) for the UI. */
  icon: string;
  /** Plain-English lines, one per op ("Competitor price cut: 50% more likely"). */
  effects: string[];
}

/** An intervention on the timeline that belongs to a fork (plan item, milestone, lever). */
export interface ForkIntervention {
  item: string;
  label: string;
  kind: TimelineItemKind;
  /** Month variable of the marker; null for a fixed month. */
  monthVar: string | null;
  month: number;
}

/**
 * How a fork is recomputed when it or its date changes (Docs 09-ACTORS-AND-FORKS.md): `overlay` = values on top of
 * the same scenario paths (instant); `from_date` = the paths are rerun from the fork's start date (parametric);
 * `structural` = new actors or rules, the whole horizon is rerun. The baseline fork has none (null).
 */
export type RecomputeClass = 'overlay' | 'from_date' | 'structural';

/** A fork = an option of the model, a decision to compare. */
export interface Fork {
  id: string;
  kind: 'decision';
  label: string;
  /** One line. */
  description: string;
  /** Lucide icon name (kebab case). */
  icon: string;
  /** Colour slot 0..4 (Plancy chart palette), stable per fork. */
  color: number;
  /** Lever variable -> value this fork sets. */
  levers: Record<string, number>;
  interventions: ForkIntervention[];
  /** The baseline fork (RAG limits come from it). */
  baseline: boolean;
  /** Recompute class; null for the baseline (no intervention). Help: `recompute.<class>`, `recompute.baseline`. */
  recompute: RecomputeClass | null;
}

/** Lanes of the timeline of interventions (step 1). */
export type Lane = 'decisions' | 'people' | 'clients_vendors' | 'factors';

/** A person of the company behind an intervention or a shock. */
export interface CasePerson {
  /** HR id of the company history (LW-E06). */
  id: string;
  name: string;
  title: string;
  /** Knowledge-base entity id, exact KB form (person.lw-e06). */
  entityId: string;
}

/** A model name (derived value, criterion or variable) that an item or a shock moves, with a short label for people. */
export interface Affect { id: string; label: string }

/** The value of an item the inspector edits (a size or an amount). */
export interface ItemValue {
  /** Edit path: v|<variable>|value or o|<option>|<variable>|value. */
  path: Path;
  variable: string;
  label: string;
  unit: string;
  min: number;
  max: number;
  step: number;
}

/**
 * Case metadata of a timeline item the user moves or places: a plan item (a lever of a fork) or a placeable event.
 * Keyed by the timeline item name in CaseConfig.items; the marker itself is the model's TimelineItem.
 */
export interface CaseItem {
  /** `lever`: a plan item of one or more forks; `event`: a certain event the user can place (all forks). */
  kind: 'lever' | 'event';
  lane: Lane;
  /** For people, without emoji ("Daniel Kowalski leaves"). */
  label: string;
  /** One or two sentences, plain English. */
  description: string;
  /** Person id (CaseConfig.people) who decides or handles it. */
  owner: string | null;
  /** Events: person id the event is about (the engineer who leaves); null otherwise. */
  person: string | null;
  /** Knowledge-base entity id behind it (org.VN-03, org.CL-001, person.lw-e06), exact KB form. */
  entityId: string | null;
  affects: Affect[];
  /** Events: the model risk (shock) that stands for the same event at random; null for levers. */
  shock: string | null;
  /** Edit path of the editable value (lever amount or event size); null when there is none. Same as value.path. */
  valuePath: Path | null;
  value: ItemValue | null;
  /** Variable of the uncertain lead time or size (a PERT range) the inspector shows; edit path v|<range>. */
  range: string | null;
  /** Source tag for the inspector ("plan · HR", "event · external"). */
  source: string;
}

/** Case metadata of a random shock (a model risk), keyed by the risk name in CaseConfig.shocks. */
export interface CaseShock {
  label: string;
  lane: Lane;
  owner: string | null;
  person: string | null;
  entityId: string | null;
  affects: Affect[];
  /** The placeable timeline event that stands for the same event on a certain date, if any. */
  event: string | null;
}

/** A goal line on the charts of a criterion (dashed). */
export interface GoalLine { value: number; label: string }

export interface HelpEntry {
  title: string;
  /** Short text for the hover popover. */
  body: string;
  /** Longer text for the pinned modal. */
  more?: string;
  /** Formula, shown in mono. */
  formula?: string;
  /** Metric sheet to link from the modal (a model name: criterion, derived value or variable). */
  metric?: string;
}

export type StepId = 'forks' | 'month' | 'week' | 'day';
/** The page's sections in page order: world, forks, stays | month-* | week-review, week | day-review, day. */
export type SectionId =
  | 'world'
  | 'forks'
  | 'stays'
  | 'month-answer'
  | 'month-compare'
  | 'month-time'
  | 'month-plan'
  | 'month-why'
  | 'month-details'
  | 'week-review'
  | 'week'
  | 'day-review'
  | 'day';

/** State parts a section reset clears (see resetSection). */
export type StatePart = 'activePresets' | 'edits' | 'prefs' | 'timeline' | 'timelineOption' | 'n' | 'seed' | 'selectedForks';

export interface CaseStep {
  id: StepId;
  index: number;
  title: string;
  question: string;
  /** Help key of the step ("step.<id>"). */
  help: string;
}

export interface CaseSection {
  id: SectionId;
  step: StepId;
  /** Short name for the progress bar and the heading ("Answer", "As before"). */
  title: string;
  /** The question the section answers. */
  question: string;
  /** One phrase "what is here". */
  summary: string;
  /** Help key of the "?" icon. */
  help: string;
  collapsed: boolean;
  /** True: the content comes from the preview file (<case>.preview.json), not from the engine. */
  preview: boolean;
  resets: StatePart[];
}

export interface CaseConfig extends ExportedModel {
  schema: typeof CASE_SCHEMA;
  id: string;
  /** Letter on the cases list (A, B). */
  letter: string;
  title: string;
  /** The company of the case (Larchwood Systems). The model `question` names it too. */
  company: string;
  /** One or two sentences under the title. */
  lead: string;
  /** Person id (people) who owns the case. */
  owner: string;
  /** The fork date T (ISO): nothing after T is known; months count from T. */
  forkDate: string;
  /** Months of the model horizon (12 for a company year, 24 for a delivery). */
  horizonMonths: number;
  /** Plugin template the model comes from (company_model). */
  template: string;
  /** The preview file of the future sections, relative to config/cases/. */
  previewFile: string | null;
  defaults: CaseDefaults;
  priorityPresets: PriorityPreset[];
  caution: CautionLevel[];
  hypotheses: Hypothesis[];
  forks: Fork[];
  /** People behind the items and shocks (the owner of the case first). */
  people: CasePerson[];
  /** Timeline item name -> case metadata, for every plan item (lever) and placeable event. */
  items: Record<string, CaseItem>;
  /** Risk name -> case metadata, for every model risk (shock). */
  shocks: Record<string, CaseShock>;
  /** The causal chain on "Why": columns left to right, one or more derived values or criteria per column. */
  chainPath: string[][];
  /** Chain value -> true when higher is better (colour of the deltas); missing: neutral. */
  chainHigher: Record<string, boolean>;
  /** Input blocks in display order (Model settings). */
  blocks: { id: string; label: string }[];
  /** Inputs no fork, hypothesis, item or shock moves, in block order (section "stays"). */
  stays: string[];
  /** Criterion -> goal line on its charts. */
  goals: Record<string, GoalLine>;
  /** Best-path risk ref (criterion, rule or timeline id, or "fragile") -> risk-calendar row id of the preview. */
  riskRows: Record<string, string>;
  /** Page help key -> config help key (the page's short names for the "?" icons). */
  helpKeys: Record<string, string>;
  help: Record<string, HelpEntry>;
  steps: CaseStep[];
  sections: CaseSection[];
  /** Labels for every id (options, criteria, rules, derived values, variables, risks, timeline items, levers). */
  labels: Record<string, string>;
}

// ---------------------------------------------------------------- the cases list (workspace/config/cases/index.json)

export const CASES_INDEX_SCHEMA = 'variator-cases/1' as const;

export type CaseStatus = 'ready' | 'preparing';

export interface CaseIndexEntry {
  id: string;
  letter: string;
  title: string;
  /** The question for people, with the company name. */
  question: string;
  template: string;
  status: CaseStatus;
  /** Config file, relative to config/cases/ (<id>.json); null while preparing. */
  config: string | null;
  /** Preview file, relative to config/cases/; null when there is none. */
  preview: string | null;
  /** Owner name for the list. */
  owner: string;
  forks: number;
  hypotheses: number;
}

export interface CasesIndex { schema: typeof CASES_INDEX_SCHEMA; cases: CaseIndexEntry[] }

// ---------------------------------------------------------------- the user layer (CaseState)

export type EditValue = number | boolean;

/**
 * Paths (as on the VARIATOR pages):
 *   v|<variable>|<key>            variable distribution parameter (value, low, mode, high, mean, std, p)
 *   o|<option>|<variable>|<key>   option override parameter
 *   r|<risk>|p                    risk probability
 *   rm|<risk>|<key>               risk magnitude parameter
 *   w|<criterion>, w|__risk       weight, risk aversion (CaseState.prefs)
 *   v|<monthVar>|value            a moved timeline marker (CaseState.timeline.moves)
 *   ev|<event item>               a placed event (CaseState.timeline.events)
 *   tloff|<item>                  a deleted milestone or goal (CaseState.timeline.off)
 *   preset|<name>                 an active hypothesis (CaseState.activePresets)
 * isModified and resetPath accept a prefix: "v|cash0" covers "v|cash0|value".
 */
export type Path = string;

export interface CasePrefs {
  /** Criterion -> weight set by the user (or a priority preset). Missing: the default (after presets). */
  weights: Record<string, number>;
  /** Missing (null): the default. */
  riskAversion: number | null;
  /** The priority preset last applied; null after a manual weight change. */
  priority: string | null;
}

export interface CaseTimelineState {
  /** Month variable -> month (a moved marker, a suggested date or an applied plan). */
  moves: Record<string, number>;
  /** Placed events (timeline item names). */
  events: string[];
  /** Deleted milestones and goals (item names). */
  off: string[];
}

export interface CaseState {
  version: 1;
  caseId: string;
  /** Active hypotheses; they apply in definition order, not in this order. */
  activePresets: string[];
  /** Path -> value: the user's edits; they win over the presets. */
  edits: Record<Path, EditValue>;
  /** Backup plans of the user: effect rules «if <value> <op> <threshold> then <variable> += <change>» (formula model only). */
  backups?: { id: string; if: string; variable: string; change: number; desc: string }[];
  prefs: CasePrefs;
  timeline: CaseTimelineState;
  /** "best" or an option name: the option the plan section shows. */
  timelineOption: string;
  /** Forks that go on to the week and day steps (review card). */
  selectedForks: string[];
  n: number;
  seed: number;
}

/** Resolved preferences (complete). */
export interface Prefs { weights: Record<string, number>; riskAversion: number }

/** [preset label, value before, value after] per path. */
export type PresetTrail = Record<Path, [string, number, number][]>;

export interface EffectiveModel { model: ExportedModel; trail: PresetTrail }

// ---------------------------------------------------------------- results

export interface CritStats {
  mean: number;
  p5: number;
  p10: number;
  p25: number;
  p50: number;
  p75: number;
  p90: number;
  p95: number;
  /** Mean of the worst 5% (the low tail when higher is better). */
  cvar: number;
  /** Sorted values (length n), for box plots and histograms. */
  srt: Float64Array;
}

/** The result of one heavy simulation (the page's R before ranking). Arrays are [s * O + j] or [(s * O + j) * K + k]. */
export interface SimRun {
  caseId: string;
  /** The effective model that was simulated (defaults -> presets -> edits -> timeline). */
  model: ExportedModel;
  n: number;
  seed: number;
  ms: number;
  options: string[];
  criteria: string[];
  higher: boolean[];
  /** Criterion values [(s * O + j) * K + k]. */
  X: Float64Array;
  /** RAG colour set by flag rules: -1 none, 0 green, 1 amber, 2 red. */
  FLAG: Int8Array;
  /** 1 = a check rule failed (scenario infeasible) [s * O + j]. */
  BAD: Uint8Array;
  /** Rule id -> per option, the count of scenarios where the rule fired. */
  fire: Record<string, number[]>;
  /** Varying inputs after the shocks [s * O + j] (for "what to watch" and the timeline conflicts). */
  IN: Record<string, Float64Array>;
  /** Timeline item -> 1/0 "met" [s * O + j]. */
  TM: Record<string, Float64Array>;
  /** Timeline item -> value of its forecast formula [s * O + j]. */
  TS: Record<string, Float64Array>;
  chainNames: string[];
  /** Per option: chain value -> mean. */
  chain: Record<string, number>[];
  /** [option][criterion]. */
  stats: CritStats[][];
  /** Per option: share of scenarios where no check fails. */
  feasible: number[];
  /**
   * Per criterion: RAG limits. Fixed limits of the criterion (`rag`) when set; else from the baseline option 0
   * (green = its median, red = its p10 on the bad side).
   */
  limits: { green: number; red: number; fixed: boolean }[];
  /** Per criterion: the meaningful difference (0 = every difference counts), as Python `Criterion.tie_within`. */
  ties: number[];
  /** Per criterion: the fixed scale of the score (0 at lo, 1 at lo + span), from the baseline (Python `mcda.anchors`). */
  anchors: { lo: number; span: number }[];
}

export type RagCounts = [number, number, number];

/** `equal`: criteria (labels) where the two differ only within the meaningful difference. */
export interface Tradeoff { leader: string; runner: string | null; leaderBetter: string[]; runnerBetter: string[]; equal: string[]; text: string }

export interface Ranking {
  /** Normalised weights (sum 1), criterion order. */
  w: number[];
  /** Weights as given (not normalised), criterion order. */
  rawWeights: number[];
  riskAversion: number;
  /** [option][criterion]: (1 - a) * mean + a * cvar. */
  Mx: number[][];
  /** [option][criterion]: Mx with only the gap to the baseline beyond the meaningful difference (beyondTie); the score scales these. */
  Sx: number[][];
  /** [option][criterion]: 0..1 across the options (1 = best). */
  norm: number[][];
  score: number[];
  /** score * feasibleKey: the order key. */
  key: number[];
  /** Feasible shares of the order key: within 1 point equal when a criterion has a meaningful difference. */
  feasibleKey: number[];
  /** Per option: scenarios where it has the best weighted score (ties share). */
  best: number[];
  /** best / valid. */
  pBest: number[];
  /** Scenarios with at least one feasible option. */
  valid: number;
  /** Per option: counts of [green, amber, red] of the worst colour over the weighted criteria. */
  overall: RagCounts[];
  /** [option][criterion]: counts of [green, amber, red]. */
  rag: RagCounts[][];
  /** Option indices, best first. */
  order: number[];
  tradeoff: Tradeoff;
}

export interface BreakChange {
  /** Machine form, as Python best_path: "weight of cash x0.5", "risk aversion 1". */
  change: string;
  /** Winner option id. */
  winner: string;
  /** For people: "weight of Cash at year end ×0.5". */
  label: string;
  winnerLabel: string;
}

export interface RiskItem {
  /** R1, R2, ... in order. */
  id: string;
  kind: 'fragile' | 'check' | 'criterion' | 'goal' | 'red';
  /** Criterion, rule or goal id the risk is about (null for "fragile" and "red"). */
  ref: string | null;
  emoji: string;
  /** The consequence, without the emoji. */
  headline: string;
  p: number;
  /** Odds in words: "3 in 10", "1 in 12", "below 1 in 50". */
  odds: string;
  /** "Cash at year end below 1.62M USD; expected 2.10M USD". */
  evidence: string;
  /** Another option that makes it less likely by more than 2 points. */
  choice: { option: string; label: string; odds: string } | null;
  /** The input that correlates most with the bad outcome. */
  watch: { variable: string; label: string; high: boolean } | null;
  score: number;
  /** The page's line as plain text. */
  text: string;
}

export interface ExpectedValue { p5: number; median: number; p95: number; unit: string }

export interface BestPath {
  decision: string;
  j: number;
  runner: string | null;
  runnerLabel: string | null;
  /** Feasible share of the decision. */
  feasible: number;
  /** P(best) of the decision. */
  pBest: number;
  breaks: BreakChange[];
  /** Criteria (labels) where the decision and the runner-up differ only within the meaningful difference. */
  ties: string[];
  risks: RiskItem[];
  expected: Record<string, ExpectedValue>;
  /** Active hypotheses (labels). */
  situation: string[];
  /** 1-2 sentences, plain text. */
  strategy: string;
}

/** [p10, p50, p90, p80] of a forecast month. */
export type Spread = [number, number, number, number];

export interface TimelineGoal {
  name: string;
  label: string;
  emoji: string;
  kind: TimelineItemKind;
  /** The month of the marker in this run's model. */
  month: number;
  /** Month variable when the marker is movable; null for a fixed date. */
  monthVar: string | null;
  depends: string[];
  options: string[] | null;
  /** Per option: P(met); null when the item does not apply to the option. */
  p: (number | null)[];
  /** Per option: forecast spread (empty without a forecast formula). */
  spread: Spread[];
}

export interface TimelineConflict {
  goal: string;
  goalLabel: string;
  j: number;
  option: string;
  variable: string;
  variableLabel: string;
  /** Correlation of the input with "met"; < 0: high values cause the miss. */
  r: number;
  direction: 'too high' | 'too low';
}

export interface TimelineResult {
  /** 0.8: a milestone or goal is met when P(met) >= target. */
  target: number;
  horizon: number;
  goals: TimelineGoal[];
  /** Item -> per option spread (all items with a forecast formula, also starts). */
  spreads: Record<string, Spread[]>;
  /** Top 3 per goal and option, for goals below the target. */
  conflicts: TimelineConflict[];
}

export interface PlanStep {
  variable: string;
  label: string;
  unit: string;
  mode: string | null;
  modeValue: number | null;
  value: number;
  current: number;
}

export interface Plan {
  option: string;
  j: number;
  steps: PlanStep[];
  /** P(met) with the plan, in the order of `names`. */
  p: number[];
  names: string[];
  met: boolean;
}

/** What the toast compares: one per state (before and after a change). */
export interface CaseSnapshot {
  /** The option the timeline shows. */
  j: number;
  option: string;
  optionLabels: string[];
  /** Leader (option index). */
  decision: number;
  goals: {
    name: string;
    label: string;
    emoji: string;
    p: number | null;
    p80: number | null;
    month: number;
    movable: boolean;
  }[];
  conflicts: { goal: string; goalLabel: string; variable: string; variableLabel: string; r: number }[];
  /** Median of the first criterion for option j. */
  k0: number;
  crit0: { name: string; label: string; unit: string };
}

export interface PMetChange { goal: string; label: string; before: number; after: number; deltaPp: number }

/** The change toast: what changed, the effect, what is required / recommended / supporting, the risks that rise. */
export interface ChangeReport {
  what: string;
  j: number;
  option: string;
  optionLabel: string;
  effect: string[];
  /** Plan steps (or a sentence) once a plan is known; see `requiredPending`. */
  required: string[];
  /** True: goals are missed and no plan was given yet ("Searching a plan..."). */
  requiredPending: boolean;
  recommended: string[];
  supporting: string[];
  risks: string[];
  missed: number;
  tone: 'good' | 'bad' | '';
  leaderChanged: { from: string; to: string } | null;
  pMetChanges: PMetChange[];
}

// ---------------------------------------------------------------- preview of the future sections

export interface FanPoint { month: number; p10: number; p25: number; p50: number; p75: number; p90: number }

export interface PreviewMetric { id: string; label: string; unit: string; higher: boolean; description: string }

/**
 * What fails in a risk-calendar row: `check` = a check rule fails (the scenario is not acceptable); `flag` = a flag rule
 * turns red; `threshold` = a monthly value crosses a limit (cash below zero); `milestone` = a milestone is late (it
 * fails at its marker month); `goal` = a goal is missed (it fails at its month, usually the horizon).
 */
export type RiskRowKind = 'check' | 'flag' | 'threshold' | 'milestone' | 'goal';

export interface RiskCalendarRow {
  id: string;
  label: string;
  /** What it means for people, one line, plain English. */
  consequence: string;
  kind: RiskRowKind;
  /** Rule, timeline item or criterion behind the row (sla_breach, hires_ready, cash); null for a plain threshold. */
  ref: string | null;
  /** P(first failure <= month m), m = 1..horizonMonths. */
  pByMonth: number[];
  /** P(it happens within the horizon). */
  pEver: number;
  /** Median first-failure month (1..horizonMonths) among the scenarios that fail; null when none fails. */
  medianFirstMonth: number | null;
}

export interface PreviewOption {
  preview: true;
  option: string;
  label: string;
  /** Metric -> months 0..horizonMonths. The last month matches the L0 distribution of the option at the horizon. */
  fans: Record<string, FanPoint[]>;
  /** Rows that apply to this fork (a milestone row only for the forks the milestone belongs to). */
  riskCalendar: RiskCalendarRow[];
}

export interface WeekNode {
  /** "w<week>n<index>". */
  id: string;
  week: number;
  /** Index of the parent in the previous level (-1 for the root). */
  parent: number;
  /** Share of the final paths that go through this node (branch thickness). */
  share: number;
  /** Metric id (PreviewFuture.weekMetrics) -> value at the end of the week. */
  values: Record<string, number>;
  summary: string;
}

export interface WeekBand { week: number; p10: number; p50: number; p90: number }

export interface WeekTree {
  preview: true;
  option: string;
  label: string;
  k: number;
  maxNodes: number;
  weeks: number;
  /** levels[w] = nodes of week w (0 = today). */
  levels: WeekNode[][];
  /** Metric id -> per week: p10, p50, p90 of the node values over the kept nodes (small multiples). */
  bands: Record<string, WeekBand[]>;
}

export interface DayPoint {
  date: string;
  /** Metric id (PreviewFuture.dayMetrics) -> value of the day. */
  values: Record<string, number>;
}

export type DayEventType = 'incident' | 'payment' | 'shock' | 'decision' | 'people' | 'client' | 'vendor' | 'milestone';

export interface DayEvent {
  date: string;
  type: DayEventType;
  text: string;
  /** Knowledge-base entity id from the company history, exact KB form (org.CL-001, org.VN-02, person.lw-e06). */
  entityId?: string;
  /** HR id of the person in the event (LW-E06), when there is one. */
  person?: string;
}

export interface DayExample {
  preview: true;
  option: string;
  kind: 'pessimistic' | 'median' | 'optimistic';
  label: string;
  /** One line: how the horizon ends in this example. */
  summary: string;
  /** Criterion values of this L0 scenario at the horizon (year end for a company year). */
  yearEnd: Record<string, number>;
  days: DayPoint[];
  events: DayEvent[];
}

export interface PreviewFuture {
  preview: true;
  caseId: string;
  /** What made the data (a stand-in until the month Monte Carlo on actors exists). */
  source: string;
  note: string;
  seed: number;
  n: number;
  /** The fork date T (ISO). */
  anchor: string;
  horizonMonths: number;
  /** The monthly fan metrics. */
  metrics: PreviewMetric[];
  /** The metrics of the week-tree nodes (WeekNode.values). */
  weekMetrics: PreviewMetric[];
  /** The metrics of the example days (DayPoint.values). */
  dayMetrics: PreviewMetric[];
  options: Record<string, PreviewOption>;
  /** Every fork. */
  weekTrees: Record<string, WeekTree>;
  /** Every fork: pessimistic, median, optimistic. */
  dayExamples: Record<string, DayExample[]>;
}

// ---------------------------------------------------------------- worker protocol

export type EngineRequest =
  | { type: 'simulate'; id: number; config: CaseConfig; state: CaseState }
  | { type: 'solve'; id: number; config: CaseConfig; state: CaseState; option: number; n?: number; seed?: number };

export type EngineResponse =
  | { type: 'run'; id: number; run: SimRun }
  | { type: 'plan'; id: number; plan: Plan }
  | { type: 'error'; id: number; message: string };
