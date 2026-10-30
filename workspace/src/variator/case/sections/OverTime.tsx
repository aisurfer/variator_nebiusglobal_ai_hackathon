// ② Month — Over time: fan charts per criterion or chain value for 2–3 forks side by side, the risk calendar (check ×
// month, with a headline sentence from its rows) and the goals with P(met). The fans and the calendar come from the
// month simulation on the company's actors (case/actors, month.json of the future engine) when the API has it; else
// from the preview of the case, with its badge.
import { useMemo, useState } from 'react';
import { Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { FlaskConical, History } from 'lucide-react';
import { source } from '../model/source';
import { useCase } from '../state';
import { calMonth, fmt, forkColor, forkLabel, pct } from '../ui/format';
import { HelpTip, TipBadge } from '../ui/HelpTip';
import { Empty, ForkName, ForkPicker, SectionHeader, Segmented } from '../ui/parts';
import { calendarHeadline, isProgressRow, previewFuture, SourceBadge, useActors, useRankOrder, type CalendarRowView, type FutureView } from '../actors';
import { Loading } from './common';

/** The forks on the charts by default: the leader, the runner-up and the baseline, by the answer's ranking (the actor
 *  ranking while it is current, else the formula's). */
export function useDefaultForks(max = 3) {
  const c = useCase(), { order } = useRankOrder();
  return useMemo(() => {
    const base = c.cfg.forks.find(f => f.baseline)!.id;
    return [...new Set([order[0], order[1], base].filter(Boolean))].slice(0, max) as string[];
  }, [order.join('|')]); // eslint-disable-line react-hooks/exhaustive-deps
}

/** The future data of the page: the actor simulation when it is there, else the preview (null until the first run). */
export function useFuture(): FutureView | null {
  const c = useCase(), a = useActors();
  const crit = useMemo(() => c.cfg.criteria.map(x => x.id), [c.cfg]);
  const pv = useMemo(() => c.run ? previewFuture(c.run.preview, crit) : null, [c.run?.preview, crit]); // eslint-disable-line react-hooks/exhaustive-deps
  return a.view ?? pv;
}

/** The source badge of a future section: «Actors» with its state, or «Preview» (and «Original case» once you edit). */
export function FutureBadges({ level = 'month', treesCurrent = false }: { level?: 'month' | 'week' | 'day'; treesCurrent?: boolean }) {
  const c = useCase(), a = useActors();
  // the week's event trees are of the page's simulation state (computed for it when it changed)
  if (a.view && level === 'week' && treesCurrent) return <SourceBadge source="actors" state={a.state === 'defaults' ? 'defaults' : 'current'} scope={a.scope} />;
  // the rerun returns the months only: the tree and the day examples stay those of the defaults, and say so
  if (a.view && level !== 'month' && a.treeFromDefaults) return <SourceBadge source="actors" state="stale" scope={a.scope} testId="future-source" text="Week and day: for the defaults" tip="The rerun covers the month; the week tree and the example days are precomputed for the defaults." />;
  if (a.view) return <SourceBadge source="actors" state={a.state} scope={a.scope} />;
  return <><SourceBadge source="preview" note={c.run?.preview.note} /><OriginalOnly /></>;
}

interface GoalRow { id: string; label: string; kind: 'goal' | 'milestone'; month: number; p: Record<string, number | null> }
/** The goals of the page state: from the actor result when it is current (`month.goals` of the engine), from the
 *  formula model as a draft after edits, from the formula model without actor data; null: no goals to show. */
function useGoals(): { source: 'actors' | 'draft' | 'formula'; rows: GoalRow[] } | null {
  const c = useCase(), a = useActors(), run = c.run!;
  const formula = run.goals.map(g => ({ id: g.id, label: g.label, kind: g.kind, month: g.month, p: g.p }));
  if (!a.view || !a.month) return { source: 'formula', rows: formula };
  if (a.state === 'stale') return { source: 'draft', rows: formula };
  const rows = actorGoals(a.month as unknown as { goals?: unknown; options?: Record<string, { goals?: unknown; milestones?: unknown }> }, c.cfg.horizonMonths);
  return rows?.length ? { source: 'actors', rows } : null;
}
/** The engine's goals and milestones (WP18): per fork in `options[f].goals` ({id, label, kind, month, pMet}) and
 *  `options[f].milestones` ({id, label, markerMonth, pMet}, only the forks that own one); a top-level `goals` list or
 *  map ({id, label, kind, month, pMet | p: {fork: P}}) is read too. */
function actorGoals(m: { goals?: unknown; options?: Record<string, { goals?: unknown; milestones?: unknown }> }, horizon: number): GoalRow[] | null {
  type G = { id?: string; name?: string; label?: string; kind?: string; month?: number; markerMonth?: number; p?: Record<string, number>; pMet?: Record<string, number> | number };
  const list = (x: unknown): G[] => Array.isArray(x) ? x as G[] : x && typeof x === 'object' ? Object.entries(x as Record<string, G>).map(([id, g]) => ({ id, ...g })) : [];
  const out = new Map<string, GoalRow>();
  for (const g of list(m.goals)) {
    const id = g.id ?? g.name, p = (typeof g.pMet === 'object' ? g.pMet : g.p) ?? {};
    if (id) out.set(id, { id, label: g.label ?? id, kind: g.kind === 'milestone' ? 'milestone' : 'goal', month: g.month ?? horizon, p });
  }
  for (const [f, o] of Object.entries(m.options ?? {})) for (const [g, ms] of [...list(o.goals).map(x => [x, false] as const), ...list(o.milestones).map(x => [x, true] as const)]) {
    const id = g.id ?? g.name, pm = typeof g.pMet === 'number' ? g.pMet : g.p?.[f];
    if (!id || pm == null) continue;
    const row = out.get(id) ?? { id, label: g.label ?? id, kind: ms || g.kind === 'milestone' ? 'milestone' : 'goal', month: g.month ?? g.markerMonth ?? horizon, p: {} };
    row.p[f] = pm; out.set(id, row);
  }
  return out.size ? [...out.values()] : null;
}

export function OverTimeSection() {
  const c = useCase(), sec = c.cfg.sections.find(s => s.id === 'overtime')!;
  return <section id="overtime" className="cs-section" aria-labelledby="overtime-title">
    <SectionHeader id="overtime" title="Over time" purpose={sec.purpose} help={sec.help} badges={<FutureBadges />} />
    {!c.run || !c.ranking ? <Loading what="the months" /> : <OverTimeBody />}
  </section>;
}

/** The monthly preview shows the original case: once you edit a value, a compact badge says so (its tooltip explains). */
function OriginalOnly() {
  const c = useCase();
  const edited = Object.keys(c.edits).some(k => !/^(w\||caution$|priority$|week$|day$|dayExample$)/.test(k));
  return edited ? <TipBadge tone="preview" testId="preview-original" icon={<History size={12} aria-hidden />} text="Original case" title="The preview does not follow your edits" tip="The monthly preview shows the original case. It does not follow your edits: the simulation on actors does, once it reruns." /> : null;
}

function OverTimeBody() {
  const c = useCase(), cfg = c.cfg, run = c.run!, F = useFuture()!;
  const def = useDefaultForks();
  const [picked, setPicked] = useState<string[] | null>(null);
  const forks = (picked ?? def).filter(f => run.forks.includes(f));
  // the monthly metrics: the criteria first, then the links of the chain (actors), or the preview's own metrics
  const metrics = F.monthMetrics.filter(m => forks.some(f => F.fans[f]?.[m.id]));
  const crits = metrics.filter(m => F.criteria.includes(m.id)), chain = metrics.filter(m => !F.criteria.includes(m.id));
  const [critPick, setCrit] = useState(metrics[0]?.id ?? cfg.criteria[0].id);
  const crit = metrics.some(m => m.id === critPick) ? critPick : metrics[0]?.id ?? critPick;
  const cr = metrics.find(m => m.id === crit) ?? { id: crit, label: cfg.criteria[0].label, unit: cfg.criteria[0].unit, higher: cfg.criteria[0].higher };
  const all = forks.flatMap(f => F.fans[f]?.[crit] ?? []);
  const wide = all.some(p => p.p5 != null && p.p95 != null);
  // a criterion with fixed RAG limits (runway): its check line is the red limit of this run, so it follows an edit of it
  const lim = run.limits[crit], goal0 = cfg.goals[crit];
  const goal = goal0 && lim?.fixed ? { value: lim.red, label: `Check: ${fmt(lim.red, cr.unit)}` } : goal0;
  const lo = Math.min(...all.map(p => p.p5 ?? p.p10), goal?.value ?? Infinity), hi = Math.max(...all.map(p => p.p95 ?? p.p90), goal?.value ?? -Infinity);
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.05 || 1, domain: [number, number] = [lo - pad, hi + pad];
  const [calFork, setCalFork] = useState<string | null>(null);
  const lead = useRankOrder().order.find(f => run.forks.includes(f)) ?? c.ranking!.order[0];
  const cf = calFork && run.forks.includes(calFork) ? calFork : lead;
  const long = F.horizon > 12;
  // one source of numbers per page state: the actors' goals while the actor result is current (none until the engine
  // sends them: then the card stays out, the calendar carries the milestones); the formula's as a draft after edits
  const goals = useGoals();
  const metricLabel = (id: string) => (F.metricLabels[id] ?? id).replace(/, (year|project) to date$/, '');
  return <div className={`cs-body ${c.stale ? 'is-stale' : ''}`}>
    {!metrics.length ? <Empty icon={<FlaskConical size={22} />} title="The monthly fans of this case are being prepared" text="The fans per month appear here once the simulation of the case exists. The risk calendar below shows the milestones of the live run." /> : <>
    <div className="cs-toolbar">
      <div className="cs-metric-pick">
        {crits.length > 0 && <Segmented<string> label="Metric" value={crits.some(m => m.id === crit) ? crit : ''} options={crits.map(m => ({ id: m.id, label: metricLabel(m.id) }))} onChange={setCrit} />}
        {chain.length > 0 && <select className="cs-select" aria-label="Link of the chain" data-testid="chain-metric" value={chain.some(m => m.id === crit) ? crit : ''} onChange={e => e.target.value && setCrit(e.target.value)}>
          <option value="">{crits.length ? 'More: the chain…' : 'Metric…'}</option>
          {chain.map(m => <option key={m.id} value={m.id}>{metricLabel(m.id)}</option>)}
        </select>}
      </div>
      <ForkPicker label="Forks on the charts" value={forks} onChange={setPicked} forks={run.forks} max={3} min={1} />
    </div>
    <div className="cs-fans" style={{ gridTemplateColumns: `repeat(${forks.length}, minmax(0,1fr))` }} data-testid="fans" data-source={F.source}>
      {forks.map(f => { const data = F.fans[f]?.[crit] ?? [], end = data[data.length - 1]; return <article key={f} className="card cs-card cs-fan" data-testid={`fan-${f}`}>
        <div className="cs-card-head"><ForkName fork={f} strong /><HelpTip entry={cfg.help.fan} label="Fan chart" />{end && <span className="cs-fan-end">{calMonth(cfg, end.month)}: <b>{fmt(end.p50, cr.unit)}</b></span>}</div>
        {data.length ? <Fan data={data} unit={cr.unit} domain={domain} goal={goal} wide={wide} label={`${metricLabel(crit)}, ${forkLabel(cfg, f)}`} /> : <p className="cs-muted cs-nopreview">No monthly fan for this fork yet.</p>}
      </article>; })}
    </div>
    <p className="cs-legend"><i className="is-line" />median<i className="is-dark" />25–75%<i className="is-light" />{wide ? '5–95%' : '10–90%'}{goal && <><i className="is-goal" />{goal.label}</>}</p>
    </>}
    <div className="cs-grid">
      <article className={`card cs-card ${long || !goals ? 'cs-span-12' : 'cs-span-8'}`} data-tour="risk-calendar" data-testid="risk-calendar" data-source={F.source}>
        <div className="cs-card-head"><h3>Risk calendar</h3><HelpTip entry={cfg.help.calendar} label="Risk calendar" more={['A cell: the share of the scenarios where it has happened by that month. The ring marks the median month of the first failure; the small bar under a cell, the share where it first happens in that month.']} />
          <span className="cs-card-tools"><select className="cs-select" aria-label="Fork of the risk calendar" value={cf} onChange={e => setCalFork(e.target.value)}>{run.forks.map(f => <option key={f} value={f}>{forkLabel(cfg, f)}</option>)}</select></span></div>
        <RiskCalendar fork={cf} F={F} />
      </article>
      {goals && <article className={`card cs-card ${long ? 'cs-span-12 cs-goals-wide' : 'cs-span-4'}`} data-testid="goals" data-source={goals.source}>
        <div className="cs-card-head"><h3>Goals</h3><HelpTip entry={cfg.help.goalsMet} label="Goals" more={[goals.source === 'actors' ? 'P(met) per fork from the simulation on your company\'s actors, in the order of the charts; the target is 8 in 10.' : 'P(met) per fork from the quick formula model of the case, in the order of the charts; the target is 8 in 10.']} /></div>
        {goals.source === 'draft' && <p className="cs-draft-strip cs-goals-draft" data-testid="goals-draft"><span><b>Draft after your edits</b> · the quick formula model. Rerun on actors in «Answer» to update.</span></p>}
        <ul className="cs-goals">{goals.rows.filter(g => forks.some(f => g.p[f] != null)).map(g => <li key={g.id}>
          <span className="cs-goal-name">{g.label}<small>{g.kind === 'goal' ? `at ${calMonth(cfg, g.month)}` : `by ${calMonth(cfg, g.month)}`}</small></span>
          <span className="cs-goal-pills">{forks.map(f => g.p[f] == null ? null : <span key={f} className={`cs-pill is-${(g.p[f] ?? 0) >= 0.8 ? 'ok' : 'bad'}`} title={`${forkLabel(cfg, f)}: met in ${pct(g.p[f]!)}`}><i style={{ background: forkColor(cfg, f) }} />{pct(g.p[f]!)}</span>)}</span>
        </li>)}</ul>
      </article>}
    </div>
  </div>;
}

type FanDatum = { month: number; p5?: number; p10: number; p25: number; p50: number; p75: number; p90: number; p95?: number };
function Fan({ data, unit, domain, goal, label, wide }: { data: FanDatum[]; unit: string; domain: [number, number]; goal?: { value: number; label: string }; label: string; wide: boolean }) {
  const c = useCase();
  const rows = data.map(p => ({ month: p.month, outer: wide ? [p.p5 ?? p.p10, p.p95 ?? p.p90] : [p.p10, p.p90], inner: [p.p25, p.p75], p50: p.p50 }));
  const span = domain[1] - domain[0];
  // round ticks (1, 2, 2.5 or 5 times a power of ten); money reads «$1.5M», «−$2M», «$0» with the sign before the dollar
  const raw = span / 5, pow = 10 ** Math.floor(Math.log10(raw || 1)), step = [1, 2, 2.5, 5, 10].map(k => k * pow).find(k => k >= raw) ?? 10 * pow;
  const ticks: number[] = [];
  for (let t = Math.ceil(domain[0] / step) * step; t <= domain[1] + step * 1e-9; t += step) ticks.push(Math.abs(t) < step * 1e-9 ? 0 : t);
  const usdTick = (v: number) => { const a = Math.abs(v), s = v < 0 ? '−' : ''; if (a === 0) return '$0'; return step >= 1e5 ? `${s}$${+(a / 1e6).toFixed(step >= 1e6 ? 0 : step >= 2.5e5 && step % 5e5 ? 2 : 1)}M` : `${s}$${+(a / 1e3).toFixed(0)}k`; };
  const tick = (v: number) => unit === 'USD' || unit.startsWith('USD') ? usdTick(v) : unit === 'share' ? `${(v * 100).toFixed(Math.abs(v) > 0.9 ? 1 : 0)}%` : unit === 'months' ? `${Math.round(v)}` : v.toFixed(span < 5 ? 1 : 0);
  const last = data[data.length - 1].month, every = last > 12 ? 6 : 3;
  const band = wide ? '9 of 10' : '8 of 10';
  return <div className="cs-fan-plot" role="img" aria-label={`${label}: median from ${fmt(data[0].p50, unit)} to ${fmt(data[data.length - 1].p50, unit)}`}>
    <ResponsiveContainer width="100%" height={210}>
      <ComposedChart data={rows} margin={{ top: 8, right: 10, left: 0, bottom: 0 }}>
        <CartesianGrid vertical={false} stroke="#eef1f2" />
        <XAxis dataKey="month" axisLine={false} tickLine={false} tick={{ fill: '#76828a', fontSize: 11 }} tickFormatter={m => m === 0 ? 'T' : m % every === 0 ? calMonthShort(c.cfg.forkDate, m) : ''} interval={0} tickMargin={6} />
        <YAxis domain={domain} ticks={ticks} axisLine={false} tickLine={false} tick={{ fill: '#76828a', fontSize: 11 }} tickFormatter={tick} width={52} />
        <Tooltip cursor={{ stroke: '#9aa6ad', strokeWidth: 1 }} contentStyle={{ border: '1px solid #e5e8eb', borderRadius: 10, fontSize: 12, boxShadow: '0 6px 20px #18202c12', fontFamily: 'DM Sans' }}
          labelFormatter={m => Number(m) === 0 ? 'Fork date T' : `Month ${m} · ${calMonthShort(c.cfg.forkDate, Number(m))}`}
          formatter={(v, name) => name === 'p50' ? [fmt(Number(v), unit), 'Median'] : name === 'outer' ? [`${fmt((v as number[])[0], unit)} – ${fmt((v as number[])[1], unit)}`, band] : [`${fmt((v as number[])[0], unit)} – ${fmt((v as number[])[1], unit)}`, 'Half']} />
        {goal && <ReferenceLine y={goal.value} stroke="#9aa6ad" strokeDasharray="4 4" />}
        <Area type="monotone" dataKey="outer" stroke="none" fill="#b9e475" fillOpacity={0.22} isAnimationActive={false} />
        <Area type="monotone" dataKey="inner" stroke="none" fill="#9fcf5a" fillOpacity={0.45} isAnimationActive={false} />
        <Line type="monotone" dataKey="p50" stroke="#3e4c53" strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: '#fff', strokeWidth: 2 }} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  </div>;
}
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function calMonthShort(forkDate: string, m: number) { const t = new Date(`${forkDate}T12:00:00Z`); const d = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + m, 1)); return `${MON[d.getUTCMonth()]}${d.getUTCMonth() === 0 ? ` ’${String(d.getUTCFullYear()).slice(2)}` : ''}`; }

const RAMP = ['#f7f3f1', '#f6e3dc', '#efc9bd', '#e5a897', '#d7826f', '#c0604f', '#a14638'];
const cellColor = (p: number) => p < 0.005 ? '#f6f7f8' : RAMP[Math.min(RAMP.length - 1, 1 + Math.floor(p / 0.6 * (RAMP.length - 1)))];
/** A progress row («Live by the month») in the green of «good», not the red of a failure. */
const GOOD = ['#f6f7f8', '#e8f1ec', '#d3e6db', '#b6d5c3', '#8fbea4', '#5f9c84', '#3b7c69'];
const goodColor = (p: number) => p < 0.005 ? GOOD[0] : GOOD[Math.min(GOOD.length - 1, 1 + Math.floor(p * (GOOD.length - 1.01)))];

function RiskCalendar({ fork, F }: { fork: string; F: FutureView }) {
  const c = useCase(), cfg = c.cfg, run = c.run!;
  // actors: the rows of the actor file (checks, flags and milestones per month); preview: the preview's checks, then the
  // milestones of the fork from the live run, missed at their target month
  const rows: CalendarRowView[] = F.source === 'actors' && F.calendar[fork] ? F.calendar[fork] : source.calendar(run, cfg, fork);
  const H = F.horizon;
  const [hover, setHover] = useState<{ r: number; m: number } | null>(null);
  return <div className={`cs-cal ${H > 12 ? 'is-long' : ''}`} onMouseLeave={() => setHover(null)}>
    <p className="cs-cal-headline" data-testid="calendar-headline">{calendarHeadline(cfg, rows, F.n)}</p>
    <table className="cs-cal-table">
      <thead><tr><th>Check</th>{Array.from({ length: H }, (_, m) => <th key={m}>{calMonthShort(cfg.forkDate, m + 1)}</th>)}<th className="cs-num">{H === 12 ? 'Year' : 'All'}</th></tr></thead>
      <tbody>{rows.map((r, i) => {
        const first = r.firstByMonth, top = first ? Math.max(...first, 1e-9) : 1, good = isProgressRow(r);
        return <tr key={r.id} className={`${r.live ? 'is-live' : ''} ${good ? 'is-progress' : ''}`} data-row={r.id}>
          <th scope="row"><span>{r.label}</span>{r.consequence && <HelpTip variant="info" size={12} label={r.label} entry={{ title: r.label, short: r.consequence, full: '' }} />}</th>
          {r.byMonth.map((p, m) => <td key={m} onMouseEnter={() => setHover({ r: i, m })}>
            <span className={`cs-cell ${r.medianMonth === m + 1 && r.total >= 0.005 ? 'is-median' : ''} ${p >= 0.35 ? 'is-dark' : ''}`} style={{ background: good ? goodColor(p) : cellColor(p), color: good ? (p >= 0.5 ? '#fff' : '#2f6f5d') : undefined, position: 'relative' }}
              title={`${r.label} by ${calMonth(cfg, m + 1)}: ${pct(p)} of the scenarios${first ? ` · first in this month: ${pct(first[m] ?? 0)}` : ''}${r.medianMonth === m + 1 ? ' · median month' : ''}`}>
              {p >= 0.05 ? Math.round(p * 100) : ''}
              {first && (first[m] ?? 0) >= 0.005 && <i className="cs-cell-first" style={{ right: 'auto', width: `calc((100% - 6px) * ${(first[m] / top).toFixed(3)})` }} aria-hidden />}
            </span>
          </td>)}
          <td className="cs-num"><b>{pct(r.total)}</b></td>
        </tr>;
      })}</tbody>
    </table>
    <div className="cs-cal-foot">
      <span className="cs-cal-legend">0%{RAMP.slice(1).map(col => <i key={col} style={{ background: col }} />)}60%+</span>
      <span><i className="cs-cal-ring" />median month</span>
      {hover && rows[hover.r] && <span className="cs-cal-read">{rows[hover.r].label} by {calMonth(cfg, hover.m + 1)}: <b>{pct(rows[hover.r].byMonth[hover.m])}</b>{rows[hover.r].firstByMonth ? <> · first in this month <b>{pct(rows[hover.r].firstByMonth![hover.m] ?? 0)}</b></> : null}</span>}
    </div>
  </div>;
}
