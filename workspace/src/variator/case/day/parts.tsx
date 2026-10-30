// WP24: pieces of the Day section: a knowledge-base link of an event, an event row (the lane and the day card share
// it), the card of the selected day, and the events lane that follows the selected day.
import { DayFix } from './DayFix';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ArrowUpRight, CalendarDays, ChevronLeft, ChevronRight, X } from 'lucide-react';
import type { PageContext } from '../../../pages';
import { dayValue, GROUP_LABEL, GROUP_ORDER, itemsOn, kbTarget, longDate, shortDate, type DayExampleData, type DayItem, type DayMetric, duration } from './model';

/** A link of an event into the knowledge base: the dossier on the example's branch, else in the company history. */
export function KbLink({ ctx, tenant, entityId, children, title, className = 'cs-kb-link' }: { ctx: PageContext; tenant?: string; entityId?: string; children: ReactNode; title?: string; className?: string }) {
  const t = kbTarget(entityId, tenant, ctx.org, ctx.path);
  if (!t) return null;
  const where = t.branch ? 'on this future’s branch of the knowledge base' : 'in the company history (this future is not in the knowledge base)';
  return <a className={className} data-branch={t.branch ?? ''} href={ctx.link(t.path, t.params)} title={title ? `${title} · ${where}` : `Open ${where}`}
    onClick={e => { e.preventDefault(); e.stopPropagation(); if (t.branch) ctx.go(t.path, t.params); else ctx.openEntity(entityId!); }}>{children}</a>;
}

/** Whether any event of the example links somewhere. */
export const hasLinks = (ex: DayExampleData, ctx: PageContext) => ex.items.some(e => kbTarget(e.entityId, ex.tenant, ctx.org, ctx.path) || kbTarget(e.personEntity, ex.tenant, ctx.org, ctx.path));

/** What an event says: an incident's service and fault with its clients, duration and the person on call; any other
 *  event's sentence and its person; the record's dossier. */
function ItemBody({ e, ctx, tenant, withTitle }: { e: DayItem; ctx: PageContext; tenant?: string; withTitle: boolean }) {
  const person = e.person && (kbTarget(e.personEntity, tenant, ctx.org, ctx.path)
    ? <KbLink ctx={ctx} tenant={tenant} entityId={e.personEntity} className="cs-kb-person" title={e.person}>{e.person}</KbLink> : <span>{e.person}</span>);
  const meta = e.group === 'incident'
    ? [e.clients != null && `${e.clients} client${e.clients === 1 ? '' : 's'}`, e.minutes != null && `restored in ${duration(e.minutes)}`, e.cause && `cause: ${e.cause}`].filter(Boolean) as string[]
    : [];
  return <>
    <span className="cs-dayitem-line">
      {e.severity ? <span className={`cs-sev is-${e.severity.toLowerCase()}`}>{e.severity}</span> : withTitle && <span className="cs-dayitem-kind">{e.title}</span>}
      <strong>{e.text || e.title}</strong>
    </span>
    {(meta.length > 0 || person || e.entityId) && <span className="cs-dayitem-meta">
      {meta.map(x => <span key={x}>{x}</span>)}
      {person && <span>{e.group === 'incident' ? 'on call ' : ''}{person}</span>}
      <KbLink ctx={ctx} tenant={tenant} entityId={e.entityId} title={e.text || e.title}>Open<ArrowUpRight size={12} aria-hidden /></KbLink>
    </span>}
  </>;
}

/** The card of the selected day: its date, the metric values that day and everything that happened, by kind. */
export function DayCard({ ex, date, metrics, chosen, ctx, fork, onStep, onClose }: { ex: DayExampleData; date: string; fork?: string; metrics: DayMetric[]; chosen: string[]; ctx: PageContext; onStep: (delta: number) => void; onClose: () => void }) {
  const p = ex.points.find(x => x.date === date);
  const items = itemsOn(ex, date);
  const groups = GROUP_ORDER.map(g => ({ g, xs: items.filter(e => e.group === g) })).filter(x => x.xs.length);
  const first = ex.points[0]?.date === date, last = ex.points[ex.points.length - 1]?.date === date;
  // the charted metrics; the rest of the catalogue behind «All metrics» (kept open while the reader moves day to day)
  const [all, setAll] = useState(false);
  const rest = metrics.filter(m => !chosen.includes(m.id));
  const shown = [...metrics.filter(m => chosen.includes(m.id)), ...(all ? rest : [])];
  return <article className="card cs-card cs-daycard" data-testid="day-card" data-date={date} aria-label={`What happened on ${longDate(date)}`}>
    <header className="cs-daycard-head">
      <div className="cs-daycard-title"><CalendarDays size={15} aria-hidden /><h3>{longDate(date)}</h3><span className="cs-muted">day {p?.day ?? '—'} · {ex.label.toLowerCase()}</span></div>
      <div className="cs-daycard-nav">
        <button type="button" className="cs-icon-btn" aria-label="Previous day" disabled={first} onClick={() => onStep(-1)}><ChevronLeft size={16} /></button>
        <button type="button" className="cs-icon-btn" aria-label="Next day" disabled={last} onClick={() => onStep(1)}><ChevronRight size={16} /></button>
        <button type="button" className="cs-icon-btn" aria-label="Close the day" onClick={onClose}><X size={16} /></button>
      </div>
    </header>
    <dl className="cs-daycard-values">{shown.map(m => <div key={m.id} className={chosen.includes(m.id) ? 'is-on' : ''} title={m.description}><dt>{m.label}</dt><dd>{dayValue(m, p?.values[m.id])}</dd></div>)}</dl>
    {rest.length > 0 && <button type="button" className="cs-text-btn cs-daycard-more" aria-expanded={all} data-testid="day-card-all" onClick={() => setAll(x => !x)}>{all ? 'Fewer metrics' : `All metrics that day`}<span className="cs-count">{all ? metrics.length : rest.length}</span></button>}
    {groups.length ? <div className="cs-daycard-groups">{groups.map(({ g, xs }) => <section key={g} className="cs-daycard-group" data-group={g}>
      <h4>{GROUP_LABEL[g]}<span className="cs-count">{xs.length}</span></h4>
      <ul>{xs.map(e => <li key={e.i} className={`cs-dayitem is-${e.tone}`}>{e.date !== date && <span className="cs-dayitem-date">{shortDate(e.date)}</span>}<ItemBody e={e} ctx={ctx} tenant={ex.tenant} withTitle={false} />{fork && <DayFix e={e} fork={fork} day={date} />}</li>)}</ul>
    </section>)}</div> : <p className="cs-daycard-quiet">A quiet day: no events.</p>}
  </article>;
}

/** The events lane: every event of the example; those of the selected day are highlighted and scrolled to. */
export function EventLane({ ex, date, active, ctx, onPick, note }: { ex: DayExampleData; date: string | null; active: number | null; ctx: PageContext; onPick: (e: DayItem) => void; note: string | null }) {
  const box = useRef<HTMLElement>(null), list = useRef<HTMLOListElement>(null);
  const on = new Set(itemsOn(ex, date).map(e => e.i));
  useEffect(() => {
    const c = box.current, first = date ? list.current?.querySelector<HTMLElement>('li.is-day') : null;
    if (!c || !first) return;
    // scroll the lane only (never the page): the day's first event to the top of the lane
    const head = [...c.querySelectorAll<HTMLElement>(':scope > .cs-card-head, :scope > .cs-dayfeed-note')].reduce((h, e) => h + e.offsetHeight, 0);
    const top = first.getBoundingClientRect().top - c.getBoundingClientRect().top + c.scrollTop - head - 4;
    c.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  }, [date, ex.digest]);
  return <article ref={box} className="card cs-card cs-feed cs-dayfeed" data-testid="day-events">
    <div className="cs-card-head"><h3>Events</h3><span className="cs-muted">{ex.items.length} in {ex.points.length} days</span></div>
    {note && <p className="cs-dayfeed-note" data-testid="day-history-note">{note}</p>}
    <ol ref={list} className="cs-feed-list">{ex.items.map(e => <li key={e.i} data-date={e.date} className={`cs-feed-item cs-dayitem is-${e.tone} ${on.has(e.i) ? 'is-day' : ''} ${active === e.i ? 'is-active' : ''}`} onClick={() => onPick(e)}>
      <button type="button" className="cs-feed-date" aria-pressed={on.has(e.i)} aria-label={`Show ${longDate(e.date)}`} onClick={ev => { ev.stopPropagation(); onPick(e); }}>
        <CalendarDays size={12} aria-hidden />{shortDate(e.date)} · day {e.day}
      </button>
      <ItemBody e={e} ctx={ctx} tenant={ex.tenant} withTitle />
    </li>)}</ol>
  </article>;
}

