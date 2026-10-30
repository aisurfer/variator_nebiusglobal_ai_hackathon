// Small layout pieces shared by detail views (the metric drill-down and views built on it) and the workspace pages: a
// titled section, a "Know more" popover, a dated timeline and a grid of properties, in the visual form of the sheets.
// The product rule: only titles, numbers, controls, the headline and warnings stay visible; secondary texts (intros,
// how a number is computed, notes, long captions) go into a "Know more" popover or a disclosure closed by default.
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { ChevronDown, Info } from 'lucide-react';
import { longDay } from '../primitives/format';

/** A section with a title. `detail` (a caption) goes into a "Know more" popover; `closed`: a disclosure closed by
 * default whose summary is the title. */
export function Block({ title, detail, closed = false, children }: { title: string; detail?: string; closed?: boolean; children: ReactNode }) {
  if (closed) return <details className="detail-section know-more-details"><summary><ChevronDown size={15} aria-hidden /><h3>{title}</h3>{detail && <KnowMore label={`About ${title.toLowerCase()}`}>{detail}</KnowMore>}</summary>{children}</details>;
  return <section className="detail-section"><h3 className="know-more-title">{title}{detail && <KnowMore label={`About ${title.toLowerCase()}`}>{detail}</KnowMore>}</h3>{children}</section>;
}

/** Whether a «Know more» popover is open inside `root`. A dialog uses it on Escape: the key closes the popover, not
 * the dialog (Radix listens for Escape on the document in the capture phase, before the popover). */
export function knowMoreOpen(root: ParentNode | null | undefined): boolean {
  return !!root?.querySelector('.know-more-pop');
}

/** An Info button that opens a short text next to it; a second press, a press outside or Escape closes it. */
export function KnowMore({ label, children, align = 'start' }: { label: string; children: ReactNode; align?: 'start' | 'end' }) {
  const [open, setOpen] = useState(false);
  const [shift, setShift] = useState(0);
  const ref = useRef<HTMLSpanElement>(null);
  const pop = useRef<HTMLSpanElement>(null);
  const id = useId();
  // keep the popover inside the viewport (a button near the edge of a phone screen)
  useLayoutEffect(() => {
    if (!open || !pop.current) { setShift(0); return; }
    const r = pop.current.getBoundingClientRect(), edge = 12, right = window.innerWidth - edge;
    setShift(r.right > right ? right - r.right : r.left < edge ? edge - r.left : 0);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const outside = (e: Event) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    // Escape closes only the popover: a side sheet around it checks for an open popover (knowMoreOpen) and stays
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [open]);
  return <span className={`know-more is-${align}`} ref={ref}>
    <button type="button" className="know-more-button" aria-label={label} title={label} aria-expanded={open} aria-controls={id}
      onClick={e => { e.preventDefault(); e.stopPropagation(); setOpen(o => !o); }}><Info size={15} aria-hidden /></button>
    {open && <span id={id} ref={pop} role="note" className="know-more-pop" style={shift ? { transform: `translateX(${Math.round(shift)}px)` } : undefined} onClick={e => e.stopPropagation()}>{children}</span>}
  </span>;
}

export interface TimelineStep { date: string | null; title: string; detail?: ReactNode; tone?: 'good' | 'warn' | 'bad' | 'neutral' | 'future' }
export function Timeline({ steps }: { steps: TimelineStep[] }) {
  return <ol className="g-timeline">{steps.map((s, i) => <li key={i} className={`g-timeline-step is-${s.tone ?? 'neutral'}`}>
    <span className="g-timeline-dot" aria-hidden />
    <div><small>{s.date ? longDay(s.date) : 'Not yet'}</small><strong>{s.title}</strong>{s.detail && <div className="g-timeline-detail">{s.detail}</div>}</div>
  </li>)}</ol>;
}

export function Properties({ items }: { items: { label: string; value: ReactNode }[] }) {
  return <div className="detail-property-grid">{items.map(i => <div key={i.label}><span>{i.label}</span>{typeof i.value === 'string' ? <strong>{i.value}</strong> : i.value}</div>)}</div>;
}
