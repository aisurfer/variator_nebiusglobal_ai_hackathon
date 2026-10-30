// Contracts of the knowledge-base integration (/api/kb/*), shared by server/kb and src/kb.
// The workspace of the company is computed from the live knowledge base.

/** The user roles of the workspace. The MVP has one role, the CEO, who sees every metric; the company file keeps a list
 * of roles, so a later release can add more ids here. The head of finance and the head of delivery stay people of the
 * company (knowledge-base persons, owners of levers), not users. */
export type KbRoleId = 'ceo';
/** `money` — an amount in the company's `currency`; `months` — a duration in months (runway); `ms` — milliseconds;
 * `score` — 0 to 100; `index` — a number on its own scale (NPS: −100 to 100). */
export type KbUnit = 'money' | 'count' | 'hours' | 'days' | 'months' | 'ms' | 'percent' | 'score' | 'index';
export type KbStatus = 'good' | 'warn' | 'bad' | 'neutral' | 'unknown';
/** The areas of the brief, in their order on the page. */
export type KbAreaId = 'money' | 'clients' | 'delivery';

/** `principal`: whose knowledge-base ACL applies to the role; in the MVP the CEO uses a principal with full access.
 * `metadata`: the role sees subject, counterparty and date of external correspondence and business documents it may
 * not open (management visibility). */
export interface KbRole { id: KbRoleId; label: string; title: string; name: string; personId: string; principal: string; metadata?: boolean }
/** A further value of the weekly report: the page property, its label in records and texts, a stable field name
 * (derived from the label when omitted), whether the brief's one-line summary shows it (default yes). */
export interface KbNorthStarMeasure { property: string; label: string; field?: string; summary?: boolean }
/** The company's North Star, read from weekly report pages (contract 05 §4.4): pages with the label `reportLabel` whose
 * property `property` holds the number. Texts not given are composed from `title`, `unit`, `target` and `by`. */
export interface KbNorthStar {
  title: string; reportLabel: string; property: string; secondary?: (string | KbNorthStarMeasure)[]; target: number | null; by: string | null;
  unit?: string; // what is counted, plural: «customers», «clients»
  period?: string; // property with the reported period
  field?: string; // stable field name of the North Star value (default northStar)
  question?: string; definition?: string; howCalculated?: string[];
  headline?: string; // the brief's sentence after «North Star: », «{value}» is the number
}
/** A simulated future of a case, loaded as its own knowledge-base tenant (WP19 names it
 * `<company tenant>-<case letter>-<fork>-p<10|50|90>`). It holds the state at the fork date `from` and every record
 * the simulation made after it: never real history. `place`: its order among the three example days of the fork, by
 * cash at the end (the `-p<n>` of the tenant name is not a percentile among the scenarios); `label`: «Keep the plan ·
 * middle example». */
export interface KbBranch {
  tenant: string; caseId: string; fork: string; kind: 'pessimistic' | 'median' | 'optimistic'; place: 'lowest' | 'middle' | 'highest'; label: string; from: string;
}

/** The company as the workspace knows it (config/company.json): `tenant` — its knowledge-base tenant; `currency` — of
 * its amounts (ISO 4217); `engineeringDepartment` — the directory department whose people make the engineering metrics.
 * `branches` (from /organizations): the simulated futures loaded in the knowledge base; `branch` (server side): the
 * branch a request reads instead of the company tenant. */
export interface KbOrganization {
  id: string; tenant: string; name: string; sector: string; headcount: number; timezone: string; ownDomain: string; roles: KbRole[];
  currency?: string; engineeringDepartment?: string; northStar?: KbNorthStar | null;
  askExamples?: Partial<Record<KbRoleId, string[]>>; // questions the «Ask» page offers, per role (the MVP: the CEO only)
  dataEnd?: string; // optional override of the last day of data (YYYY-MM-DD): the default view date of the workspace
  branches?: KbBranch[];
  branch?: KbBranch;
}

/** The view date of the workspace: one date for every page (URL parameter asOf), default the last day of data. */
export interface KbDates { start: string; end: string; max: string; default: string; timezone: string }

/** One artefact behind a number. `restricted`: the viewer's ACL hides it; it is shown as metadata only when the
 * role has metadata visibility and the item is external correspondence or a business document, else anonymised. */
export interface KbRecord {
  id: string;
  kind: string;
  title: string;
  date: string | null;
  facts: Record<string, string | number | null>;
  restricted?: boolean;
  relatedId?: string | null; // e.g. the decision a reversal replaced
  relatedTitle?: string | null;
  relatedLabel?: string | null; // how the related artefact relates: «replaces», «changed source»
}

export interface KbMetricDef {
  id: string;
  roles: KbRoleId[]; // the MVP: ['ceo'] on every metric (the CEO covers all)
  area?: KbAreaId; // the area of the brief that shows the metric
  title: string;
  question: string;
  definition: string;
  howCalculated: string[];
  unit: KbUnit;
  direction: 'higher' | 'lower' | 'context';
  target: number | null;
  bands: { good: number; warn: number } | null; // in metric units, read with `direction`
  view: 'kpi' | 'list' | 'breakdown';
  sources: string[];
  itemsLabel?: string; // heading of the records behind the number, when «Behind the number» does not fit
  reference?: KbMetricRef; // where the definition comes from (src/variator/metrics/catalog/core.py MetricRef)
}

/** The source of a metric's definition: a public page (`url`), or a VARIATOR definition with a `note` saying why. */
export interface KbMetricRef { name: string; url: string | null; note: string }

/** How to show a value when the number alone misleads (src/variator/metrics/results.py KbDisplay). `value`: the text
 * that replaces the number («60+ mo» for a runway at its cap); `label`: the title to show instead; `negate`: the
 * number, its series and its change are shown negated (a negative burn as the net cash flow); `note`: why (tooltip). */
export interface KbDisplay { value?: string; label?: string; note?: string; negate?: boolean }

export interface KbMetricResult extends KbMetricDef {
  asOf: string;
  value: number | null;
  status: KbStatus;
  previous: number | null; // one week earlier
  series: { date: string; value: number | null }[]; // weekly, oldest first
  breakdown: { key: string; label: string; value: number }[];
  items: KbRecord[];
  itemsTotal: number;
  restrictedTotal: number;
  sample: number | null; // records behind a median or rate
  caveats: string[];
  baseline?: number | null; // the metric's own usual level (e.g. an 8-week average), drawn as a reference line
  display?: KbDisplay;
}

/** One area of the brief: its title, the question it answers and its metric ids in order (top-row metrics left out). */
export interface KbBriefArea { id: KbAreaId; title: string; question: string; metrics: string[] }

/** The brief of the role. `metrics` holds every metric once; `top` (the key numbers, 4–6) and `areas` place them on the
 * page. The workspace computes it from the knowledge base; the VARIATOR metric catalogue writes the same shape
 * (`src/variator/metrics/results.py`, `KbBrief`). */
export interface KbBrief {
  organization: KbOrganization;
  role: KbRole;
  asOf: string;
  range: { start: string; end: string };
  weeks: string[]; // as-of dates the stepper offers (weekly, oldest first)
  headline: string[];
  metrics: KbMetricResult[];
  top: string[];
  areas: KbBriefArea[];
  builtAt: string;
  watermark: number | null;
  branch?: KbBranch; // the brief of a simulated future (never real history)
}

/** One dossier opened before the current one in the sheet: a step of its breadcrumbs. */
export interface KbTrailStep { id: string; title: string }
export interface KbDossierLink { linkId: string; predicate: string; label: string; inferred: boolean; status: string; quote: string | null; other: { id: string; title: string; kind: string; date: string | null } }
export interface KbDossier {
  id: string;
  kind: string;
  title: string;
  url: string | null;
  source: string | null;
  created: string | null;
  updated: string | null;
  facts: { label: string; value: string }[];
  people: { role: string; name: string }[];
  excerpt: string;
  history: { seq: number; at: string; actor: string | null; changed: string[] }[];
  why: KbDossierLink[];
  effects: KbDossierLink[];
  chains: string[];
  /** The objects related to this one, by relation and kind: a person's tasks, incidents on call, merge requests and
   * meetings; a client's invoices and tickets; what this object links to. */
  related: KbDossierGroup[];
  asOf: string | null; // the view date: the dossier shows what was known at the end of that day
  versionsLater: number; // versions recorded after the view date (not shown)
  /** A client or vendor: since when it works with the company («Client since», the earliest contract start). */
  since?: { label: string; date: string } | null;
}
/** One object of a related group: its own dossier opens by `id`; `via`: the relation («assignee»). */
export interface KbDossierRelated { id: string; title: string; kind: string; date: string | null; status: string | null; via: string }
/** «Assigned: tasks»: every object once, newest first; `items` holds at most 50 of the `total`. */
export interface KbDossierGroup { key: string; label: string; kind: string; total: number; items: KbDossierRelated[] }

/** What the knowledge base understood: the plan of the question over the semantic graph (kb.reasoning.plan). */
export interface KbAskPlan {
  planner: string; operation: string; kinds: string[]; understood: string; text: string;
  entities: { text: string; id: string; label: string; kind: string }[];
  relations: { from: string; predicate: string; to: string; negate: boolean }[];
  time: { start: string | null; end: string | null; event: string; knownAt: string | null; interpretation: string | null };
  notes: string[];
}
export interface KbAskAnswer {
  answer: string; mode: string; intent: string | null; citations: { id: string; title: string; kind: string; versionId: string }[]; unknowns: string[]; latencyMs: number;
  asOf: string; now: string; knownAt: string; plan: KbAskPlan | null; total: number | null; shown: number | null;
  /** A «why» answer for people (results.why of the knowledge base): the subject, its declared chains of causes
   * (effect first) and the root causes; null when the question is not a «why» or nothing was grounded. */
  why: KbWhy | null;
}
/** One object of a «why» answer: its entity (the dossier opens by `id`), title, kind label, date and actor; `via`: the
 * relation from the previous step («caused by», «implements»). */
export interface KbWhyNode { id: string; title: string; kind: string; date: string | null; actor: string | null; via: string | null; statement: string | null }
export interface KbWhy { subject: KbWhyNode; chains: KbWhyNode[][]; roots: KbWhyNode[] }

export interface KbSourceStatus {
  tenant: string; reachable: boolean; watermark: number | null; projections: Record<string, number>; entitiesByKind: Record<string, number>; sources: { id: string; provider: string; name: string }[]; causal: Record<string, number>; error?: string;
  asOf: string | null; // the date the record counts are computed for
  causalAsOf: string | null; // null: link statuses are the current ones (the knowledge base has no date parameter for them yet)
}
