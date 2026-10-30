// The guided tour: one region at a time in a spotlight (the rest dimmed, the page still scrollable and usable), and a
// step card that never covers its region. The card sits below, right, left or above the region, whichever has room,
// and flips as you scroll; at 760 px and less it is a bottom sheet. Esc closes it (a modal of the page closes first),
// the focus moves into the card and comes back where it was, and motion follows prefers-reduced-motion.
// The host page fills the texts from its live state, performs the actions («Show me») and resets the page.
// Its stylesheet (tour.css) is loaded by the browser entry (main.tsx): no module a unit test imports loads CSS.
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { ArrowLeft, ArrowRight, Check, Compass, Eye, Lightbulb, MousePointerClick, RotateCcw, Undo2 } from 'lucide-react';
import type { TourStepView, TourTarget } from './types';
import { placeCard, type Box, type Mode, type Side } from './placement';

export interface TourProps {
  /** The name of the tour for assistive technology («Guided tour of Best year»). */
  label: string;
  steps: TourStepView[];
  index: number;
  onIndex: (i: number) => void;
  /** Height of what is sticky at the top of the page: a region scrolls to just under it. */
  barOffset: number;
  onAction: (i: number) => void;
  actionDone: (i: number) => boolean;
  /** Values the visitor changed since the tour started: on the way out, the tour offers to reset them. */
  changes: number;
  onReset: () => void;
  onClose: (how: 'finish' | 'skip') => void;
  /** While one of these is on the page (a modal of the page), the tour steps aside and Esc is the modal's. */
  pauseSelector?: string;
  /** Where the focus goes back when the element focused before the tour is gone. */
  fallbackFocus?: () => HTMLElement | null;
  /** Pixels on the right that a wide card leaves free (the page's notices sit there during the tour). */
  reserveRight?: number;
  /** The label of the last step's button (default «Finish»), when finishing leads on (to the next tour). */
  finishLabel?: string;
}

type Where = 'base' | 'then' | 'see';
interface View { spot: Box | null; top: number; left: number; width: number | null; side: Side; mode: Mode; ready: boolean; paused: boolean }

const SHEET_MAX = 760, PAD = 6;
const reduced = () => !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const sheet = () => window.innerWidth <= SHEET_MAX;

/** The element of a target: `[data-tour=anchor]`, or the `[data-tour=focus]` inside it when there is one. */
export function findTarget(t: TourTarget | undefined): HTMLElement | null {
  if (!t) return null;
  const a = document.querySelector<HTMLElement>(`[data-tour="${t.anchor}"]`);
  if (!a) return null;
  // a focus inside a closed disclosure (or otherwise not rendered) gives way to the next one, then to the anchor
  for (const f of typeof t.focus === 'string' ? [t.focus] : t.focus ?? []) { const el = a.querySelector<HTMLElement>(`[data-tour="${f}"]`); if (el && el.getClientRects().length) return opened(el); }
  return a;
}
/** A closed disclosure marked `data-tour-open` opens when a step points at it (once: the reader may close it again). */
function opened(el: HTMLElement): HTMLElement {
  if (el instanceof HTMLDetailsElement && !el.open && el.dataset.tourOpen !== undefined && !el.dataset.tourOpened) { el.open = true; el.dataset.tourOpened = '1'; }
  return el;
}
function isFixed(el: HTMLElement) {
  for (let e: HTMLElement | null = el; e && e !== document.body; e = e.parentElement) if (getComputedStyle(e).position === 'fixed') return true;
  return false;
}
const same = (a: View, b: View) => a.side === b.side && a.mode === b.mode && a.width === b.width && a.ready === b.ready && a.paused === b.paused && Math.abs(a.top - b.top) < 0.5 && Math.abs(a.left - b.left) < 0.5
  && (a.spot === b.spot || (!!a.spot && !!b.spot && Math.abs(a.spot.top - b.spot.top) < 0.5 && Math.abs(a.spot.left - b.spot.left) < 0.5 && Math.abs(a.spot.width - b.spot.width) < 0.5 && Math.abs(a.spot.height - b.spot.height) < 0.5));

export function Tour({ label, steps, index, onIndex, barOffset, onAction, actionDone, changes, onReset, onClose, pauseSelector = '.cs-modal-overlay, .detail-overlay', fallbackFocus, reserveRight = 0, finishLabel }: TourProps) {
  const step = steps[Math.min(index, steps.length - 1)], last = index >= steps.length - 1;
  const [where, setWhere] = useState<Where>('base');
  const [acted, setActed] = useState(false);
  const [ending, setEnding] = useState<null | 'finish' | 'skip'>(null);
  const [view, setView] = useState<View>({ spot: null, top: 0, left: 0, width: null, side: 'corner', mode: 'narrow', ready: false, paused: false });
  const card = useRef<HTMLDivElement>(null), before = useRef<Element | null>(null);
  // the height of each layout: the one on screen measured every frame, the other as it was last seen
  const heights = useRef<Record<Mode, number>>({ narrow: 400, wide: 200 }), modeRef = useRef<Mode>('narrow');
  const lastPlace = useRef<{ side: Side; mode: Mode } | null>(null);
  const titleId = useId(), bodyId = useId();
  const target: TourTarget | undefined = ending ? undefined : where === 'see' ? step.see : where === 'then' ? step.action?.then ?? step : step;
  const targetRef = useRef(target);
  targetRef.current = target;

  // the focus: into the card on open, back where it was on close
  useEffect(() => {
    before.current = document.activeElement;
    document.body.classList.add('vt-touring');
    return () => {
      document.body.classList.remove('vt-touring');
      document.documentElement.style.removeProperty('--vt-inset-bottom');
      const el = before.current as HTMLElement | null;
      const back = el?.isConnected && el !== document.body ? el : fallbackFocus?.();
      back?.focus?.({ preventScroll: true });
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setWhere('base'); setActed(false); lastPlace.current = null; }, [index]);
  useEffect(() => {
    const el = card.current;
    if (el && !el.contains(document.activeElement)) el.focus({ preventScroll: true });
  }, [index, ending]);

  // scroll the region of the step (or of its second stop) to just under the sticky bar, once it is on the page
  const key = `${index}|${where}|${ending ?? ''}`;
  useEffect(() => {
    lastPlace.current = null; // a new region: the card picks its side again
    if (ending) return;
    let raf = 0, tries = 0;
    const go = () => {
      const el = findTarget(targetRef.current);
      if (!el) { if (tries++ < 900) raf = requestAnimationFrame(go); return; }
      if (isFixed(el)) return;
      const r = el.getBoundingClientRect();
      window.scrollTo({ top: Math.max(0, r.top + window.scrollY - barOffset - 12), behavior: reduced() ? 'auto' : 'smooth' });
    };
    raf = requestAnimationFrame(go);
    return () => cancelAnimationFrame(raf);
  }, [key, barOffset]); // eslint-disable-line react-hooks/exhaustive-deps

  // follow the region every frame: it moves when you scroll, when a section finishes computing, when a modal opens
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const paused = !!document.querySelector(pauseSelector);
      const el = findTarget(targetRef.current), c = card.current;
      const vw = document.documentElement.clientWidth, vh = window.innerHeight;
      let spot: Box | null = null;
      if (el) { const r = el.getBoundingClientRect(); if (r.width || r.height) spot = { top: r.top - PAD, left: r.left - PAD, width: r.width + PAD * 2, height: r.height + PAD * 2 }; }
      if (c && !sheet()) heights.current[modeRef.current] = c.offsetHeight;
      let next: View;
      if (sheet()) {
        next = { spot, top: 0, left: 0, width: null, side: 'sheet', mode: 'narrow', ready: !!c, paused };
        document.documentElement.style.setProperty('--vt-inset-bottom', `${c?.offsetHeight ?? 0}px`);
      } else {
        document.documentElement.style.removeProperty('--vt-inset-bottom');
        const p = placeCard(spot, heights.current, vw, vh, barOffset, reserveRight, lastPlace.current ?? undefined);
        lastPlace.current = p.side === 'corner' ? null : { side: p.side, mode: p.mode };
        next = { spot, ...p, ready: !!c && p.mode === modeRef.current, paused };
      }
      modeRef.current = next.mode;
      setView(v => same(v, next) ? v : next);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [barOffset, pauseSelector, reserveRight]);

  const close = useCallback((how: 'finish' | 'skip') => { if (changes > 0) setEnding(how); else onClose(how); }, [changes, onClose]);
  const next = () => last ? close('finish') : onIndex(index + 1);
  const back = () => { if (index > 0) onIndex(index - 1); };

  // Esc: a modal or dialog of the page closes first; the tour closes on the next Esc. Arrows step inside the card.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (document.querySelector(pauseSelector) || document.querySelector('[role="dialog"][data-state="open"]:not([data-tour-ui])')) return;
        if (ending) onClose(ending); else close('skip');
        return;
      }
      const inCard = !!card.current?.contains(document.activeElement);
      if (!inCard || ending || (e.target as HTMLElement | null)?.closest?.('input,textarea,select')) return;
      if (e.key === 'ArrowRight') { e.preventDefault(); next(); } else if (e.key === 'ArrowLeft') { e.preventDefault(); back(); }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  });

  const done = !!step.action && actionDone(index);
  const show = () => { onAction(index); setActed(true); if (step.action?.then) setWhere('then'); };
  const see = () => setWhere(w => w === 'see' ? (acted && step.action?.then ? 'then' : 'base') : 'see');
  const style = view.side === 'sheet' ? undefined : { top: view.top, left: view.left, ...(view.width ? { width: view.width } : {}) };
  const cls = `vt-card is-${view.side} is-${view.mode} ${view.ready ? 'is-ready' : ''} ${view.paused ? 'is-paused' : ''}`;
  return createPortal(<>
    {view.spot && !ending && <div className={`vt-spot ${view.paused ? 'is-paused' : ''}`} aria-hidden data-tour-ui style={{ top: view.spot.top, left: view.spot.left, width: view.spot.width, height: view.spot.height }} />}
    {(!view.spot || ending) && <div className={`vt-dim ${view.paused ? 'is-paused' : ''}`} aria-hidden data-tour-ui />}
    <div ref={card} className={cls} style={style} role="dialog" aria-modal="false" aria-label={ending ? `${label}: your changes` : undefined} aria-labelledby={ending ? undefined : titleId} aria-describedby={ending ? undefined : bodyId}
      tabIndex={-1} data-tour-ui data-testid="tour-card" data-step={ending ? 'end' : step.id}>
      {ending ? <EndCard changes={changes} onReset={() => { onReset(); onClose(ending); }} onKeep={() => onClose(ending)} /> : <>
        <div className="vt-main">
        <div className="vt-head">
          <span className="vt-count"><Compass size={13} aria-hidden />Step {index + 1} of {steps.length}</span>
          <span className="vt-dots" aria-hidden>{steps.map((s, i) => <i key={s.id} className={i === index ? 'is-on' : i < index ? 'is-done' : ''} />)}</span>
        </div>
        <h2 id={titleId} className="vt-title">{step.title}</h2>
        <p id={bodyId} className="vt-body">{step.body}</p>
        {step.tryText && <p className="vt-try"><Lightbulb size={14} aria-hidden /><span><b>Try:</b> {step.tryText}</span></p>}
        </div>
        <div className="vt-side">
        {(step.action || step.see) && <div className="vt-acts">
          {step.action && <button type="button" className={`button vt-show ${done ? 'is-done' : ''}`} onClick={show} disabled={done} title={step.action.label} data-testid="tour-show">
            {done ? <><Check size={15} aria-hidden />Done</> : <><MousePointerClick size={15} aria-hidden />Show me</>}</button>}
          {step.see && <button type="button" className={`button vt-see ${where === 'see' ? 'is-on' : ''}`} onClick={see} aria-pressed={where === 'see'} data-testid="tour-see">
            {where === 'see' ? <><Undo2 size={15} aria-hidden />Back to {step.title.toLowerCase()}</> : <><Eye size={15} aria-hidden />{step.see.label}</>}</button>}
        </div>}
        {step.extra}
        <div className="vt-foot">
          <button type="button" className="cs-text-btn vt-skip" onClick={() => close('skip')}>Skip</button>
          <span className="vt-grow" />
          {index > 0 && <button type="button" className="button" onClick={back}><ArrowLeft size={14} aria-hidden />Back</button>}
          <button type="button" className="button button-primary vt-next" onClick={next}>{last ? finishLabel ?? 'Finish' : <>Next<ArrowRight size={14} aria-hidden /></>}</button>
        </div>
        </div>
      </>}
    </div>
    <div className="sr-only" aria-live="polite">{ending ? 'The tour is over.' : `Step ${index + 1} of ${steps.length}: ${step.title}`}</div>
  </>, document.body);
}

function EndCard({ changes, onReset, onKeep }: { changes: number; onReset: () => void; onKeep: () => void }) {
  return <>
    <div className="vt-head"><span className="vt-count"><Compass size={13} aria-hidden />End of the tour</span></div>
    <h2 className="vt-title">Keep what you changed?</h2>
    <p className="vt-body">You changed {changes} value{changes === 1 ? '' : 's'} of this case during the tour. They stay in this browser until you reset them.</p>
    <div className="vt-foot">
      <button type="button" className="button" onClick={onReset}><RotateCcw size={14} aria-hidden />Back to the defaults</button>
      <span className="vt-grow" />
      <button type="button" className="button button-primary" onClick={onKeep}>Keep my changes</button>
    </div>
  </>;
}

/** «Take the tour»: calm, the secondary button style with a compass. */
export function TourButton({ onStart }: { onStart: () => void }) {
  return <button type="button" className="button vt-start" onClick={onStart} data-tour="tour-button"><Compass size={15} aria-hidden />Take the tour</button>;
}

/** The first visit offers the tour in a small card; the answer is remembered by the host. */
export function TourOffer({ minutes, onStart, onLater, children }: { minutes: number; onStart: () => void; onLater: () => void; children?: ReactNode }) {
  return createPortal(<div className="vt-offer" role="dialog" aria-label="Guided tour" data-testid="tour-offer" data-tour-ui>
    <span className="vt-offer-icon" aria-hidden><Compass size={18} /></span>
    <div className="vt-offer-text"><strong>New here?</strong><span>A {minutes}-minute tour of this case{children}</span></div>
    <div className="vt-offer-acts">
      <button type="button" className="button" onClick={onLater}>Not now</button>
      <button type="button" className="button button-primary" onClick={onStart}>Start</button>
    </div>
  </div>, document.body);
}
