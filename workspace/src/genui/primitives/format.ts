// Pure formatting, status and plain-language helpers: the single source for the primitives and the
// drill-down. Keep the exported API stable. No React here: everything is unit-tested in
// tests/genui-layout.test.ts.
import type { MetricSpec, MetricStatus, WidgetSize } from '../contracts';

export type Unit = MetricSpec['unit'];
export type Direction = MetricSpec['direction'];
export type Tone = 'good' | 'bad' | 'neutral' | 'unknown';

const MINUS = '−';
const finite = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v);

/** 12-column grid: sm/md/lg/xl = 3/4/6/12 columns. Phones always use the full width (CSS). */
export function spanFor(size: WidgetSize): 3 | 4 | 6 | 12 {
  return size === 'sm' ? 3 : size === 'md' ? 4 : size === 'lg' ? 6 : 12;
}

function trim(n: number, digits: number) {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 0 }).format(n);
}
/** A text that shows a zero (every digit 0) carries no sign: a change of −0.01 rounded to "0" is not "−0". */
const isZeroText = (text: string) => !/[1-9]/.test(text);
const signed = (sign: string, body: string) => (isZeroText(body) ? '' : sign) + body;
function compactUsd(abs: number): string {
  if (abs >= 1e9) return `$${trim(abs / 1e9, abs >= 1e10 ? 0 : 1)}B`;
  if (abs >= 1e6) return `$${trim(abs / 1e6, abs >= 1e7 ? 1 : 1)}M`;
  if (abs >= 1e3) return `$${trim(abs / 1e3, abs >= 1e5 ? 0 : 1)}K`;
  return `$${trim(abs, 0)}`;
}
/** Money as the brief tiles write it (kb/api.ts formatValue): «$2.00M», «$12.0M», «$871,303». */
function briefUsd(abs: number): string {
  if (abs >= 1e6) return `$${(abs / 1e6).toFixed(abs >= 1e7 ? 1 : 2)}M`;
  return `$${trim(abs, 0)}`;
}
/** At most one decimal, as the brief tiles round: «3.5 days», «59.9 days», «12 h». */
const smart = (abs: number) => trim(abs, 1);
/** A share keeps one decimal, as on the brief tile («12.3%»); close to 100 it keeps three (99.991% is not 100%). */
const share = (abs: number) => trim(abs, abs >= 99 && abs < 100 ? 3 : 1);

/** The number and a small unit suffix, for large figures where the unit is set smaller. `brief`: money as the brief
 * tile writes it, so a sheet repeats the number of the tile it was opened from. */
export function valueParts(value: number | null | undefined, unit: Unit, opts: { compact?: boolean; brief?: boolean } = {}): { main: string; suffix: string } {
  if (!finite(value)) return { main: '—', suffix: '' };
  const sign = value < 0 ? MINUS : '';
  const abs = Math.abs(value);
  const parts = (body: string, suffix: string) => ({ main: signed(sign, body), suffix });
  switch (unit) {
    case 'usd': return parts(opts.brief ? briefUsd(abs) : opts.compact === false ? `$${trim(abs, 0)}` : compactUsd(abs), '');
    case 'percent': return parts(share(abs), '%');
    case 'score': return parts(trim(abs, 0), '/100');
    case 'index': return parts(trim(abs, 0), ''); // its own scale: no «/100» (NPS: −100 to 100)
    case 'days': return parts(smart(abs), abs === 1 ? 'day' : 'days');
    case 'weeks': return parts(smart(abs), abs === 1 ? 'week' : 'weeks');
    case 'months': return parts(smart(abs), abs === 1 ? 'month' : 'months');
    case 'hours': return parts(smart(abs), 'h');
    case 'ms': return parts(trim(abs, 0), 'ms');
    case 'multiple': return parts(trim(abs, abs < 10 ? 1 : 0), '×');
    case 'ratio': return parts(trim(abs, 2), '');
    case 'count': default: return parts(trim(abs, 2), ''); // «9.75 deploys a week», as on the tile
  }
}

/** Inline value with its unit, e.g. "$4.8M", "64%", "3.5 days", "72". Unknown is an em dash. */
export function formatValue(value: number | null | undefined, unit: Unit, opts: { compact?: boolean; brief?: boolean } = {}): string {
  const { main, suffix } = valueParts(value, unit, opts);
  if (main === '—') return main;
  if (unit === 'percent' || unit === 'multiple') return main + suffix;
  if (unit === 'score') return main;
  if (unit === 'days' || unit === 'weeks' || unit === 'months' || unit === 'hours' || unit === 'ms') return `${main} ${suffix}`;
  return main;
}

/** A change of a share, a score or an index, in points; the one rule of the tile and the sheet: one decimal, or up to
 * three when the change is below 0.1 («−0.03 pts»). A change that rounds to zero has no sign («0 pts»). */
export function formatPoints(diff: number): string {
  const abs = Math.abs(diff);
  return `${signed(diff > 0 ? '+' : diff < 0 ? MINUS : '', trim(abs, abs < 0.1 ? 3 : 1))} pts`;
}

/** Signed change in the metric's own unit; percent and score changes are points. A change that rounds to zero carries
 * no sign. */
export function formatDiff(diff: number, unit: Unit): string {
  const sign = diff > 0 ? '+' : diff < 0 ? MINUS : '';
  const abs = Math.abs(diff);
  const s = (body: string) => signed(sign, body);
  switch (unit) {
    case 'usd': return s(compactUsd(abs));
    case 'percent': case 'score': case 'index': return formatPoints(diff);
    case 'days': return `${s(smart(abs))} ${abs === 1 ? 'day' : 'days'}`;
    case 'weeks': return `${s(smart(abs))} ${abs === 1 ? 'week' : 'weeks'}`;
    case 'months': return `${s(smart(abs))} ${abs === 1 ? 'month' : 'months'}`;
    case 'hours': return `${s(smart(abs))} h`;
    case 'ms': return `${s(trim(abs, 0))} ms`;
    case 'multiple': return `${s(trim(abs, 1))}×`;
    case 'ratio': return s(trim(abs, 2));
    default: return s(trim(abs, Number.isInteger(abs) ? 0 : 1));
  }
}

/** Whether a change is good depends on the metric's direction, never on the sign alone. */
export function toneFor(diff: number | null, direction: Direction): Tone {
  if (diff === null) return 'unknown';
  if (diff === 0 || direction === 'context') return 'neutral';
  return (diff > 0) === (direction === 'higher') ? 'good' : 'bad';
}

const EPSILON = 1e-9;
export function formatDelta(current: number | null | undefined, previous: number | null | undefined, unit: Unit, direction: Direction): { text: string; tone: Tone; diff: number | null } {
  if (!finite(current) || !finite(previous)) return { text: 'No comparison', tone: 'unknown', diff: null };
  const raw = current - previous;
  const diff = Math.abs(raw) < EPSILON ? 0 : Number(raw.toFixed(6));
  const text = diff === 0 ? '' : formatDiff(diff, unit);
  // a change too small to show («0 pts», «$0») is no change: never «−0 pts»
  if (diff === 0 || isZeroText(text)) return { text: 'No change', tone: 'neutral', diff: 0 };
  return { text, tone: toneFor(diff, direction), diff };
}

/** Mirror of the engine rule: bands with direction; target when there are no bands; otherwise neutral. */
export function statusFor(spec: Pick<MetricSpec, 'direction' | 'bands' | 'target'>, value: number | null | undefined): MetricStatus {
  if (!finite(value)) return 'unknown';
  if (spec.direction === 'context') return 'neutral';
  const higher = spec.direction === 'higher';
  if (spec.bands) {
    const { good, warn } = spec.bands;
    if (higher) return value >= good ? 'good' : value >= warn ? 'warn' : 'bad';
    return value <= good ? 'good' : value <= warn ? 'warn' : 'bad';
  }
  if (spec.target) return (higher ? value >= spec.target.value : value <= spec.target.value) ? 'good' : 'warn';
  return 'neutral';
}

/** The one status vocabulary of the app: the brief tiles, the metric sheets, the headline («(watch)») and the area
 * badges («2 to watch», «1 needs attention») all use these words. */
export const STATUS_LABEL = { good: 'On track', warn: 'Watch', bad: 'Needs attention', neutral: 'Context', unknown: 'Unknown' } as const satisfies Record<MetricStatus, string>;
export const compareLabel = (compare: 'day' | 'week') => compare === 'day' ? 'vs yesterday' : 'vs last week';
export const sinceLabel = (compare: 'day' | 'week') => compare === 'day' ? 'since yesterday' : 'since last week';

// Dates as in the rest of the app (case/ui/format.ts longDate, kb/api.ts formatDate): day before month, «27 Oct»,
// «27 Oct 2026», never the US «Oct 27». An ISO timestamp counts by its own calendar day (the engine writes local time).
/** en-GB as the case pages write it: Chrome's en-GB says «Sept», other browsers and the case pages «Sep». */
export const gbDate = (d: Date, opts: Intl.DateTimeFormatOptions, withTime = false) =>
  (withTime ? d.toLocaleString('en-GB', opts) : d.toLocaleDateString('en-GB', opts)).replace(/\bSept\b/g, 'Sep');
const dateFmt = (iso: string, opts: Intl.DateTimeFormatOptions) => {
  const day = /^\d{4}-\d{2}-\d{2}/.exec(iso)?.[0];
  const d = new Date(day ? `${day}T12:00:00Z` : iso);
  return Number.isNaN(d.getTime()) ? iso : gbDate(d, { timeZone: 'UTC', ...opts });
};
export const shortDay = (iso: string) => dateFmt(iso, { month: 'short', day: 'numeric' });
export const longDay = (iso: string) => dateFmt(iso, { month: 'short', day: 'numeric', year: 'numeric' });
export const weekday = (iso: string) => dateFmt(iso, { weekday: 'short', month: 'short', day: 'numeric' });
/** A month of the engine («2026-09») as people write it: «Sep 2026». */
export const monthLabel = (ym: string) => { const d = new Date(`${ym}-15T12:00:00Z`); return Number.isNaN(d.getTime()) ? ym : gbDate(d, { timeZone: 'UTC', month: 'short', year: 'numeric' }); };
/** Local wall-clock time of an ISO timestamp, e.g. "18 Jun, 07:00". */
export function stamp(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${gbDate(d, { month: 'short', day: 'numeric' })}, ${d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`;
}
/** Whole days between two ISO dates (b − a). */
export function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(`${b.slice(0, 10)}T12:00:00Z`) - Date.parse(`${a.slice(0, 10)}T12:00:00Z`)) / 86_400_000);
}
/** Plain-language field names for record tables: `assigneeId.department` → "Assignee department". */
export function fieldLabel(name: string): string {
  const words = name.replace(/Id\b/g, '').replace(/\./g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
export function fieldValue(value: string | number | boolean | null): string {
  if (value === null) return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return Number.isInteger(value) ? trim(value, 0) : trim(value, 2);
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return longDay(value);
  return value;
}

/** A lineage value with an optional unit; unit-less numbers keep at most two decimals. */
export function formatNodeValue(value: number | null, unit: Unit | null): string {
  if (value === null) return 'Unknown';
  if (unit) return formatValue(value, unit, { brief: true });
  return trim(value, Math.abs(value) < 10 && !Number.isInteger(value) ? 2 : 0);
}

// ---------------------------------------------------------------------------------------------
// Plain language for engine fields. The engine speaks in catalog field names; executives read
// phrases. Deterministic dictionaries only; unknown fields fall back to a de-camelCased label.
// ---------------------------------------------------------------------------------------------
type FieldValue = string | number | boolean | null;
const FIELD_LABEL: Record<string, string> = {
  toldFirstScore: 'told-first score', followThroughScore: 'follow-through score', warningScore: 'warning-time score', withPlanScore: 'came-with-a-plan score',
  execAwareAt: 'date you learned', knownToExec: 'known to you', execAwareVia: 'how it reached you', surfacedBy: 'who raised it', statusAsOf: 'status',
  dueAt: 'due date', dueDate: 'due date', issuedAt: 'issue date', completedAt: 'completion date', firstSignalAt: 'first signal', openAgeDays: 'days open',
  resolvedAsOf: 'resolved', resolvedAt: 'resolution date', selfResolved: 'resolved by the team', isOpen: 'open', detectedAt: 'detection date', remindersAsOf: 'reminders',
  madeAt: 'promise date', fulfilledAt: 'fulfilment date', at: 'date', ageDays: 'age in days', daysLate: 'days late', leadDays: 'days from first signal to you',
  execFirst: 'you learned first', proactive: 'raised early', withPlan: 'came with a plan', chased: 'chased', onTime: 'on time', impactAt: 'impact date',
};
const DATE_PHRASE: Record<string, string> = { execAwareAt: 'that reached you', dueAt: 'due', dueDate: 'due', issuedAt: 'issued', firstSignalAt: 'first signalled', at: 'sent', detectedAt: 'detected', madeAt: 'made', completedAt: 'completed', resolvedAt: 'resolved' };
const BOOL_PHRASE: Record<string, [string, string]> = {
  knownToExec: ['known to you', 'not yet known to you'], resolvedAsOf: ['resolved', 'still open'], selfResolved: ['resolved by the team', 'not resolved by the team'],
  isOpen: ['open', 'closed'], execFirst: ['you learned first', 'told first by the team'], onTime: ['on time', 'late'], chased: ['chased', 'not chased'],
  proactive: ['raised early', 'not raised early'], decisionReserved: ['decision reserved for you', 'not a reserved decision'], withPlan: ['came with a plan', 'came without a plan'], silentSlip: ['moved without warning', 'no silent slip'],
};
const VALUE_WORD: Record<string, string> = { doneOnTime: 'done on time', doneLate: 'done late', 'own-discovery': 'your own discovery', 'owner-report': 'owner report', 'team-escalation': 'team escalation', 'peer-report': 'another department', 'not-yet': 'not yet', 'status-chase': 'status chase', 'missed-deadline': 'missed deadline' };
const VIA_PHRASE: Record<string, string> = { client: 'A client told you first', external: 'An outside party told you first', 'own-discovery': 'You found it yourself', 'owner-report': 'The owner reported it', 'team-escalation': 'The team escalated it', 'peer-report': 'Another department reported it', system: 'A system alert raised it', 'not-yet': 'Not known to you yet' };
const SURFACED_PHRASE: Record<string, string> = { owner: 'Raised by the owner', team: 'Raised by the team', peer: 'Raised by another department', executive: 'You raised it yourself', client: 'Raised by a client', external: 'Raised by an outside party', monitoring: 'Raised by monitoring' };
const STATUS_WORD: Record<string, string> = { open: 'Open', overdue: 'Overdue', doneOnTime: 'Done on time', doneLate: 'Done late' };
const REASON_WORD: Record<string, string> = { 'status-chase': 'Chasing status', 'missed-deadline': 'Missed deadline', surprise: 'Unannounced problem', quality: 'Quality concern' };

// acronyms keep their capitals: «lost ARR», «ACV»
const decamel = (name: string) => name.replace(/Id\b/g, '').replace(/\./g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').split(' ').map(w => /^[A-Z][A-Z0-9]+$/.test(w) ? w : w.toLowerCase()).join(' ').trim();
/** Plain label of a catalog field, e.g. `execAwareAt` → "date you learned". */
export const plainField = (name: string) => FIELD_LABEL[name] ?? decamel(name);
const pct = (v: number) => `${Math.round(v * 100)}%`;
const times = (n: number) => n === 1 ? 'once' : n === 2 ? 'twice' : `${n} times`;
const days = (n: number) => `${n} ${n === 1 ? 'day' : 'days'}`;

/** One plain phrase for a record field, or null when it says nothing on its own. */
export function factPhrase(field: string, value: FieldValue): string | null {
  if (value === null || value === '') return null;
  const n = typeof value === 'number' ? value : null;
  const s = typeof value === 'string' ? value : null;
  switch (field) {
    case 'execAwareVia': return VIA_PHRASE[String(value)] ?? `Reached you via ${value}`;
    case 'surfacedBy': return SURFACED_PHRASE[String(value)] ?? `Raised by ${value}`;
    case 'toldFirstScore': return n === 1 ? 'Raised first by the owner or team' : n === 0 ? 'You learned first' : null;
    case 'warningScore': return n === null ? null : `${pct(n)} of the warning time kept`;
    case 'withPlanScore': return n === 1 ? 'Came with a plan' : n === 0 ? 'You had to step in' : n === null ? null : 'Question handed up';
    case 'followThroughScore': return n === 1 ? 'Done on time, no chasing' : n === 0.6 ? 'Done after one reminder' : n === 0.3 ? 'Late with warning, or chased twice' : n === 0 ? 'Late without warning' : null;
    case 'statusAsOf': return STATUS_WORD[String(value)] ?? String(value);
    case 'reason': return REASON_WORD[String(value)] ?? capital(String(value));
    case 'remindersAsOf': return n === null ? null : n ? `Chased ${times(n)}` : 'Not chased';
    case 'daysLate': return n === null ? null : `${days(n)} late`;
    case 'openAgeDays': case 'ageDays': return n === null ? null : `Open ${days(n)}`;
    case 'leadDays': return n === null ? null : `${days(n)} from first signal to you`;
    case 'severity': return `${value} severity`;
    case 'utilization': return n === null ? null : `${trim(n, 0)}% reported utilization`;
    case 'revenue': case 'value': return n === null ? null : formatValue(n, 'usd');
    case 'assignee': case 'owner': case 'person': case 'target': case 'project': case 'client': case 'department': return String(value);
  }
  if (typeof value === 'boolean' && BOOL_PHRASE[field]) { const p = BOOL_PHRASE[field][value ? 0 : 1]; return p.charAt(0).toUpperCase() + p.slice(1); }
  if (s && /^\d{4}-\d{2}-\d{2}/.test(s)) {
    const d = longDay(s.slice(0, 10)); // with its year: a term end in 2027 next to an end in 2026
    const label: Record<string, string> = { execAwareAt: `You learned ${d}`, dueAt: `Due ${d}`, dueDate: `Due ${d}`, firstSignalAt: `First signal ${d}`, resolvedAt: `Resolved ${d}`, issuedAt: `Issued ${d}`, detectedAt: `Detected ${d}`, madeAt: `Promised ${d}`, completedAt: `Done ${d}`, fulfilledAt: `Fulfilled ${d}`, at: d, impactAt: `Impact ${d}` };
    return label[field] ?? `${capital(plainField(field))} ${d}`;
  }
  // an amount reads as money, never as a bare number («ACV $81,600», «Lost ARR $81,600»)
  if (n !== null && MONEY_FIELD.test(field)) return `${capital(plainField(field))} ${formatValue(n, 'usd', { brief: true })}`;
  return `${capital(plainField(field))}: ${fieldValue(value)}`;
}
const MONEY_FIELD = /\b(ACV|ARR|MRR|amount|price|cost|balance)\b/i;
const capital = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);
const FACT_ORDER = ['assignee', 'owner', 'person', 'target', 'project', 'client', 'lost ARR', 'new ARR', 'ended', 'department', 'execAwareVia', 'surfacedBy', 'statusAsOf', 'followThroughScore', 'toldFirstScore', 'withPlanScore', 'warningScore', 'daysLate', 'remindersAsOf', 'openAgeDays', 'ageDays', 'dueAt', 'dueDate', 'execAwareAt', 'firstSignalAt', 'resolvedAt', 'at', 'reason', 'severity'];

/** Fields with the same value on every record of a list (usually the filter itself) carry no information per row. */
export function constantFields(records: { fields: Record<string, FieldValue> }[]): Set<string> {
  const out = new Set<string>();
  if (records.length < 2) return out;
  for (const key of Object.keys(records[0].fields)) if (records.every(r => r.fields[key] === records[0].fields[key])) out.add(key);
  return out;
}
/** Up to `limit` plain phrases explaining a record; `omit` drops non-informative fields. Redundant pairs collapse. */
/** Filter flags that are true for every record a metric can match; saying them adds nothing. */
const IMPLICIT: Record<string, FieldValue> = { knownToExec: true, isOpen: true };
export function recordPhrases(fields: Record<string, FieldValue>, omit: Set<string> = new Set(), limit = 4, dataset?: string): string[] {
  const keys = Object.keys(fields).filter(k => !omit.has(k) && fields[k] !== null && fields[k] !== '' && IMPLICIT[k] !== fields[k]);
  if (keys.includes('execAwareVia') || keys.includes('surfacedBy')) { const i = keys.indexOf('toldFirstScore'); if (i >= 0) keys.splice(i, 1); }
  if (keys.includes('statusAsOf') && keys.includes('followThroughScore')) keys.splice(keys.indexOf('statusAsOf'), 1);
  keys.sort((a, b) => (FACT_ORDER.indexOf(a) + 1 || 99) - (FACT_ORDER.indexOf(b) + 1 || 99));
  const out: string[] = [];
  for (const k of keys) { const p = k === 'value' && dataset === 'sectorMetrics' ? sectorValuePhrase(fields) : factPhrase(k, fields[k]); if (p && !out.includes(p)) out.push(p); if (out.length >= limit) break; }
  return out;
}

/** Sector measures keep their authored unit: a stored ratio shown as a percentage is not money. */
function sectorValuePhrase(fields: Record<string, FieldValue>): string | null {
  const shown = typeof fields.displayValue === 'number' ? fields.displayValue : typeof fields.value === 'number' ? fields.value : null;
  if (shown === null) return null;
  const unit = String(fields.displayUnit ?? fields.unit ?? '').toLowerCase();
  if (unit === 'percent' || unit === '%') return `Value: ${trim(shown, 1)}%`;
  if (unit === 'usd') return `Value: ${formatValue(shown, 'usd')}`;
  return `Value: ${fieldValue(shown)}${unit && unit !== 'ratio' ? ` ${fields.displayUnit ?? fields.unit}` : ''}`;
}

// ---------------------------------------------------------------------------------------------
// Engine texts at display time. The metric catalogue writes its explanations, labels and worked formulas once, into
// the artifact sets the VARIATOR API serves as files; they are never recomputed per request. So the sheet rewrites
// them here: dates in the app's order, events and causes in words, amounts as money, no ids and no snake_case codes.
// ---------------------------------------------------------------------------------------------
const ISO_TIME = /\b(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g;
const DAY_RANGE = /\b(\d{4}-\d{2}-\d{2})(\s*(?:–|—|…|to)\s*)(\d{4}-\d{2}-\d{2})\b/g;
const MONTH_RANGE = /\b(\d{4}-(?:0[1-9]|1[0-2]))(\s*(?:–|—|…|to)\s*)(\d{4}-(?:0[1-9]|1[0-2]))\b(?!-\d)/g;
const rangeSep = (sep: string) => sep.trim() === 'to' ? ' to ' : ' – ';

/** Dates of an engine text as the app writes them: «2026-10-01T07:14:29+01:00» → «1 Oct 2026», «2026-09» → «Sep 2026».
 * A range names its year once when both ends share it («22 May – 18 Jun 2026»), else on both ends («27 Oct 2025 to
 * 26 Oct 2026»): a 365-day window that ends on the view date starts a year earlier. */
export function plainDates(text: string): string {
  return text
    .replace(ISO_TIME, (_, day: string) => day)
    .replace(DAY_RANGE, (_, a: string, sep: string, b: string) => `${a.slice(0, 4) === b.slice(0, 4) ? shortDay(a) : longDay(a)}${rangeSep(sep)}${longDay(b)}`)
    .replace(MONTH_RANGE, (_, a: string, sep: string, b: string) => a.slice(0, 4) === b.slice(0, 4) ? `${monthLabel(a).split(' ')[0]}–${monthLabel(b)}` : `${monthLabel(a)}${rangeSep(sep)}${monthLabel(b)}`)
    .replace(/\b\d{4}-\d{2}-\d{2}\b/g, d => longDay(d))
    .replace(/\b(\d{4}-(?:0[1-9]|1[0-2]))\b(?!-\d)/g, (_, ym: string) => monthLabel(ym));
}

const EVENT_WORD: Record<string, string> = {
  contract_amended_upsell: 'Upsell signed', contract_amended_downgrade: 'Downgrade signed', requisition_interviewing: 'Interviews started',
};
/** An event kind of the engine in words: `contract_terminated` → «Contract terminated». */
export const eventWords = (kind: string) => EVENT_WORD[kind] ?? capital(kind.replace(/_/g, ' '));
const ENGINE_EVENT = /^Event ([a-z][a-z_]*) of (\S+?), known at (\S+)$/;
const ENGINE_CAUSE = /^Declared cause \(([a-z_]+)\) of (.+)$/;

/** Numbers of an engine text: «2000000.0» → «2,000,000», «0.150» → «0.15»; ids such as BNK-01655 keep their digits. */
function plainNumbers(text: string): string {
  return text.replace(/(?<![\w.$\-−])(\d+)\.(\d+)(?![\w.])/g, (m, int: string, dec: string) => {
    const kept = dec.replace(/0+$/, '');
    return `${Number(int) >= 10_000 ? trim(Number(int), 0) : int}${kept ? `.${kept}` : ''}`;
  }).replace(/(?<![\w.$\-−,])(\d{5,})(?![\w.,])/g, d => trim(Number(d), 0));
}

/** Engine explanations name catalog fields and codes; replace them with plain words and dates. */
export function plainEngineText(text: string): string {
  let t = text.replace(/^excluded by "(.+)"$/, (_, cond: string) => `outside the filter (${cond})`);
  // «Event contract_terminated of sim.CTR-0016, known at …»: what happened and when, never the record id
  t = t.replace(ENGINE_EVENT, (_, kind: string, _id: string, at: string) => `${eventWords(kind)} · ${longDay(at)}`);
  t = t.replace(ENGINE_CAUSE, (_, _type: string, what: string) => `Recorded cause of ${what}`);
  t = plainDates(t);
  t = t.replace(/\bwith ([a-z][A-Za-z]*) (in the last|on or before|outside)/g, (m, f: string, rest: string) => DATE_PHRASE[f] ? `${DATE_PHRASE[f]} ${rest}` : `with ${plainField(f)} ${rest}`);
  t = t.replace(/\b([a-z][A-Za-z]*) is (true|false)\b/g, (m, f: string, v: string) => BOOL_PHRASE[f] ? BOOL_PHRASE[f][v === 'true' ? 0 : 1] : `${plainField(f)} is ${v === 'true' ? 'yes' : 'no'}`);
  t = t.replace(/\bis one of ([^.;")]+)/g, (m, list: string) => { const items = list.split(/,\s*/).map(v => VALUE_WORD[v.trim()] ?? v.trim()); return `is ${items.length > 1 ? `${items.slice(0, -1).join(', ')} or ${items.at(-1)}` : items[0]}`; });
  t = t.replace(/\b(doneOnTime|doneLate|own-discovery|owner-report|team-escalation|peer-report|not-yet|status-chase|missed-deadline)\b/g, v => VALUE_WORD[v] ?? v);
  t = t.replace(/\b([a-z]+(?:[A-Z][a-z]+)+(?:Id)?)\b/g, f => plainField(f));
  // what is left of the codes: knowledge-base ids go, snake_case reads as words («go_live_late_days» → «go live late days»)
  t = t.replace(/\s*\bsim\.[A-Za-z0-9-]+\b/g, '');
  t = t.replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g, w => w.replace(/_/g, ' '));
  return plainNumbers(t);
}

/** A name or a label of the engine (a breakdown step, a record, a group): its dates in the app's format, its first
 * letter a capital. */
export const plainLabel = (text: string) => capital(plainDates(text));

type FactRecord = { id: string; label: string; fields: Record<string, FieldValue> };
type LineageLike = { kind: string; label: string; value: number | null; unit: Unit | null; explanation: string; formulaText: string | null; children: LineageLike[]; records?: FactRecord[] };

/** The line under an event of the breakdown: who, what happened, when — «Maple Energy · Contract terminated · 1 Oct
 * 2026». The client of the record names it; else its title. Null: not an engine event. */
export function eventLine(n: Pick<LineageLike, 'explanation' | 'label' | 'records'>): string | null {
  const m = ENGINE_EVENT.exec(n.explanation);
  if (!m) return null;
  const [, kind, id, at] = m;
  const rec = n.records?.find(r => r.id === id) ?? n.records?.[0];
  const client = rec?.fields.client;
  const who = typeof client === 'string' && client ? client : rec?.label ?? n.label.split(': ')[0];
  return `${plainLabel(who)} · ${eventWords(kind)} · ${longDay(at)}`;
}

/** The amount an event moved, from the record that makes it count (an event has no value of its own): «$81,600» of ARR
 * lost. Null when the record carries none; the sheet then shows no value rather than «Unknown». */
const EVENT_AMOUNT: [string, string][] = [['lost ARR', 'ARR lost'], ['new ARR', 'new ARR']];
export function eventAmount(n: Pick<LineageLike, 'kind' | 'value' | 'records'>): { text: string; caption: string } | null {
  if (n.kind !== 'event' || n.value !== null) return null;
  const fields = n.records?.[0]?.fields ?? {};
  for (const [key, caption] of EVENT_AMOUNT) { const v = fields[key]; if (typeof v === 'number') return { text: formatValue(v, 'usd', { brief: true }), caption }; }
  return null;
}

/** The unit of a metric's breakdown. Its groups may add up to one step of the lineage in another unit than the metric
 * (churn by reason: the ARR lost, in dollars, not percent; days sales outstanding by client: the open receivables):
 * then they take that step's unit, and `of` names the step for the header. Otherwise the metric's own unit. */
export function breakdownUnit(rows: { value: number | null }[], lineage: Pick<LineageLike, 'label' | 'value' | 'unit' | 'children'>, unit: Unit): { unit: Unit; of: string | null } {
  const total = rows.reduce((a, r) => a + (r.value ?? 0), 0);
  if (!rows.length || !total) return { unit, of: null };
  const queue = [lineage];
  for (let n = queue.shift(); n; n = queue.shift()) {
    if (n.value !== null && n.unit && Math.abs(n.value - total) <= Math.max(Math.abs(total) * 0.005, 0.01)) return n.unit === unit ? { unit, of: null } : { unit: n.unit, of: n.label };
    queue.push(...n.children);
  }
  return { unit, of: null };
}

/** A number of a worked formula in its unit: money in whole dollars («$1,507,200»), a share with its %. */
function formulaNumber(v: number, unit: Unit | null): string {
  const sign = v < 0 ? MINUS : '';
  const abs = Math.abs(v);
  if (unit === 'usd') return signed(sign, `$${trim(abs, 0)}`);
  if (unit === 'percent') return signed(sign, `${trim(abs, 3)}%`);
  return signed(sign, trim(abs, 4));
}
/** The formula of a breakdown step with its numbers as people read them: «1507200.0 / 12217200.0 × 100» →
 * «$1,507,200 / $12,217,200 × 100 = 12.3%». A number that is the value of a step below takes that step's unit; other
 * large numbers of a money step are money; the result follows when the numbers are those of the steps below. */
export function plainFormula(n: Pick<LineageLike, 'kind' | 'value' | 'unit' | 'formulaText' | 'children'>): string | null {
  if (!n.formulaText) return null;
  const steps = n.children.filter(c => c.value !== null && c.unit);
  const money = n.unit === 'usd' || steps.some(c => c.unit === 'usd');
  let worked = false;
  // numbers first, dates after: a date («2026-09») or an id («BNK-01128») joined by a hyphen is not a number here
  const text = plainDates(n.formulaText.replace(/(?<![\w.$-])(-?\d+(?:\.\d+)?)(?![\w.-])/g, (m, num: string) => {
    const v = Number(num);
    const step = steps.find(c => Math.abs((c.value as number) - v) < 0.006);
    if (step) { worked = true; return formulaNumber(v, step.unit); }
    if (money && Math.abs(v) >= 1000) return formulaNumber(v, 'usd');
    return formulaNumber(v, null);
  }));
  // a name in snake_case reads as words («annual_budget» → «annual budget»); one-letter subscripts of the math stay
  // («mean_d», «w_s»: the mean over days, the weight of a service)
  const named = text.replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]{2,})+\b/g, w => w.replace(/_/g, ' '));
  return worked && n.value !== null && !named.includes('=') ? `${named} = ${formatNodeValue(n.value, n.unit)}` : named;
}

/** Bars that may be negative: zero sits where negatives end, so lengths stay comparable. */
export function barGeometry(values: (number | null)[]): (v: number | null) => { left: number; width: number; negative: boolean } {
  const nums = values.filter((v): v is number => v !== null);
  const neg = Math.max(0, ...nums.map(v => -v)), pos = Math.max(0, ...nums.map(v => v));
  const span = neg + pos || 1, zero = neg / span * 100;
  return v => v === null ? { left: zero, width: 0, negative: false } : v < 0 ? { left: zero - (-v / span * 100), width: -v / span * 100, negative: true } : { left: zero, width: v / span * 100, negative: false };
}
/** Share of a weight within all weights, e.g. 20 of 100 → "20%". The single rule for index weights. */
export const weightShare = (weight: number, total: number) => total > 0 ? `${Math.round(weight / total * 100)}%` : '—';
/** The facts every record of a list shares, said once for the whole list (e.g. "Overdue · Due Jun 12"). */
export function sharedPhrases(records: { fields: Record<string, FieldValue> }[]): string[] {
  const shared = constantFields(records);
  if (!shared.size) return [];
  const only = Object.fromEntries([...shared].map(k => [k, records[0].fields[k]]));
  return recordPhrases(only, new Set(), 3);
}
