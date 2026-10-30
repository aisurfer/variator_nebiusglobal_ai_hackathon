// WP24: «Metrics»: a compact menu of checkboxes over the day metrics the data provides; up to four are charted. The
// arrow keys move in the menu, Space or Enter turns a metric on or off, Esc closes it; «Default» goes back to cash,
// ARR, load and incidents (the choice itself is kept per viewer by the section).
import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Check, ChevronDown, SlidersHorizontal } from 'lucide-react';
import { MAX_METRICS, type DayMetric } from './model';

export function MetricPicker({ metrics, chosen, isDefault, onToggle, onReset }: { metrics: DayMetric[]; chosen: string[]; isDefault: boolean; onToggle: (id: string) => void; onReset: () => void }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null), btn = useRef<HTMLButtonElement>(null), menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', away);
    menu.current?.querySelector<HTMLElement>('[role=menuitemcheckbox]')?.focus();
    return () => document.removeEventListener('pointerdown', away);
  }, [open]);
  const close = () => { setOpen(false); btn.current?.focus(); };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const items = [...(menu.current?.querySelectorAll<HTMLElement>('[role=menuitemcheckbox], .cs-mpick-reset') ?? [])];
    const i = items.indexOf(document.activeElement as HTMLElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
    else if (e.key === 'Tab') setOpen(false);
  };
  const full = chosen.length >= MAX_METRICS;
  return <div ref={wrap} className="cs-mpick">
    <button ref={btn} type="button" className="button cs-mpick-btn" data-testid="day-metrics" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(o => !o)}>
      <SlidersHorizontal size={14} aria-hidden />Metrics<span className="cs-count">{chosen.length}</span><ChevronDown size={14} aria-hidden />
    </button>
    {open && <div ref={menu} className="cs-mpick-menu" role="menu" aria-label="Day metrics" data-testid="day-metrics-menu" onKeyDown={onKey}>
      <span className="cs-mpick-head">Up to {MAX_METRICS} on the charts</span>
      {[
        // each group names its source; our own code or config is named by meaning, never by file (lessons-pages.md)
        { head: 'The run, day by day', source: 'Source: the company simulation, as per project configuration', xs: metrics.filter(m => !m.catalogue) },
        { head: 'CEO brief, on this future’s records', source: 'Source: the VARIATOR metric catalogue; each metric links its public definition', xs: metrics.filter(m => m.catalogue) }].filter(g => g.xs.length).map(g => <div key={g.head} role="group" aria-label={g.head} className="cs-mpick-group">
        {metrics.some(m => m.catalogue) && <span className="cs-mpick-sub">{g.head}</span>}
        <span className="cs-mpick-source">{g.source}</span>
        {g.xs.map(m => {
          const on = chosen.includes(m.id), off = !on && full, last = on && chosen.length === 1;
          return <button key={m.id} type="button" role="menuitemcheckbox" aria-checked={on} aria-disabled={off || last} data-metric={m.id} className={`cs-mpick-item ${on ? 'is-on' : ''}`}
            title={off ? `Up to ${MAX_METRICS}: turn one off first` : `${m.description ? `${m.description} ` : ''}(${m.catalogue ? 'the VARIATOR metric catalogue' : 'the company simulation, as per project configuration'})`} onClick={() => { if (!off && !last) onToggle(m.id); }}>
            <span className="cs-mpick-box" aria-hidden>{on && <Check size={12} strokeWidth={2.5} />}</span>{m.label}
          </button>;
        })}
      </div>)}
      <button type="button" className="cs-text-btn cs-mpick-reset" disabled={isDefault} onClick={() => { onReset(); close(); }}>Default</button>
    </div>}
  </div>;
}
