// ② Month — Plan: the VARIATOR timeline in the Plancy style. Milestone targets (diamonds) with forecast bands and
// their months written inside, P(met), «N mo spare / needs N mo more / on the edge», work bars, finish-to-start
// arrows, placed events (pink), goals at the horizon; inputs in conflict C1.., the levers plan P1.., «Suggest dates».
// Dragging a target or a start recomputes the case and reports the effect in a notice.
import { useEffect } from 'react';
import { CalendarCheck2, CheckCircle2, LoaderCircle, RotateCcw, Sparkles, Trash2, WandSparkles, XCircle } from 'lucide-react';
import type { Dist, Plan } from '../model/types';
import { source } from '../model/source';
import { useCase } from '../state';
import { calMonth, fmtValue, forkColor, forkLabel, pct } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { MonthTrack, type TrackArrow, type TrackBand, type TrackMarker, type TrackRow } from '../ui/MonthTrack';
import { ForkDot, SectionHeader } from '../ui/parts';
import { Loading } from './common';
import { FormulaEstimate, NumbersBadge, goalViews, numbersOf, onActors, planFork, sourceOf } from '../numbers';
import { useActors } from '../actors';
import { leverColor, num } from './Forks';

const fixed = (v: number): Dist => ({ kind: 'fixed', value: v });
const m1 = (v: number) => `${+v.toFixed(1)}`;

export function PlanSection() {
  const c = useCase(), cfg = c.cfg, sec = cfg.sections.find(s => s.id === 'plan')!, src = sourceOf(useActors());
  const paths = new Set(cfg.milestones.map(m => m.monthPath).filter(Boolean) as string[]);
  // one source of numbers: P(met) and the forecast months of the actors (month.json) while their result is current, of
  // the formula model as the draft after edits; the inputs in conflict and the levers plan are formula tools: closed
  // as a «Formula estimate» next to the actor numbers
  return <section id="plan" className="cs-section" aria-labelledby="plan-title">
    <SectionHeader id="plan" title="Plan" purpose={sec.purpose} help={sec.help} paths={p => paths.has(p) || p.startsWith('goal|')}
      badges={<NumbersBadge source={src} />} simulation={!onActors(src)} />
    {!c.run || !c.ranking ? <Loading what="the plan" /> : <PlanBody />}
  </section>;
}

const WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];
/** «Both milestones and goals are met», «All five …», «The milestone is met». */
export function allMetText(n: number): string {
  return n === 1 ? 'The one milestone or goal is met' : n === 2 ? 'Both milestones and goals are met' : `All ${WORDS[n] ?? n} milestones and goals are met`;
}

function PlanBody() {
  const c = useCase(), cfg = c.cfg, run = c.run!, H = cfg.horizonMonths;
  const a = useActors(), src = sourceOf(a), nums = numbersOf(c, a), fork = planFork(c, nums), goals = goalViews(c, a, fork);
  const ms = goals.filter(g => g.kind === 'milestone'), finals = goals.filter(g => g.kind === 'goal');
  const missed = goals.filter(g => (g.p ?? 1) < 0.8);
  // the levers plan searches on the formula model: it runs for the items the formula model misses
  const formulaMissed = run.goals.filter(g => g.p[fork] != null && (g.p[fork] ?? 1) < 0.8).length;
  const plan = c.plans[fork];
  useEffect(() => { if (formulaMissed && !plan && !c.stale) c.requestPlan(fork); }, [fork, formulaMissed, plan, c.stale]); // eslint-disable-line react-hooks/exhaustive-deps
  const pill = (p: number | undefined) => p == null ? <span className="cs-pill is-none" title="No number for this item on your company's actors">—</span> : <span className={`cs-pill is-${ok(p) ? 'ok' : 'bad'}`}>{pct(p)}</span>;
  const metText = (p: number | undefined) => p == null ? 'no number on the actors' : `met in ${pct(p)}`;

  const rows: TrackRow[] = [], markers: TrackMarker[] = [], bands: TrackBand[] = [], arrows: TrackArrow[] = [];
  function ok(p: number | undefined) { return p == null || p >= 0.8; }
  const mark = (p: number | undefined) => p == null ? '#8a949b' : ok(p) ? '#3b7c69' : '#c0604f';
  for (const g of ms) {
    const cfgM = cfg.milestones.find(x => x.id === g.id)!, p = g.p, sp = g.spread;
    rows.push({ id: g.id, label: <span className="cs-tk-rowlabel"><span>{g.label}</span>{pill(p)}</span> });
    for (const dep of cfgM.dependsOn) {
      const i = cfg.interventions.find(x => x.id === dep);
      if (!i) continue;
      const start = num(c.value(i.monthPath));
      markers.push({ id: `start:${i.id}`, row: g.id, month: start, shape: 'circle', color: i.forks?.length ? forkColor(cfg, fork) : leverColor(cfg, i), draggable: true, title: `${i.label}: ${calMonth(cfg, start)}. Drag to start earlier or later.`, text: undefined });
      if (sp) bands.push({ row: g.id, from: start, to: sp[1], kind: 'work', color: '', title: `Work from ${calMonth(cfg, start)} to the median ${calMonth(cfg, sp[1])}` });
    }
    if (sp) bands.push({ row: g.id, from: sp[0], to: sp[2], mid: sp[1], color: ok(p) ? '#cfe6d9' : '#f3d3cb', text: `m ${m1(sp[0])}–${m1(sp[2])}`, title: `In 8 of 10 scenarios it is reached between month ${m1(sp[0])} and ${m1(sp[2])}; median month ${m1(sp[1])}. Your target does not change the forecast; the start, the inputs and the events do.` });
    const gap = sp ? g.month - sp[3] : null;
    const gapText = gap == null ? '' : Math.abs(gap) < 0.05 ? ' · on the edge' : gap > 0 ? ` · ${m1(gap)} mo spare` : ` · needs ${m1(-gap)} mo more`;
    markers.push({ id: `ms:${g.id}`, row: g.id, month: g.month, shape: 'diamond', color: mark(p), draggable: !!cfgM.monthPath, title: `${g.label}: target ${calMonth(cfg, g.month)}, ${metText(p)}. Drag to move the target.`, text: `by ${calMonth(cfg, g.month)}${gapText}`, textAt: sp ? Math.max(sp[2], g.month) : g.month });
  }
  const placed = cfg.interventions.filter(i => i.kind === 'event' && i.placePath && c.value(i.placePath) === true);
  rows.push({ id: 'events', label: <span className="cs-tk-rowlabel"><i className="cs-ev-dot" /><span>Events</span></span> });
  for (const ev of placed) {
    const m = num(c.value(ev.monthPath));
    markers.push({ id: `ev:${ev.id}`, row: 'events', month: m, shape: 'circle', color: 'var(--cs-event)', draggable: true, title: `${ev.label}: ${calMonth(cfg, m)}. Drag to move it.`, text: ev.label });
    for (const g of [...ms, ...finals]) { const sp = g.spread; if (!sp || sp[1] > m || g.kind === 'goal') arrows.push({ fromRow: 'events', from: m, toRow: g.id, to: sp ? sp[0] : g.month - 0.6, kind: 'ev', title: `${ev.label} in ${calMonth(cfg, m)} comes before «${g.label}», so it can change it.` }); }
  }
  for (const g of finals) {
    const p = g.p;
    rows.push({ id: g.id, label: <span className="cs-tk-rowlabel"><span>{g.label}</span>{pill(p)}</span> });
    markers.push({ id: `goal:${g.id}`, row: g.id, month: g.month, shape: 'square', color: mark(p), ring: mark(p), title: `${g.label}: fixed date ${calMonth(cfg, g.month)}, ${metText(p)}.` });
    for (const m of ms) arrows.push({ fromRow: m.id, from: m.month + 0.15, toRow: g.id, to: g.month - 0.45, kind: 'dep', hot: (m.p ?? 1) < 0.8, title: `«${g.label}» needs «${m.label}» first (finish-to-start).${(m.p ?? 1) < 0.8 ? ' The milestone misses its target: it binds.' : ''}` });
  }
  if (!placed.length) markers.push({ id: 'ev-none', row: 'events', month: 0, shape: 'circle', color: '#c9d0d4', ghost: true, title: 'No event placed. Place one in step 1.', text: 'No event placed · place one in step 1' });

  const move = (id: string, to: number, from: number) => {
    const [kind, key] = id.split(':');
    if (kind === 'ms') { const m = cfg.milestones.find(x => x.id === key)!; if (m.monthPath) c.change({ [m.monthPath]: fixed(to) }, `«${m.label}» target: ${calMonth(cfg, from)} → ${calMonth(cfg, to)}`, 'sim', fork); }
    else if (kind === 'start' || kind === 'ev') { const i = cfg.interventions.find(x => x.id === key)!; c.change({ [i.monthPath]: fixed(to) }, `${i.label}: ${calMonth(cfg, from)} → ${calMonth(cfg, to)}`, 'sim', fork); }
  };
  const suggest = () => { const patch = source.suggestDates(run, cfg, fork); if (Object.keys(patch).length) c.change(patch, `Suggested dates applied for ${forkLabel(cfg, fork)} (8 of 10 scenarios)`, 'sim', fork); };
  const conflicts = run.conflicts.filter(x => x.fork === fork);
  const off = cfg.milestones.filter(m => c.value(`goal|${m.id}|off`) === true);
  return <div className={`cs-body ${c.stale ? 'is-stale' : ''}`}>
    <div className="cs-body" data-tour="plan-timeline">
    <div className="cs-toolbar">
      <label className="cs-inline-select"><span>Show for</span><select className="cs-select" value={fork} onChange={e => c.setTimelineFork(e.target.value)} aria-label="Fork of the plan">
        {run.forks.map(f => <option key={f} value={f}>{forkLabel(cfg, f)}{f === (nums?.order[0] ?? c.ranking!.order[0]) ? ' (leader)' : ''}</option>)}
      </select></label>
      <span className="cs-toolbar-note"><ForkDot fork={fork} />{missed.length ? `${missed.length} of ${goals.length} items miss the 80% target` : allMetText(goals.length)}</span>
      <button type="button" className="button" onClick={suggest} title={cfg.help.suggest?.short}><WandSparkles size={15} />Suggest dates</button>
      <HelpTip entry={cfg.help.suggest} />
    </div>
    <article className="card cs-card cs-plan-track">
      <div className="cs-card-head"><h3>Timeline</h3><HelpTip entry={cfg.help.timeline} label="Timeline" more={['Diamond: a target you can drag. Band: where 8 of 10 scenarios reach it, with the median tick. Circle: a start you can drag. Square: a fixed date.']} /></div>
      <MonthTrack rows={rows} markers={markers} bands={bands} arrows={arrows} horizon={H} labelWidth={210} ariaLabel={`Plan timeline of ${forkLabel(cfg, fork)}`} monthLabel={m => calMonth(cfg, m, { year: 'jan' })} onMove={move} />
    </article>
    </div>
    <div className="cs-grid">
      <article className="card cs-card cs-span-4">
        <div className="cs-card-head"><h3>Milestones and goals</h3><HelpTip entry={cfg.help.pmet} label="P(met)" /></div>
        <table className="cs-table cs-ms-table"><thead><tr><th>Item</th><th>Target</th><th className="cs-num">P(met)</th><th /></tr></thead>
          <tbody>{goals.map(g => <tr key={g.id}><th scope="row">{g.label}</th><td>{calMonth(cfg, g.month)}</td><td className="cs-num">{pill(g.p)}</td>
            <td><button type="button" className="cs-icon-btn" aria-label={`Remove ${g.label} from the plan`} title="Remove from the plan" onClick={() => c.change({ [`goal|${g.id}|off`]: true }, `«${g.label}» removed from the plan`, 'sim')}><Trash2 size={14} /></button></td></tr>)}</tbody></table>
        {off.length > 0 && <div className="cs-restore">{off.map(m => <button key={m.id} type="button" className="cs-text-btn" onClick={() => c.change({ [`goal|${m.id}|off`]: false }, `«${m.label}» restored`, 'sim')}><RotateCcw size={13} />Restore {m.label}</button>)}</div>}
      </article>
      <article className="card cs-card cs-span-4">
        <div className="cs-card-head"><h3>Inputs in conflict</h3><HelpTip entry={cfg.help.conflicts} label="Inputs in conflict" /></div>
        <FormulaEstimate source={src} what="which inputs work against an item, on the quick formula model">
        {!formulaMissed ? <p className="cs-holds"><CheckCircle2 size={16} />Nothing in conflict: every item is met for {forkLabel(cfg, fork)}.</p>
          : conflicts.length ? <ol className="cs-ids">{conflicts.map((x, i) => <li key={i} className="is-conflict"><span className="cs-rid is-c">C{i + 1}</span><span><b>{x.inputLabel}</b> {x.r < 0 ? 'too high' : 'too low'} for «{run.goals.find(g => g.id === x.goal)?.label}»<small>correlation r {x.r.toFixed(2)} with «met»</small></span></li>)}</ol>
            : <p className="cs-muted">No single input stands out.</p>}
        </FormulaEstimate>
      </article>
      <article className="card cs-card cs-span-4">
        <div className="cs-card-head"><h3>Levers plan</h3><HelpTip entry={cfg.help.levers} label="Levers plan" /></div>
        <FormulaEstimate source={src} what="the levers that meet every item, searched on the quick formula model">
        <LeversPlan plan={plan} missed={formulaMissed} fork={fork} />
        </FormulaEstimate>
      </article>
    </div>
  </div>;
}

function LeversPlan({ plan, missed, fork }: { plan: Plan | 'pending' | undefined; missed: number; fork: string }) {
  const c = useCase(), cfg = c.cfg;
  const stepText = (v: number, unit: string) => unit === 'month' ? calMonth(cfg, v) : fmtValue(v, unit);
  if (!missed) return <p className="cs-holds"><CalendarCheck2 size={16} />The current plan meets every milestone and goal of {forkLabel(cfg, fork)}.</p>;
  if (!plan || plan === 'pending') return <p className="cs-muted cs-wait"><LoaderCircle size={14} className="spin" />Searching a plan: trying each start month…</p>;
  return <div className="cs-plan">
    <p className={`cs-plan-verdict is-${plan.met ? 'ok' : 'bad'}`}>{plan.met ? <><CheckCircle2 size={15} />The plan meets all milestones and goals.</> : <><XCircle size={15} />Not reachable with these levers. The best result:</>}</p>
    <ol className="cs-ids">{plan.steps.map((s, i) => <li key={s.path}><span className="cs-rid is-p">P{i + 1}</span><span>{s.label}{s.unit === 'month' ? ' ' : ': '}<b>{stepText(s.value, s.unit)}</b>{s.mode && <>, <b>{s.mode.label}</b></>}{(s.value !== s.current || (s.mode && s.mode.value !== s.mode.current)) && <small>now {stepText(s.current, s.unit)}</small>}</span></li>)}</ol>
    {!plan.steps.length && <p className="cs-muted">No lever for this fork.</p>}
    <p className="cs-plan-p">With the plan: {Object.entries(plan.p).map(([g, p]) => `${c.run!.goals.find(x => x.id === g)?.label ?? g} ${pct(p)}`).join(' · ')}</p>
    {plan.steps.some(s => s.value !== s.current || (s.mode && s.mode.value !== s.mode.current)) && <button type="button" className="button button-primary" onClick={() => c.change(Object.fromEntries(plan.steps.flatMap(s => [[s.path, fixed(s.value)] as const, ...(s.mode ? [[s.mode.path, fixed(s.mode.value)] as const] : [])])), `Levers plan applied for ${forkLabel(cfg, fork)}`, 'sim', fork)}><Sparkles size={14} />Apply the plan</button>}
  </div>;
}
