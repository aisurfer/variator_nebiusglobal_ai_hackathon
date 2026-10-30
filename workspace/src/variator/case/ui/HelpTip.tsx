// The «?» of the case page, and its two relatives: the «Know more» info icon (lucide Info) and a compact badge with a
// tooltip (a warning such as «Preview data» or «Not saved»). Hover (or keyboard focus) shows a short description after
// a short pause; a click pins the full text as a modal; Esc, the close button or a click outside closes it. Texts come
// from the case config (`help`); secondary lines of a section (its intro, how to read a chart) come as `more`.
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import * as Dialog from '@radix-ui/react-dialog';
import { CircleHelp, Info, X } from 'lucide-react';
import type { HelpEntry } from '../model/types';

const DELAY = 700;

export interface HelpTipProps {
  entry: HelpEntry | undefined;
  /** The name of the thing it explains (aria label); default the entry's title. */
  label?: string;
  size?: number;
  /** «?» (how to read it), «Know more» (an info icon), or a badge (children are the badge). */
  variant?: 'help' | 'info' | 'badge';
  /** Extra lines of the full text (how to read a chart, what a mark means), after the entry's own text. */
  more?: (string | false | null | undefined)[];
  children?: ReactNode;
  className?: string;
}

export function HelpTip({ entry, label, size = 15, variant = 'help', more, children, className }: HelpTipProps) {
  const [hover, setHover] = useState(false), [pinned, setPinned] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number; above: boolean } | null>(null);
  const btn = useRef<HTMLButtonElement>(null), pop = useRef<HTMLDivElement>(null), timer = useRef<number | undefined>(undefined);
  const id = useId();
  const show = () => { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => setHover(true), variant === 'badge' ? 250 : DELAY); };
  const hide = () => { window.clearTimeout(timer.current); timer.current = window.setTimeout(() => setHover(false), 120); };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (!hover) return;
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setHover(false); };
    const scroll = () => setHover(false);
    document.addEventListener('keydown', esc); window.addEventListener('scroll', scroll, { passive: true });
    return () => { document.removeEventListener('keydown', esc); window.removeEventListener('scroll', scroll); };
  }, [hover]);
  useLayoutEffect(() => {
    if (!hover || !btn.current) { setPos(null); return; }
    const r = btn.current.getBoundingClientRect(), w = Math.min(320, window.innerWidth - 24), h = pop.current?.offsetHeight ?? 120;
    const above = r.bottom + h + 12 > window.innerHeight && r.top > h + 12;
    setPos({ top: above ? r.top - h - 8 : r.bottom + 8, left: Math.max(12, Math.min(window.innerWidth - w - 12, r.left + r.width / 2 - w / 2)), above });
  }, [hover]);
  if (!entry) return children ? <span className={className}>{children}</span> : null;
  const name = label ?? entry.title;
  const lines = (more ?? []).filter((x): x is string => !!x);
  const full = entry.full && entry.full.trim() !== entry.short.trim() ? entry.full : '';
  const aria = variant === 'info' ? `Know more: ${name}` : variant === 'badge' ? `${name}: more` : `About ${name}`;
  return <>
    <button ref={btn} type="button" className={variant === 'badge' ? `cs-tip-badge ${className ?? ''}` : `cs-help ${variant === 'info' ? 'is-info' : ''} ${className ?? ''}`} aria-label={variant === 'badge' ? undefined : aria} aria-describedby={hover ? id : undefined}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}
      onClick={e => { e.stopPropagation(); window.clearTimeout(timer.current); setHover(false); setPinned(true); }}>
      {variant === 'badge' ? children : variant === 'info' ? <Info size={size} strokeWidth={1.8} aria-hidden /> : <CircleHelp size={size} strokeWidth={1.8} aria-hidden />}
    </button>
    {hover && !pinned && createPortal(<div ref={pop} id={id} role="tooltip" className={`cs-pop ${pos ? 'is-placed' : ''}`} style={pos ? { top: pos.top, left: pos.left } : { top: -9999, left: -9999 }}
      onMouseEnter={() => window.clearTimeout(timer.current)} onMouseLeave={hide}>
      <strong>{entry.title}</strong><span>{entry.short}</span>{(full || lines.length > 0) && <em>Click for more</em>}
    </div>, document.body)}
    <Dialog.Root open={pinned} onOpenChange={setPinned}>
      <Dialog.Portal>
        <Dialog.Overlay className="cs-modal-overlay" />
        <Dialog.Content className="cs-modal" aria-describedby={`${id}-full`} onCloseAutoFocus={e => { e.preventDefault(); btn.current?.focus({ preventScroll: true }); }}>
          <div className="cs-modal-head"><span className="cs-eyebrow">{variant === 'help' ? 'How to read it' : 'Know more'}</span><Dialog.Close className="cs-icon-btn" aria-label="Close"><X size={17} /></Dialog.Close></div>
          <Dialog.Title className="cs-modal-title">{entry.title}</Dialog.Title>
          <p className="cs-modal-lead">{entry.short}</p>
          <div id={`${id}-full`} className="cs-modal-body">
            {full && <p>{full}</p>}
            {lines.length > 0 && <ul className="cs-modal-more">{lines.map(x => <li key={x}>{x}</li>)}</ul>}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </>;
}

/** A compact badge (a warning or a status) whose explanation sits in a tooltip and a modal, not in a paragraph. */
export function TipBadge({ icon, text, title, tip, more, tone = 'neutral', testId }: { icon?: ReactNode; text: string; title: string; tip: string; more?: string[]; tone?: 'neutral' | 'warn' | 'preview'; testId?: string }) {
  return <span className="cs-tip-wrap" data-testid={testId}>
    <HelpTip variant="badge" className={`cs-badge is-${tone}`} entry={{ title, short: tip, full: '' }} more={more} label={text}>{icon}{text}</HelpTip>
  </span>;
}
