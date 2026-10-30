// Deterministic morning-brief rules (after Stephen Few): what changed, what needs attention,
// what is stable. Significance is decided here by fixed rules, never by a language model.
import type { DashboardSpec, LineageNode, MetricResult, MetricSpec, MetricStatus, PrimitiveId, RecordRef, WidgetSpec } from '../contracts';
import { formatDelta, formatValue, gbDate, sinceLabel, statusFor, type Tone } from './format';

/** Relative move that counts as "changed" on a morning brief (2 % of the comparison value). */
export const SIGNIFICANT_RELATIVE_CHANGE = 0.02;
/** Widgets shown in "Needs attention" before the rest folds behind "+N more". */
export const ATTENTION_LIMIT = 4;
/** Records per exception list on the brief; the full list is one click away. */
export const BRIEF_RECORD_LIMIT = 3;

export interface ChangeItem {
  metricId: string;
  title: string;
  kind: 'status' | 'delta' | 'records';
  tone: Tone;
  value: string;
  deltaText: string | null;
  statusFrom: MetricStatus | null;
  statusTo: MetricStatus | null;
  added: number;
  removed: number;
  relative: number;
}

const severity: Record<MetricStatus, number> = { bad: 3, warn: 2, good: 1, neutral: 0, unknown: 0 };
const known = (r: MetricResult | undefined): r is MetricResult => !!r && r.value !== null && r.status !== 'unknown';

function relativeMove(value: number | null, previous: number | null): number {
  if (value === null || previous === null) return 0;
  const diff = Math.abs(value - previous);
  if (diff === 0) return 0;
  return diff / Math.max(Math.abs(previous), 1);
}

/** All movements of the selected comparison, ranked: status changes, then relative delta, then record churn. */
export function rankChanges(specs: MetricSpec[], results: Record<string, MetricResult>, compare: 'day' | 'week', limit = 5): ChangeItem[] {
  const items: ChangeItem[] = [];
  for (const spec of specs) {
    const r = results[spec.id];
    if (!known(r)) continue;
    const previous = r.previous[compare];
    const delta = formatDelta(r.value, previous, spec.unit, spec.direction);
    const relative = relativeMove(r.value, previous);
    const base = { metricId: spec.id, title: spec.title, value: formatValue(r.value, spec.unit), deltaText: delta.diff ? delta.text : null, added: r.changes.addedTotal ?? r.changes.added.length, removed: r.changes.removedTotal ?? r.changes.removed.length, relative };
    // Claim a status change only when our band rule reproduces the engine's current status.
    const before = previous === null ? 'unknown' : statusFor(spec, previous);
    if (statusFor(spec, r.value) === r.status && before !== 'unknown' && before !== r.status && (severity[before] || severity[r.status])) {
      const worse = severity[r.status] > severity[before];
      items.push({ ...base, kind: 'status', tone: worse ? 'bad' : 'good', statusFrom: before, statusTo: r.status });
    } else if (relative >= SIGNIFICANT_RELATIVE_CHANGE) {
      items.push({ ...base, kind: 'delta', tone: delta.tone, statusFrom: null, statusTo: null });
    } else if (base.added + base.removed > 0) {
      items.push({ ...base, kind: 'records', tone: 'neutral', statusFrom: null, statusTo: null });
    }
  }
  const order = { status: 0, delta: 1, records: 2 } as const;
  items.sort((a, b) => order[a.kind] - order[b.kind]
    || (a.kind === 'status' ? (b.tone === 'bad' ? 1 : 0) - (a.tone === 'bad' ? 1 : 0) : 0)
    || (a.kind === 'records' ? (b.added + b.removed) - (a.added + a.removed) : b.relative - a.relative)
    || a.title.localeCompare(b.title));
  return items.slice(0, limit);
}

const needsAttention = (r: MetricResult | undefined) => !!r && (r.status === 'bad' || r.status === 'warn');
function moved(spec: MetricSpec | undefined, r: MetricResult | undefined, compare: 'day' | 'week'): boolean {
  if (!spec || !known(r)) return false;
  const previous = r.previous[compare];
  if (previous === null) return false;
  return statusFor(spec, previous) !== r.status && statusFor(spec, r.value) === r.status || relativeMove(r.value, previous) >= SIGNIFICANT_RELATIVE_CHANGE;
}

const TILES = new Set<PrimitiveId>(['kpi', 'kpiTarget', 'scoreGauge']);
export const isTile = (w: WidgetSpec) => TILES.has(w.primitive);
/** Person breakdowns are never ranked or coloured (T06 principle 5: no leaderboards of people). */
export const isPersonBreakdown = (spec: MetricSpec | undefined) => spec?.breakdown?.dimension === 'employee';
/** Display order of breakdown groups: as computed, except people, who are listed alphabetically. */
export function orderBreakdown<T extends { label: string }>(spec: MetricSpec, rows: T[]): { person: boolean; rows: T[] } {
  const person = isPersonBreakdown(spec);
  return { person, rows: person ? [...rows].sort((a, b) => a.label.localeCompare(b.label)) : rows };
}

export interface MorningBrief {
  headline: WidgetSpec[];
  headlineTitle: string;
  attention: WidgetSpec[];
  /** Attention widgets beyond ATTENTION_LIMIT, folded behind "+N more". */
  attentionMore: WidgetSpec[];
  changed: WidgetSpec[];
  stable: WidgetSpec[];
  /** Metric ids that cannot be computed this morning (or are missing); never shown as "stable". */
  unknown: string[];
  /** Widgets left out of the brief because the same metric is already shown (still in "As composed"). */
  repeated: WidgetSpec[];
}

/**
 * The layout's first section is the headline and always stays visible. Each metric then appears at
 * most once as a tile and once as a detail widget (for an index, its component breakdown wins). Every
 * remaining widget goes to one bucket: needs attention (status warn/bad, or an exception list with
 * records), changed (status change or a significant move), unknown (cannot be computed), or stable.
 * Layout order is kept inside a bucket, compact tiles first.
 */
export function arrangeBrief(dashboard: DashboardSpec, specs: MetricSpec[], results: Record<string, MetricResult>, compare: 'day' | 'week'): MorningBrief {
  const [first, ...rest] = dashboard.sections;
  const byId = new Map(specs.map(s => [s.id, s]));
  const brief: MorningBrief = { headline: [], headlineTitle: first?.title ?? 'Today', attention: [], attentionMore: [], changed: [], stable: [], unknown: [], repeated: [] };
  const tileShown = new Set<string>(), detailShown = new Set<string>();
  const later = rest.flatMap(s => s.widgets);
  // For an index, prefer its component breakdown as the one detail widget.
  const preferredDetail = new Map<string, string>();
  for (const w of later) for (const id of w.metricIds) if (w.primitive === 'indexBreakdown' && byId.get(id)?.formula.op === 'index' && !preferredDetail.has(id)) preferredDetail.set(id, w.id);
  const claim = (w: WidgetSpec): boolean => {
    if (w.primitive === 'comparison') return true; // several metrics side by side is an explicit ask
    const slot = isTile(w) ? tileShown : detailShown;
    if (w.metricIds.every(id => slot.has(id))) return false;
    if (!isTile(w) && w.metricIds.some(id => preferredDetail.has(id) && preferredDetail.get(id) !== w.id)) return false;
    w.metricIds.forEach(id => slot.add(id));
    return true;
  };
  for (const w of first?.widgets ?? []) { claim(w); brief.headline.push(w); }
  const unknown = new Set<string>();
  const attention: WidgetSpec[] = [];
  for (const widget of later) {
    if (!claim(widget)) { brief.repeated.push(widget); continue; }
    const rs = widget.metricIds.map(id => results[id]);
    const allUnknown = widget.metricIds.every(id => !byId.get(id) || !known(results[id]));
    if (allUnknown) { widget.metricIds.forEach(id => unknown.add(id)); continue; }
    if (widget.primitive === 'exceptionList' ? rs.some(r => (r?.highlight.length ?? 0) > 0) : rs.some(needsAttention)) attention.push(widget);
    else if (widget.metricIds.some(id => moved(byId.get(id), results[id], compare))) brief.changed.push(widget);
    else brief.stable.push(widget);
  }
  const tilesFirst = (ws: WidgetSpec[]) => ws.map((w, i) => ({ w, i })).sort((a, b) => (isTile(a.w) ? 0 : 1) - (isTile(b.w) ? 0 : 1) || a.i - b.i).map(x => x.w);
  // Attention order: an index's component breakdown (why the headline moved), then at risk before
  // attention, then compact tiles before cards, then layout order. Only the first few stay open.
  const rank = (w: WidgetSpec) => {
    const indexDetail = w.primitive === 'indexBreakdown' ? 0 : 1;
    const worst = Math.max(...w.metricIds.map(id => severity[results[id]?.status ?? 'unknown']));
    return [indexDetail, -worst, isTile(w) ? 0 : 1];
  };
  const sortedAttention = attention.map((w, i) => ({ w, i, r: rank(w) })).sort((a, b) => a.r[0] - b.r[0] || a.r[1] - b.r[1] || a.r[2] - b.r[2] || a.i - b.i).map(x => x.w);
  brief.attention = sortedAttention.slice(0, ATTENTION_LIMIT);
  brief.attentionMore = sortedAttention.slice(ATTENTION_LIMIT);
  brief.changed = tilesFirst(brief.changed);
  brief.stable = tilesFirst(brief.stable);
  // Headline metrics that are unknown are already visible with their reason; list the rest.
  const headlineIds = new Set(brief.headline.flatMap(w => w.metricIds));
  brief.unknown = [...unknown].filter(id => !headlineIds.has(id));
  return brief;
}

/**
 * One deterministic sentence for the top of the morning screen, over every active metric of the
 * workspace. "Within range" counts only metrics in their good band; metrics without bands are said
 * to have no band set. `onLayout` (ids placed on the dashboard) adds how many are not on it.
 */
export function summaryLine(specs: MetricSpec[], results: Record<string, MetricResult>, compare: 'day' | 'week', onLayout?: Set<string>): string {
  const present = specs.filter(s => results[s.id]);
  const total = present.length;
  const count = (f: (r: MetricResult, s: MetricSpec) => boolean) => present.filter(s => f(results[s.id], s)).length;
  const unknown = count(r => !known(r));
  const attention = count(r => needsAttention(r));
  const good = count(r => known(r) && r.status === 'good');
  const noBand = count(r => known(r) && r.status === 'neutral');
  const movedCount = count((r, s) => moved(s, r, compare));
  const ranked = rankChanges(present, results, compare, present.length);
  const recordsOnly = ranked.filter(c => c.kind === 'records').length;
  const since = sinceLabel(compare);
  const plural = (n: number, one: string, many: string) => n === 1 ? one : many;
  let head: string;
  if (attention > 0) head = `${attention} of ${total} measures ${plural(attention, 'needs', 'need')} attention`;
  else if (good > 0) head = good === total ? (total === 1 ? 'The measure is within range' : `All ${total} measures are within range`) : `None of ${total} measures needs attention`;
  else head = total === 1 ? 'The measure needs no attention' : `None of ${total} measures needs attention`;
  let line = movedCount ? `${head}, ${movedCount} moved ${since}` : `${head}, and nothing moved materially ${since}`;
  if (recordsOnly) line += ` (${recordsOnly} more with record changes only)`;
  line += '.';
  const top = ranked.find(c => c.kind !== 'records');
  if (top?.deltaText) line += ` Biggest move: ${top.title} ${top.deltaText} to ${top.value}.`;
  if (attention > 0 && good > 0) line += ` ${good} within range.`;
  if (noBand) line += ` ${noBand} ${plural(noBand, 'has', 'have')} no band set.`;
  if (unknown) line += ` ${unknown} cannot be computed for this morning.`;
  if (onLayout) { const off = present.filter(s => !onLayout.has(s.id)).length; if (off) line += ` ${off} ${plural(off, 'is', 'are')} not on this layout.`; }
  return line;
}

// ---------------------------------------------------------------------------------------------
// Surprises beside the No-Surprises Index (T06.7): absolute numbers, never an invented zero.
// ---------------------------------------------------------------------------------------------
export interface SurpriseFacts { surprises: number; issues: number; outside: number | null; outsideLabel: string | null }

/** Returns false when a node's record list was truncated (dashboard views cap lineage). */
function collectRecords(node: LineageNode, out: Map<string, RecordRef>): boolean {
  if (node.records && node.recordsTotal !== undefined && node.recordsTotal > node.records.length) return false;
  for (const r of node.records ?? []) if (r.dataset === 'issues' && !out.has(r.id)) out.set(r.id, r);
  return node.children.every(child => collectRecords(child, out));
}
const OUTSIDE_VALUES = ['client', 'external'];
/** A companion metric counting issues that reached the executive from clients or outside parties. */
export function outsideSurpriseSpec(specs: MetricSpec[]): { spec: MetricSpec; label: string } | null {
  for (const s of specs) {
    const f = s.formula;
    if (f.op !== 'count' || f.dataset !== 'issues') continue;
    const cond = f.filter.find(c => (c.field === 'execAwareVia' || c.field === 'surfacedBy') && (c.op === 'in' || c.op === 'eq'));
    if (!cond) continue;
    const values = (Array.isArray(cond.value) ? cond.value : [cond.value]).map(String);
    if (!values.includes('client') || values.some(v => !OUTSIDE_VALUES.includes(v))) continue;
    return { spec: s, label: values.includes('external') ? 'clients or outside parties' : 'clients' };
  }
  return null;
}
/**
 * Surprises = issues in the index lineage not raised first by their owner or team (`toldFirstScore` 0,
 * or `execFirst`). "From outside" comes from record fields when the engine includes them, otherwise
 * from a companion metric on the same workspace; when neither exists it is null and not shown.
 * Null overall when there are no such issue records, or the lists were truncated (load the full result).
 */
export function surpriseCounts(result: MetricResult, companions?: { specs: MetricSpec[]; results: Record<string, MetricResult> }): SurpriseFacts | null {
  const records = new Map<string, RecordRef>();
  if (!collectRecords(result.lineage, records)) return null;
  const relevant = [...records.values()].filter(r => 'toldFirstScore' in r.fields || 'execFirst' in r.fields);
  if (!relevant.length) return null;
  const surprise = (r: RecordRef) => 'toldFirstScore' in r.fields ? r.fields.toldFirstScore === 0 : r.fields.execFirst === true;
  const surprises = relevant.filter(surprise);
  const withVia = relevant.filter(r => 'execAwareVia' in r.fields || 'surfacedBy' in r.fields);
  let outside: number | null = null, outsideLabel: string | null = null;
  if (withVia.length === relevant.length) {
    outside = surprises.filter(r => OUTSIDE_VALUES.includes(String(r.fields.execAwareVia ?? r.fields.surfacedBy))).length;
    outsideLabel = 'clients or outside parties';
  } else if (companions) {
    const found = outsideSurpriseSpec(companions.specs);
    const value = found ? companions.results[found.spec.id]?.value ?? null : null;
    if (found && value !== null) { outside = value; outsideLabel = found.label; }
  }
  return { surprises: surprises.length, issues: relevant.length, outside, outsideLabel };
}

export function morningLabel(iso: string): string {
  const d = new Date(`${iso}T12:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  // day before month, as in the rest of the app: «Thu 18 Jun 2026»
  const part = (o: Intl.DateTimeFormatOptions) => gbDate(d, { timeZone: 'UTC', ...o });
  return `${part({ weekday: 'short' })} ${part({ day: 'numeric' })} ${part({ month: 'short' })} ${part({ year: 'numeric' })}`;
}

/** Neighbouring morning in `dates` (sorted ascending), or null at the ends. */
export function stepDate(dates: string[], current: string, dir: -1 | 1): string | null {
  const sorted = [...dates].sort();
  if (dir < 0) return [...sorted].reverse().find(d => d < current) ?? null;
  return sorted.find(d => d > current) ?? null;
}

/** The comparison morning of a result, only if it exists in the result's own history (never a 400). */
export function comparedMorning(result: MetricResult, compare: 'day' | 'week'): string | null {
  const dates = result.series.map(p => p.date).filter(d => d < result.asOf).sort();
  if (!dates.length) return null;
  if (compare === 'day') return dates.at(-1)!;
  const target = new Date(`${result.asOf}T12:00:00Z`); target.setUTCDate(target.getUTCDate() - 7);
  const iso = target.toISOString().slice(0, 10);
  return dates.includes(iso) ? iso : null;
}
