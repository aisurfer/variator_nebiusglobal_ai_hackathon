// WP24: the Day section's data, pure (no React, no fetch: tests/case-day.test.ts).
// - examplesFor: the three example futures of a fork from WP19's day file (schema variator.day/1), else, while the
//   whole page is on the preview, the preview's; never the week-tree bridge (its three leaves share their first weeks,
//   so its example days came out identical) and never identical examples;
// - eventView: an event of the data in people's words («A competitor cuts prices by about 13%»), with its group, its
//   tone and, for an incident, its severity, duration, clients and the person on call;
// - day selection: the events of a day, the next and the previous day of the series;
// - the metric choice: up to four of the day metrics the data provides (cash, ARR, load and incidents by default);
// - kbTarget: where an entity link opens (the dossier on the example's branch, else the company history).
import { money, noEmoji, pct } from '../../engine/format';
import { dayMonth, longDate as appLongDate, weekdayDate } from '../ui/format';
import type { ActorDayEvent, ActorDayExample, DayFile, ExampleKind } from '../actors/types';
import type { DayExample as PreviewDayExample } from '../model/types';
import { branchLink } from '../actors/branch';

// ------------------------------------------------------------------------------------------------ types
export type DayKind = ExampleKind;
export interface DayMetric {
  id: string; label: string; unit: string; higher: boolean | null; bars: boolean; description?: string; catalogue?: boolean;
  /** The catalogue's display hint: shown with its sign turned (a burn as a net cash flow), a gain with «+». */
  negate?: boolean; signed?: boolean;
}
/** The display hint of a catalogue metric (as the brief reads it, KbDisplay). */
export interface DisplayHint { label?: string; negate?: boolean; note?: string }
export type EventGroup = 'incident' | 'client' | 'payment' | 'people' | 'decision' | 'shock' | 'other';
export interface DayItem {
  /** Index in the example's events (stable key). */
  i: number;
  /** ISO date; days after the fork date. */
  date: string; day: number;
  type: string; group: EventGroup;
  title: string; text: string; tone: 'good' | 'bad' | 'event';
  /** The record of the event (a knowledge-base entity of the example's branch, or of the company directory). */
  entityId?: string;
  /** The person of the event (for an incident: the one on call), the name and the dossier id. */
  personId?: string; person?: string; personEntity?: string;
  severity?: string; minutes?: number; clients?: number; service?: string; cause?: string;
}
export interface DayPoint { date: string; day: number; values: Record<string, number> }
export interface DayExampleData {
  id: DayKind; label: string;
  /** The knowledge-base branch tenant of this future (WP19); `kb` false: records only, not in the knowledge base. */
  tenant?: string; kb?: boolean; percentile?: number;
  /** Checked why-questions of the branch (WP19), the incidents ones first; at most four. */
  ask: string[];
  /** Where the example's cash falls among all the scenarios of the fork at the end of its window (WP19's
   *  `percentileAtWindowEnd`, 0–100), among how many, and that date; unknown until the data says it. */
  truePercentile?: number; ensembleN?: number; endDate?: string;
  headline: { value: number; unit: string; label: string };
  outcome: string;
  points: DayPoint[];
  items: DayItem[];
  /** A short fingerprint of the series and the events: two examples with one fingerprint show the same days. */
  digest: string;
}
export type DaySource = 'day-file' | 'preview' | 'none';
export interface People { [id: string]: { name: string; entity?: string } }

// ------------------------------------------------------------------------------------------------ dates
const MS_DAY = 864e5;
const at = (iso: string) => new Date(`${iso}T12:00:00Z`);
export const isoAdd = (iso: string, days: number) => { const t = at(iso); t.setUTCDate(t.getUTCDate() + days); return t.toISOString().slice(0, 10); };
export const daysBetween = (from: string, to: string) => Math.round((at(to).getTime() - at(from).getTime()) / MS_DAY);
/** «12 Nov». */
export const shortDate = dayMonth;
/** «Thu, 12 Nov 2026». */
export const longDate = weekdayDate;
/** «26 Apr 2027». */
const endDate = appLongDate;

// ------------------------------------------------------------------------------------------------ metrics
// Two kinds of day metrics (WP19): the run's own series (`cash`, `load`, `incidents`, …: the state of each day) and the
// CEO brief's catalogue metrics computed on the example's records (`fin.runway`, `rev.churn`, …: often over a rolling
// window). A catalogue metric that is the same quantity as a run series is left out (one «Cash», one «ARR»).
/** The day charts by default: these four, in this order (the rest by relevance after them). */
export const DAY_DEFAULT = ['cash', 'arr', 'load', 'incidents'];
const RELEVANCE = [...DAY_DEFAULT, 'availability', 'fin.runway', 'fin.burn', 'rev.churn', 'rev.new_arr', 'cust.nps', 'fin.gross_margin', 'fin.dso',
  'headcount', 'ops.sla', 'ops.mttr', 'ops.deploys', 'ops.incidents', 'p95_ms', 'ops.latency', 'fin.revenue', 'fin.costs', 'fin.budget_variance', 'fin.taxes', 'ops.open_requisitions',
  'active_clients', 'churned_arr', 'receivables_overdue', 'deploys', 'support_tickets'];
export const MAX_METRICS = 4;
/** Catalogue metrics that repeat a run series. */
const SAME_AS: Record<string, string> = { 'fin.cash': 'cash', 'rev.arr': 'arr', 'ppl.headcount': 'headcount' };
/** A catalogue metric (of the CEO brief) rather than a series of the run. */
export const isCatalogue = (m: { id: string; source?: string }) => !!m.source || m.id.includes('.');

/** A label or a description without the catalogue's template fields: «Incidents · {window_days} days» -> «Incidents · rolling». */
export function cleanText(s: string, label = false): string {
  const t = noEmoji(s);
  return label
    ? t.replace(/\s*·\s*\{window_days\}\s*days?/g, ' · rolling').replace(/\s*\{[^}]+\}/g, '').trim()
    : t.replace(/the last \{window_days\} days/g, 'a rolling window').replace(/\{window_days\} days/g, 'a rolling window').replace(/\{[^}]+\}/g, '…');
}

/** The brief's hint for a metric whose day file does not carry one yet: the burn reads as a net cash flow. */
const DISPLAY_FALLBACK: Record<string, DisplayHint> = { 'fin.burn': { label: 'Net cash flow per month', negate: true } };
type MetricIn = { id: string; label: string; unit: string; higher?: boolean | null; description?: string; source?: string; display?: DisplayHint | null };

export function metricDef(m: MetricIn): DayMetric {
  const catalogue = isCatalogue(m);
  const d = m.display ?? DISPLAY_FALLBACK[m.id] ?? {};
  const higher = m.higher ?? null, desc = m.description ? cleanText(m.description) : undefined;
  // a count of the day (incidents that started that day) is a bar; a catalogue count is a level over its window
  return {
    id: m.id, label: cleanText(d.label ?? m.label, true), unit: m.unit, higher: d.negate && higher != null ? !higher : higher, bars: m.unit === 'count' && !catalogue, catalogue,
    description: d.negate ? `${desc ? `${desc} ` : ''}Shown with its sign turned: money in minus money out.` : desc,
    ...(d.negate ? { negate: true, signed: true } : {}),
  };
}
/** The metrics of a day file: duplicates of the run series left out, and a catalogue label that repeats a run label
 *  marked as rolling («p95 latency · rolling»). */
export function dayMetrics(defs: MetricIn[]): DayMetric[] {
  const all = defs.map(metricDef), run = new Set(all.filter(m => !m.catalogue).map(m => m.id));
  const runLabels = new Set(all.filter(m => !m.catalogue).map(m => m.label.toLowerCase()));
  return all.filter(m => !(m.catalogue && run.has(SAME_AS[m.id] ?? '')))
    .map(m => m.catalogue && runLabels.has(m.label.toLowerCase()) ? { ...m, label: `${m.label} · rolling` } : m);
}
const rank = (id: string, order: string[]) => { const r = RELEVANCE.indexOf(id); return r >= 0 ? r : RELEVANCE.length + order.indexOf(id); };
/** Metric ids in the order of the charts: by relevance, then in the order of the data. */
export const byRelevance = (ids: string[], order: string[]) => [...ids].sort((a, b) => rank(a, order) - rank(b, order));
/** The four most relevant of the metrics the data has. */
export const defaultMetricIds = (available: string[]) => byRelevance(available, available).slice(0, MAX_METRICS);
/** The viewer's saved choice, kept to what the data has; the default when nothing of it is left. */
export function pickMetrics(saved: readonly unknown[] | null | undefined, available: string[]): string[] {
  const keep = (saved ?? []).filter((x): x is string => typeof x === 'string' && available.includes(x));
  return keep.length ? byRelevance([...new Set(keep)], available).slice(0, MAX_METRICS) : defaultMetricIds(available);
}
/** Turns a metric on or off: at least one and at most four stay on. */
export function toggleMetric(current: string[], id: string, available: string[]): string[] {
  if (current.includes(id)) return current.length > 1 ? current.filter(x => x !== id) : current;
  if (current.length >= MAX_METRICS || !available.includes(id)) return current;
  return byRelevance([...current, id], available);
}

/** A value of a day metric for people: money with $, shares and the team load in %, counts whole. */
export function dayValue(m: Pick<DayMetric, 'id' | 'unit'> & { signed?: boolean }, v: number | null | undefined, opts: { axis?: boolean } = {}): string {
  if (v == null || !Number.isFinite(v)) return '—';
  if (m.signed && v > 0) return `+${dayValue({ id: m.id, unit: m.unit }, v, opts)}`;
  switch (m.unit) {
    case 'USD': case 'USD/year': case 'money': return money(v);
    case 'percent': return `${v < 0 ? '−' : ''}${Math.abs(v).toFixed(Math.abs(v) >= 99 && Math.abs(v) < 100 ? 2 : Math.abs(v) >= 10 || opts.axis ? 0 : 1)}%`;
    case 'months': return v >= 59.95 ? '60+ mo' : `${v.toFixed(v < 10 ? 1 : 0)} mo`;
    case 'index': return v.toFixed(0);
    case 'share': return `${(v * 100).toFixed(opts.axis ? (v > 0.99 ? 2 : 0) : v >= 0.95 && v < 1 ? 2 : 1)}%`;
    case 'ratio': return m.id === 'load' ? `${Math.round(v * 100)}%` : v.toFixed(2);
    case 'ms': return `${Math.round(v)} ms`;
    case 'days': return `${Math.round(v)} d`;
    case 'hours': return v < 1 && !opts.axis ? `${Math.round(v * 60)} min` : `${v.toFixed(1)} h`;
    case 'points': case 'score': return v.toFixed(0);
    default: return Number.isInteger(v) || Math.abs(v) >= 10 ? String(Math.round(v)) : v.toFixed(1);
  }
}

// ------------------------------------------------------------------------------------------------ events
const GROUP_OF: Record<string, EventGroup> = {
  incident: 'incident', client: 'client', payment: 'payment', people: 'people', hire: 'people', decision: 'decision',
  shock: 'shock', vendor: 'shock', signal: 'other', milestone: 'other',
};
export const GROUP_LABEL: Record<EventGroup, string> = {
  incident: 'Incidents', client: 'Clients', payment: 'Payments', people: 'Hires and exits', decision: 'Decisions', shock: 'Shocks', other: 'Other',
};
const TITLE: Record<EventGroup, string> = { incident: 'Incident', client: 'Client', payment: 'Payment', people: 'People', decision: 'Decision', shock: 'Shock', other: 'Signal' };
export const GROUP_ORDER: EventGroup[] = ['incident', 'client', 'payment', 'people', 'decision', 'shock', 'other'];

/** «94 min», «1 h 34 min». */
export const duration = (min: number) => min < 60 ? `${Math.round(min)} min` : `${Math.floor(min / 60)} h${min % 60 ? ` ${Math.round(min % 60)} min` : ''}`;

const INCIDENT = /^(SEV\d)\s+incident(?::\s*(\S+)\s+(.+?))?(?:\s*\((\d+)\s+clients?,\s*(\d+)\s*min\))?\s*$/i;
const SIZE = /^(.*?):\s*size\s+(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*$/i;
const MONEY_K = String.raw`\$?([\d.,]+[kKM]?)`;

/** A shock of the formula's risk list with its drawn size («A competitor cuts prices: size 0.127»), in words. */
export function shockText(label: string, size: number): string {
  const L = label.trim(), a = Math.abs(size);
  if (a >= 1000) return `${L}: about ${money(a)}`;
  if (/GPU/i.test(L)) return `${L}: about ${pct(a)} of the GPU capacity is lost`;
  if (/key account/i.test(L)) return `${L}: about ${pct(a)} of ARR`;
  if (/reputation/i.test(L)) return `${L}: churn up by about ${pct(a)}`;
  if (/demand|downturn/i.test(L)) return `${L} by about ${pct(a)}`;
  if (/price|cost|currency|raises|cuts/i.test(L)) return `${L} by about ${pct(a)}`;
  return a <= 1.5 ? `${L} (about ${pct(a)})` : `${L} (size ${+a.toFixed(2)})`;
}

/** An event text of the data in people's words, by its type (the engine's machine forms become sentences). */
export function peopleEventText(type: string, raw: string): string {
  let t = noEmoji(raw).trim();
  const size = SIZE.exec(t);
  if (size) return shockText(size[1], Number(size[2]));
  // a load signal: «Engineering load over capacity in Nov 2026: 1.07» -> «…: 107% of capacity»
  t = t.replace(/(over capacity[^:]*):\s*(\d+(?:\.\d+)?)$/i, (_m, head: string, v: string) => `${head}: ${Math.round(Number(v) * 100)}% of capacity`);
  if (type === 'client' || type === 'payment') {
    t = t.replace(new RegExp(String.raw`^Deal won:\s*(.+?),\s*ACV\s*${MONEY_K}$`), (_m, who: string, v: string) => `New client ${who} signs: $${v} a year`)
      .replace(new RegExp(String.raw`gives notice:\s*ACV\s*${MONEY_K}$`), (_m, v: string) => `gives notice: $${v} a year`)
      .replace(new RegExp(String.raw`buys more:\s*\+${MONEY_K}\s*ACV$`), (_m, v: string) => `buys more: +$${v} a year`);
  }
  return t.replace(new RegExp(String.raw`ACV\s*${MONEY_K}`, 'g'), (_m, v: string) => `$${v} a year`).replace(/\s{2,}/g, ' ');
}

function toneOf(group: EventGroup, text: string): DayItem['tone'] {
  const s = text.toLowerCase();
  if (group === 'incident' || group === 'shock') return 'bad';
  if (/gives notice|leaves|resigns|pay late|pays late|reduces|rose|fell in .* by \$|over capacity|hands in notice|outage/.test(s)) return 'bad';
  if (/signs|buys|joins|pays inv|incidents fell|shows an effect|hire/.test(s)) return 'good';
  return 'event';
}

/** One event of an example for the page. */
/** An event as WP19 writes it: a sentence for people, and for some types the facts behind it (`raw`). */
export type RawDayEvent = ActorDayEvent & {
  personName?: string; minutes?: number; durationMin?: number; clients?: number; service?: string;
  raw?: { severity?: string; component?: string; symptom?: string; clients?: number; restore_minutes?: number; cause?: string; [k: string]: unknown };
};
/** «SEV3 incident on ML inference, restored in 0.3 h (on call: Rhys Llewellyn)» */
const INCIDENT_SENTENCE = /^SEV\d\s+incident on (?:the )?(.+?),\s*restored in [^(]*(?:\(on call:\s*([^)]+)\))?/i;

/** One event of an example for the page. */
export function eventView(ev: RawDayEvent, i: number, forkDate: string, people: People = {}): DayItem {
  const group = GROUP_OF[ev.type] ?? 'other';
  const text = peopleEventText(ev.type, ev.text);
  const pid = ev.person, who = pid ? people[pid] : undefined;
  const base: DayItem = {
    i, date: ev.date, day: daysBetween(forkDate, ev.date), type: ev.type, group, title: TITLE[group], text, tone: toneOf(group, text),
    entityId: ev.entityId, personId: pid, person: ev.personName ?? who?.name, personEntity: who?.entity ?? (pid && /^[A-Z]{2}-E\d+$/i.test(pid) ? `person.${pid.toLowerCase()}` : undefined),
  };
  if (group === 'incident') {
    // the facts: WP19's `raw` first, then fields of the event, then the older machine text («SEV3 incident: api … (2 clients, 15 min)»)
    const r = ev.raw ?? {}, m = INCIDENT.exec(text), s = INCIDENT_SENTENCE.exec(text);
    const severity = (r.severity ?? ev.severity ?? m?.[1] ?? /^(SEV\d)/i.exec(text)?.[1])?.toUpperCase();
    const service = s?.[1] ?? ev.service ?? r.component ?? m?.[2];
    const what = r.symptom ?? m?.[3];
    const clients = r.clients ?? ev.clients ?? (m?.[4] ? Number(m[4]) : undefined);
    const minutes = r.restore_minutes ?? ev.minutes ?? ev.durationMin ?? (m?.[5] ? Number(m[5]) : undefined);
    const person = base.person ?? s?.[2]?.trim();
    return { ...base, person, title: severity ? `${severity} incident` : 'Incident', text: service && what ? `${service}: ${what}` : service ? service : m ? '' : text,
      severity, service, clients, minutes, cause: typeof r.cause === 'string' ? r.cause : undefined };
  }
  // «Decision: hire 5 engineers» -> title Decision, text «Hire 5 engineers»
  const lead = /^(Decision|Signal|Shock):\s*/i.exec(text);
  return lead ? { ...base, text: text.slice(lead[0].length).replace(/^./, c => c.toUpperCase()) } : base;
}

// ------------------------------------------------------------------------------------------------ examples
const ORDER: DayKind[] = ['pessimistic', 'median', 'optimistic'];
const KIND_LABEL: Record<DayKind, string> = { pessimistic: 'Pessimistic', median: 'Median', optimistic: 'Optimistic' };

/** FNV-1a of a string, 8 hex digits. */
function fnv(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  return h.toString(16).padStart(8, '0');
}
export const digestOf = (points: DayPoint[], items: DayItem[]) =>
  fnv(JSON.stringify([points.map(p => [p.date, p.values]), items.map(e => [e.date, e.type, e.text])]));

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
/** «28 incidents · 3 client notices · 2 new clients»: what the example's lane holds, the three largest parts. */
export function outcomeOf(items: DayItem[]): string {
  const n = (f: (e: DayItem) => boolean) => items.filter(f).length;
  const parts: [number, string][] = [
    [n(e => e.group === 'incident'), 'incident'],
    [n(e => e.group === 'client' && /gives notice/i.test(e.text)), 'client notice'],
    [n(e => e.group === 'client' && /signs/i.test(e.text)), 'new client'],
    [n(e => e.group === 'people' && /resigns|leaves|hands in notice/i.test(e.text)), 'exit'],
    [n(e => e.group === 'people' && /joins/i.test(e.text)), 'hire'],
  ];
  return parts.filter(([k]) => k > 0).slice(0, 3).map(([k, w]) => plural(k, w)).join(' · ');
}

/** WP19's examples of one fork for the page. */
/** A day example as WP19 writes it (variator.day/1): `kb` and the branch's checked questions besides the actor fields. */
export type RawDayExample = Omit<ActorDayExample, 'tenant' | 'events'> & {
  tenant?: string | null; kb?: boolean; askExamples?: string[] | null; events: RawDayEvent[];
  /** The true percentile of the example's cash among the fork's scenarios at the window end (a number 0–100, or
   *  {percentile|value|p, n}); and the number of those scenarios. */
  percentileAtWindowEnd?: number | { percentile?: number; value?: number; p?: number; n?: number } | null; ensembleN?: number | null; scenarios?: number | null;
};

/** The true position of an example: the percentile (0–100) and among how many scenarios, when the data says it. */
export function truePosition(e: Pick<RawDayExample, 'percentileAtWindowEnd' | 'ensembleN' | 'scenarios'>): { p?: number; n?: number } {
  const raw = e.percentileAtWindowEnd;
  const p = typeof raw === 'number' ? raw : raw ? raw.percentile ?? raw.value ?? raw.p : undefined;
  const n = (typeof raw === 'object' && raw ? raw.n : undefined) ?? e.ensembleN ?? e.scenarios ?? undefined;
  // a share (0–1) is read as a percent
  const pct100 = p != null && Number.isFinite(p) ? (p <= 1 && p > 0 && !Number.isInteger(p) ? p * 100 : p) : undefined;
  return { p: pct100 != null ? Math.max(0, Math.min(100, pct100)) : undefined, n: n != null && Number.isFinite(n) && n > 0 ? Math.round(n) : undefined };
}

const ORDINAL = (n: number) => { const r = n % 100; return `${n}${r >= 11 && r <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`; };
const PLACE: Record<DayKind, string> = { pessimistic: 'Lowest of the three', median: 'Middle of the three', optimistic: 'Highest of the three' };
/**
 * The card line of an example, honest about where it falls: its place among the three examples (by cash at the end
 * of the window), then its true percentile among all the scenarios of the fork when the data gives it
 * («Lowest of the three · ≈ 52nd percentile of cash among 1,000 scenarios on 26 Apr 2027»); never a «p10» it is not.
 */
export function positionLabel(e: Pick<DayExampleData, 'id' | 'truePercentile' | 'ensembleN' | 'endDate' | 'headline'>, scenariosFallback?: number): string {
  const place = PLACE[e.id], when = e.endDate ? endDate(e.endDate) : null;
  if (e.truePercentile == null) return `${place} · ${e.headline.label}`;
  const n = e.ensembleN ?? scenariosFallback;
  return `${place} · ≈ ${ORDINAL(Math.round(e.truePercentile))} percentile of cash${n ? ` among ${n.toLocaleString('en-US')} scenarios` : ''}${when ? ` on ${when}` : ''}`;
}

const ASK_TOPIC = [/incident/i, /cash|burn|runway/i, /client|contract|notice|churn/i];
/** The branch's questions for the page: the incidents ones first (the day's charts lead there), then cash, then
 *  clients; at most four, each once. */
export function askOrder(qs: readonly unknown[] | null | undefined): string[] {
  const xs = [...new Set((qs ?? []).filter((q): q is string => typeof q === 'string' && !!q.trim()).map(q => q.trim()))];
  const topic = (q: string) => { const k = ASK_TOPIC.findIndex(r => r.test(q)); return k < 0 ? ASK_TOPIC.length : k; };
  return xs.map((q, i) => ({ q, i })).sort((a, b) => topic(a.q) - topic(b.q) || a.i - b.i).map(x => x.q).slice(0, 4);
}

export function fromDayFile(exs: RawDayExample[], forkDate: string, people: People = {}): DayExampleData[] {
  return [...exs].sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind)).map(e => {
    const points: DayPoint[] = e.days.map(d => ({ date: d.date, day: daysBetween(forkDate, d.date), values: d.values }));
    const items = e.events.map((ev, i) => eventView(ev, i, forkDate, people));
    const last = points[points.length - 1];
    const cash = e.yearEnd?.cash ?? last?.values.cash ?? NaN;
    const end = e.horizonEnd ?? last?.date;
    return {
      id: e.kind, label: e.label || KIND_LABEL[e.kind], tenant: e.tenant ?? undefined, kb: e.kb ?? !!e.tenant, percentile: e.percentile,
      ask: e.kb === false || !e.tenant ? [] : askOrder(e.askExamples),
      ...(() => { const t = truePosition(e); return { truePercentile: t.p, ensembleN: t.n }; })(), endDate: end ?? undefined,
      headline: { value: cash ?? NaN, unit: 'USD', label: end ? `cash on ${endDate(end)}` : 'cash at the end' },
      outcome: outcomeOf(items), points, items, digest: digestOf(points, items),
    };
  });
}

/** The preview's examples (the formula model; only while the whole page is on the preview). */
export function fromPreview(exs: PreviewDayExample[], forkDate: string): DayExampleData[] {
  return exs.map(e => {
    const points: DayPoint[] = e.daily.map(({ day, ...values }) => ({ date: isoAdd(forkDate, day), day, values }));
    const items: DayItem[] = e.events.map((ev, i) => ({
      i, date: isoAdd(forkDate, ev.day), day: ev.day, type: 'event', group: 'other' as EventGroup, title: ev.title, text: peopleEventText('event', ev.text), tone: ev.tone, entityId: ev.entityId, person: ev.person,
    }));
    return { id: e.id, label: e.label, headline: e.headline, outcome: e.outcome, points, items, ask: [], digest: digestOf(points, items) };
  });
}

/** Every two examples differ in their days or their events. */
export const distinct = (exs: Pick<DayExampleData, 'digest'>[]) => new Set(exs.map(e => e.digest)).size === exs.length;

/** How many example days a fork has in the day file (the tour's «{examples}»). */
export const exampleCount = (file: DayFile | null, fork: string) => file?.dayExamples?.[fork]?.length ?? 0;

/** The case whose example days this version makes (WP19's day file is of case B only). */
export const DAY_CASE = 'company_best_year';

/** The forks with example days in the day file. */
export const coveredForks = (file: DayFile | null) => Object.entries(file?.dayExamples ?? {}).filter(([, exs]) => exs.length).map(([f]) => f);

/**
 * The examples of a fork and where they come from: WP19's day file; the preview only while the page has no actor
 * set at all (`onActors` false); else none («No example days for this fork yet»). Identical examples count as none.
 */
export function examplesFor(fork: string, src: {
  file: DayFile | null; onActors: boolean; forkDate: string; people?: People;
  preview?: { days: Record<string, PreviewDayExample[]>; dayMetrics: { id: string; label: string; unit: string; higher?: boolean | null }[] } | null;
}): { source: DaySource; examples: DayExampleData[]; metrics: DayMetric[] } {
  const has = (examples: DayExampleData[]) => (m: DayMetric) => examples.some(e => e.points.some(p => Number.isFinite(p.values[m.id])));
  const raw = src.file?.dayExamples?.[fork] as RawDayExample[] | undefined;
  if (raw?.length) {
    const examples = fromDayFile(raw, src.forkDate, src.people);
    if (distinct(examples)) {
      const metrics = dayMetrics((src.file!.dayMetrics ?? []) as MetricIn[]).filter(has(examples));
      // a metric shown with its sign turned (the burn as a net cash flow): its values turn too, everywhere
      const neg = metrics.filter(m => m.negate).map(m => m.id);
      const turned = !neg.length ? examples : examples.map(e => ({ ...e, points: e.points.map(p => ({ ...p, values: { ...p.values, ...Object.fromEntries(neg.filter(id => Number.isFinite(p.values[id])).map(id => [id, -p.values[id]])) } })) }));
      return { source: 'day-file', examples: turned, metrics };
    }
  }
  const pv = !src.onActors ? src.preview?.days[fork] : undefined;
  if (pv?.length) {
    const examples = fromPreview(pv, src.forkDate);
    if (distinct(examples)) return { source: 'preview', examples, metrics: (src.preview!.dayMetrics ?? []).map(metricDef).filter(has(examples)) };
  }
  return { source: 'none', examples: [], metrics: [] };
}

// ------------------------------------------------------------------------------------------------ the selected day
/** The day of the series an event date falls on: that date, else the next day of the series, else the last. */
export function seriesDate(points: DayPoint[], date: string): string | null {
  if (!points.length) return null;
  return points.find(p => p.date >= date)?.date ?? points[points.length - 1].date;
}
/** The events of a day of the series (with those of the days before it that are not in the series: a weekend). */
export function itemsOn(ex: Pick<DayExampleData, 'points' | 'items'>, date: string | null): DayItem[] {
  if (!date) return [];
  return ex.items.filter(e => seriesDate(ex.points, e.date) === date);
}
/** The day `delta` days of the series away from `current` (none selected: the first or the last day). */
export function stepDay(points: DayPoint[], current: string | null, delta: number): string | null {
  if (!points.length) return null;
  if (!current) return delta >= 0 ? points[0].date : points[points.length - 1].date;
  const i = points.findIndex(p => p.date === current);
  const at = i < 0 ? points.findIndex(p => p.date > current) : i;
  return points[Math.max(0, Math.min(points.length - 1, (at < 0 ? points.length - 1 : at) + delta))].date;
}
/** The day a key press on the charts picks: the day under the pointer, the day already picked, the first day with an
 *  event, the first day. */
export function startDay(ex: Pick<DayExampleData, 'points' | 'items'>, hover: string | null, current: string | null): string | null {
  return hover ?? current ?? (ex.items.length ? seriesDate(ex.points, ex.items[0].date) : ex.points[0]?.date ?? null);
}
/** The plot inside each day chart: the y axis on the left and a margin on the right (px). */
export const PLOT = { yAxis: 58, right: 12 };
/** The index of the day under a click at `clientX` on a chart of `n` days (one band per day) drawn in `rect`; none
 *  left of the plot or right of it. */
export function bandIndex(clientX: number, rect: { left: number; width: number }, n: number): number | null {
  const w = rect.width - PLOT.yAxis - PLOT.right, x = clientX - rect.left - PLOT.yAxis;
  if (n <= 0 || w <= 0 || x < -4 || x > w + 4) return null;
  return Math.max(0, Math.min(n - 1, Math.floor(x / (w / n))));
}
/** The worst tone of the events of each day (the dots on the first chart). */
export function dayTones(ex: Pick<DayExampleData, 'points' | 'items'>): Map<string, DayItem['tone']> {
  const out = new Map<string, DayItem['tone']>();
  for (const e of ex.items) {
    const d = seriesDate(ex.points, e.date);
    if (!d) continue;
    const was = out.get(d);
    out.set(d, was === 'bad' || e.tone === 'bad' ? 'bad' : was === 'event' || e.tone === 'event' ? 'event' : 'good');
  }
  return out;
}

// ------------------------------------------------------------------------------------------------ knowledge-base links
export interface KbTarget { path: string; params: Record<string, string | null>; branch: string | null }
/** A directory entity (a person, an organisation) exists in the company history too; a record of the simulation
 *  (`sim.…`) only on its branch. */
export const inHistory = (id: string) => /^(person|org)\./.test(id);
/**
 * Where a link to an entity of the example opens: its dossier on the example's branch (`/brief?branch=…&entity=…`)
 * once the knowledge base lists that branch; else the dossier in the company history over the current page (a
 * directory entity only); else nowhere.
 */
export function kbTarget(entityId: string | undefined, tenant: string | undefined, org: unknown, here: string): KbTarget | null {
  if (!entityId) return null;
  const b = branchLink(tenant, org);
  if (b.open && b.params) return { path: '/brief', params: { ...b.params, entity: entityId }, branch: tenant! };
  return inHistory(entityId) ? { path: here, params: { entity: entityId }, branch: null } : null;
}
