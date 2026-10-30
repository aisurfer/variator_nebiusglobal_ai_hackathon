// The result contract of metrics: a metric definition (spec with its formula), its evaluated result with the lineage
// down to the records behind the number, and widget layouts. The GenUI primitives and the metric drill-down render it;
// a metric engine (the VARIATOR metrics on the month, week and day levels) produces it.
// Zod schemas are the single source of truth for the definitions; results are plain interfaces.
import { z } from 'zod';

export { CONTRACT_VERSION } from './contractVersion';

// ---------------------------------------------------------------------------------------------
// 1. Metric DSL: the formula of a metric; only a deterministic engine evaluates it.
// ---------------------------------------------------------------------------------------------

export const DatasetId = z.enum([
  'projects', 'employees', 'risks', 'commitments', 'deals', 'processes', 'quarters', 'finance',
  'evidence', 'directives', 'issues', 'followUps', 'sectorMetrics',
  // datasets of a VARIATOR simulation (src/variator/metrics/results.py mirrors this list)
  'contracts', 'invoices', 'payments', 'bank', 'people', 'requisitions', 'payroll', 'incidents', 'deploys',
  'support_tickets', 'cloud_bills', 'tax_payments', 'decisions', 'events', 'nps', 'monitoring',
]);
export type DatasetId = z.infer<typeof DatasetId>;

export const Condition = z.object({
  field: z.string(),
  op: z.enum(['eq', 'neq', 'in', 'notIn', 'gt', 'gte', 'lt', 'lte', 'contains', 'isNull', 'notNull']),
  value: z.union([z.string(), z.number(), z.boolean(), z.null(), z.array(z.string())]),
});
export type Condition = z.infer<typeof Condition>;

export const Window = z.discriminatedUnion('kind', [
  z.object({ kind: z.literal('rolling'), days: z.number().int().min(1).max(366) }),
  z.object({ kind: z.literal('periodToDate'), period: z.enum(['week', 'month', 'quarter', 'year']) }),
  z.object({ kind: z.literal('all') }),
]);
export type Window = z.infer<typeof Window>;

export type Expr =
  | { op: 'count' | 'sum' | 'avg' | 'median' | 'min' | 'max' | 'p90'; dataset: DatasetId; field: string | null; filter: Condition[]; window: Window | null; dateField: string | null; label: string }
  | { op: 'duration'; dataset: DatasetId; from: string; to: string; unit: 'days' | 'hours'; agg: 'avg' | 'median' | 'p90' | 'max'; filter: Condition[]; window: Window | null; dateField: string | null; label: string }
  | { op: 'ratio'; numerator: Expr; denominator: Expr; asPercent: boolean; label: string }
  | { op: 'add' | 'sub' | 'mul' | 'div'; args: Expr[]; label: string }
  | { op: 'const'; value: number; label: string }
  | { op: 'index'; components: { label: string; weight: number; expr: Expr; worst: number; best: number; minSample: number }[]; minCoverage: number; label: string }
  | { op: 'metric'; metricId: string; label: string };

const aggregateBase = {
  dataset: DatasetId,
  filter: z.array(Condition), // AND; empty array = no filter
  window: Window.nullable(), // null inherits MetricSpec.window
  dateField: z.string().nullable(), // null uses the dataset's default date field from the catalog
  label: z.string(), // plain-language name shown in the drilldown tree
};
export const Expr: z.ZodType<Expr> = z.lazy(() => z.discriminatedUnion('op', [
  z.object({ op: z.enum(['count', 'sum', 'avg', 'median', 'min', 'max', 'p90']), field: z.string().nullable(), ...aggregateBase }),
  // `to` may be the literal 'asOf' to measure the age of still-open items.
  z.object({ op: z.literal('duration'), from: z.string(), to: z.string(), unit: z.enum(['days', 'hours']), agg: z.enum(['avg', 'median', 'p90', 'max']), ...aggregateBase }),
  z.object({ op: z.literal('ratio'), numerator: Expr, denominator: Expr, asPercent: z.boolean(), label: z.string() }),
  z.object({ op: z.enum(['add', 'sub', 'mul', 'div']), args: z.array(Expr).min(2), label: z.string() }),
  z.object({ op: z.literal('const'), value: z.number(), label: z.string() }),
  // Composite 0..100 score. Each component is mapped linearly from worst (0) to best (100) and clamped;
  // worst > best means lower raw values are better. Unknown components are skipped and weights
  // renormalized; below minCoverage (share of weight known) the index itself is unknown.
  z.object({
    op: z.literal('index'),
    // minSample: records the component needs (denominator records for a ratio); fewer makes it unknown, not zero.
    components: z.array(z.object({ label: z.string(), weight: z.number().positive(), expr: Expr, worst: z.number(), best: z.number(), minSample: z.number().int().min(0) })).min(1),
    minCoverage: z.number().min(0).max(1),
    label: z.string(),
  }),
  z.object({ op: z.literal('metric'), metricId: z.string(), label: z.string() }),
]));

export const Dimension = z.enum(['project', 'employee', 'department', 'team', 'source', 'category', 'severity', 'month', 'week', 'client', 'stage']);
export type Dimension = z.infer<typeof Dimension>;

// src/variator/metrics/results.py GenUnit mirrors this list (months: runway; ms: latency; index: a number on its own
// scale, not 0–100, such as NPS from −100 to 100)
export const Unit = z.enum(['count', 'percent', 'ratio', 'usd', 'days', 'weeks', 'months', 'hours', 'ms', 'score', 'index', 'multiple']);
export const PrimitiveId = z.enum([
  'kpi', // atom-built tile: value, delta, status, sparkline
  'kpiTarget', // value against target (bullet/progress)
  'trend', // line/area over the history window, optional target line
  'barBreakdown', // horizontal bars by a dimension
  'indexBreakdown', // contribution of components of an `index` metric
  'scoreGauge', // 0..100 score with bands
  'exceptionList', // records that need attention (MetricSpec.highlight)
  'table', // breakdown table with values and links
  'calendarHeatmap', // daily values over weeks
  'comparison', // several metrics side by side
  'note', // short deterministic text: definition or what changed
]);
export type PrimitiveId = z.infer<typeof PrimitiveId>;

/** The definition of a metric. Every field is required (nullable instead of optional). */
export const MetricSpecDraft = z.object({
  title: z.string(), // the executive's own name for it, any language
  businessQuestion: z.string(), // the question the number answers
  plainDefinition: z.string(), // one paragraph a non-technical executive understands
  howCalculated: z.array(z.string()).min(1), // numbered plain steps mirroring `formula`
  formula: Expr,
  unit: Unit,
  direction: z.enum(['higher', 'lower', 'context']),
  target: z.object({ value: z.number(), basis: z.string() }).nullable(),
  bands: z.object({ good: z.number(), warn: z.number() }).nullable(), // thresholds in metric units, interpreted with `direction`
  window: Window, // default evaluation window for aggregates
  scope: z.object({ kind: z.enum(['organization', 'reportingLine', 'projects']), employeeId: z.string().nullable(), projectIds: z.array(z.string()) }),
  breakdown: z.object({ dimension: Dimension, limit: z.number().int().min(1).max(25) }).nullable(),
  highlight: z.object({ label: z.string(), expr: Expr }).nullable(), // a `count` whose matched records are "needs attention"
  cadence: z.enum(['daily', 'weekly']),
  comparison: z.enum(['previousDay', 'previousWeek', 'both']),
  visualization: z.object({ preferred: PrimitiveId, alternatives: z.array(PrimitiveId), rationale: z.string() }),
  dataSources: z.array(z.string()), // source families used (Linear, Meetings, ...), for the canvas
  reference: z.object({ name: z.string(), url: z.string().nullable(), note: z.string() }).optional(), // where the definition comes from
  assumptions: z.array(z.string()),
  openQuestions: z.array(z.string()),
  limitations: z.array(z.string()),
  confidence: z.enum(['high', 'medium', 'low']), // confidence in the interpretation, not in the data
});
export type MetricSpecDraft = z.infer<typeof MetricSpecDraft>;

export const MetricSpec = MetricSpecDraft.extend({
  id: z.string(),
  contractVersion: z.number().int(),
  version: z.number().int().min(1),
  organizationId: z.string(),
  role: z.enum(['ceo']), // the one user role of the MVP (src/kb/types.ts KbRoleId)
  status: z.enum(['draft', 'active', 'archived']),
  origin: z.object({ kind: z.enum(['conversation', 'seed', 'manual']), messageIds: z.array(z.string()) }),
  createdAt: z.string(),
  updatedAt: z.string(),
});
export type MetricSpec = z.infer<typeof MetricSpec>;

// ---------------------------------------------------------------------------------------------
// 2. Dataset catalog: what a formula may reference and what the engine can compute.
// ---------------------------------------------------------------------------------------------

export interface CatalogField {
  name: string; // fields may be addressed one foreign-key hop away: `projectId.category`, `assigneeId.department`
  type: 'string' | 'number' | 'boolean' | 'date' | 'enum' | 'id';
  description: string; // shown in the spec card
  values?: string[]; // for enums
  references?: DatasetId; // for ids
  derived?: boolean; // computed by the engine as of the evaluation date (e.g. directive.statusAsOf)
  populated?: boolean; // false: no record of this organization has a value; prefer another field
  hasHistory?: boolean; // false: values before today are unknown; overrides the dataset flag
}
export interface CatalogDataset {
  id: DatasetId;
  label: string;
  description: string;
  sourceFamilies: string[];
  defaultDateField: string | null; // used for windows; null means a snapshot without history
  hasHistory: boolean; // false: values before today are unknown, not equal to today
  fields: CatalogField[];
  dimensions: Partial<Record<Dimension, string>>; // dimension -> field path on this dataset
  recordKind: string; // the detail kind its records open as
}
export interface Catalog { organizationId: string; asOf: string; historyStart: string; datasets: CatalogDataset[]; primitives: PrimitiveDescriptor[] }

export interface PrimitiveDescriptor {
  id: PrimitiveId;
  level: 'molecule' | 'organism';
  label: string;
  purpose: string; // when to choose it
  requires: ('series' | 'breakdown' | 'target' | 'components' | 'highlight' | 'multipleMetrics')[];
  sizes: WidgetSize[];
}

// ---------------------------------------------------------------------------------------------
// 3. Evaluation result and lineage (drill-down to atomic records and source evidence).
// ---------------------------------------------------------------------------------------------

export interface RecordRef {
  dataset: DatasetId;
  id: string;
  label: string;
  /** What opens when the record is clicked; null: not openable. Kind `kbEntity`: the knowledge-base entity `id`
   * (its dossier); `gmetric`: a metric step. */
  detail: { kind: string; id: string } | null;
  fields: Record<string, string | number | boolean | null>; // the values that made it match/contribute
  evidenceIds: string[];
}
export interface LineageNode {
  id: string; // stable path inside the result, e.g. "root.numerator.0"
  label: string;
  // simulation results add two levels below the aggregates: events and the declared causes of an event
  kind: 'metric' | 'operation' | 'component' | 'aggregate' | 'record' | 'event' | 'cause';
  value: number | null;
  unit: z.infer<typeof Unit> | null;
  explanation: string; // deterministic text: "Directives due in the last 28 days: 14"
  formulaText: string | null; // e.g. "9 / 14 × 100"
  weight?: number; // for index components
  normalized?: number | null; // 0..100 for index components
  unknownReason?: string; // why value is null
  children: LineageNode[];
  records?: RecordRef[]; // only on aggregate nodes; complete in /metrics/:id/result, may be truncated in views
  recordsTotal?: number; // matched records before truncation
  excluded?: { count: number; reason: string; sample: RecordRef[] }[]; // e.g. open promises left out of reliability
  sample?: number; // records behind an index component, compared with minSample
  cause?: { type: string; id: string }; // only on cause nodes: the declared cause (decision, rule, habit…) and its id
}
export type MetricStatus = 'good' | 'warn' | 'bad' | 'neutral' | 'unknown';
export interface MetricResult {
  metricId: string;
  specVersion: number;
  asOf: string;
  value: number | null;
  unit: z.infer<typeof Unit>;
  status: MetricStatus;
  previous: { day: number | null; week: number | null };
  series: { date: string; value: number | null }[]; // one point per cadence step over the history window
  breakdown: { key: string; label: string; value: number | null; detail: RecordRef['detail'] }[];
  highlight: RecordRef[];
  changes: { comparedTo: string; added: RecordRef[]; removed: RecordRef[]; addedTotal: number; removedTotal: number }; // records entering/leaving root aggregates
  lineage: LineageNode;
  coverage: { records: number; unknownComponents: string[]; historyAvailable: boolean };
  computedAt: string;
  engineVersion: string;
}

/** `path` is a JSON Pointer into the draft; `hint` lists allowed values. */
export interface SpecValidationIssue { path: string; code: string; message: string; hint: string | null; severity: 'error' | 'warning' }

// ---------------------------------------------------------------------------------------------
// 4. Widget layout: which primitive shows which metric, grouped in sections.
// ---------------------------------------------------------------------------------------------

export const WidgetSize = z.enum(['sm', 'md', 'lg', 'xl']); // 3 / 4 / 6 / 12 of 12 columns; full width on phones
export type WidgetSize = z.infer<typeof WidgetSize>;
export const WidgetSpec = z.object({
  id: z.string(),
  primitive: PrimitiveId,
  metricIds: z.array(z.string()).min(1), // one, except `comparison`
  size: WidgetSize,
  title: z.string().nullable(), // null uses the metric title
  options: z.object({
    seriesDays: z.number().int().min(7).max(120).nullable(),
    showTarget: z.boolean().nullable(),
    limit: z.number().int().min(1).max(25).nullable(),
    noteKind: z.enum(['definition', 'changes']).nullable(),
  }),
  rationale: z.string(), // why this primitive for this metric; visible on the canvas
});
export const DashboardSpecDraft = z.object({
  title: z.string(),
  subtitle: z.string(),
  sections: z.array(z.object({ id: z.string(), title: z.string(), description: z.string().nullable(), widgets: z.array(WidgetSpec) })).min(1),
});
export const DashboardSpec = DashboardSpecDraft.extend({
  id: z.string(),
  contractVersion: z.number().int(),
  organizationId: z.string(),
  role: z.enum(['ceo']),
  version: z.number().int().min(1),
  generatedBy: z.object({ kind: z.enum(['agent', 'fallback', 'seed']), model: z.string().nullable(), at: z.string() }),
});
export type WidgetSpec = z.infer<typeof WidgetSpec>;
export type DashboardSpecDraft = z.infer<typeof DashboardSpecDraft>;
export type DashboardSpec = z.infer<typeof DashboardSpec>;
