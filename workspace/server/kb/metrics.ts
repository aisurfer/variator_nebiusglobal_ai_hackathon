// The brief metrics over the KB read model (the MVP has one role, the CEO, with every metric), computed as of a date
// from the versions effective by then: the number, a weekly series, a breakdown and the artefacts behind it. Formulas
// are plain code, never a model. A date is evaluated
// on the read model as known at the end of that date (Snapshot.view): versions recorded by then, links stale then
// (the as-of rule, README.md).
import type { KbAreaId, KbBriefArea, KbDisplay, KbMetricDef, KbMetricResult, KbOrganization, KbRecord, KbRoleId, KbStatus } from '../../src/kb/types.js';
import { ESCALATION, PRICE_TOPIC, REPLY_PREFIX, SENIOR, currencySymbol, dateText, engineeringOf, northStarOf } from './org.js';
import { TICKET_KINDS, type Item, type Mail, type Person, type Snapshot } from './snapshot.js';

export const DAY = 86_400_000;
export const OPEN = new Set(['In Progress', 'Review', 'Ready for QA', 'Reopened']);
export const CLOSED = new Set(['Done', "Won't Do"]);
export const CLIENT_KINDS = new Set(['client', 'prospect']);
/** Stages of a sales deal (provider sim) with an offer on the table. */
const OPEN_DEAL = new Set(['proposal', 'negotiation']);

export interface MetricCtx { s: Snapshot; t: number; tz: string; visible: Set<string>; metadata?: boolean }
export interface Computed { value: number | null; items?: KbRecord[]; breakdown?: { key: string; label: string; value: number }[]; sample?: number | null; caveats?: string[]; status?: KbStatus; baseline?: number | null; display?: KbDisplay }
/** A built-in metric. Its roles and area come from the brief layout (BRIEF), not from each definition. */
export interface Metric extends KbMetricDef { compute(c: MetricCtx): Computed; seriesless?: boolean }
type MetricBody = Omit<Metric, 'roles' | 'area'>;

// ------------------------------------------------------------------------------------------------ time
const partsFmt = new Map<string, Intl.DateTimeFormat>();
function local(t: number, tz: string) {
  let f = partsFmt.get(tz);
  if (!f) { f = new Intl.DateTimeFormat('en-GB', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', weekday: 'short', hourCycle: 'h23' }); partsFmt.set(tz, f); }
  const p = Object.fromEntries(f.formatToParts(new Date(t)).map(x => [x.type, x.value]));
  return { date: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour), minute: Number(p.minute), weekday: p.weekday };
}
export function localDate(t: number, tz: string): string { return local(t, tz).date; }
/** End of the local day `date` (YYYY-MM-DD) as an instant. */
export function endOfDay(date: string, tz: string): number {
  const guess = Date.parse(`${date}T23:59:59.999Z`);
  const l = local(guess, tz);
  const wall = Date.parse(`${l.date}T${String(l.hour).padStart(2, '0')}:${String(l.minute).padStart(2, '0')}:00Z`);
  return guess - (wall - Math.floor(guess / 60_000) * 60_000); // minus the zone offset at that instant
}
/** Working hours between two instants: Mon–Fri, 09:00–19:00 local, summed day by day. */
export function businessHours(a: number, b: number, tz: string): number {
  if (b <= a) return 0;
  let ms = 0;
  for (let date = localDate(a, tz); ; date = nextDate(date)) {
    const start = endOfDay(date, tz) + 1 - DAY;
    if (start >= b) break;
    const wd = new Date(`${date}T12:00:00Z`).getUTCDay();
    if (wd !== 0 && wd !== 6) ms += Math.max(0, Math.min(b, start + 19 * 3_600_000) - Math.max(a, start + 9 * 3_600_000));
  }
  return ms / 3_600_000;
}
export const nextDate = (d: string) => new Date(Date.parse(`${d}T12:00:00Z`) + DAY).toISOString().slice(0, 10);
export const median = (xs: number[]) => { if (!xs.length) return null; const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const round1 = (x: number | null) => x === null ? null : Math.round(x * 10) / 10;
const days = (ms: number) => Math.round(ms / DAY * 10) / 10;
const fmtDate = (iso: string | number) => typeof iso === 'number' ? new Date(iso).toISOString() : iso;

// ------------------------------------------------------------------------------------------------ helpers
const EXTERNAL_KINDS = new Set(['Invoice', 'Act', 'Contract', 'Amendment', 'CommercialProposal', 'Claim', 'Letter']);
/** A record for the viewer: hidden items are anonymised unless the role may see metadata of external material. */
export function rec(c: MetricCtx, id: string, kind: string, title: string, date: string | number | null, facts: KbRecord['facts'], extra: Partial<KbRecord> = {}, external = EXTERNAL_KINDS.has(kind)): KbRecord {
  const when = date === null ? null : fmtDate(date);
  if (c.visible.has(id)) return { id, kind, title, date: when, facts, ...extra };
  if (c.metadata && external) return { id, kind, title, date: when, facts, ...extra, restricted: true };
  return { id: '', kind, title: `Restricted ${kind.toLowerCase()}`, date: when?.slice(0, 10) ?? null, facts: {}, restricted: true };
}
const known = (c: MetricCtx, item: Item) => Date.parse(item.created) <= c.t;
export function docAmount(c: MetricCtx, item: Item): { amount: number; date: string | null; org: ReturnType<Snapshot['orgs']['get']> | null; number: string | null } {
  const v = c.s.at(item, c.t);
  const amount = Number(c.s.field(v, 'amount') ?? 0) || 0;
  // A simulated invoice (provider sim) has no counterparty or doc_date: it names the client and the vendor, its
  // direction (receivable: we bill the client; payable: a vendor bills us) and its issue date.
  const direction = c.s.field(v, 'direction');
  const orgId = c.s.field(v, 'counterparty')
    ?? (direction === 'receivable' ? c.s.field(v, 'client') : direction === 'payable' ? c.s.field(v, 'vendor') : null);
  return { amount, date: c.s.field(v, 'doc_date') ?? c.s.field(v, 'issued_at'), org: orgId ? c.s.orgs.get(orgId) ?? null : null, number: c.s.field(v, 'number') };
}
const inWindow = (date: string | null, c: MetricCtx, d: number) => { if (!date) return false; const t = Date.parse(`${date}T12:00:00Z`); return t <= c.t && t > c.t - d * DAY; };
export const clientMail = (m: Mail) => !m.internal && !!m.fromOrg && CLIENT_KINDS.has(m.fromOrg.kind);

/** Inbound client letters with the first internal reply in the same thread after them (by t). */
function clientExchanges(c: MetricCtx) {
  return c.s.mails.filter(m => m.at <= c.t && clientMail(m)).map(mail => ({ mail, reply: answered(c, mail) }));
}
function statusAt(c: MetricCtx, item: Item, t: number): string | null { return c.s.field(c.s.at(item, t), 'status'); }
export function transitions(item: Item): { at: number; from: string | null; to: string }[] {
  const out: { at: number; from: string | null; to: string }[] = [];
  let prev: string | null = null;
  for (const v of item.versions) {
    const st = v.fields?.status as string | undefined;
    if (st && st !== prev) { out.push({ at: Date.parse(v.effective_from), from: prev, to: st }); prev = st; }
  }
  return out;
}
function engineering(c: MetricCtx): Set<string> { const dept = engineeringOf(c.s.org); return new Set([...c.s.persons.values()].filter(p => p.department === dept).map(p => p.name)); }
function teamTickets(c: MetricCtx): Item[] {
  const team = engineering(c);
  return [...TICKET_KINDS].flatMap(k => c.s.kind(k)).filter(i => known(c, i) && i.versions.some(v => team.has(String(v.fields?.assignee ?? ''))));
}
/** Subject without reply/forward prefixes, for matching a reply sent as a new letter. */
const subjectKey = (s: string) => s.toLowerCase().replace(REPLY_PREFIX, '').replace(/\s+/g, ' ').trim();
/** Internal letters indexed by thread, by the letter they reply to and by (client domain, subject); time-sorted. */
const replyIndexes = new WeakMap<Snapshot, Map<string, Mail[]>>();
function replyIndex(s: Snapshot): Map<string, Mail[]> {
  let idx = replyIndexes.get(s);
  if (idx) return idx;
  idx = new Map();
  const add = (k: string, m: Mail) => (idx!.get(k) ?? idx!.set(k, []).get(k)!).push(m);
  for (const m of s.mails) {
    if (!m.internal) continue;
    add(`t|${m.threadId}`, m);
    if (m.inReplyTo) add(`r|${m.inReplyTo}`, m);
    for (const d of new Set(m.to.map(a => a.split('@').pop() ?? ''))) add(`s|${d}|${subjectKey(m.subject)}`, m);
  }
  replyIndexes.set(s, idx);
  return idx;
}
const firstAfter = (xs: Mail[] | undefined, after: number, until: number) => {
  if (!xs) return null;
  let lo = 0, hi = xs.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (xs[mid].at <= after) lo = mid + 1; else hi = mid; }
  return lo < xs.length && xs[lo].at <= until ? xs[lo] : null;
};
/** Did the company write to this client after `m` (same thread, a direct reply, or a letter with the same subject)? */
export function answered(c: MetricCtx, m: Mail): Mail | null {
  const idx = replyIndex(c.s);
  const domain = m.from.split('@').pop() ?? '';
  const hits = [firstAfter(idx.get(`t|${m.threadId}`), m.at, c.t), firstAfter(idx.get(`r|${m.messageId}`), m.at, c.t),
    firstAfter(idx.get(`s|${domain}|${subjectKey(m.subject)}`), m.at, c.t)].filter((x): x is Mail => !!x);
  return hits.sort((a, b) => a.at - b.at)[0] ?? null;
}
export function lastContact(c: MetricCtx, domain: string): Mail | null {
  let last: Mail | null = null;
  for (const m of c.s.mails) { if (m.at > c.t) break; if (m.from.endsWith(`@${domain}`) || m.to.some(x => x.endsWith(`@${domain}`))) last = m; }
  return last;
}

// ------------------------------------------------------------------------------------------------ north star
const num = (x: unknown) => { if (x === null || x === undefined || x === '') return null; const n = Number(String(x).replace(/\s/g, '').replace(',', '.')); return Number.isFinite(n) ? n : null; };
/** One weekly report: the North Star value and the secondary values by their dataset field names. */
export interface Kpi { item: Item; at: number; period: string; value: number; values: Record<string, number | null> }
const kpiCache = new WeakMap<Snapshot, Kpi[]>();
/** The company's weekly reports (pages labelled `northStar.reportLabel` whose `northStar.property` is a number),
 * oldest first; none when the company configures no North Star. */
export function kpiReports(s: Snapshot): Kpi[] {
  let out = kpiCache.get(s);
  if (out) return out;
  const ns = northStarOf(s.org);
  const label = ns?.reportLabel.toLowerCase() ?? '';
  out = !ns ? [] : [...s.kind('Report'), ...s.kind('Document')].flatMap(item => {
    const f = item.versions[item.versions.length - 1].fields ?? {};
    if (label && !(Array.isArray(f.labels) && f.labels.some(x => String(x).toLowerCase() === label))) return [];
    const p = (f.properties ?? {}) as Record<string, unknown>;
    const value = num(p[ns.property]);
    if (value === null) return [];
    return [{ item, at: Date.parse(item.created), period: ns.period ? String(p[ns.period] ?? '') : '', value, values: Object.fromEntries(ns.secondary.map(x => [x.field, num(p[x.property])])) }];
  }).sort((a, b) => a.at - b.at);
  kpiCache.set(s, out);
  return out;
}
const valueLabel = (title: string) => { const t = title.replace(/^(weekly|monthly|daily)\s+/i, ''); return t && !/^[A-Z]{2}/.test(t) ? t[0].toLowerCase() + t.slice(1) : t; };

// ------------------------------------------------------------------------------------------------ team activity
// Every version the knowledge base attributes to an employee (a letter, a ticket created, commented or moved, a merge
// request opened, reviewed or merged, a page written) is one action. Derived records (decisions and action items of a
// page) are not counted twice; meetings attended are counted separately.
const DERIVED_KINDS = new Set(['Decision', 'ActionItem', 'Organization', 'Person']);
interface TeamIndex { people: Person[]; actions: Map<string, number[]>; meetings: Map<string, number[]>; openByAssignee: (t: number) => Map<string, { item: Item; since: number; status: string }[]> }
const teamCache = new WeakMap<Snapshot, TeamIndex>();
const upper = (xs: number[], x: number) => { let lo = 0, hi = xs.length; while (lo < hi) { const mid = (lo + hi) >> 1; if (xs[mid] <= x) lo = mid + 1; else hi = mid; } return lo; };
const countIn = (xs: number[] | undefined, a: number, b: number) => (xs ? upper(xs, b) - upper(xs, a) : 0); // (a, b]
function teamIndex(s: Snapshot): TeamIndex {
  const hit = teamCache.get(s);
  if (hit) return hit;
  const people = [...s.persons.values()].filter(p => !p.external && !!p.department);
  const ids = new Set(people.map(p => p.id));
  const actions = new Map<string, number[]>(), meetings = new Map<string, number[]>();
  const push = (m: Map<string, number[]>, k: string, t: number) => (m.get(k) ?? m.set(k, []).get(k)!).push(t);
  for (const item of s.items.values()) {
    if (DERIVED_KINDS.has(item.kind)) continue;
    for (const v of item.versions) if (v.actor_id && ids.has(v.actor_id)) push(actions, v.actor_id, Date.parse(v.effective_from));
    if (item.kind === 'Meeting') {
      const f = item.versions[0].fields ?? {};
      const at = Date.parse(String(f.held_at ?? item.created));
      for (const name of (Array.isArray(f.attendees) ? f.attendees : []) as string[]) { const p = s.personByName.get(String(name)); if (p && ids.has(p.id)) push(meetings, p.id, at); }
    }
  }
  for (const m of [actions, meetings]) for (const xs of m.values()) xs.sort((a, b) => a - b);
  const tickets = [...TICKET_KINDS].flatMap(k => s.kind(k));
  const openMemo = new Map<number, Map<string, { item: Item; since: number; status: string }[]>>();
  const openByAssignee = (t: number) => {
    let out = openMemo.get(t);
    if (out) return out;
    out = new Map();
    for (const item of tickets) {
      const v = s.at(item, t);
      const st = s.field(v, 'status'), who = s.field(v, 'assignee');
      if (!v || !st || !who || !OPEN.has(st)) continue;
      (out.get(who) ?? out.set(who, []).get(who)!).push({ item, since: Date.parse(v.effective_from), status: st });
    }
    openMemo.set(t, out);
    return out;
  };
  const idx = { people, actions, meetings, openByAssignee };
  teamCache.set(s, idx);
  return idx;
}
export interface PersonWeek { person: Person; actions: number; usual: number | null; meetings: number; idle: { item: Item; since: number; status: string }[]; unanswered: Mail[]; activity: number | null; score: number | null; present: boolean }
const scoreMemo = new WeakMap<Snapshot, Map<number, PersonWeek[]>>();
/** Each employee's week ending at t: actions against their own usual week (median of the previous eight weeks with any
 * activity), work idle a week or longer, client letters to them unanswered for two working days. */
export function teamWeek(s: Snapshot, t: number, tz: string): PersonWeek[] {
  const memo = scoreMemo.get(s) ?? scoreMemo.set(s, new Map()).get(s)!;
  const hit = memo.get(t);
  if (hit) return hit;
  const idx = teamIndex(s);
  const ctx: MetricCtx = { s, t, tz, visible: new Set() };
  const open = idx.openByAssignee(t);
  const inbound = s.mails.filter(m => m.at <= t && m.at > t - 14 * DAY && clientMail(m));
  const out = idx.people.map(person => {
    const xs = idx.actions.get(person.id);
    const actions = countIn(xs, t - 7 * DAY, t);
    const prior = Array.from({ length: 8 }, (_, k) => countIn(xs, t - (k + 2) * 7 * DAY, t - (k + 1) * 7 * DAY)).filter(n => n > 0);
    const usual = prior.length >= 2 ? median(prior) : null;
    const idle = (open.get(person.name) ?? []).filter(x => x.since <= t - 7 * DAY);
    const unanswered = person.email ? inbound.filter(m => m.to.includes(person.email) && businessHours(m.at, t, tz) >= 20 && !answered(ctx, m)) : [];
    const present = actions > 0;
    const activity = usual && present ? Math.min(100, 100 * actions / usual) : null;
    const score = activity === null ? null : Math.max(0, Math.min(100, Math.round(activity - 8 * Math.min(idle.length, 5) - 6 * Math.min(unanswered.length, 5))));
    return { person, actions, usual, meetings: countIn(idx.meetings.get(person.id), t - 7 * DAY, t), idle, unanswered, activity, score, present };
  });
  memo.set(t, out);
  return out;
}
const teamValue = (s: Snapshot, t: number, tz: string) => { const xs = teamWeek(s, t, tz).map(p => p.score).filter((x): x is number => x !== null); return xs.length ? Math.round(xs.reduce((a, b) => a + b, 0) / xs.length) : null; };
/** A reason to talk: a much quieter week than usual, two or more idle tickets, or a client waiting for them. */
const needsAttention = (p: PersonWeek) => p.present && ((!!p.usual && p.actions < p.usual * 0.6) || p.idle.length >= 2 || p.unanswered.length >= 1);
function attentionReasons(p: PersonWeek): string[] {
  const out: string[] = [];
  if (p.usual && p.actions < p.usual * 0.6) out.push(`activity ${Math.round(100 * p.actions / p.usual)}% of usual (${p.actions} vs ${p.usual} a week)`);
  if (p.idle.length) out.push(`${p.idle.length} ticket${p.idle.length > 1 ? 's' : ''} without movement 7+ days`);
  if (p.unanswered.length) out.push(`${p.unanswered.length} client letter${p.unanswered.length > 1 ? 's' : ''} unanswered 2+ working days`);
  return out;
}

// ------------------------------------------------------------------------------------------------ definitions
const DEFINITIONS: MetricBody[] = [
  {
    id: 'company.northStar', view: 'kpi', unit: 'count', direction: 'higher', target: null, bands: null,
    title: 'North Star', question: 'Is the company’s North Star moving the right way?',
    definition: 'The company’s North Star from its weekly report. The company has not configured one yet (northStar in config/company.json).',
    howCalculated: ['The latest weekly report published by the date.', 'Its North Star value, as configured for the company.'],
    sources: ['Confluence weekly metrics report'],
    compute(c) {
      const ns = northStarOf(c.s.org);
      if (!ns) return { value: null, caveats: ['No North Star is configured for the company.'] };
      const rows = kpiReports(c.s).filter(k => k.at <= c.t);
      const last = rows[rows.length - 1];
      if (!last) return { value: null, caveats: ['No weekly metrics report has been published by this date.'] };
      const base = rows[Math.max(0, rows.length - 5)], span = rows.length - 1 - rows.indexOf(base);
      const pace = span ? (last.value - base.value) / span : null;
      const sign = (x: number) => `${x >= 0 ? '+' : '−'}${Math.abs(x).toFixed(1)}`;
      const fmt = (x: number | null) => (x === null ? '—' : x.toLocaleString('en-US'));
      const items = [...rows].reverse().map(k => rec(c, k.item.id, 'Report', k.item.title, k.at, { [valueLabel(ns.title)]: k.value, ...Object.fromEntries(ns.secondary.map(x => [x.label, k.values[x.field]])) }, {}, false));
      const summary = ns.secondary.filter(x => x.summary).map(x => /,\s*%$/.test(x.label) ? `${x.label.replace(/,\s*%$/, '')} ${fmt(last.values[x.field])}%` : `${x.label} ${fmt(last.values[x.field])}`);
      const latest = `Latest report: ${last.period || localDate(last.at, c.tz)}${summary.map(x => ` · ${x}`).join('')}.`;
      if (ns.target === null || !ns.by) return { value: last.value, items, status: 'neutral', caveats: [latest] };
      const need = (ns.target - last.value) / Math.max(1, (Date.parse(`${ns.by}T23:59:59Z`) - c.t) / (7 * DAY));
      const status: KbStatus = need <= 0 ? 'good' : pace === null ? 'unknown' : pace >= need ? 'good' : pace >= need / 2 ? 'warn' : 'bad';
      // the status is the pace towards a dated goal, not «value ≥ target»: the tile names the goal in its note
      return { value: last.value, items, status, display: { note: `Goal ${ns.target.toLocaleString('en-US')} by ${dateText(ns.by)}: on track when the average weekly gain of the last reports reaches the gain still needed each week.` }, caveats: [
        `Needs ${sign(need)} ${ns.unit} a week to reach ${ns.target} by ${dateText(ns.by)}; the last ${span || 1} report${span === 1 ? '' : 's'} averaged ${pace === null ? '—' : sign(pace)}.`,
        latest,
      ] };
    },
  },
  {
    id: 'team.activity', view: 'kpi', unit: 'score', direction: 'higher', target: null, bands: { good: 80, warn: 65 }, itemsLabel: 'People needing attention',
    title: 'Team activity index', question: 'Is the team working at its usual pace, and who may need help?',
    definition: 'A 0–100 index of how the week went for the team compared with each person’s own usual week: their actions in mail, Jira, GitLab and Confluence against their typical weekly volume, minus work left without movement for a week and client letters left unanswered. People with no trace at all in the week (leave, sick days) are left out, not counted as zero.',
    howCalculated: [
      'For every employee: actions in the last 7 days — letters sent, tickets created, commented or moved, merge requests opened, reviewed or merged, pages written.',
      'Their usual week: the median of the previous eight weeks that had any activity (at least two such weeks are needed).',
      'Personal score = min(100, 100 × actions ÷ usual) − 8 per ticket assigned to them in progress without movement 7+ days − 6 per client letter to them unanswered 2+ working days (each capped at 5), within 0–100.',
      'The index is the average personal score of everyone present in the week; the baseline is the index averaged over the previous eight weeks.',
      'People needing attention: activity below 60% of their usual week, two or more idle tickets, or a client letter waiting for them — a reason to talk, not a rating.',
    ],
    sources: ['Mail', 'Jira', 'GitLab', 'Confluence', 'Directory'],
    compute(c) {
      const week = teamWeek(c.s, c.t, c.tz);
      const value = teamValue(c.s, c.t, c.tz);
      const past = Array.from({ length: 8 }, (_, k) => teamValue(c.s, c.t - (k + 1) * 7 * DAY, c.tz)).filter((x): x is number => x !== null);
      const baseline = past.length ? Math.round(past.reduce((a, b) => a + b, 0) / past.length) : null;
      const scored = week.filter(p => p.score !== null);
      const attention = week.filter(needsAttention).sort((a, b) => (a.score ?? 101) - (b.score ?? 101));
      const items = attention.map(p => rec(c, p.person.id, 'Person', p.person.name, null, { department: p.person.department, score: p.score, why: attentionReasons(p).join('; '), 'actions this week': p.actions, 'usual week': p.usual, meetings: p.meetings }, {}, false));
      const byDept = new Map<string, number[]>();
      scored.forEach(p => (byDept.get(p.person.department!) ?? byDept.set(p.person.department!, []).get(p.person.department!)!).push(p.score!));
      const absent = week.filter(p => !p.present && p.usual !== null).map(p => p.person.name);
      return { value, items, baseline, sample: scored.length,
        breakdown: [...byDept].map(([k, v]) => ({ key: k, label: k, value: Math.round(v.reduce((a, b) => a + b, 0) / v.length) })).sort((a, b) => a.value - b.value),
        caveats: [
          `Baseline (average of the previous ${past.length} weeks): ${baseline ?? '—'}. ${attention.length} ${attention.length === 1 ? 'person needs' : 'people need'} attention.`,
          ...(absent.length ? [`No trace this week, left out: ${absent.join(', ')}.`] : []),
        ] };
    },
  },
  {
    id: 'clients.awaiting', view: 'list', unit: 'count', direction: 'lower', target: 0, bands: { good: 1, warn: 3 },
    title: 'Client letters awaiting a reply', question: 'Which clients are waiting for us right now?',
    definition: 'Letters from clients and prospects of the last 14 days that nobody from the company has answered for at least one working day — neither in the thread nor in a new letter with the same subject.',
    howCalculated: ['Inbound letters from client and prospect domains received in the last 14 days; in each thread only the latest one counts.', 'Answered when the company wrote later in the same thread, replied to it, or wrote to that client with the same subject.', 'Count the unanswered ones older than 8 working hours (Mon–Fri, 09:00–19:00 local); older silences are reported separately.'],
    sources: ['Mail'],
    compute(c) {
      const items: KbRecord[] = [];
      let older = 0;
      for (const thread of c.s.threads.values()) {
        const inbound = thread.filter(m => m.at <= c.t && clientMail(m));
        const last = inbound[inbound.length - 1];
        if (!last || answered(c, last)) continue;
        const waited = businessHours(last.at, c.t, c.tz);
        if (waited < 8) continue;
        if (last.at < c.t - 14 * DAY) { older++; continue; }
        items.push(rec(c, last.id, 'Email', last.subject, last.at, { client: last.fromOrg!.name, from: last.fromPerson?.name ?? last.from, 'working days': Math.round(waited / 10 * 10) / 10 }, {}, true));
      }
      items.sort((a, b) => Number(b.facts['working days'] ?? 0) - Number(a.facts['working days'] ?? 0));
      return { value: items.length, items, breakdown: tally(items, 'client'), caveats: older ? [`${older} older client letters (over 14 days) were never answered in writing.`] : [] };
    },
  },
  {
    id: 'clients.firstReply', view: 'kpi', unit: 'hours', direction: 'lower', target: 4, bands: { good: 4, warn: 8 },
    title: 'First reply to clients', question: 'How quickly do clients hear back from us?',
    definition: 'Median working hours between a client’s letter and the company’s first answer to it, for letters received in the last 30 days.',
    howCalculated: ['Inbound letters from client and prospect domains received in the last 30 days.', 'The first company letter after it in the same thread, in reply to it, or to that client with the same subject.', 'Working hours between them (Mon–Fri, 09:00–19:00); the median. Unanswered letters are in «Client letters awaiting a reply».'],
    sources: ['Mail'],
    compute(c) {
      const rows = clientExchanges(c).filter(x => x.mail.at > c.t - 30 * DAY);
      const answered = rows.filter(x => x.reply).map(x => ({ ...x, h: businessHours(x.mail.at, x.reply!.at, c.tz) }));
      const byClient = new Map<string, number[]>();
      answered.forEach(x => (byClient.get(x.mail.fromOrg!.name) ?? byClient.set(x.mail.fromOrg!.name, []).get(x.mail.fromOrg!.name)!).push(x.h));
      const items = answered.sort((a, b) => b.h - a.h).slice(0, 12).map(x => rec(c, x.mail.id, 'Email', x.mail.subject, x.mail.at, { client: x.mail.fromOrg!.name, 'reply after, h': round1(x.h), 'answered by': x.reply!.fromPerson?.name ?? x.reply!.from }, {}, true));
      return { value: round1(median(answered.map(x => x.h))), items, sample: answered.length, breakdown: [...byClient].map(([k, v]) => ({ key: k, label: k, value: round1(median(v)) ?? 0 })).sort((a, b) => b.value - a.value), caveats: answered.length < 8 ? [`Only ${answered.length} answered letters in the window.`] : [] };
    },
  },
  {
    id: 'clients.escalations', view: 'list', unit: 'count', direction: 'lower', target: 0, bands: { good: 0, warn: 2 },
    title: 'Client escalations · 14 days', question: 'Where is a client relationship heating up?',
    definition: 'Letters from clients in the last 14 days that either come from their senior management or carry escalation language (urgent, claim, complaint, demand, penalty, termination, apology).',
    howCalculated: ['Inbound letters from client and prospect domains in the last 14 days.', 'Sender’s role from the contact directory, or escalation words in the subject.', 'Count; open each letter for the thread.'],
    sources: ['Mail', 'Directory'],
    compute(c) {
      const items = c.s.mails.filter(m => clientMail(m) && m.at <= c.t && m.at > c.t - 14 * DAY && (ESCALATION.test(m.subject) || SENIOR.test(m.fromPerson?.role ?? '')))
        .map(m => rec(c, m.id, 'Email', m.subject, m.at, { client: m.fromOrg!.name, from: m.fromPerson ? `${m.fromPerson.name} · ${m.fromPerson.role}` : m.from, why: ESCALATION.test(m.subject) ? 'escalation wording' : 'senior sender' }, {}, true))
        .reverse();
      return { value: items.length, items, breakdown: tally(items, 'client') };
    },
  },
  {
    id: 'money.invoiced', view: 'kpi', unit: 'money', direction: 'higher', target: null, bands: null,
    title: 'Invoiced to clients · 30 days', question: 'How much did we bill clients this month?',
    definition: 'Sum of invoices the company issued to client organisations, by invoice date, over the last 30 days. Documents, not cash: payment is not visible in these sources.',
    howCalculated: ['Invoices attached to company letters whose counterparty is a client.', 'Invoice date within the last 30 days.', 'Sum of amounts ({currency}); split by client.'],
    sources: ['Mail attachments'],
    compute(c) {
      const docs = c.s.kind('Invoice').filter(i => known(c, i)).map(i => ({ i, ...docAmount(c, i) })).filter(d => d.org?.kind === 'client' && inWindow(d.date, c, 30));
      const items = docs.map(d => rec(c, d.i.id, 'Invoice', d.i.title, d.date, { client: d.org!.name, number: d.number, amount: d.amount }));
      return { value: docs.reduce((a, d) => a + d.amount, 0), items, breakdown: sumBy(docs.map(d => [d.org!.name, d.amount])) };
    },
  },
  {
    id: 'money.payables', view: 'kpi', unit: 'money', direction: 'context', target: null, bands: null,
    title: 'Supplier bills · 30 days', question: 'What did suppliers bill us this month?',
    definition: 'Sum of invoices received from suppliers, the landlord and other non-client counterparties, by invoice date, over the last 30 days.',
    howCalculated: ['Invoices whose counterparty is not a client or prospect.', 'Invoice date within the last 30 days.', 'Sum of amounts ({currency}); split by counterparty.'],
    sources: ['Mail attachments'],
    compute(c) {
      const docs = c.s.kind('Invoice').filter(i => known(c, i)).map(i => ({ i, ...docAmount(c, i) })).filter(d => d.org && !CLIENT_KINDS.has(d.org.kind) && inWindow(d.date, c, 30));
      const items = docs.map(d => rec(c, d.i.id, 'Invoice', d.i.title, d.date, { counterparty: d.org!.name, number: d.number, amount: d.amount }));
      return { value: docs.reduce((a, d) => a + d.amount, 0), items, breakdown: sumBy(docs.map(d => [d.org!.name, d.amount])) };
    },
  },
  {
    id: 'decisions.staleWork', view: 'list', unit: 'count', direction: 'lower', target: 0, bands: { good: 2, warn: 6 },
    title: 'Work resting on changed grounds', question: 'What still relies on a document or decision that has since changed?',
    definition: 'Declared dependencies (meeting notes that consider a document, tasks that implement a decision…) whose source changed after the dependent was written and the dependent was not revisited.',
    howCalculated: ['Causal links of the knowledge base with status STALE: the cause has a later version that touched the fields the link depends on.', 'Counted as of the date the cause changed.', 'Each row names the dependent and the changed source.'],
    sources: ['Knowledge graph'],
    compute(c) {
      const rows = c.s.stale.filter(x => Date.parse(x.changedAt) <= c.t);
      const items = rows.map(x => rec(c, x.effectId, x.effectKind, x.effectTitle, x.changedAt, { relies: x.predicate }, { relatedId: c.visible.has(x.causeId) ? x.causeId : null, relatedTitle: x.causeTitle, relatedLabel: 'changed source' }));
      return { value: rows.length, items: items.reverse() };
    },
  },
  {
    id: 'deals.agreements', view: 'list', unit: 'count', direction: 'context', target: null, bands: null,
    title: 'Contracts and amendments · 30 days', question: 'Which commitments did we sign or receive?',
    definition: 'Contracts and amendments that arrived or were sent in the last 30 days, with the amount when the document states one.',
    howCalculated: ['Contract and amendment documents attached to letters.', 'Recorded in the last 30 days.', 'Count; amounts as stated in the document.'],
    sources: ['Mail attachments'],
    compute(c) {
      const docs = [...c.s.kind('Contract'), ...c.s.kind('Amendment')].filter(i => known(c, i) && Date.parse(i.created) > c.t - 30 * DAY).map(i => ({ i, ...docAmount(c, i) }));
      const items = docs.sort((a, b) => b.i.created.localeCompare(a.i.created)).map(d => rec(c, d.i.id, d.i.kind, d.i.title, d.i.created, { counterparty: d.org?.name ?? null, amount: d.amount || null }));
      return { value: docs.length, items, breakdown: tally(items, 'counterparty') };
    },
  },
  {
    id: 'deals.proposals', view: 'kpi', unit: 'money', direction: 'higher', target: null, bands: null,
    title: 'Open proposals', question: 'What business is on the table and not yet contracted?',
    definition: 'Business offered to clients and prospects and not yet contracted. With sales deals in the knowledge base (a simulated history): the deals whose stage on the date is proposal or negotiation. Else: the latest commercial proposal to each client or prospect in the last 90 days that has not been followed by a contract with that counterparty.',
    howCalculated: ['Sales deals (Deal records), when the knowledge base has them: the stage of each deal as known at the end of the date.', 'Open: the stage is proposal or negotiation (not lead, qualified, won or lost).', 'Without deals: commercial proposals with a client or prospect counterparty, the latest per counterparty within 90 days, dropped when a later contract with that counterparty exists.', 'Sum of amounts ({currency}).'],
    sources: ['Sales deals', 'Mail attachments', 'Mail'],
    itemsLabel: 'Open proposals, largest first',
    compute(c) {
      const deals = c.s.kind('Deal').filter(i => known(c, i));
      if (deals.length) {
        const open = deals.map(i => ({ i, v: c.s.at(i, c.t) })).filter(x => OPEN_DEAL.has(String(c.s.field(x.v, 'status') ?? '')))
          .map(({ i, v }) => ({ i, v, amount: Number(c.s.field(v, 'amount') ?? 0) || 0, client: c.s.field(v, 'client_name') ?? c.s.orgs.get(c.s.field(v, 'client') ?? '')?.name ?? null }))
          .sort((a, b) => b.amount - a.amount);
        const items = open.map(d => rec(c, d.i.id, 'Deal', d.i.title, d.v!.effective_from, { client: d.client, stage: c.s.field(d.v, 'status'), amount: d.amount, 'expected close': c.s.field(d.v, 'expected_close'), owner: c.s.field(d.v, 'owner_name') }, {}, false));
        return { value: open.reduce((a, d) => a + d.amount, 0), items, sample: deals.length, breakdown: sumBy(open.map(d => [d.client ?? '—', d.amount])) };
      }
      const latest = new Map<string, { i: Item; amount: number; date: string | null; org: NonNullable<ReturnType<typeof docAmount>['org']>; number: string | null }>();
      for (const i of c.s.kind('CommercialProposal').filter(x => known(c, x))) {
        const d = docAmount(c, i);
        if (!d.org || !CLIENT_KINDS.has(d.org.kind) || !inWindow(d.date, c, 90)) continue;
        const prev = latest.get(d.org.id);
        if (!prev || (d.date ?? '') > (prev.date ?? '')) latest.set(d.org.id, { i, ...d, org: d.org });
      }
      const contracted = (orgId: string, after: string) => c.s.kind('Contract').some(i => known(c, i) && docAmount(c, i).org?.id === orgId && i.created.slice(0, 10) > after);
      const open = [...latest.values()].filter(d => !contracted(d.org.id, d.date ?? ''));
      const items = open.map(d => { const lc = lastContact(c, d.org.domain); return rec(c, d.i.id, 'CommercialProposal', d.i.title, d.date, { client: d.org.name, amount: d.amount, 'last contact': lc ? localDate(lc.at, c.tz) : null, 'days silent': lc ? days(c.t - lc.at) : null }); });
      return { value: open.reduce((a, d) => a + d.amount, 0), items, breakdown: sumBy(open.map(d => [d.org.name, d.amount])) };
    },
  },
  {
    id: 'deals.silent', view: 'list', unit: 'count', direction: 'lower', target: 0, bands: { good: 0, warn: 1 },
    title: 'Prospects without contact · 7+ days', question: 'Which deals are going quiet?',
    definition: 'Prospects, and clients with an open proposal, whose last letter in either direction is more than seven days old.',
    howCalculated: ['Prospect organisations from the directory plus counterparties of open proposals.', 'Last letter to or from their domain as of the date.', 'Count those silent for more than 7 days.'],
    sources: ['Mail', 'Directory'],
    compute(c) {
      const orgs = [...c.s.orgs.values()].filter(o => o.kind === 'prospect' && o.domain);
      const items: KbRecord[] = [];
      for (const o of orgs) {
        const lc = lastContact(c, o.domain);
        if (!lc) continue;
        const silent = days(c.t - lc.at);
        if (silent > 7) items.push(rec(c, lc.id, 'Email', lc.subject, lc.at, { prospect: o.name, 'days silent': silent, 'last from': lc.internal ? 'us' : 'them' }, {}, true));
      }
      return { value: items.length, items: items.sort((a, b) => Number(b.facts['days silent']) - Number(a.facts['days silent'])) };
    },
  },
  {
    id: 'deals.discounts', view: 'list', unit: 'count', direction: 'context', target: null, bands: null,
    title: 'Discounts and term changes · 60 days', question: 'Which commercial terms did we change for clients?',
    definition: 'Amendments to client contracts and company letters to clients about discounts or prices in the last 60 days.',
    howCalculated: ['Amendment documents with a client counterparty.', 'Company letters to client domains whose subject mentions a discount, price or tariff.', 'Count, newest first.'],
    sources: ['Mail attachments', 'Mail'],
    compute(c) {
      const amend = c.s.kind('Amendment').filter(i => known(c, i) && Date.parse(i.created) > c.t - 60 * DAY).map(i => ({ i, d: docAmount(c, i) })).filter(x => x.d.org && CLIENT_KINDS.has(x.d.org.kind));
      const letters = c.s.mails.filter(m => m.internal && m.at <= c.t && m.at > c.t - 60 * DAY && PRICE_TOPIC.test(m.subject) && m.to.some(a => { const o = c.s.orgOfAddress(a); return !!o && CLIENT_KINDS.has(o.kind); }));
      const items = [
        ...amend.map(x => rec(c, x.i.id, 'Amendment', x.i.title, x.i.created, { client: x.d.org!.name })),
        ...letters.map(m => rec(c, m.id, 'Email', m.subject, m.at, { client: m.to.map(a => c.s.orgOfAddress(a)?.name).find(Boolean) ?? null, by: m.fromPerson?.name ?? m.from }, {}, true)),
      ].sort((a, b) => String(b.date).localeCompare(String(a.date)));
      return { value: items.length, items, breakdown: tally(items, 'client') };
    },
  },
  {
    id: 'eng.throughput', view: 'kpi', unit: 'count', direction: 'higher', target: null, bands: null,
    title: 'Tickets done · 7 days', question: 'How much engineering work did we finish this week?',
    definition: 'Jira tickets of the engineering team (assignee in the {engineering} department) that moved to Done in the last 7 days.',
    howCalculated: ['Tickets ever assigned to an engineer.', 'Status transitions to Done within the last 7 days, from the version history.', 'Count; tickets created in the same week are shown for comparison.'],
    sources: ['Jira'],
    compute(c) {
      const tickets = teamTickets(c);
      const done = tickets.filter(i => transitions(i).some(x => x.to === 'Done' && x.at <= c.t && x.at > c.t - 7 * DAY));
      const created = tickets.filter(i => Date.parse(i.created) > c.t - 7 * DAY).length;
      const items = done.map(i => rec(c, i.id, i.kind, i.title, transitions(i).filter(x => x.to === 'Done').pop()!.at, { assignee: c.s.field(c.s.at(i, c.t), 'assignee') }));
      return { value: done.length, items, caveats: [`${created} new tickets in the same 7 days.`] };
    },
  },
  {
    id: 'eng.wip', view: 'breakdown', unit: 'count', direction: 'context', target: null, bands: null,
    title: 'Work in progress', question: 'What is the team carrying right now, and what is stuck?',
    definition: 'Engineering tickets in progress, in review, ready for QA or reopened as of the date; rows sorted by how long they have been in that status.',
    howCalculated: ['Status of each team ticket as of the date.', 'Count those in an active status; split by status.', 'Days in the current status from the last status change.'],
    sources: ['Jira'],
    compute(c) {
      const rows = teamTickets(c).map(i => ({ i, st: statusAt(c, i, c.t), since: transitions(i).filter(x => x.at <= c.t).pop()?.at ?? Date.parse(i.created) })).filter(x => x.st && OPEN.has(x.st));
      const items = rows.sort((a, b) => a.since - b.since).map(x => rec(c, x.i.id, x.i.kind, x.i.title, x.since, { status: x.st, 'days in status': days(c.t - x.since), assignee: c.s.field(c.s.at(x.i, c.t), 'assignee') }));
      return { value: rows.length, items, breakdown: tally(items, 'status') };
    },
  },
  {
    id: 'eng.cycle', view: 'kpi', unit: 'days', direction: 'lower', target: 5, bands: { good: 5, warn: 10 },
    title: 'Cycle time', question: 'How long does a ticket take once work starts?',
    definition: 'Median calendar days from the first move to In Progress to Done, for engineering tickets finished in the last 60 days.',
    howCalculated: ['Team tickets that reached Done in the last 60 days.', 'From the first In Progress to the first Done after it.', 'Median in days; the sample size is shown.'],
    sources: ['Jira'],
    compute(c) {
      const rows = teamTickets(c).map(i => { const tr = transitions(i); const start = tr.find(x => x.to === 'In Progress' && x.at <= c.t); const done = start && tr.find(x => x.to === 'Done' && x.at > start.at && x.at <= c.t); return { i, start, done }; })
        .filter(x => x.start && x.done && x.done.at > c.t - 60 * DAY).map(x => ({ ...x, d: (x.done!.at - x.start!.at) / DAY }));
      const items = rows.sort((a, b) => b.d - a.d).map(x => rec(c, x.i.id, x.i.kind, x.i.title, x.done!.at, { 'cycle days': round1(x.d) }));
      return { value: round1(median(rows.map(x => x.d))), items, sample: rows.length, caveats: rows.length < 5 ? [`Small sample: ${rows.length} finished tickets.`] : [] };
    },
  },
  {
    id: 'eng.reopened', view: 'list', unit: 'count', direction: 'lower', target: 0, bands: { good: 0, warn: 2 },
    title: 'Reopened after review or QA · 30 days', question: 'Where does work bounce back?',
    definition: 'Status changes from Review, Ready for QA or Done back to an earlier status in the last 30 days.',
    howCalculated: ['Status history of team tickets.', 'Transitions from Review / Ready for QA / Done to Reopened, In Progress or To Do within 30 days.', 'Count of such bounces.'],
    sources: ['Jira'],
    compute(c) {
      const items: KbRecord[] = [];
      for (const i of teamTickets(c)) for (const x of transitions(i)) if (x.at <= c.t && x.at > c.t - 30 * DAY && x.from && ['Review', 'Ready for QA', 'Done'].includes(x.from) && ['Reopened', 'In Progress', 'To Do'].includes(x.to)) items.push(rec(c, i.id, i.kind, i.title, x.at, { from: x.from, to: x.to }));
      return { value: items.length, items: items.reverse() };
    },
  },
  {
    id: 'eng.reviewQueue', view: 'list', unit: 'count', direction: 'lower', target: 0, bands: { good: 1, warn: 3 },
    title: 'Merge requests waiting 2+ days', question: 'Is code review the bottleneck?',
    definition: 'Merge requests opened at least two days before the date and still neither merged nor closed.',
    howCalculated: ['GitLab merge requests and their state history.', 'Open as of the date and created more than 48 hours earlier.', 'Count; the median time to merge over the last 30 days is shown for context.'],
    sources: ['GitLab'],
    compute(c) {
      const mrs = c.s.kind('MergeRequest').filter(i => known(c, i));
      const open = mrs.map(i => ({ i, v: c.s.at(i, c.t) })).filter(x => x.v && c.s.field(x.v, 'state') === 'opened' && Date.parse(x.i.created) < c.t - 2 * DAY);
      const merged = mrs.map(i => { const v = c.s.at(i, c.t); const m = c.s.field(v, 'merged_at'); return m ? (Date.parse(m) - Date.parse(String(c.s.field(v, 'created') ?? i.created))) / 3_600_000 : null; }).filter((h): h is number => h !== null && h >= 0);
      const items = open.sort((a, b) => a.i.created.localeCompare(b.i.created)).map(x => rec(c, x.i.id, 'MergeRequest', x.i.title, x.i.created, { 'days open': days(c.t - Date.parse(x.i.created)), author: c.s.field(x.v, 'author') }));
      const m = median(merged);
      return { value: open.length, items, caveats: [m === null ? 'No merge requests merged yet.' : `Median time to merge: ${round1(m)} h over ${merged.length} merged.`] };
    },
  },
  {
    id: 'eng.bugs', view: 'list', unit: 'count', direction: 'lower', target: 0, bands: { good: 2, warn: 5 },
    title: 'Open high-priority bugs', question: 'What could hurt customers right now?',
    definition: 'Bugs with priority Highest or High that are not Done or Won’t Do as of the date.',
    howCalculated: ['Jira bugs and their versions.', 'Priority and status as of the date.', 'Count; oldest first.'],
    sources: ['Jira'],
    compute(c) {
      const rows = c.s.kind('Bug').filter(i => known(c, i)).map(i => ({ i, v: c.s.at(i, c.t) })).filter(x => x.v && ['Highest', 'High'].includes(String(c.s.field(x.v, 'priority'))) && !CLOSED.has(String(c.s.field(x.v, 'status'))));
      const items = rows.sort((a, b) => a.i.created.localeCompare(b.i.created)).map(x => rec(c, x.i.id, 'Bug', x.i.title, x.i.created, { priority: c.s.field(x.v, 'priority'), status: c.s.field(x.v, 'status'), 'age, days': days(c.t - Date.parse(x.i.created)) }));
      return { value: rows.length, items };
    },
  },
];

// ------------------------------------------------------------------------------------------------ the brief layout
/** The MVP has one user role, the CEO, and the CEO brief shows every metric: a short top row of key numbers, then
 * three areas. Every metric is in exactly one place (tests/kb.test.ts checks it). */
export const MVP_ROLE: KbRoleId = 'ceo';
export const BRIEF: { top: string[]; areas: KbBriefArea[] } = {
  top: ['company.northStar', 'money.invoiced', 'clients.awaiting', 'team.activity', 'eng.bugs'],
  areas: [
    { id: 'money', title: 'Money', question: 'What do we bill, what do we owe, and which commitments did we sign?',
      metrics: ['money.payables', 'deals.agreements'] },
    { id: 'clients', title: 'Clients and growth', question: 'Do clients hear back from us, and is new business moving?',
      metrics: ['clients.firstReply', 'deals.proposals', 'clients.escalations', 'deals.silent'] },
    { id: 'delivery', title: 'Delivery and service', question: 'Does the team finish its work, and what slows it down?',
      metrics: ['eng.throughput', 'eng.cycle', 'eng.wip', 'eng.reviewQueue', 'eng.reopened', 'decisions.staleWork'] },
  ],
};
/** Metrics not on the brief: they read Plancy document kinds (amendments, price letters) that the simulated history
 * does not have, so they would always show 0. They still open by id. */
export const OFF_BRIEF: { id: string; area: KbAreaId }[] = [{ id: 'deals.discounts', area: 'money' }];
const TOP_AREA: Record<string, KbAreaId> = { 'company.northStar': 'clients', 'money.invoiced': 'money', 'clients.awaiting': 'clients', 'team.activity': 'delivery', 'eng.bugs': 'delivery' };
const areaOf = (id: string): KbAreaId | undefined => TOP_AREA[id] ?? BRIEF.areas.find(a => a.metrics.includes(id))?.id ?? OFF_BRIEF.find(x => x.id === id)?.area;
/** Every metric in the order of the brief: the top row, then the areas; the metrics off the brief last. */
const ORDER = [...BRIEF.top, ...BRIEF.areas.flatMap(a => a.metrics), ...OFF_BRIEF.map(x => x.id)];
const ON_BRIEF = new Set([...BRIEF.top, ...BRIEF.areas.flatMap(a => a.metrics)]);
export const METRICS: Metric[] = DEFINITIONS
  .map(d => ({ ...d, roles: [MVP_ROLE], area: areaOf(d.id) }) as Metric)
  .sort((a, b) => ORDER.indexOf(a.id) - ORDER.indexOf(b.id));

function tally(items: KbRecord[], fact: string) {
  const m = new Map<string, number>();
  items.forEach(i => { const k = i.facts[fact] !== undefined ? String(i.facts[fact] ?? '—') : i.restricted ? 'Restricted' : '—'; m.set(k, (m.get(k) ?? 0) + 1); });
  return [...m].map(([k, v]) => ({ key: k, label: k, value: v })).sort((a, b) => b.value - a.value);
}
function sumBy(rows: [string, number][]) {
  const m = new Map<string, number>();
  rows.forEach(([k, v]) => m.set(k, (m.get(k) ?? 0) + v));
  return [...m].map(([k, v]) => ({ key: k, label: k, value: v })).sort((a, b) => b.value - a.value);
}

/** The status a tile shows next to «target X»: on track (good) exactly when the value meets the target; a value
 * that misses it is never good (the bands give warn or bad). Without a target: the bands alone
 * (src/variator/metrics/results.py status_with_target is the same rule). */
export function statusOf(def: KbMetricDef, value: number | null): KbStatus {
  if (value === null) return 'unknown';
  if (def.direction === 'context') return 'neutral';
  const byBands: KbStatus = !def.bands ? 'neutral'
    : def.direction === 'lower' ? (value <= def.bands.good ? 'good' : value <= def.bands.warn ? 'warn' : 'bad')
    : (value >= def.bands.good ? 'good' : value >= def.bands.warn ? 'warn' : 'bad');
  if (def.target === null) return byBands;
  const meets = def.direction === 'lower' ? value <= def.target : value >= def.target;
  return meets ? 'good' : byBands === 'good' || byBands === 'neutral' ? 'warn' : byBands;
}

/** Weekly as-of dates from the first data week to `asOf` (local dates), oldest first. */
export function weeks(s: Snapshot, asOf: string, tz: string, max = 15): string[] {
  const out: string[] = [];
  for (let t = endOfDay(asOf, tz); t >= s.start && out.length < max; t -= 7 * DAY) out.unshift(localDate(t, tz));
  return out;
}

const results = new WeakMap<Snapshot, Map<string, KbMetricResult>>();
/** Evaluate a metric as of a date; results are memoised per snapshot (journal watermark) and viewer. */
export function evaluate(m: Metric, s: Snapshot, asOf: string, tz: string, visible: Set<string>, limit = 25, metadata = false, viewer?: string): KbMetricResult {
  const memo = results.get(s) ?? results.set(s, new Map()).get(s)!;
  const key = viewer ? `${m.id}|${asOf}|${limit}|${metadata}|${viewer}` : null;
  const hit = key ? memo.get(key) : undefined;
  if (hit) return hit;
  const out = compute(m, s, asOf, tz, visible, limit, metadata);
  if (key) memo.set(key, out);
  return out;
}

/** A metric as of a date (the as-of rule, README.md): the number, the previous week and the weekly series are all computed on the
 *  read model as known at the end of that date (Snapshot.view) — data recorded later is not there, so it cannot change
 *  any of them, and a knowledge base loaded only up to the date gives the same sheet. The earlier points of the series
 *  are the business weeks as known at the date (later corrections of those weeks recorded by then included). */
/** The record kinds a metric reads. A knowledge base with none of them (a simulated future has no mail, Jira or
 * GitLab) cannot give the number: it is unknown, not zero, and never «On track». Metrics not listed read links. */
const READS: Record<string, string[]> = {
  'company.northStar': ['Report', 'Document'],
  'team.activity': ['Email', 'Task', 'Bug', 'Story', 'Epic', 'MergeRequest', 'Meeting', 'Document'],
  'clients.awaiting': ['Email'], 'clients.firstReply': ['Email'], 'clients.escalations': ['Email'], 'deals.silent': ['Email'],
  'money.invoiced': ['Invoice', 'Act'], 'money.payables': ['Invoice', 'Act'],
  'deals.agreements': ['Contract', 'Amendment'], 'deals.proposals': ['Deal', 'CommercialProposal'], 'deals.discounts': ['Amendment', 'Email'],
  'eng.throughput': ['Task', 'Bug', 'Story', 'Epic'], 'eng.wip': ['Task', 'Bug', 'Story', 'Epic'], 'eng.cycle': ['Task', 'Bug', 'Story', 'Epic'],
  'eng.reopened': ['Task', 'Bug', 'Story', 'Epic'], 'eng.bugs': ['Bug', 'Task'], 'eng.reviewQueue': ['MergeRequest'],
};
/** The kinds a metric reads that this knowledge base has none of (all of them: the metric is unknown here). */
export function missingSources(m: Pick<KbMetricDef, 'id'>, snap: Snapshot): string[] | null {
  const kinds = READS[m.id];
  if (!kinds) return null;
  const mail = kinds.includes('Email') && snap.mails.length > 0;
  return mail || kinds.some(k => k !== 'Email' && snap.kind(k).length > 0) ? null : kinds;
}

function compute(m: Metric, snap: Snapshot, asOf: string, tz: string, visible: Set<string>, limit: number, metadata: boolean): KbMetricResult {
  const t = endOfDay(asOf, tz);
  const s = snap.view(t);
  // judged on what was known by the date (the as-of rule): a base loaded up to the date gives the same answer
  const missing = missingSources(m, s);
  if (missing) {
    const { compute: _c, seriesless: _s, ...def } = m;
    const series = weeks(s, asOf, tz).map(date => ({ date, value: null }));
    return { ...def, asOf, value: null, status: 'unknown', previous: null, series, breakdown: [], items: [], itemsTotal: 0, restrictedTotal: 0, sample: null,
      caveats: [`This knowledge base has no ${missing.join(', ')} records by this date: the number is unknown, not zero.`] };
  }
  const now = m.compute({ s, t, tz, visible, metadata });
  const prev = m.compute({ s, t: t - 7 * DAY, tz, visible, metadata });
  const series = weeks(s, asOf, tz).map(date => ({ date, value: date === asOf ? now.value : m.compute({ s, t: endOfDay(date, tz), tz, visible, metadata }).value }));
  const items = now.items ?? [];
  const { compute: _c, seriesless: _s, ...def } = m;
  return {
    ...def, asOf, value: now.value, status: now.status ?? statusOf(m, now.value), previous: prev.value, series,
    breakdown: (now.breakdown ?? []).slice(0, 12), items: items.slice(0, limit), itemsTotal: items.length,
    restrictedTotal: items.filter(i => i.restricted).length, sample: now.sample ?? null, caveats: now.caveats ?? [],
    ...(now.baseline !== undefined ? { baseline: now.baseline } : {}),
    ...(now.display ? { display: now.display } : {}),
  };
}

// ------------------------------------------------------------------------------------------------ per company
const fill = (text: string, o: KbOrganization | null) => text.replace('{currency}', o ? currencySymbol(o) : '$')
  .replace('{engineering}', o ? engineeringOf(o) : 'Engineering');
/** A built-in metric as the company sees it: its currency, its engineering department, its North Star. */
function adapt(m: Metric, o: KbOrganization | null): Metric {
  const out: Metric = { ...m, definition: fill(m.definition, o), howCalculated: m.howCalculated.map(x => fill(x, o)) };
  const ns = m.id === 'company.northStar' && o ? northStarOf(o) : null;
  return ns ? { ...out, title: ns.metricTitle, question: ns.question, definition: ns.definition, howCalculated: ns.howCalculated, target: ns.target } : out;
}
const orgMetrics = new WeakMap<KbOrganization, Metric[]>();
let plain: Metric[] | null = null;
/** The built-in metrics with the texts, units and targets of the company (without one: dollar defaults). */
export function metricsOf(o?: KbOrganization | null): Metric[] {
  if (!o) return (plain ??= METRICS.map(m => adapt(m, null)));
  let out = orgMetrics.get(o);
  if (!out) { out = METRICS.map(m => adapt(m, o)); orgMetrics.set(o, out); }
  return out;
}
export function metricsFor(role: KbRoleId, o?: KbOrganization | null): Metric[] { return metricsOf(o).filter(m => m.roles.includes(role)); }
/** The metrics of the role's brief: the top row and the areas, in that order (OFF_BRIEF left out). */
export function briefMetricsFor(role: KbRoleId, o?: KbOrganization | null): Metric[] { return metricsFor(role, o).filter(m => ON_BRIEF.has(m.id)); }
export function metricById(id: string, o?: KbOrganization | null): Metric | undefined { return metricsOf(o).find(m => m.id === id); }
