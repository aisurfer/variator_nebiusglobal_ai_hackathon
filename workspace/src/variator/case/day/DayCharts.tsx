// WP24: the day charts of an example: up to four metrics as small multiples on one time axis, the event days as dots
// on the first chart, and the selected day as a vertical marker on all of them. A click (a tap) on a chart selects the
// day under it; on the focused charts Enter or Space selects the day under the pointer (else the first day with an
// event), the arrow keys move the selected day (Shift: a week), Home and End go to the ends, Esc clears it.
// The day of a click comes from its x on the plot (one band per day on every chart), not from the chart's hover
// state, which lags a fast click or a tap by a frame.
import { useMemo, useRef, type KeyboardEvent, type MouseEvent } from 'react';
import { Bar, CartesianGrid, ComposedChart, Line, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { bandIndex, dayTones, dayValue, longDate, shortDate, startDay, stepDay, PLOT, type DayExampleData, type DayMetric } from './model';

type Row = { date: string } & Record<string, number | string>;
interface Pointer { activeLabel?: string | number; activeTooltipIndex?: number | string | null }

const TONE: Record<string, string> = { bad: '#c0604f', good: '#5f9c84', event: '#d9558f' };
const INK = '#263830';

export function DayCharts({ ex, metrics, selected, onSelect }: { ex: DayExampleData; metrics: DayMetric[]; selected: string | null; onSelect: (date: string | null) => void }) {
  const hover = useRef<string | null>(null);
  const rows = useMemo<Row[]>(() => ex.points.map(p => ({ ...p.values, date: p.date })), [ex]);
  const tones = useMemo(() => dayTones(ex), [ex]);
  const ticks = useMemo(() => tickDates(rows.map(r => r.date)), [rows]);
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const wrap = (e.target as Element).closest('.cs-day-chart')?.querySelector('.recharts-wrapper');
    if (!wrap) return;
    const i = bandIndex(e.clientX, wrap.getBoundingClientRect(), rows.length);
    if (i != null) onSelect(rows[i].date);
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 5 : 1;
    let next: string | null | undefined;
    if (e.key === 'Enter' || e.key === ' ') next = startDay(ex, hover.current, selected);
    else if (e.key === 'ArrowRight') next = stepDay(ex.points, selected, step);
    else if (e.key === 'ArrowLeft') next = stepDay(ex.points, selected, -step);
    else if (e.key === 'Home') next = ex.points[0]?.date ?? null;
    else if (e.key === 'End') next = ex.points[ex.points.length - 1]?.date ?? null;
    else if (e.key === 'Escape' && selected) next = null;
    if (next === undefined) return;
    e.preventDefault(); e.stopPropagation();
    onSelect(next);
  };
  const hoverOf = (s: Pointer) => {
    if (typeof s.activeLabel === 'string' && s.activeLabel) hover.current = s.activeLabel;
    else { const i = Number(s.activeTooltipIndex); if (Number.isInteger(i) && rows[i]) hover.current = rows[i].date; }
  };
  return <div className="cs-daycharts" tabIndex={0} role="group" data-testid="day-chart-area" data-selected={selected ?? ''}
    aria-label={`Day by day, ${ex.label.toLowerCase()} example: click a day, or press Enter, to see what happened; arrow keys move the day, Escape clears it`}
    onKeyDown={onKey} onClick={onClick} onMouseLeave={() => { hover.current = null; }}>
    {metrics.map((m, k) => <DayChart key={`${ex.id}-${m.id}`} rows={rows} m={m} all={metrics} first={k === 0} last={k === metrics.length - 1} ticks={ticks} tones={k === 0 ? tones : null}
      selected={selected} onHover={hoverOf} />)}
    <span className="cs-sr" aria-live="polite">{selected ? `Selected ${longDate(selected)}` : ''}</span>
  </div>;
}

/** Ticks on days of the series: the first, about every month (two on a long series), the last. */
function tickDates(dates: string[]): string[] {
  if (dates.length < 2) return dates;
  const every = dates.length > 150 ? 42 : 21, out = [dates[0]];
  for (let i = every; i < dates.length - every / 3; i += every) out.push(dates[i]);
  out.push(dates[dates.length - 1]);
  return out;
}

/** One tooltip for all the charts (on the first): the day and the values of the charted metrics. */
function DayTip({ active, label, rows, all }: { active?: boolean; label?: string | number; rows: Row[]; all: DayMetric[] }) {
  const r = active && typeof label === 'string' ? rows.find(x => x.date === label) : undefined;
  if (!r) return null;
  return <div className="cs-daytip"><strong>{longDate(r.date)}</strong>
    {all.map(m => <span key={m.id}><em>{m.label}</em>{dayValue(m, Number(r[m.id]))}</span>)}
    <small>Click for the day</small></div>;
}

function DayChart({ rows, m, all, first, last, ticks, tones, selected, onHover }: {
  rows: Row[]; m: DayMetric; all: DayMetric[]; first: boolean; last: boolean; ticks: string[]; tones: Map<string, string> | null; selected: string | null;
  onHover: (s: Pointer) => void;
}) {
  const vals = rows.map(r => Number(r[m.id])).filter(Number.isFinite);
  if (!vals.length) return null;
  const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.1;
  const f = (v: number) => dayValue(m, v, { axis: true });
  const byDate = new Map(rows.map(r => [r.date, r]));
  return <div className="cs-day-chart" data-metric={m.id}>
    <span className="cs-day-label">{m.label}</span>
    <ResponsiveContainer width="100%" height={last ? 104 : 86}>
      <ComposedChart data={rows} margin={{ top: 6, right: PLOT.right, left: 0, bottom: 0 }} syncId="cs-day" accessibilityLayer={false} onMouseMove={s => onHover(s as Pointer)}>
        <CartesianGrid vertical={false} stroke="#eef1f2" />
        <XAxis dataKey="date" scale="band" hide={!last} axisLine={false} tickLine={false} tick={{ fill: '#76828a', fontSize: 11 }} ticks={ticks} interval="preserveStartEnd" minTickGap={14} tickFormatter={d => shortDate(String(d))} />
        <YAxis domain={m.bars ? [0, Math.max(2, hi)] : [lo - pad || lo - 1, hi + pad || hi + 1]} axisLine={false} tickLine={false} tick={{ fill: '#76828a', fontSize: 10 }} tickFormatter={f} width={PLOT.yAxis} tickCount={3} allowDecimals={!m.bars} />
        <Tooltip cursor={{ stroke: '#9aa6ad', strokeWidth: 1 }} isAnimationActive={false} allowEscapeViewBox={{ x: false, y: true }}
          content={first ? (p: { active?: boolean; label?: string | number }) => <DayTip active={p.active} label={p.label} rows={rows} all={all} /> : () => null} />
        {m.bars ? <Bar dataKey={m.id} fill="#9aa7ad" radius={[2, 2, 0, 0]} isAnimationActive={false} />
          : <Line type="monotone" dataKey={m.id} stroke="#3e4c53" strokeWidth={first ? 2 : 1.5} dot={false} isAnimationActive={false} />}
        {selected && <ReferenceLine x={selected} stroke={INK} strokeWidth={1.5} ifOverflow="extendDomain" />}
        {tones && [...tones].map(([d, tone]) => {
          const y = Number(byDate.get(d)?.[m.id]);
          return Number.isFinite(y) ? <ReferenceDot key={d} x={d} y={y} r={d === selected ? 6.5 : 4} fill={TONE[tone] ?? TONE.event} stroke="#fff" strokeWidth={d === selected ? 2.5 : 1.5} /> : null;
        })}
      </ComposedChart>
    </ResponsiveContainer>
  </div>;
}
