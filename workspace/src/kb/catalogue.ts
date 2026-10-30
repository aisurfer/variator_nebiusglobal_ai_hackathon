// The VARIATOR metric catalogue (src/variator/metrics/catalog) as the source of the CEO brief: its artifact sets are
// read through the VARIATOR API proxy (/api/variator/artifacts/<set>/<file>). A set holds the CEO brief
// (briefs/ceo.json, a KbBrief) and one file per metric ({ metric, result, params }: the card, the drill-down and the
// parameters). The brief uses a set only when its as-of date is the view date (nothing after the view date may
// count); otherwise the page falls back to the brief the workspace computes from the knowledge base. The company reads
// the history sets (kind `history-metrics`); a simulated future reads only its own set (kind `branch-metrics`,
// `tenant` its branch tenant, WP19), never the history.
import { useEffect, useState } from 'react';
import type { MetricResult, MetricSpec } from '../genui/contracts';
import { CONTRACT_VERSION } from '../genui/contractVersion';
import { plainFormula, plainLabel } from '../genui/primitives/format';
import type { KbBrief, KbMetricResult, KbOrganization } from './types';
import { formatDay, kbBranch } from './api';
import { apiUrl } from '../appUrl';
import { earlyFetch } from '../early';

/** The list of artifact sets; a file of a set is under it. */
export const API = apiUrl('/api/variator/artifacts');
const TIMEOUT_MS = 6000;

/** An artifact set with a CEO brief (manifest schema 2 of `variator.metrics.catalog.artifacts`). */
export interface CatalogueSet {
  id: string;
  /** null: the company's history; else the branch tenant of a simulated future */
  tenant: string | null;
  asOf: string;
  builtAt: string;
  briefFile: string;
  /** metric id -> its file in the set */
  metrics: Record<string, string>;
}
export interface CatalogueMetric { metric: KbMetricResult; result: MetricResult; params?: CatalogueParams | null }
export interface CatalogueParams { target?: number | null; bands?: { good: number; warn: number } | null; window_days?: number | null; options?: Record<string, unknown> }

/** The sets of a `GET /api/variator/artifacts` answer that carry a CEO brief for the company (`branch` null: the
 * history sets) or for one simulated future (its own branch set only); other sets are left out. */
export function catalogueSets(body: unknown, branch: string | null = null): CatalogueSet[] {
  const sets = (body as { sets?: unknown[] } | null)?.sets;
  if (!Array.isArray(sets)) return [];
  return sets.flatMap(raw => {
    const s = raw as { id?: unknown; manifest?: Record<string, unknown> };
    const m = s.manifest;
    if (typeof s.id !== 'string' || !m || Number(m.schema) < 2) return [];
    const tenant = typeof m.tenant === 'string' ? m.tenant : typeof (m.history as { tenant?: unknown } | undefined)?.tenant === 'string' ? String((m.history as { tenant: string }).tenant) : null;
    if (branch ? m.kind !== 'branch-metrics' || tenant !== branch : m.kind !== 'history-metrics') return [];
    const brief = m.brief as { role?: unknown; file?: unknown } | undefined;
    if (!brief || brief.role !== 'ceo' || typeof brief.file !== 'string' || typeof m.asOf !== 'string') return [];
    const metrics = Object.fromEntries(Object.entries((m.metrics ?? {}) as Record<string, { file?: unknown }>)
      .filter(([, v]) => typeof v?.file === 'string').map(([k, v]) => [k, String(v.file)]));
    return [{ id: s.id, tenant: branch, asOf: m.asOf, builtAt: String(m.builtAt ?? ''), briefFile: brief.file, metrics }];
  });
}

/** The set for a view date: its as-of date must be the view date; the newest build wins. */
export function pickSet(sets: CatalogueSet[], viewDate: string | null | undefined): CatalogueSet | null {
  if (!viewDate) return null;
  return sets.filter(s => s.asOf === viewDate).sort((a, b) => b.builtAt.localeCompare(a.builtAt))[0] ?? null;
}

/** Why the page shows the knowledge-base brief instead of the catalogue brief (a quiet note), or null. */
export function fallbackNote(sets: CatalogueSet[] | null, viewDate: string | null | undefined, error = ''): string | null {
  const b = kbBranch();
  if (b) {
    const own = sets?.length ? ` (the catalogue brief of this future is computed as of ${[...new Set(sets.map(s => s.asOf))].sort().map(shortDate).join(', ')})` : '';
    return `A simulated future (${b.label}): this brief is computed from its own branch of the knowledge base, never from real history${own}.`;
  }
  const kb = 'this brief is computed from the knowledge base';
  if (error) return `The VARIATOR metric catalogue could not be read (${error}), so ${kb}.`;
  if (!sets?.length) return `The VARIATOR metric catalogue is not available, so ${kb}.`;
  if (pickSet(sets, viewDate)) return null;
  const dates = [...new Set(sets.map(s => s.asOf))].sort().map(shortDate);
  return `The VARIATOR metric catalogue is computed as of ${dates.join(', ')}; for this date ${kb}.`;
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const shortDate = (iso: string) => { const [, m, d] = iso.split('-'); return `${Number(d)} ${MONTHS[Number(m) - 1] ?? m}`; };

/** A brief of a simulated future as people read it: it says first that it is simulated (a catalogue brief does not
 * know it is), shows only the metrics with data in that future and only the weeks from its fork date on. */
export function onBranch(b: KbBrief, org: KbOrganization, tenant = kbBranch()?.tenant): KbBrief {
  const branch = b.branch ?? org.branches?.find(x => x.tenant === tenant);
  if (!branch) return b;
  const said = b.headline[0]?.startsWith('Simulated future');
  const metrics = b.metrics.filter(m => m.value !== null);
  const kept = new Set(metrics.map(m => m.id));
  return {
    ...b, branch, metrics,
    headline: said ? b.headline : [`Simulated future: ${branch.label}, simulated from ${formatDay(branch.from)}. Not real history.`, ...b.headline],
    top: b.top.filter(id => kept.has(id)),
    areas: b.areas.map(a => ({ ...a, metrics: a.metrics.filter(id => kept.has(id)) })).filter(a => a.metrics.length > 0),
    weeks: b.weeks.filter(w => w >= branch.from),
  };
}

/** A brief file of the catalogue, checked for the parts the page reads. */
export function asBrief(body: unknown): KbBrief {
  const b = body as KbBrief;
  if (!b || !Array.isArray(b.metrics) || !Array.isArray(b.top) || !Array.isArray(b.areas) || !Array.isArray(b.headline) || typeof b.asOf !== 'string')
    throw new Error('the brief file has an unknown shape');
  return b;
}

/** The definition and the result the drill-down renders, from a metric file of the catalogue. Catalogue metrics
 * are code, not a DSL formula: the spec names the metric (`op: 'metric'`) and the lineage carries the formulas. */
export function drilldownOf(a: CatalogueMetric, organizationId: string): { spec: MetricSpec; result: MetricResult } {
  const m = a.metric, r = a.result, p = a.params ?? null;
  const days = p?.window_days && p.window_days >= 1 && p.window_days <= 366 ? Math.round(p.window_days) : null;
  const spec: MetricSpec = {
    id: m.id, contractVersion: CONTRACT_VERSION, version: r.specVersion, organizationId, role: 'ceo', status: 'active',
    origin: { kind: 'seed', messageIds: [] }, createdAt: r.computedAt, updatedAt: r.computedAt,
    title: m.title, businessQuestion: m.question, plainDefinition: m.definition, howCalculated: m.howCalculated.length ? m.howCalculated : ['—'],
    formula: { op: 'metric', metricId: m.id, label: r.lineage.formulaText ?? m.title },
    unit: r.unit, direction: m.direction,
    target: m.target !== null ? { value: m.target, basis: 'parameter of the metric catalogue' } : null,
    bands: m.bands, window: days ? { kind: 'rolling', days } : { kind: 'all' },
    scope: { kind: 'organization', employeeId: null, projectIds: [] },
    breakdown: null,
    highlight: r.highlight.length ? { label: m.itemsLabel ?? 'Behind the number', expr: { op: 'const', value: r.highlight.length, label: m.itemsLabel ?? 'Behind the number' } } : null,
    cadence: 'weekly', comparison: 'both',
    visualization: { preferred: 'kpi', alternatives: ['trend'], rationale: 'The metric catalogue brief' },
    dataSources: m.sources, reference: m.reference, assumptions: [], openQuestions: [], limitations: m.caveats, confidence: 'high',
  };
  return { spec, result: r };
}

/** The parameters of a catalogue metric for people: target, bands, window and the options of its formula. */
export function parameterRows(p: CatalogueParams | null | undefined, show: (v: number) => string): { label: string; value: string }[] {
  if (!p) return [];
  const rows: { label: string; value: string }[] = [];
  if (p.target != null) rows.push({ label: 'Target', value: show(p.target) });
  if (p.bands) rows.push({ label: 'Bands', value: `good ${show(p.bands.good)} · watch ${show(p.bands.warn)}` });
  if (p.window_days != null) rows.push({ label: 'Window', value: `${p.window_days} days` });
  for (const [k, v] of Object.entries(p.options ?? {})) {
    const value = v !== null && typeof v === 'object' ? Object.entries(v as Record<string, unknown>).map(([a, b]) => `${a} ${b}`).join(' · ') : String(v);
    rows.push({ label: k.replace(/_/g, ' '), value });
  }
  return rows;
}

/** Steps of the lineage for people: every operation and aggregate with its formula or explanation. */
export function lineageSteps(r: MetricResult): string[] {
  const out: string[] = [];
  const walk = (n: MetricResult['lineage'], depth: number) => {
    if (depth > 0 && (n.kind === 'operation' || n.kind === 'aggregate' || n.kind === 'component')) out.push(`${plainLabel(n.label)}: ${plainFormula(n) ?? n.explanation}`);
    if (depth < 3) n.children.forEach(c => walk(c, depth + 1));
  };
  walk(r.lineage, 0);
  return out.slice(0, 12);
}

// ------------------------------------------------------------------------------------------------ fetching
const files = new Map<string, Promise<unknown>>();
async function getJson(url: string): Promise<unknown> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    // the first view starts the list and the brief file before this code has arrived (src/firstView.ts)
    const res = await earlyFetch(url, { signal: ctl.signal });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      // the workspace proxy answers { error: text }, the VARIATOR API { error: { status, message } }
      const err = (body as { error?: unknown } | null)?.error;
      throw new Error((typeof err === 'string' ? err : (err as { message?: string } | undefined)?.message) || `HTTP ${res.status}`);
    }
    return body;
  } catch (e) {
    throw new Error((e as Error)?.name === 'AbortError' ? 'no answer in time' : (e as Error)?.message || 'unreachable');
  } finally { clearTimeout(timer); }
}
/** A file of a set; files of a set do not change, so each is fetched once (a failed fetch is retried). */
function cached(url: string): Promise<unknown> {
  let p = files.get(url);
  if (!p) { p = getJson(url); files.set(url, p); p.catch(() => files.delete(url)); }
  return p;
}
const fileUrl = (set: CatalogueSet, file: string) => `${API}/${encodeURIComponent(set.id)}/${file.split('/').map(encodeURIComponent).join('/')}`;
/** The URL of the brief file of a set. */
export const briefFileUrl = (set: CatalogueSet) => fileUrl(set, set.briefFile);
export const catalogueApi = {
  sets: async (branch: string | null = null) => catalogueSets(await getJson(API), branch),
  /** every set of the API, once per page load: the company's and each branch's sets are taken from it */
  all: () => getJson(API),
  brief: async (set: CatalogueSet) => asBrief(await cached(briefFileUrl(set))),
  metric: async (set: CatalogueSet, id: string) => {
    const file = set.metrics[id];
    if (!file) throw new Error(`The catalogue has no metric ${id}`);
    return await cached(fileUrl(set, file)) as CatalogueMetric;
  },
};

// The list of sets is read once per page load and shared by the brief and the metric sheet; the company takes its
// history sets from it, a simulated future its own branch set only.
let listPromise: Promise<unknown> | null = null;
export type CatalogueState = { status: 'loading' } | { status: 'ready'; sets: CatalogueSet[] } | { status: 'missing'; error: string };
export function useCatalogue(): CatalogueState {
  const branch = kbBranch()?.tenant ?? null;
  const [state, setState] = useState<CatalogueState>({ status: 'loading' });
  useEffect(() => {
    let live = true;
    setState({ status: 'loading' });
    listPromise ??= catalogueApi.all();
    listPromise.then(body => { if (live) setState({ status: 'ready', sets: catalogueSets(body, branch) }); },
      (e: Error) => { listPromise = null; if (live) setState({ status: 'missing', error: e.message }); });
    return () => { live = false; };
  }, [branch]);
  return state;
}
