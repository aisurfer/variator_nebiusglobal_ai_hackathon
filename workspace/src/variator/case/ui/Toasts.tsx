// Change notices: what was done, the effect, the required / recommended / supporting changes, the risks that grow, and
// Undo. Each stays about 15 s, longer while the pointer is on the notice itself; at most three at once. They never
// cover a primary control (WP25 R2): they sit top right (the owner's rule), compact (the heads only) when the controls of
// the Answer or the priorities are under them, and bottom right only when even the heads would cover those controls.
// While a side sheet is open the notices never cover it either: bottom left where the page has room beside the sheet,
// else held (hidden, their time paused) until the sheet closes.
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { AlertTriangle, ArrowRightLeft, CheckCircle2, ListChecks, LoaderCircle, Undo2, Wrench, X, HeartHandshake } from 'lucide-react';
import type { ToastItem } from '../state';

const LIFE = 15_000;
/** The side sheet (.detail-panel, at most 650 px wide) plus a notice beside it (380 px) and the gaps. */
const ROOM = '(min-width: 1080px)';

/** Where the notices go: `corner` (the usual: bottom right, or top on a phone), `aside` (bottom left, beside an open
 *  side sheet), `held` (no room beside the sheet). */
export function noticePlace(sheetOpen: boolean, room: boolean): 'corner' | 'aside' | 'held' {
  return !sheetOpen ? 'corner' : room ? 'aside' : 'held';
}

/** The primary controls a notice must never cover: the Answer's (rerun, the presets that settle a close call), the
 *  priorities, the timeline's inspector (its values, ranges and dates; WP25 W2), and any control marked so. */
export const PRIMARY = '[data-primary-control], .cs-av .cs-card-tools, .cs-close-presets, .cs-presets, .cs-inspector';
export interface Box { top: number; bottom: number; left: number; right: number }
const meets = (a: Box, b: Box) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

export type Edge = { edge: 'top' | 'bottom'; compact: boolean } | 'held';

/** Where the stack goes: the usual edge, else the other one, else either in its compact form (the titles only), else
 *  held (hidden, its time paused) until the controls are out of the way. `stack`: its width, its height in full and
 *  compact, its right side; `inset`: the gap from the top (under the sticky bar) and from the bottom. `sizeFirst`: the
 *  usual edge in its compact form comes before the other edge (the notices keep to the top right, by the owner's rule). */
export function cornerEdge(usual: 'top' | 'bottom', stack: { width: number; height: number; compactHeight: number; right: number }, view: { height: number }, inset: { top: number; bottom: number }, controls: Box[], sizeFirst = false): Edge {
  const at = (edge: 'top' | 'bottom', h: number): Box => edge === 'top'
    ? { top: inset.top, bottom: inset.top + h, left: stack.right - stack.width, right: stack.right }
    : { top: view.height - inset.bottom - h, bottom: view.height - inset.bottom, left: stack.right - stack.width, right: stack.right };
  const other = usual === 'top' ? 'bottom' : 'top';
  const tries = sizeFirst ? [[usual, false], [usual, true], [other, false], [other, true]] as const : [[usual, false], [other, false], [usual, true], [other, true]] as const;
  for (const [edge, compact] of tries) {
    const box = at(edge, compact ? stack.compactHeight : stack.height);
    if (!controls.some(c => meets(box, c))) return { edge, compact };
  }
  return 'held';
}

/** A compact notice: its head only (the title and the fork), about this tall; the gap between notices. */
const COMPACT = 58, GAP = 10;

/** Where the notices go now (rechecked on scroll, resize and new notices). */
function useEdge(ref: React.RefObject<HTMLDivElement | null>, active: boolean, count: number): Edge {
  const phone = () => window.matchMedia('(max-width: 760px)').matches;
  const [edge, setEdge] = useState<Edge>({ edge: 'top', compact: false });
  const full = useRef(0);
  useLayoutEffect(() => {
    if (!active) return;
    let raf = 0;
    const same = (a: Edge, b: Edge) => a === b || (typeof a === 'object' && typeof b === 'object' && a.edge === b.edge && a.compact === b.compact);
    const put = (e: Edge) => setEdge(p => same(p, e) ? p : e);
    const check = () => {
      raf = 0;
      const el = ref.current;
      // the owner's rule: the notices sit in the upper right corner (the lower one hid content), on a phone too; over a
      // primary control they shrink to their heads, and step down only when even the heads would cover it
      const usual = 'top';
      // the tour's bottom sheet on a phone and the menus that send the notices down keep their own place
      if (!el || !count || document.body.classList.contains('vt-touring') || document.body.classList.contains('cs-confirming')) { put({ edge: usual, compact: false }); return; }
      const r = el.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
      // the full height is measured while the stack is full (a compact or held stack keeps the last one)
      if (!el.classList.contains('is-compact') && !el.classList.contains('is-held') && r.height > 0) full.current = r.height;
      const controls = [...document.querySelectorAll<HTMLElement>(PRIMARY)].map(x => x.getBoundingClientRect()).filter(b => b.width > 0 && b.bottom > 0 && b.top < vh && b.right > 0 && b.left < vw);
      put(cornerEdge(usual, { width: r.width, height: full.current || r.height, compactHeight: count * COMPACT + (count - 1) * GAP, right: r.right }, { height: vh }, phone() ? { top: 66, bottom: 12 } : { top: 76, bottom: 20 }, controls, true));
    };
    const soon = () => { if (!raf) raf = window.requestAnimationFrame(check); };
    check();
    window.addEventListener('scroll', soon, { passive: true });
    window.addEventListener('resize', soon);
    return () => { window.removeEventListener('scroll', soon); window.removeEventListener('resize', soon); if (raf) window.cancelAnimationFrame(raf); };
  }, [active, count]); // eslint-disable-line react-hooks/exhaustive-deps
  return edge;
}

/** Whether a side sheet is open (any .detail-panel in the document), and whether the page has room beside it. */
function useSheet(): { open: boolean; room: boolean } {
  const read = () => ({ open: !!document.querySelector('.detail-panel'), room: window.matchMedia(ROOM).matches });
  const [s, set] = useState(read);
  useEffect(() => {
    const upd = () => set(p => { const n = read(); return n.open === p.open && n.room === p.room ? p : n; });
    const mo = new MutationObserver(upd), mq = window.matchMedia(ROOM);
    mo.observe(document.body, { childList: true });
    mq.addEventListener('change', upd);
    upd();
    return () => { mo.disconnect(); mq.removeEventListener('change', upd); };
  }, []);
  return s;
}

export function ToastStack({ toasts, onDismiss }: { toasts: ToastItem[]; onDismiss: (id: number) => void }) {
  const sheet = useSheet(), place = noticePlace(sheet.open, sheet.room), ref = useRef<HTMLDivElement>(null);
  const edge = useEdge(ref, place === 'corner', toasts.length);
  // the owner's rule: the notices sit in the upper right corner (the lower one hid content), except over a primary control
  const at = place !== 'corner' ? place : edge === 'held' ? 'held' : edge.edge, compact = place === 'corner' && edge !== 'held' && edge.compact;
  return createPortal(<div ref={ref} className={`cs-toasts is-${at} ${compact ? 'is-compact' : ''}`} data-place={at} data-compact={compact || undefined} aria-live="polite" aria-relevant="additions" aria-hidden={at === 'held' || undefined}>
    {toasts.map(t => <Toast key={t.id} t={t} held={at === 'held'} onDismiss={() => onDismiss(t.id)} />)}
  </div>, document.body);
}

function Toast({ t, held, onDismiss }: { t: ToastItem; held: boolean; onDismiss: () => void }) {
  const [hover, setPaused] = useState(false), paused = hover || held;
  const left = useRef(LIFE), started = useRef(Date.now()), timer = useRef<number | undefined>(undefined), dismiss = useRef(onDismiss);
  dismiss.current = onDismiss;
  useEffect(() => {
    if (paused) { left.current -= Date.now() - started.current; window.clearTimeout(timer.current); return; }
    started.current = Date.now();
    timer.current = window.setTimeout(() => dismiss.current(), Math.max(800, left.current));
    return () => window.clearTimeout(timer.current);
  }, [paused]);
  // the seconds left, ticking on the Ack button (it stops while the notice is paused)
  const [secs, setSecs] = useState(Math.ceil(LIFE / 1000));
  useEffect(() => {
    const tick = () => setSecs(Math.max(0, Math.ceil((left.current - (paused ? 0 : Date.now() - started.current)) / 1000)));
    tick();
    if (paused) return;
    const id = window.setInterval(tick, 250);
    return () => window.clearInterval(id);
  }, [paused]);
  const r = t.report;
  const list = (title: string, Icon: typeof Wrench, xs: string[], cls = '') => xs.length ? <div className={`cs-toast-group ${cls}`}><p><Icon size={13} aria-hidden />{title}</p><ul>{xs.map((x, i) => <li key={i}>{x}</li>)}</ul></div> : null;
  return <div className={`cs-toast is-${r.tone} ${hover ? 'is-paused' : ''}`} role="status" onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)} onFocus={() => setPaused(true)} onBlur={() => setPaused(false)}>
    <div className="cs-toast-head">
      <span className="cs-toast-icon">{r.tone === 'bad' ? <AlertTriangle size={15} /> : <ArrowRightLeft size={15} />}</span>
      <div><strong>{t.title}</strong><small>{t.context}</small></div>
      <button type="button" className="cs-icon-btn" aria-label="Close the notice" onClick={onDismiss}><X size={15} /></button>
    </div>
    <div className="cs-toast-body">
      {list('Effect', CheckCircle2, r.effect.length ? r.effect : ['No measurable change (below 1 pp).'])}
      {t.planPending ? <div className="cs-toast-group"><p><ListChecks size={13} aria-hidden />Required</p><ul><li className="cs-toast-wait"><LoaderCircle size={12} className="spin" aria-hidden />Searching a plan…</li></ul></div> : list('Required', ListChecks, r.required)}
      {list('Recommended', Wrench, r.recommended)}
      {list('Supporting', HeartHandshake, r.supporting)}
      {list('Risks rising', AlertTriangle, r.risks, 'is-risk')}
    </div>
    <div className="cs-toast-foot">
      {t.undo ? <button type="button" className="cs-text-btn" onClick={() => { t.undo?.(); onDismiss(); }}><Undo2 size={14} />Undo</button> : <span />}
      {/* Acknowledge closes the notice now, as × does; it counts down the seconds to the automatic close (frozen while paused, so its size never changes) */}
      <button type="button" className="button cs-toast-ack" onClick={onDismiss} title={paused ? 'Kept while the pointer is here' : `Closes by itself in ${secs} s`}>Acknowledge<span className="cs-toast-secs" aria-hidden>{secs} s</span></button>
    </div>
    <i className="cs-toast-timer" style={{ animationDuration: `${LIFE}ms`, animationPlayState: paused ? 'paused' : 'running' }} aria-hidden />
  </div>;
}
