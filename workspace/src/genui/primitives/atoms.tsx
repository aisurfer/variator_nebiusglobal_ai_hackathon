// Atoms: the smallest pieces every primitive is built from.
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { ArrowDown, ArrowUp, Info, Minus } from 'lucide-react';
import type { MetricStatus } from '../contracts';
import { formatDelta, formatValue, shortDay, stamp, STATUS_LABEL, valueParts, type Direction, type Tone, type Unit } from './format';

/** A value in large type. `brief`: money as the brief tile writes it, so a sheet repeats the number of its tile. A %
 * or × stays on the number («12.3%», no space); a word unit is set smaller beside it. */
export function Value({ value, unit, size = 'lg', brief = false }: { value: number | null | undefined; unit: Unit; size?: 'xl' | 'lg' | 'md' | 'sm'; brief?: boolean }) {
  const { main, suffix } = valueParts(value, unit, { brief });
  const tight = suffix === '%' || suffix === '×';
  return <span className={`g-value g-value-${size}${main === '—' ? ' g-value-unknown' : ''}`}><span className="g-value-main">{tight ? main + suffix : main}</span>{suffix && !tight && <span className="g-value-suffix">{suffix}</span>}</span>;
}

/** Colour by direction (higher/lower is better), never by sign; unknown and context are neutral. */
export function DeltaBadge({ current, previous, unit, direction, label, quiet = false }: { current: number | null | undefined; previous: number | null | undefined; unit: Unit; direction: Direction; label?: string; quiet?: boolean }) {
  const delta = formatDelta(current, previous, unit, direction);
  const Icon = delta.diff === null || delta.diff === 0 ? Minus : delta.diff > 0 ? ArrowUp : ArrowDown;
  const meaning = delta.tone === 'good' ? 'better' : delta.tone === 'bad' ? 'worse' : '';
  return <span className={`g-delta g-tone-${delta.tone}${quiet ? ' g-delta-quiet' : ''}`}>
    {delta.diff !== null && <Icon size={12} strokeWidth={2.4} aria-hidden />}
    <span className="g-delta-text">{delta.text}</span>
    {label && <span className="g-delta-label">{label}</span>}
    {meaning && <span className="sr-only"> ({meaning})</span>}
  </span>;
}

/** Status always carries a word. "good" stays quiet: colour is for exceptions. */
export function StatusPill({ status, showQuiet = true }: { status: MetricStatus; showQuiet?: boolean }) {
  if (status === 'neutral') return null;
  if ((status === 'good' || status === 'unknown') && !showQuiet) return null;
  return <span className={`g-status g-status-${status}`}><i aria-hidden />{STATUS_LABEL[status]}</span>;
}

export function Tag({ tone = 'neutral', children }: { tone?: Tone | 'warn'; children: ReactNode }) {
  return <span className={`g-tag g-tone-${tone}`}>{children}</span>;
}

/** Pure-SVG trend without axes. Gaps where the value is unknown; the last known point is marked. */
export function Sparkline({ series, unit, label, target, height = 36, tone = 'neutral' }: { series: { date: string; value: number | null }[]; unit: Unit; label: string; target?: number | null; height?: number; tone?: 'neutral' | 'warn' | 'bad' }) {
  const [hover, setHover] = useState<number | null>(null);
  const width = 160, pad = 4;
  const values = series.map(p => p.value).filter((v): v is number => v !== null);
  if (values.length < 2) return <span className="g-spark-empty">{values.length ? 'One point · no trend yet' : 'No history'}</span>;
  const all = target != null ? [...values, target] : values;
  const min = Math.min(...all), max = Math.max(...all);
  const span = max - min || 1;
  const x = (i: number) => pad + i * (width - pad * 2) / Math.max(1, series.length - 1);
  const y = (v: number) => height - pad - (v - min) / span * (height - pad * 2);
  let d = '', pen = false;
  series.forEach((p, i) => { if (p.value === null) { pen = false; return; } d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`; pen = true; });
  const lastIndex = series.map(p => p.value).lastIndexOf(values.at(-1)!);
  const first = values[0], last = values.at(-1)!;
  const point = hover !== null ? series[hover] : null;
  return <span className={`g-spark g-spark-${tone}`}>
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={`${label}: ${formatValue(first, unit)} to ${formatValue(last, unit)} over ${series.length} points`}
      onMouseMove={e => { const r = e.currentTarget.getBoundingClientRect(); setHover(Math.max(0, Math.min(series.length - 1, Math.round((e.clientX - r.left) / r.width * (series.length - 1))))); }} onMouseLeave={() => setHover(null)}>
      {target != null && <line className="g-spark-target" x1={pad} x2={width - pad} y1={y(target)} y2={y(target)} vectorEffect="non-scaling-stroke" />}
      <path d={d} className="g-spark-line" vectorEffect="non-scaling-stroke" />
      {point?.value != null && <line className="g-spark-rule" x1={x(hover!)} x2={x(hover!)} y1={0} y2={height} vectorEffect="non-scaling-stroke" />}
    </svg>
    <i className="g-spark-dot" style={{ left: `${x(hover ?? lastIndex) / width * 100}%`, top: `${y((point?.value ?? last)) / height * 100}%`, visibility: hover !== null && point?.value == null ? 'hidden' : undefined }} aria-hidden />
    {point && <span className="g-spark-tip" aria-hidden>{shortDay(point.date)} · {formatValue(point.value, unit)}</span>}
  </span>;
}

/** 0..100 bar with an optional ghost marker (e.g. the comparison value). */
export function MiniBar({ value, previous, tone = 'neutral', label }: { value: number | null | undefined; previous?: number | null; tone?: 'neutral' | 'warn' | 'bad' | 'muted'; label: string }) {
  const clamp = (v: number) => Math.max(0, Math.min(100, v));
  return <span className={`g-minibar g-minibar-${tone}`} role="img" aria-label={value == null ? `${label}: unknown` : `${label}: ${Math.round(value)} of 100${previous != null ? `, was ${Math.round(previous)}` : ''}`}>
    {value != null && <i className="g-minibar-fill" style={{ width: `${clamp(value)}%` }} />}
    {previous != null && <i className="g-minibar-ghost" style={{ left: `${clamp(previous)}%` }} />}
  </span>;
}

export function FreshnessStamp({ computedAt, prefix = 'Computed' }: { computedAt: string | null | undefined; prefix?: string }) {
  const text = stamp(computedAt);
  return <span className="g-fresh">{text ? `${prefix} ${text}` : 'Not computed yet'}</span>;
}

/** Small disclosure for a definition. A real button: click, Enter or Space; Escape or outside click closes. */
export function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => { if (e instanceof KeyboardEvent ? e.key === 'Escape' : !ref.current?.contains(e.target as Node)) { if (e instanceof KeyboardEvent) e.stopPropagation(); setOpen(false); } };
    document.addEventListener('mousedown', close); document.addEventListener('keydown', close, true);
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', close, true); };
  }, [open]);
  return <span className="g-infotip" ref={ref}>
    <button type="button" className="g-icon-button" aria-label={`About ${label}`} aria-expanded={open} aria-controls={id} onClick={e => { e.stopPropagation(); setOpen(v => !v); }}><Info size={15} /></button>
    {open && <span className="g-infotip-pop" id={id} role="note">{children}</span>}
  </span>;
}
