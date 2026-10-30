// ④ Day: three example futures (pessimistic, median, optimistic) of a fork kept at the review. The selected example
// drives everything below it: the day charts (up to four metrics of the data, picked in «Metrics»), the day card of a
// clicked day and the events lane. The examples are WP19's day files (variator.day/1): forward runs of the company day
// by day, each a branch of the knowledge base; a fork without them says so. Only a page without any actor set (the
// whole page on the preview) shows the preview's examples. Data and rules: case/day/model.ts (WP24).
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowUpRight, CircleDot, Footprints, GitFork, MessagesSquare, TrendingDown, TrendingUp } from 'lucide-react';
import type { PageContext } from '../../../pages';
import { appHref } from '../../../appUrl';
import { useCase } from '../state';
import { fmt, forkLabel } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { Empty, ForkName, SectionHeader, Segmented } from '../ui/parts';
import { SourceBadge, useActors } from '../actors';
import { DEFAULT_KEY, simKey } from '../actors/adapt';
import { branchLink } from '../actors/branch';
import { FutureBadges } from './OverTime';
import { Loading } from './common';
import { coveredForks, DAY_CASE, examplesFor, positionLabel, pickMetrics, seriesDate, stepDay, toggleMetric, defaultMetricIds, type DayExampleData, type DayItem, type People } from '../day/model';
import { saveMetrics, savedMetrics, useDayData } from '../day/load';
import { DayCharts } from '../day/DayCharts';
import { MetricPicker } from '../day/MetricPicker';
import { DayCard, EventLane, hasLinks } from '../day/parts';
import '../day/day.css';

const HISTORY_NOTE = 'This future is not in the knowledge base; showing the company’s history';

export function DaySection({ ctx }: { ctx: PageContext }) {
  const c = useCase(), sec = c.cfg.sections.find(s => s.id === 'day')!, a = useActors();
  const week = ((c.value('week') ?? []) as string[]);
  const sel = ((c.value('day') ?? []) as string[]).filter(f => week.includes(f) && c.run?.forks.includes(f));
  const onActors = !!a.view;
  // a link to «#day» (case A's Day points at case B's) lands on the section once the page has its results
  // (its review first while no fork is kept for the day)
  const ready = !!c.run, target = sel.length ? 'day' : 'day-review';
  useEffect(() => {
    if (!ready || window.location.hash !== '#day') return;
    // again while the sections above settle (charts, the actor verdict), until the reader scrolls or types
    const go = () => (document.getElementById(target) ?? document.getElementById('day'))?.scrollIntoView({ block: 'start' });
    const timers = [0, 400, 1200, 2500, 4000].map(ms => window.setTimeout(go, ms));
    const stop = () => timers.forEach(t => window.clearTimeout(t));
    const opts = { once: true, passive: true } as const;
    window.addEventListener('wheel', stop, opts); window.addEventListener('touchmove', stop, opts); window.addEventListener('keydown', stop, opts);
    return () => { stop(); window.removeEventListener('wheel', stop); window.removeEventListener('touchmove', stop); window.removeEventListener('keydown', stop); };
  }, [ready]); // eslint-disable-line react-hooks/exhaustive-deps
  return <section id="day" className="cs-section" aria-labelledby="day-title">
    <SectionHeader id="day" title="Day" purpose={onActors ? 'Three example scenarios of a fork, day by day: the metrics you pick and every event; click a day to see what happened.' : sec.purpose}
      help={onActors ? 'day.actors' : sec.help} badges={<DayBadge />}
      more={onActors ? ['Each example is a forward run of the company day by day on the habits learned from its history; the examples of some forks are branches of the knowledge base, with their people, clients and records.', 'Pessimistic, median and optimistic order the three examples by their cash at the end of the run. Each card says where that cash falls among all the scenarios of the fork, once the data gives it.'] : undefined} />
    {!c.run ? <Loading what="the examples" /> : !sel.length ? <Empty icon={<Footprints size={22} />} title="Choose a fork above" text="The day level shows three example scenarios of the forks you pick at the review." action={<button type="button" className="button" onClick={() => document.getElementById('day-review')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>Go to the review</button>} /> : <DayBody forks={sel} ctx={ctx} />}
  </section>;
}

/** The source of the examples: actors (precomputed for the case defaults: they do not follow edits), or the preview. */
function DayBadge() {
  const c = useCase(), a = useActors();
  if (!a.view) return <FutureBadges level="day" />;
  if (simKey(c.edits) === DEFAULT_KEY) return <SourceBadge source="actors" state="defaults" scope={a.scope} />;
  if (a.treeFromDefaults) return <FutureBadges level="day" />;
  return <SourceBadge source="actors" state="stale" scope={a.scope} text="Day: for the defaults" tip="The example days are forward runs of the case defaults, each a branch of the knowledge base. They do not follow your edits." />;
}

function DayBody({ forks, ctx }: { forks: string[]; ctx: PageContext }) {
  const c = useCase(), cfg = c.cfg, run = c.run!, a = useActors(), d = useDayData(cfg.id);
  // the fork: the reader's pick, else the first kept (the week tree's «example days» puts its fork first)
  const [pick, setPick] = useState<string | null>(null);
  useEffect(() => setPick(null), [forks[0]]); // eslint-disable-line react-hooks/exhaustive-deps
  const fork = pick && forks.includes(pick) ? pick : forks[0];
  const onActors = a.status === 'ready';
  const people = useMemo<People>(() => ({ ...Object.fromEntries(cfg.people.map(p => [p.id, { name: p.name, entity: p.entityId }])), ...d.people }), [cfg.people, d.people]);
  const data = useMemo(() => examplesFor(fork, { file: d.file, onActors, forkDate: cfg.forkDate, people, preview: run.preview }), [fork, d.file, onActors, cfg.forkDate, people, run.preview]);
  const exId = String(c.value('dayExample') ?? 'median');
  const ex = data.examples.find(e => e.id === exId) ?? data.examples[1] ?? data.examples[0];

  // the charted metrics: the viewer's choice (kept in this browser), else the four most relevant
  const [saved, setSaved] = useState<string[] | null>(() => savedMetrics(cfg.id));
  const available = data.metrics.map(m => m.id);
  const chosenIds = pickMetrics(saved, available);
  const chosen = chosenIds.map(id => data.metrics.find(m => m.id === id)!).filter(Boolean);
  const isDefault = chosenIds.join('|') === defaultMetricIds(available).join('|');
  const setMetrics = (ids: string[] | null) => { setSaved(ids); saveMetrics(cfg.id, ids); };

  // the selected day (kept across the examples: they share their dates) and the event picked in the lane
  const [day, setDay] = useState<string | null>(null);
  const [active, setActive] = useState<number | null>(null);
  const date = ex && day ? seriesDate(ex.points, day) : null;
  const select = (dt: string | null) => { setDay(dt); setActive(null); };
  const pickEvent = (e: DayItem) => { setDay(seriesDate(ex!.points, e.date)); setActive(e.i); };

  const switcher = forks.length > 1
    ? <Segmented<string> label="Fork of the day level" value={fork} options={forks.map(f => ({ id: f, label: forkLabel(cfg, f) }))} onChange={f => { setPick(f); setActive(null); }} />
    : <ForkName fork={fork} strong />;
  if ((a.status === 'loading' || (onActors && d.status === 'loading'))) return <div className="cs-body"><div className="cs-toolbar">{switcher}</div><p className="cs-muted cs-day-wait" role="status">Loading the example days…</p></div>;
  if (!ex) return <div className="cs-body"><div className="cs-toolbar">{switcher}</div><NoExamples fork={fork} covered={coveredForks(d.file).filter(f => cfg.forks.some(x => x.id === f))} kept={forks} onPick={f => setPick(f)} noFile={onActors && !d.file && cfg.id !== DAY_CASE} /></div>;

  const note = data.source === 'day-file' && !branchLink(ex.tenant, ctx.org).open && hasLinks(ex, ctx) ? HISTORY_NOTE : null;
  return <div className={`cs-body ${c.stale ? 'is-stale' : ''}`}>
    <div className="cs-toolbar cs-day-toolbar">
      {switcher}
      <span className="cs-day-tools">
        <MetricPicker metrics={data.metrics} chosen={chosenIds} isDefault={isDefault} onToggle={id => setMetrics(toggleMetric(chosenIds, id, available))} onReset={() => setMetrics(null)} />
        <BranchLink ex={ex} ctx={ctx} />
      </span>
    </div>
    <div className="cs-examples" role="radiogroup" aria-label="Example scenario" data-tour="day-examples" data-testid="day-examples" data-source={data.source}>
      {data.examples.map(e => <button key={e.id} type="button" role="radio" aria-checked={e.id === ex.id} data-digest={e.digest} className={`card cs-example is-${e.id} ${e.id === ex.id ? 'is-on' : ''}`}
        onClick={() => { setActive(null); c.change({ dayExample: e.id }, `Day example: ${e.label}`, 'quiet'); }}>
        <span className="cs-example-top">{e.id === 'pessimistic' ? <TrendingDown size={16} /> : e.id === 'optimistic' ? <TrendingUp size={16} /> : <CircleDot size={16} />}<strong>{e.label}</strong><small data-testid="day-example-position">{positionLabel(e, a.month?.n)}</small></span>
        <span className="cs-example-cash">{fmt(e.headline.value, e.headline.unit)}</span>
        {e.outcome && <span className="cs-example-line">{e.outcome}</span>}
      </button>)}
      <HelpTip entry={{ title: 'Three example scenarios', short: 'Three runs of this fork, ordered by their cash at the end of the run: the lowest, the middle and the highest of the three. The percentile on a card is where that cash falls among all the scenarios of the fork.', full: '' }} label="Examples"
        more={[data.source === 'day-file' ? 'Each example is its own forward run of the company day by day: its own days, events and people.' : 'A preview from the formula model of the case: the page has no actor simulation yet.',
          data.source === 'day-file' ? (ex.kb ? 'This fork’s examples are branches of the knowledge base: open one to read its records, dossiers and causes.' : 'This fork’s examples are records only: they are not in the knowledge base.') : null]} />
    </div>
    {ex.ask.length > 0 && <AskChips ex={ex} ctx={ctx} />}
    <div className="cs-day">
      <div className="cs-day-main">
        <article className="card cs-card cs-day-charts" data-testid="day-charts" data-example={ex.id} data-digest={ex.digest} data-metrics={chosenIds.join(',')}>
          <div className="cs-card-head"><h3>Day by day</h3>{!date && <span className="cs-muted cs-day-hint">Click a day</span>}
            <HelpTip entry={{ title: 'Day by day', short: 'Each chart is one metric of the selected example, day by day. Dots are days with events; click a day to see what happened.', full: '' }} label="Day by day"
              more={[`${ex.points.length} working days, ${ex.points[0]?.date ?? ''} to ${ex.points[ex.points.length - 1]?.date ?? ''}.`, 'On the focused charts: Enter picks a day, the arrow keys move it (Shift: a week), Escape clears it.', '«Metrics» picks up to four of the metrics this example has; the choice stays in this browser.']} /></div>
          <DayCharts ex={ex} metrics={chosen} selected={date} onSelect={select} />
        </article>
        {date && <DayCard ex={ex} date={date} metrics={data.metrics} chosen={chosenIds} ctx={ctx} fork={fork} onStep={k => select(stepDay(ex.points, date, k))} onClose={() => select(null)} />}
      </div>
      <EventLane ex={ex} date={date} active={active} ctx={ctx} onPick={pickEvent} note={note} />
    </div>
  </div>;
}

/** A fork without example days: said plainly, with the forks that have them. A case without a day file (case A in
 *  this version) points at the case that has one. */
function NoExamples({ fork, covered, kept, onPick, noFile }: { fork: string; covered: string[]; kept: string[]; onPick: (f: string) => void; noFile: boolean }) {
  const c = useCase(), navigate = useNavigate();
  if (noFile) return <div className="cs-day-none" role="status" data-tour="day-examples" data-testid="day-examples" data-source="none" data-fork={fork} data-reason="other-case">
    <Footprints size={20} aria-hidden />
    <strong>Example days are made for case B in this version</strong>
    <a className="cs-text-btn cs-day-none-link" data-testid="day-other-case" href={appHref(`/cases/${DAY_CASE}#day`)}
      onClick={e => { e.preventDefault(); navigate({ pathname: `/cases/${DAY_CASE}`, hash: '#day' }); }}>Open case B’s Day<ArrowUpRight size={13} aria-hidden /></a>
    <HelpTip entry={{ title: 'Example days', short: 'Example days are forward runs of the company day by day, three per fork, each a branch of the knowledge base. This version makes them for case B; this case shows its months and weeks.', full: '' }} label="Example days" />
  </div>;
  return <div className="cs-day-none" role="status" data-tour="day-examples" data-testid="day-examples" data-source="none" data-fork={fork}>
    <Footprints size={20} aria-hidden />
    <strong>No example days for this fork yet</strong>
    {covered.length > 0 && <span className="cs-day-none-has">Example days: {covered.map((f, i) => <span key={f}>{i > 0 && ', '}{kept.includes(f) && f !== fork
      ? <button type="button" className="cs-text-btn" onClick={() => onPick(f)}>{forkLabel(c.cfg, f)}</button> : forkLabel(c.cfg, f)}</span>)}</span>}
    <HelpTip entry={{ title: 'Example days', short: 'Example days are forward runs of the company day by day: three per fork (pessimistic, median, optimistic), each a branch of the knowledge base. This fork has none yet.', full: '' }} label="Example days" />
  </div>;
}

/** «Ask this future»: the branch's checked why-questions (WP19); a chip opens Ask on the branch with the question
 *  (`/ask?branch=…&q=…`). Off, with the reason, while the knowledge base does not list the branch. */
function AskChips({ ex, ctx }: { ex: DayExampleData; ctx: PageContext }) {
  const { open, params, why } = branchLink(ex.tenant, ctx.org);
  return <div className="cs-day-ask" data-testid="day-ask" data-tenant={ex.tenant ?? ''}>
    <span className="cs-day-ask-lead"><MessagesSquare size={14} aria-hidden />Ask this future:</span>
    {ex.ask.map(q => {
      const to = open && params ? { ...params, q } : null;
      return <a key={q} className="cs-day-ask-chip" aria-disabled={!to} href={to ? ctx.link('/ask', to) : undefined} title={to ? `Ask the knowledge base of this future: ${q}` : why}
        onClick={e => { e.preventDefault(); if (to) ctx.go('/ask', to); }}><span>{q}</span>{to && <ArrowUpRight size={13} aria-hidden />}</a>;
    })}
  </div>;
}

/** «Open this future in the knowledge base»: the brief on the example's branch tenant (marked as a simulation there).
 *  Off, with the reason in its tooltip, while the example has no branch or the knowledge base does not list it yet. */
function BranchLink({ ex, ctx }: { ex: DayExampleData; ctx: PageContext }) {
  const b = branchLink(ex.tenant, ctx.org), { open, params } = b;
  const why = ex.kb === false ? 'This future has records only: it is not a branch of the knowledge base.' : b.why;
  return <a className="button cs-branch-link" data-testid="open-branch" data-tenant={ex.tenant ?? ''} aria-disabled={!open} title={why || `Opens the brief, the dossiers and Ask on the branch «${ex.tenant}», marked as a simulation`}
    href={open && params ? ctx.link('/brief', params) : undefined} onClick={e => { e.preventDefault(); if (open && params) ctx.go('/brief', params); }}>
    <GitFork size={15} aria-hidden />Open this future in the knowledge base{open && <ArrowUpRight size={14} aria-hidden />}
  </a>;
}
