// A month track: rows of markers over the months 0..H, drawn 1:1 in SVG. Used by the timeline of interventions (step
// 1) and by the plan timeline (month level). Drag a marker with the mouse (a dashed ghost keeps its old place; Esc
// cancels) or focus it and press ← → (Shift: a whole month). Bands, work bars and finish-to-start arrows sit below.
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react';

export interface TrackRow { id: string; label: ReactNode; kind?: 'group' | 'lane' | 'row'; muted?: boolean; height?: number }
export interface TrackMarker {
  id: string; row: string; month: number; shape: 'circle' | 'diamond' | 'square';
  color: string; draggable?: boolean; ghost?: boolean; selected?: boolean; dim?: boolean;
  /** Short text right of the marker (or left with textLeft). */
  text?: string; textLeft?: boolean; textAt?: number;
  title: string; ring?: string;
}
export interface TrackBand { row: string; from: number; to: number; mid?: number; color: string; text?: string; title: string; kind?: 'band' | 'range' | 'work' }
export interface TrackArrow { fromRow: string; from: number; toRow: string; to: number; kind: 'dep' | 'ev'; title: string; hot?: boolean }

const ROW = 34, LANE = 26, GROUP = 34, TOP = 30, BOTTOM = 22, PAD_R = 26, PAD_L = 14;
const snap = (m: number, h: number) => Math.min(h, Math.max(0, Math.round(m * 2) / 2));

export function MonthTrack({ rows, markers, bands = [], arrows = [], horizon, monthLabel, onMove, onSelect, labelWidth = 190, ariaLabel, minWidth = 520 }: {
  rows: TrackRow[]; markers: TrackMarker[]; bands?: TrackBand[]; arrows?: TrackArrow[]; horizon: number; monthLabel: (m: number) => string;
  onMove?: (id: string, month: number, from: number) => void; onSelect?: (id: string) => void; labelWidth?: number; ariaLabel: string; minWidth?: number;
}) {
  const wrap = useRef<HTMLDivElement>(null), svg = useRef<SVGSVGElement>(null);
  const [W, setW] = useState(800);
  const [drag, setDrag] = useState<{ id: string; from: number; now: number } | null>(null);
  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(Math.max(minWidth, Math.round(el.clientWidth))));
    ro.observe(el);
    setW(Math.max(minWidth, Math.round(el.clientWidth)));
    return () => ro.disconnect();
  }, [minWidth]);
  useEffect(() => {
    if (!drag) return;
    const esc = (e: globalThis.KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setDrag(null); } };
    document.addEventListener('keydown', esc, true);
    return () => document.removeEventListener('keydown', esc, true);
  }, [drag]);

  const heights = rows.map(r => r.height ?? (r.kind === 'lane' ? LANE : r.kind === 'group' ? GROUP : ROW));
  const tops: number[] = [];
  heights.reduce((y, h) => { tops.push(y); return y + h; }, TOP);
  const H = TOP + heights.reduce((a, b) => a + b, 0) + BOTTOM;
  const yOf = (row: string) => { const i = rows.findIndex(r => r.id === row); return i < 0 ? -100 : tops[i] + heights[i] / 2 + 0.5; };
  const x = (m: number) => Math.round(PAD_L + Math.min(Math.max(m, 0), horizon) / horizon * (W - PAD_L - PAD_R)) + 0.5;
  const monthAt = (clientX: number) => { const r = svg.current!.getBoundingClientRect(); return (clientX - r.left - PAD_L) / (W - PAD_L - PAD_R) * horizon; };

  const down = (e: PointerEvent, m: TrackMarker) => {
    onSelect?.(m.id);
    if (!m.draggable || e.button !== 0) return;
    e.preventDefault();
    try { (e.currentTarget as Element).setPointerCapture?.(e.pointerId); } catch { /* a pointer that is gone */ }
    setDrag({ id: m.id, from: m.month, now: m.month });
  };
  const move = (e: PointerEvent) => { if (drag) setDrag({ ...drag, now: snap(monthAt(e.clientX), horizon) }); };
  const up = () => { if (!drag) return; const d = drag; setDrag(null); if (d.now !== d.from) onMove?.(d.id, d.now, d.from); };
  const key = (e: KeyboardEvent, m: TrackMarker) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect?.(m.id); return; }
    const d = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 0.5 : e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? -0.5 : 0;
    if (!d || !m.draggable) return;
    e.preventDefault();
    const to = snap(m.month + (e.shiftKey ? 2 * d : d), horizon);
    if (to !== m.month) onMove?.(m.id, to, m.month);
  };
  const shape = (m: TrackMarker, cx: number, cy: number, ghost = false) => {
    const cls = `cs-tk-mark ${ghost ? 'is-ghost' : ''} ${m.ghost ? 'is-unplaced' : ''}`;
    if (m.shape === 'diamond') return <path className={cls} d={`M${cx},${cy - 8}L${cx + 8},${cy}L${cx},${cy + 8}L${cx - 8},${cy}Z`} style={ghost ? undefined : { fill: m.ghost ? '#fff' : m.color, stroke: m.ring ?? m.color }} />;
    if (m.shape === 'square') return <rect className={cls} x={cx - 7} y={cy - 7} width={14} height={14} rx={3} style={{ fill: '#5d6b73', stroke: m.ring ?? m.color }} />;
    return <circle className={cls} cx={cx} cy={cy} r={7} style={ghost ? undefined : { fill: m.ghost ? '#fff' : m.color, stroke: m.ring ?? m.color }} />;
  };
  const ticks = Array.from({ length: horizon + 1 }, (_, m) => m);
  // a long horizon (24 months) on a narrow plot: month names every 2nd or 3rd month, numbers where they fit
  const per = (W - PAD_L - PAD_R) / horizon;
  const nameEvery = [1, 2, 3, 4, 6, 12].find(k => k * per >= 34) ?? 12, numEvery = [1, 2, 3, 6].find(k => k * per >= 16) ?? 6;
  const major = horizon > 12 && per < 30 ? 6 : 3;
  const bandWidth = (b: TrackBand) => Math.max(x(b.to), x(b.from) + 2) - x(b.from);
  // the text of a band too narrow to hold it goes after the marker text of its row
  const narrow = new Map(bands.filter(b => b.kind === 'range' && b.text && bandWidth(b) < 70).map(b => [b.row, b.text!]));
  return <div className="cs-track" style={{ gridTemplateColumns: `${labelWidth}px minmax(0,1fr)` }}>
    <div className="cs-track-labels" style={{ height: H }}>
      <div className="cs-track-scale" style={{ height: TOP }}>Month</div>
      {rows.map((r, i) => <div key={r.id} className={`cs-track-label ${r.kind === 'lane' ? 'is-lane' : r.kind === 'group' ? 'is-group' : ''} ${r.muted ? 'is-muted' : ''}`} data-row={r.kind === 'group' ? r.id : undefined} style={{ top: tops[i], height: heights[i] }}>{r.label}</div>)}
    </div>
    <div className="cs-track-scroll" ref={wrap}>
      <svg ref={svg} width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="group" aria-label={ariaLabel} onPointerMove={move} onPointerUp={up} onPointerCancel={() => setDrag(null)} className={drag ? 'is-dragging' : ''}>
        <defs>
          {(['dep', 'ev', 'hot'] as const).map(k => <marker key={k} id={`cs-ah-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" className={`cs-ah-${k}`} /></marker>)}
        </defs>
        {rows.map((r, i) => r.kind === 'lane' || r.kind === 'group' ? <line key={r.id} className={r.kind === 'group' ? 'cs-tk-group-line' : 'cs-tk-lane-line'} x1={0} x2={W} y1={tops[i] + heights[i] - 0.5} y2={tops[i] + heights[i] - 0.5} />
          : <rect key={r.id} className={`cs-tk-row ${i % 2 ? 'is-odd' : ''}`} x={0} y={tops[i] + 2} width={W} height={heights[i] - 4} rx={5} />)}
        {ticks.map(m => <g key={m}>
          <line className={`cs-tk-grid ${m % major === 0 ? 'is-major' : ''}`} x1={x(m)} x2={x(m)} y1={TOP - 4} y2={H - BOTTOM + 2} />
          {(m === 0 || m % nameEvery === 0) && <text className="cs-tk-month" x={x(m)} y={TOP - 10} textAnchor="middle">{m === 0 ? 'T' : monthLabel(m)}</text>}
          {m % numEvery === 0 && <text className="cs-tk-num" x={x(m)} y={H - 6} textAnchor="middle">{m}</text>}
        </g>)}
        {bands.map((b, i) => {
          const cy = yOf(b.row), x1 = x(b.from), x2 = Math.max(x(b.to), x1 + 2), w = x2 - x1;
          if (b.kind === 'work') return <rect key={i} className="cs-tk-work" x={x1} y={cy + 9} width={w} height={3} rx={1.5}><title>{b.title}</title></rect>;
          const inside = w >= 70;
          if (!inside && b.kind === 'range') return <g key={i} className={`cs-tk-band is-${b.kind ?? 'band'}`}>
            <title>{b.title}</title>
            <rect x={x1} y={cy - 7} width={w} height={14} rx={4} style={{ fill: b.color, stroke: b.color }} />
            {b.mid != null && <><line x1={x(b.mid)} x2={x(b.mid)} y1={cy - 10} y2={cy - 4} className="cs-tk-mid" /><line x1={x(b.mid)} x2={x(b.mid)} y1={cy + 4} y2={cy + 10} className="cs-tk-mid" /></>}
          </g>;
          return <g key={i} className={`cs-tk-band is-${b.kind ?? 'band'}`}>
            <title>{b.title}</title>
            <rect x={x1} y={cy - 7} width={w} height={14} rx={4} style={{ fill: b.color, stroke: b.color }} />
            {b.mid != null && <><line x1={x(b.mid)} x2={x(b.mid)} y1={cy - 10} y2={cy - 4} className="cs-tk-mid" /><line x1={x(b.mid)} x2={x(b.mid)} y1={cy + 4} y2={cy + 10} className="cs-tk-mid" /></>}
            {b.text && (() => {
              // centred in the band, unless a marker of the row covers it: then left of the band (or not at all)
              const est = b.text.length * 5.6, mid = x1 + w / 2;
              const covered = inside && markers.some(m => m.row === b.row && Math.abs(x(m.month) - mid) < est / 2 + 10);
              if (inside && !covered) return <text className="cs-tk-band-text" x={mid} y={cy + 4} textAnchor="middle">{b.text}</text>;
              return x1 - 6 - est >= 0 ? <text className="cs-tk-band-text" x={x1 - 6} y={cy + 4} textAnchor="end">{b.text}</text> : null;
            })()}
          </g>;
        })}
        {arrows.map((a, i) => {
          const y1 = yOf(a.fromRow), y2 = yOf(a.toRow), x1 = x(a.from) + (a.kind === 'ev' ? 8 : 1), x2 = x(a.to) - 2;
          const k = Math.max(20, Math.abs(x2 - x1) / 2), ym = y2 + (y2 > y1 ? -1 : 1) * ROW / 2;
          const d = x2 - x1 >= 24 ? `M${x1},${y1} C${x1 + k},${y1} ${x2 - k},${y2} ${x2},${y2}` : `M${x1},${y1} C${x1 + 20},${y1} ${x1 + 20},${ym} ${(x1 + x2) / 2},${ym} S${x2 - 22},${y2} ${x2},${y2}`;
          return <g key={i}><title>{a.title}</title><path className="cs-tk-hit" d={d} /><path className={`cs-tk-arrow is-${a.hot ? 'hot' : a.kind}`} d={d} markerEnd={`url(#cs-ah-${a.hot ? 'hot' : a.kind})`} /></g>;
        })}
        {markers.map(m => {
          const cy = yOf(m.row), now = drag?.id === m.id ? drag.now : m.month, cx = x(now);
          // text right of the marker (or of its band); when it would run past the edge, left of the marker instead
          const extra = m.textAt != null ? narrow.get(m.row) : undefined;
          const text = drag?.id === m.id ? monthLabel(now) : `${m.text ?? ''}${extra && m.text ? ` · ${extra}` : ''}`, est = text.length * 6.1;
          let tx = m.textAt != null ? x(m.textAt) + 10 : cx + (m.textLeft ? -13 : 13), anchor: 'start' | 'end' = m.textLeft && m.textAt == null ? 'end' : 'start';
          if (anchor === 'start' && tx + est > W - 4) { tx = cx - 13; anchor = 'end'; }
          return <g key={m.id} className={`cs-tk-marker ${m.draggable ? 'is-draggable' : ''} ${m.selected ? 'is-selected' : ''} ${m.dim ? 'is-dim' : ''}`} data-marker={m.id}
            tabIndex={0} role={m.draggable ? 'slider' : 'button'} aria-label={m.title} aria-valuemin={m.draggable ? 0 : undefined} aria-valuemax={m.draggable ? horizon : undefined} aria-valuenow={m.draggable ? now : undefined}
            onPointerDown={e => down(e, m)} onKeyDown={e => key(e, m)}>
            <title>{m.title}</title>
            {drag?.id === m.id && drag.from !== drag.now && <g className="cs-tk-ghost">
              <line x1={x(drag.from)} x2={cx} y1={cy} y2={cy} />
              {shape(m, x(drag.from), cy, true)}
              <text x={x(drag.from)} y={cy - 12} textAnchor="middle">was {monthLabel(drag.from)} · Esc</text>
            </g>}
            {m.selected && <circle className="cs-tk-sel" cx={cx} cy={cy} r={12} />}
            <rect className="cs-tk-hitbox" x={cx - 14} y={cy - 14} width={28} height={28} />
            {shape(m, cx, cy)}
            {m.ghost && <path className="cs-tk-plus" d={`M${cx - 3.5},${cy}H${cx + 3.5}M${cx},${cy - 3.5}V${cy + 3.5}`} style={{ stroke: m.color }} />}
            {m.text && <text className="cs-tk-text" x={tx} y={cy + 4} textAnchor={anchor}>{text}</text>}
          </g>;
        })}
      </svg>
    </div>
  </div>;
}
