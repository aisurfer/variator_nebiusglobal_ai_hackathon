// Pure adapters of the actor engine's files to the case page's view (no React, no fetch: tests/case-actors.test.ts).
// - futureView: month.json (or a preview file) + WP19's day file -> the data of Over time, Week and Day;
// - verdictOf: the actor ranking -> leader, P(best), runner-up, the tradeoff line and the ties;
// - actorInput: the actor ranking as the input of the close call (model/closeCall.ts);
// - calendarHeadline: one sentence over the risk calendar of a fork;
// - simKey / rankKey: the keys that tell whether a result still matches the page (the body of a rerun is
//   pageState.monthBody).
import { noEmoji, type PreviewFuture, type PreviewMetric } from '../../engine';
import type { CloseCallInput } from '../model/closeCall';
import { longDate } from '../ui/format';
import type { CaseConfig, Edits, PreviewMetricDef, Unit, WeekNode } from '../model/types';
import type {
  ActorDayExample, ActorMonth, ActorWeekTree, CalendarRowView, DayExampleView, DayFile, ExampleKind, FanView, FutureView, MarkedLeaf, VerdictView,
} from './types';

// ------------------------------------------------------------------------------------------------ small helpers
const lowFirst = (s: string) => /^[A-Z][A-Z]/.test(s) ? s : s.charAt(0).toLowerCase() + s.slice(1);
const pct = (p: number) => `${Math.round(p * 100)}%`;
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
/** «Jul ’27»: month m after the fork date. */
export function monthName(forkDate: string, m: number): string {
  const t = new Date(`${forkDate}T12:00:00Z`), d = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + Math.round(m), 1));
  return `${MON[d.getUTCMonth()]} ’${String(d.getUTCFullYear()).slice(2)}`;
}
/** 1000 -> «1,000». */
export const count = (n: number) => Math.round(n).toLocaleString('en-US');
const list = (xs: string[]) => xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;
const forkLabel = (cfg: CaseConfig, id: string) => cfg.forks.find(f => f.id === id)?.label ?? id;
const critLabel = (cfg: CaseConfig, id: string) => cfg.criteria.find(c => c.id === id)?.label ?? id;

function fmtMetric(v: number, unit: string) {
  if (!Number.isFinite(v)) return '—';
  if (unit === 'USD' || unit.startsWith('USD')) { const a = Math.abs(v); return `${v < 0 ? '−' : ''}$${a >= 1e6 ? (a / 1e6).toFixed(2) + 'M' : Math.round(a / 1e3) + 'k'}`; }
  if (unit === 'share') return pct(v);
  if (unit === 'ratio') return v.toFixed(2);
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}
const metricDef = (m: PreviewMetric): PreviewMetricDef => ({ id: m.id, label: noEmoji(m.label), unit: m.unit as Unit, higher: m.higher ?? null, bars: m.unit === 'count' });

/** True for a month file of the actor engine (not a preview). */
export const isActorMonth = (x: unknown): x is ActorMonth =>
  !!x && typeof x === 'object' && (x as ActorMonth).source === 'actors' && (x as ActorMonth).preview === false && !!(x as ActorMonth).options && Array.isArray((x as ActorMonth).metrics);

// ------------------------------------------------------------------------------------------------ the future view
const EVENT_TITLE: Record<string, string> = { incident: 'Incident', payment: 'Payment', shock: 'Shock', decision: 'Decision', people: 'People', client: 'Client', vendor: 'Vendor', milestone: 'Milestone', hire: 'Hire', signal: 'Signal' };
const KIND_LABEL: Record<ExampleKind, string> = { pessimistic: 'Pessimistic', median: 'Median', optimistic: 'Optimistic' };

function treeNodes(t: ActorWeekTree, weekMetrics: PreviewMetricDef[]): WeekNode[] {
  const lead = weekMetrics[0], up = lead?.higher !== false, nodes: WeekNode[] = [];
  t.levels.forEach((level, w) => {
    const band = lead && t.bands[lead.id]?.find(b => b.week === w);
    level.forEach(n => {
      const vals = n.values, v = lead ? vals[lead.id] : NaN;
      const hi = band && v > band.p50 + (band.p90 - band.p50) * 0.5, lo = band && v < band.p50 - (band.p50 - band.p10) * 0.5;
      const tone = !band ? 'neutral' : (up ? hi : lo) ? 'good' : (up ? lo : hi) ? 'bad' : 'neutral';
      nodes.push({
        id: n.id, week: w, label: w === 0 ? 'Fork date' : `Week ${w}`, share: n.share, parent: n.parent >= 0 && w > 0 ? t.levels[w - 1][n.parent]?.id ?? null : null, tone,
        summary: noEmoji(n.summary), events: [],
        numbers: [{ label: 'Share of paths', value: pct(n.share) }, ...weekMetrics.map(m => ({ label: m.label, value: fmtMetric(vals[m.id], m.unit) }))],
        end: lead ? fmtMetric(v, lead.unit) : '',
      });
    });
  });
  // the events of a week: from the example paths that go through the node (the tree itself keeps only its summary line)
  const byId = new Map(nodes.map(n => [n.id, n]));
  for (const ex of t.examples ?? []) ex.events.forEach(e => {
    const node = byId.get(ex.nodes[e.week] ?? '');
    const text = noEmoji(e.text);
    if (node && !node.events.includes(text)) node.events.push(text);
  });
  return nodes;
}

function dayExamples(cfg: CaseConfig, anchor: string, exs: ActorDayExample[], dayMetrics: PreviewMetricDef[]): DayExampleView[] {
  const t0 = new Date(`${anchor}T12:00:00Z`).getTime(), dayOf = (iso: string) => Math.round((new Date(`${iso}T12:00:00Z`).getTime() - t0) / 864e5);
  const crit0 = cfg.criteria[0];
  const counted = dayMetrics.find(m => m.unit === 'count')?.label.replace(/s$/, '');
  const title = (type: string) => type === 'incident' && counted ? counted : EVENT_TITLE[type] ?? type.charAt(0).toUpperCase() + type.slice(1).replace(/[_-]+/g, ' ');
  const nameOf = (id?: string) => id ? cfg.people.find(p => p.id === id)?.name : undefined;
  const order: ExampleKind[] = ['pessimistic', 'median', 'optimistic'];
  return [...exs].sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind)).map(e => {
    const last = e.days[e.days.length - 1]?.values ?? {};
    const head = e.yearEnd?.[crit0.id] ?? last[crit0.id] ?? NaN;
    // a forward run of a few months (WP19) ends on its own date, not at the horizon of the case
    const end = e.horizonEnd ? longDate(e.horizonEnd) : null;
    return {
      // the summary without its kind and the headline amount the card shows anyway: «28 incidents, 12 client notices, …»
      id: e.kind, label: e.label || KIND_LABEL[e.kind], outcome: noEmoji(e.summary.replace(/^[^:]*:\s*/, '').replace(/^cash (on \S+|after \d+ weeks) [−-]?\$[\d.,]+[MkK]?[,.]?\s*/, '')).replace(/^./, ch => ch.toUpperCase()),
      headline: { value: head ?? NaN, unit: crit0.unit, label: end ? `${lowFirst(crit0.label.replace(/ at (year|project) end$/i, ''))} on ${end}` : lowFirst(crit0.label) },
      daily: e.days.map(d => ({ ...d.values, day: dayOf(d.date) })),
      events: e.events.map(ev => ({
        day: dayOf(ev.date), title: title(ev.type), text: noEmoji(ev.text),
        tone: ev.type === 'incident' || ev.type === 'shock' || ev.type === 'signal' ? 'bad' as const : ev.type === 'payment' || ev.type === 'decision' || ev.type === 'milestone' || ev.type === 'hire' ? 'good' as const : 'event' as const,
        entityId: ev.entityId, person: nameOf(ev.person),
      })),
      tenant: e.tenant,
    };
  });
}

/** The data of the future sections from a month file (actors or preview), with WP19's day file when there is one. */
export function futureView(cfg: CaseConfig, P: ActorMonth | PreviewFuture, day: DayFile | null = null): FutureView {
  const actors = isActorMonth(P), M = P as ActorMonth;
  const fans: FutureView['fans'] = {}, calendar: FutureView['calendar'] = {}, tree: FutureView['tree'] = {}, weekly: FutureView['weekly'] = {}, marked: FutureView['marked'] = {};
  for (const [f, o] of Object.entries(M.options)) {
    fans[f] = o.fans as Record<string, FanView[]>;
    calendar[f] = o.riskCalendar.map((r): CalendarRowView => ({
      id: r.id, label: noEmoji(r.label), consequence: noEmoji(r.consequence ?? ''), byMonth: r.pByMonth, medianMonth: r.medianFirstMonth, total: r.pEver, ref: r.ref ?? undefined, kind: r.kind,
      firstByMonth: r.pFirstByMonth, markerMonth: r.markerMonth, rowType: r.rowType,
    }));
  }
  const weekMetrics = (M.weekMetrics ?? []).map(metricDef);
  let paths = 0;
  for (const [f, t] of Object.entries(M.weekTrees ?? {})) {
    paths = Math.max(paths, t.maxNodes || 0);
    tree[f] = treeNodes(t, weekMetrics);
    weekly[f] = weekMetrics.flatMap(m => (t.bands[m.id] ?? []).filter(b => b.week > 0).map(b => ({ week: b.week, metric: m.id, p10: b.p10, p50: b.p50, p90: b.p90 })));
    marked[f] = (t.examples ?? []).map((e): MarkedLeaf => ({ kind: e.kind, leaf: e.leaf, nodes: e.nodes }));
  }
  // WP19's day file of a fork, when its examples follow the leaves of this tree (a newer tree makes it out of date: then
  // the bridge of the month file); a fork without a tree keeps its day file
  const fits = (f: string, exs: ActorDayExample[]) => !marked[f]?.length || exs.every(e => !e.leaf || marked[f].some(m => m.kind === e.kind && m.leaf === e.leaf));
  const dayFile = Object.fromEntries(Object.entries(day?.dayExamples ?? {}).filter(([f, exs]) => exs.length && fits(f, exs)));
  const fromDayFile = Object.keys(dayFile).length > 0;
  const dayMetrics = ((fromDayFile ? day!.dayMetrics : M.dayMetrics) ?? []).map(metricDef);
  const days: FutureView['days'] = {};
  for (const [f, exs] of Object.entries(M.dayExamples ?? {})) days[f] = dayExamples(cfg, M.anchor, exs, dayMetrics);
  for (const [f, exs] of Object.entries(dayFile)) days[f] = dayExamples(cfg, day!.anchor, exs, dayMetrics);
  const crit = new Set(cfg.criteria.map(c => c.id));
  return {
    preview: !actors, source: actors ? 'actors' : 'preview', n: M.n ?? 0,
    note: noEmoji(M.note ?? ''),
    covers: Object.keys(M.weekTrees ?? {}).filter(f => days[f]?.length),
    horizon: M.horizonMonths ?? cfg.horizonMonths,
    metricLabels: Object.fromEntries(M.metrics.map(m => [m.id, noEmoji(m.label)])),
    monthMetrics: M.metrics.map(metricDef), weekMetrics, dayMetrics, paths, fans, calendar, tree, weekly, days, marked,
    daySource: !actors ? 'preview' : fromDayFile ? 'day-file' : 'bridge', dayFileForks: Object.keys(dayFile),
    criteria: M.metrics.map(m => m.id).filter(id => crit.has(id)),
  };
}

// ------------------------------------------------------------------------------------------------ the verdict
const TRADE = /^(\S+) is better than (\S+) on: (.+?)\.?$/;
const EQUAL = /^(\S+) and (\S+) are equal within the meaningful difference on: (.+?)\.?$/;
const ids = (cfg: CaseConfig, s: string) => s.split(',').map(x => x.trim()).filter(id => cfg.criteria.some(c => c.id === id));

/** Criteria (ids) where the leader and the runner-up differ only within the meaningful difference: the engine's `ties`
 *  (of the ranking or the best path), else its «a and b are equal … on: x, y» line. */
export function tiesOf(cfg: CaseConfig, M: ActorMonth, lead: string, run: string | null): string[] {
  if (!run) return [];
  const bp = M.ranking?.bestPath;
  if (M.ranking?.ties) return M.ranking.ties;
  if (bp?.ties && bp.decision === lead && (bp.runner_up ?? null) === run) return bp.ties;
  const line = (M.ranking?.tradeoffs ?? []).map(l => EQUAL.exec(l)).find(m => m && ((m[1] === lead && m[2] === run) || (m[1] === run && m[2] === lead)));
  return line ? ids(cfg, line[3]) : [];
}

/** What each of a and b is better at, from the engine's tradeoff lines for the pair (null when it has none). */
function betterOf(cfg: CaseConfig, M: ActorMonth, a: string, b: string): { a: Set<string>; b: Set<string> } | null {
  const pair = (M.ranking?.tradeoffs ?? []).map(l => TRADE.exec(l)).filter((m): m is RegExpExecArray => !!m && ((m[1] === a && m[2] === b) || (m[1] === b && m[2] === a)));
  if (!pair.length) return null;
  const out = { a: new Set<string>(), b: new Set<string>() };
  for (const m of pair) for (const id of ids(cfg, m[3])) (m[1] === a ? out.a : out.b).add(id);
  return out;
}

/** The actor ranking for people: the leader, its P(best) and feasibility, the runner-up, the tradeoff and the ties. */
export function verdictOf(cfg: CaseConfig, M: ActorMonth, forksOn?: string[]): VerdictView | null {
  const opts = [...(M.ranking?.options ?? [])].filter(o => !forksOn || forksOn.includes(o.option)).sort((a, b) => a.rank - b.rank);
  if (!opts.length) return null;
  const lead = opts[0], run = opts[1] ?? null;
  const tiedIds = tiesOf(cfg, M, lead.option, run?.option ?? null), tied = new Set(tiedIds);
  const ties = cfg.criteria.filter(c => tied.has(c.id)).map(c => lowFirst(c.label));
  let tradeoff = '';
  if (run) {
    // the engine's lines («a is better than b on: x, y»), for the leader and the runner-up; else from the medians
    const better: Record<string, string[]> = { [lead.option]: [], [run.option]: [] };
    const lines = (M.ranking?.tradeoffs ?? []).map(l => TRADE.exec(l)).filter((m): m is RegExpExecArray => !!m);
    const pair = lines.filter(m => (m[1] === lead.option && m[2] === run.option) || (m[1] === run.option && m[2] === lead.option));
    const engineSaid = pair.length > 0 || (M.ranking?.tradeoffs ?? []).some(l => EQUAL.test(l));
    if (pair.length) for (const m of pair) better[m[1]].push(...ids(cfg, m[3]));
    else if (!engineSaid) for (const c of cfg.criteria) {
      const a = lead.stats?.[c.id]?.p50, b = run.stats?.[c.id]?.p50;
      if (a == null || b == null || a === b) continue;
      better[(a > b) === c.higher ? lead.option : run.option].push(c.id);
    }
    const words = (xs: string[]) => list(cfg.criteria.filter(c => xs.includes(c.id) && !tied.has(c.id)).map(c => lowFirst(c.label)));
    const L = forkLabel(cfg, lead.option), R = forkLabel(cfg, run.option), lw = words(better[lead.option]), rw = words(better[run.option]);
    tradeoff = lw && rw ? `${L} is better on ${lw}; ${R} on ${rw}.` : lw ? `${L} is better on ${lw}.` : rw ? `${R} is better on ${rw}.` : '';
  }
  return {
    leader: lead.option, runnerUp: run?.option ?? null, pBest: lead.pBest, runnerPBest: run?.pBest ?? null, feasible: lead.pFeasible, pBestOf: M.ranking?.options.length ?? opts.length,
    order: opts.map(o => ({ fork: o.option, pBest: o.pBest, feasible: o.pFeasible, score: o.scoreTimesFeasible ?? o.score })),
    tradeoff, ties, allTied: !!run && ties.length > 0 && ties.length === cfg.criteria.length,
    tieLine: run && ties.length ? `Equal to ${forkLabel(cfg, run.option)} within the meaningful difference on: ${list(ties)}.` : '',
  };
}

/** One row of the engine's `ranking.pairwise`: the leader `a` against a rival `b` on a criterion (or `total`), with
 *  d = (b − a) in the better direction: `bWins` / `aWins` beyond the meaningful difference `tie`, `meanDiff` and
 *  `medianMargin` of d, `scoreDiff` = weight × (normalised b − normalised a). */
export interface EnginePairwise { a: string; b: string; crit: string; bWins: number; aWins: number; meanDiff: number; medianMargin: number; scoreDiff: number; tie: number }

/** The actor ranking as the close call's input. The exact scenario data come from the engine's `ranking.pairwise`
 *  (the leader against each rival, ties respected), turned into the close call's terms (a − b, before the weight). A
 *  pair it does not cover (a fork switched off took the lead) falls back to the statistics and the tradeoff lines. */
export function actorInput(cfg: CaseConfig, M: ActorMonth, forksOn?: string[]): CloseCallInput | null {
  const opts = [...(M.ranking?.options ?? [])].filter(o => !forksOn || forksOn.includes(o.option)).sort((a, b) => a.rank - b.rank);
  if (opts.length < 2) return null;
  const byId = new Map(opts.map(o => [o.option, o]));
  const fan = (f: string, k: string) => { const pts = M.options[f]?.fans[k]; return pts?.[pts.length - 1]; };
  const given = (M.ranking as { pairwise?: EnginePairwise[] } | undefined)?.pairwise ?? [];
  const weights = M.ranking?.weights ?? {};
  const crit = (id: string) => cfg.criteria.find(c => c.id === id);
  /** The engine's rows of a pair (either way round), per criterion of the case. */
  const rows = (a: string, b: string) => {
    const fwd = given.filter(x => x.a === a && x.b === b && crit(x.crit));
    if (fwd.length) return { rows: fwd, flip: false };
    const back = given.filter(x => x.a === b && x.b === a && crit(x.crit));
    return back.length ? { rows: back, flip: true } : null;
  };
  return {
    order: opts.map(o => o.option),
    pBest: Object.fromEntries(opts.map(o => [o.option, o.pBest])),
    weights,
    feasible: Object.fromEntries(opts.map(o => [o.option, o.pFeasible])),
    spread: (f, k) => { const p = fan(f, k); return p ? p.p75 - p.p25 : Math.max(0, (byId.get(f)?.stats?.[k]?.p95 ?? 0) - (byId.get(f)?.stats?.[k]?.p5 ?? 0)) / 3; },
    // equal within the meaningful difference: the engine's ties of the pair, else the mean gap within the row's `tie`
    equal: (a, b) => {
      const named = tiesOf(cfg, M, a, b).concat(tiesOf(cfg, M, b, a));
      if (named.length) return [...new Set(named)];
      const r = rows(a, b);
      return r ? r.rows.filter(x => x.tie > 0 && Math.abs(x.meanDiff) <= x.tie).map(x => x.crit) : [];
    },
    pairwise: (a, b) => {
      const r = rows(a, b);
      if (r) return r.rows.map(x => {
        const c = crit(x.crit)!, sign = c.higher ? 1 : -1, w = weights[x.crit] ?? 0;
        // the row's d is (rival − leader) in the better direction; the close call wants a − b in raw units, before the weight
        const dAB = r.flip ? x.meanDiff : -x.meanDiff, bWins = r.flip ? x.aWins : x.bWins;
        return { crit: x.crit, bWins, meanDiff: dAB * sign, medianMargin: Math.abs(x.medianMargin), scoreDiff: w > 0 ? (r.flip ? x.scoreDiff : -x.scoreDiff) / w : 0 };
      });
      // criteria within the meaningful difference do not separate the two (a full tie is no close call)
      const tied = new Set(tiesOf(cfg, M, a, b).concat(tiesOf(cfg, M, b, a)));
      const side = betterOf(cfg, M, a, b);
      return cfg.criteria.map(c => {
        const sa = byId.get(a)?.stats?.[c.id], sb = byId.get(b)?.stats?.[c.id];
        if (!sa || !sb || tied.has(c.id)) return { crit: c.id, bWins: 0, meanDiff: 0, medianMargin: 0, scoreDiff: 0 };
        const means = opts.map(o => o.stats?.[c.id]?.mean).filter((v): v is number => v != null);
        const span = Math.max(...means) - Math.min(...means), gap = span > 0 ? Math.abs(sa.mean - sb.mean) / span : 0;
        // who is better: the engine's tradeoff lines for the pair when it has them (so the close call never contradicts
        // the tradeoff line), else the medians
        const aBetter = side ? side.a.has(c.id) : c.higher ? sa.p50 > sb.p50 : sa.p50 < sb.p50;
        const bBetter = side ? side.b.has(c.id) : c.higher ? sb.p50 > sa.p50 : sb.p50 < sa.p50;
        return { crit: c.id, bWins: bBetter ? 0.5 : 0, meanDiff: sa.mean - sb.mean, medianMargin: bBetter ? Math.abs(sb.p50 - sa.p50) : 0, scoreDiff: aBetter ? gap : bBetter ? -gap : 0 };
      });
    },
  };
}

// ------------------------------------------------------------------------------------------------ the calendar headline
/** A row that counts progress, not a failure (case A «Live by the month»): the engine's `rowType` or `kind`. */
export const isProgressRow = (r: CalendarRowView) => r.rowType === 'progress' || r.kind === 'progress';

/** One sentence over the risk calendar of a fork: whether cash runs out, and the most likely other failure with its
 *  typical month. «Cash stays above zero in all 1,000 scenarios. In 47% of them: churn above 20%, typically by Jul ’27.» */

export function calendarHeadline(cfg: CaseConfig, all0: CalendarRowView[], n: number): string {
  const rows = all0.filter(r => !isProgressRow(r));
  const all = n > 0 ? `all ${count(n)} scenarios` : 'every scenario';
  const of = n > 0 ? `the ${count(n)} scenarios` : 'the scenarios';
  const when = (r: CalendarRowView) => r.medianMonth ? `, typically by ${monthName(cfg.forkDate, r.medianMonth)}` : '';
  const cash = rows.find(r => r.kind === 'threshold');
  const parts: string[] = [];
  if (cash) parts.push(cash.total < 0.0005 ? `Cash stays above zero in ${all}.` : `In ${pct(cash.total)} of ${of}: ${lowFirst(cash.label)}${when(cash)}.`);
  const top = rows.filter(r => r !== cash && r.total >= 0.005).sort((a, b) => b.total - a.total)[0];
  if (top) parts.push(`In ${pct(top.total)} of ${cash ? 'them' : of}: ${lowFirst(top.label)}${when(top)}.`);
  else if (!cash) parts.push(`No check fails in more than 1 in 200 of ${of}.`);
  else if (cash.total < 0.0005) parts.push('No other check fails in more than 1 in 200.');
  return parts.join(' ');
}

// ------------------------------------------------------------------------------------------------ the page state
/** Edit paths that only change what the page shows (no simulation, no ranking). */
const UI_ONLY = (p: string) => p === 'week' || p === 'day' || p === 'dayExample';
/** Edit paths that change the ranking only: priorities, and forks on or off (the other forks' paths stay the same). */
const RANK_ONLY = (p: string) => p.startsWith('w|') || p === 'caution' || p === 'priority' || p.startsWith('fork|');
const stable = (e: Edits, keep: (p: string) => boolean) => JSON.stringify(Object.keys(e).filter(keep).sort().map(k => [k, e[k]]));
/** The key of the simulation input: equal keys give the same fans, calendar, tree and examples. '[]' = the defaults. */
export const simKey = (e: Edits) => stable(e, p => !UI_ONLY(p) && !RANK_ONLY(p) && !p.startsWith('sim|'));
/** The key of the ranking input: the simulation input and the priorities. */
export const rankKey = (e: Edits) => stable(e, p => !UI_ONLY(p) && !p.startsWith('sim|'));
export const DEFAULT_KEY = '[]';

/** The simulation inputs of ONE fork: its own levers, sizes and overrides, and the world (hypotheses, shocks, placed
 *  events, model inputs every fork shares). Turning other forks on or off, weights, caution and the view are not in
 *  it, so an unrelated edit keeps the fork's week tree. '[]' = the defaults of the fork. */
export function forkKey(cfg: Pick<CaseConfig, 'interventions' | 'levers'>, edits: Edits, fork: string): string {
  const owners = new Map<string, string[]>();
  for (const i of cfg.interventions) if (i.forks?.length) for (const p of [i.monthPath, ...i.values.map(v => v.path)]) owners.set(p, [...(owners.get(p) ?? []), ...i.forks]);
  for (const l of cfg.levers) if (l.forks?.length) owners.set(l.path, [...(owners.get(l.path) ?? []), ...l.forks]);
  const counts = (p: string) => {
    const head = p.split('|')[0];
    if (head === 'hyp' || head === 'r' || head === 'rm' || head === 'ev') return true;
    if (head === 'o') return p.split('|')[1] === fork;
    if (head === 'v') { const o = owners.get(p); return !o || o.includes(fork); }
    return false;
  };
  return stable(edits, counts);
}
