// ② Month — Details (collapsed): box plots per criterion with a whisker slider, and the RAG table with its limits.
import { useLayoutEffect, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import type { Stat } from '../model/types';
import { useCase } from '../state';
import { fmt, forkColor, forkLabel, pct } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { SectionHeader } from '../ui/parts';
import { lowFirst } from '../model/view';
import { RagBar } from './Compare';
import { NumbersBadge, sourceOf } from '../numbers';
import { useActors } from '../actors';

const WHISKERS = [{ id: 0, label: 'min – max', lo: 'min', hi: 'max' }, { id: 1, label: '1% – 99%', lo: 'p1', hi: 'p99' }, { id: 2, label: '5% – 95%', lo: 'p5', hi: 'p95' }, { id: 3, label: '10% – 90%', lo: 'p10', hi: 'p90' }] as const;

export function DetailsSection() {
  const c = useCase(), a = useActors(), sec = c.cfg.sections.find(s => s.id === 'details')!;
  const [open, setOpen] = useState(false);
  return <section id="details" className="cs-section" aria-labelledby="details-title">
    <SectionHeader id="details" title="Details" purpose={sec.purpose} help={sec.help} badges={<NumbersBadge source={sourceOf(a)} formulaOnly={sourceOf(a) !== 'draft'} />} simulation={false}
      actions={<button type="button" className="button" aria-expanded={open} aria-controls="details-body" onClick={() => setOpen(v => !v)}><ChevronDown size={15} className={open ? 'cs-rot' : ''} />{open ? 'Hide details' : 'Show details'}</button>} />
    {!open ? <button type="button" className="cs-collapsed" onClick={() => setOpen(true)}><span>Box plots · RAG with limits</span><ChevronDown size={16} /></button>
      : c.run && c.ranking ? <DetailsBody /> : null}
  </section>;
}

function DetailsBody() {
  const c = useCase(), cfg = c.cfg, run = c.run!, rk = c.ranking!;
  const [w, setW] = useState(2);
  const wh = WHISKERS[w];
  return <div id="details-body" className={`cs-body ${c.stale ? 'is-stale' : ''}`}>
    <div className="cs-toolbar">
      <label className="cs-whisker"><span>Whiskers<HelpTip entry={cfg.help.boxplot} size={13} /></span>
        <input type="range" min={0} max={3} step={1} value={w} onChange={e => setW(Number(e.target.value))} aria-label="Whisker percentiles" aria-valuetext={wh.label} style={{ '--fill': `${w / 3 * 100}%` } as React.CSSProperties} />
        <b>{wh.label}</b></label>
    </div>
    <div className="cs-boxes">{cfg.criteria.map(cr => <article key={cr.id} className="card cs-card cs-box-card">
      <div className="cs-card-head"><h3>{cr.label}</h3><span className="cs-muted">{cr.higher ? 'higher is better' : 'lower is better'}{/^FTE/.test(cr.unit) ? ` · in ${cr.unit}` : ''}</span></div>
      <BoxPlot crit={cr.id} unit={cr.unit} lo={wh.lo} hi={wh.hi} order={rk.order} />
    </article>)}</div>
    <article className="card cs-card">
      <div className="cs-card-head"><h3>RAG with limits</h3><HelpTip entry={cfg.help.ragTable} label="RAG" /><span className="cs-muted">limits from {forkLabel(cfg, cfg.forks.find(f => f.baseline)!.id)}{cfg.criteria.some(cr => run.limits[cr.id]?.fixed) && <>, fixed for {cfg.criteria.filter(cr => run.limits[cr.id]?.fixed).map(cr => lowFirst(cr.label)).join(', ')}</>}</span></div>
      <div className="cs-table-wrap" tabIndex={0} role="region" aria-label="RAG table">
        <table className="cs-table cs-rag-table">
          <thead><tr><th>Fork</th>{cfg.criteria.map(cr => <th key={cr.id}>{cr.label}</th>)}<th>Overall</th></tr></thead>
          <tbody>
            <tr className="cs-limits"><th scope="row">Limits</th>{cfg.criteria.map(cr => { const L = run.limits[cr.id]; return <td key={cr.id}>{L.fixed && <span className="cs-lim is-fixed">fixed limits</span>}<span className="cs-lim is-g">green {cr.higher ? '≥' : '≤'} {fmt(L.green, cr.unit)}</span><span className="cs-lim is-r">red {cr.higher ? '≤' : '≥'} {fmt(L.red, cr.unit)}</span></td>; })}<td className="cs-muted">worst of the weighted</td></tr>
            {rk.order.map(f => <tr key={f}><th scope="row"><span className="cs-fork-name"><i className="cs-dot" style={{ background: forkColor(cfg, f), width: 8, height: 8 }} />{forkLabel(cfg, f)}</span></th>
              {cfg.criteria.map(cr => <td key={cr.id}><RagBar shares={rk.rag[f][cr.id]} label={`${forkLabel(cfg, f)}, ${cr.label}`} /><small className="cs-rag-red">red {pct(rk.rag[f][cr.id][2])}</small></td>)}
              <td><RagBar shares={rk.overall[f]} label={`${forkLabel(cfg, f)} overall`} /><small className="cs-rag-red">red {pct(rk.overall[f][2])}</small></td></tr>)}
          </tbody>
        </table>
      </div>
    </article>
  </div>;
}

function BoxPlot({ crit, unit, lo: kLo, hi: kHi, order }: { crit: string; unit: string; lo: keyof Stat; hi: keyof Stat; order: string[] }) {
  const c = useCase(), run = c.run!, cfg = c.cfg, L = run.limits[crit];
  const ref = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(520);
  useLayoutEffect(() => { const el = ref.current; if (!el) return; const ro = new ResizeObserver(() => setW(Math.max(280, Math.round(el.clientWidth)))); ro.observe(el); return () => ro.disconnect(); }, []);
  const st = order.map(f => run.stats[f][crit]);
  let lo = Math.min(...st.map(s => s[kLo]), L.red, L.green), hi = Math.max(...st.map(s => s[kHi]), L.red, L.green);
  if (!(hi > lo)) hi = lo + 1;
  const pad = (hi - lo) * 0.04; lo -= pad; hi += pad;
  const left = 132, right = 30, rowH = 30, top = 8, H = top + order.length * rowH + 24;
  const x = (v: number) => Math.round(left + (v - lo) / (hi - lo) * (W - left - right)) + 0.5;
  const ticks = [0, 1, 2, 3, 4].map(i => lo + (hi - lo) * i / 4);
  return <div ref={ref} className="cs-box">
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Spread of ${crit} per fork`}>
      {ticks.map((t, i) => <g key={i}><line className="cs-box-grid" x1={x(t)} x2={x(t)} y1={top} y2={H - 20} /><text className="cs-box-tick" x={x(t)} y={H - 6} textAnchor={i === ticks.length - 1 ? 'end' : 'middle'}>{fmt(t, unit, { compact: true })}</text></g>)}
      <line className="cs-box-lim is-g" x1={x(L.green)} x2={x(L.green)} y1={top - 4} y2={H - 20}><title>Green limit {fmt(L.green, unit)}</title></line>
      <line className="cs-box-lim is-r" x1={x(L.red)} x2={x(L.red)} y1={top - 4} y2={H - 20}><title>Red limit {fmt(L.red, unit)}</title></line>
      {order.map((f, j) => {
        const s = run.stats[f][crit], y = top + j * rowH + rowH / 2, col = forkColor(cfg, f);
        return <g key={f}>
          <title>{`${forkLabel(cfg, f)}: median ${fmt(s.p50, unit)}, half between ${fmt(s.p25, unit)} and ${fmt(s.p75, unit)}, whiskers ${fmt(s[kLo], unit)} – ${fmt(s[kHi], unit)}, mean ${fmt(s.mean, unit)}`}</title>
          <text className="cs-box-label" x={left - 10} y={y + 4} textAnchor="end">{forkLabel(cfg, f)}</text>
          <line x1={x(s[kLo])} x2={x(s[kHi])} y1={y} y2={y} stroke={col} strokeWidth={1.5} />
          <line x1={x(s[kLo])} x2={x(s[kLo])} y1={y - 5} y2={y + 5} stroke={col} strokeWidth={1.5} />
          <line x1={x(s[kHi])} x2={x(s[kHi])} y1={y - 5} y2={y + 5} stroke={col} strokeWidth={1.5} />
          <rect x={x(s.p25)} y={y - 8} width={Math.max(x(s.p75) - x(s.p25), 2)} height={16} rx={4} fill={col} fillOpacity={0.22} stroke={col} />
          <line x1={x(s.p50)} x2={x(s.p50)} y1={y - 9} y2={y + 9} stroke="#263138" strokeWidth={2} />
          <circle cx={x(s.mean)} cy={y} r={3.5} fill="#fff" stroke="#263138" strokeWidth={1.5} />
        </g>;
      })}
    </svg>
  </div>;
}
