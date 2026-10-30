// ③ Week as an EVENT TREE: weeks on the x-axis; a ribbon is a group of scenarios with the same material events so far,
// its height their share; a split is labelled with the event and its share; inside a ribbon the band of a metric (the
// middle 80% of its scenarios) and its median show how the scenarios still vary. The example paths (pessimistic, median,
// optimistic) run through it. A click on a ribbon opens the node sheet (why it split, the bands, the scenarios in it);
// a click on a path opens the scenario sheet (week by week, small charts, the example days and the KB branch).
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowDown, ArrowUpRight, GitBranch, GitFork, Route, Users } from 'lucide-react';
import { MetricLink } from '../ui/MetricLink';
import { NodeActions } from './NodeActions';
import { apiUrl, appHref } from '../../../appUrl';
import type { PreviewMetricDef } from '../model/types';
import { useCase } from '../state';
import { fmt, forkLabel, money, moneyShort, pct } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { ForkDot, Segmented, Sheet } from '../ui/parts';
import { useActorData } from '../../actors/api';
import { branchLink } from './branch';
import { baselineOf, dayPaths, examplePaths, layoutEventTree, lineBoxes, metricRange, pathPoints, pathWeeks, placeLabels, splitWhat, type Band, type Box, type EventTreeView, type TreeNode, type TreePathRaw } from './eventTreeModel';
import { RERUN_WHY } from './rerun';
import { useActors } from './useActors';
import './actors.css';

const KIND_WORD: Record<string, string> = { pessimistic: 'Pessimistic', median: 'Median', optimistic: 'Optimistic' };
const KIND_COLOR: Record<string, string> = { pessimistic: '#b4533f', median: '#263138', optimistic: '#2f7a62' };
const TONE_FILL: Record<string, string> = { good: '#e2efe7', bad: '#f6e2dc', neutral: '#eceff1' };
const TONE_BAND: Record<string, string> = { good: '#8fbea4', bad: '#d99a8c', neutral: '#a9b4ba' };
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
/** «9 Jun ’27»: the first day of week w after the fork date; «Jun ’27» for month m (case A steps by months). */
const stepDate = (forkDate: string, w: number, unit: 'week' | 'month' = 'week') => {
  const d = new Date(`${forkDate}T12:00:00Z`);
  if (unit === 'month') { d.setUTCDate(1); d.setUTCMonth(d.getUTCMonth() + w); return `${MON[d.getUTCMonth()]} ’${String(d.getUTCFullYear()).slice(2)}`; }
  d.setUTCDate(d.getUTCDate() + 7 * w); return `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ’${String(d.getUTCFullYear()).slice(2)}`;
};
const Step = (t: EventTreeView) => t.unit === 'month' ? 'Month' : 'Week';
const actorOf = (e: { actor?: string; entityId?: string }) => e.actor ?? (e.entityId ? e.entityId.replace(/^(org|person)\./, '').toUpperCase() : undefined);
const metricFmt = (m: PreviewMetricDef | undefined, v: number) => !m ? String(Math.round(v)) : m.unit === 'USD' ? money(v) : m.unit === 'share' ? pct(v) : m.unit === 'count' ? v.toFixed(v < 10 ? 1 : 0) : fmt(v, m.unit);

export const EVENT_TREE_HELP = {
  title: 'Paths, week by week',
  short: 'Each ribbon is a group of scenarios that had the same material events so far; its height is their share.',
  full: 'Week by week the scenarios split where a material event happens to some of them: a client gives notice, a key engineer resigns, a GPU shortage hits. The label of a ribbon names the event that set it apart and its share of all scenarios; rare events are grouped as «Other events», and «No such event» carries the rest. Inside a ribbon, the shaded band is the middle 80% of the chosen metric over its scenarios and the line their median: the scenarios of a group still vary week by week. Select a ribbon to read why it split; select a coloured path to follow one scenario.',
};

/** The branches the knowledge base can open (WP15: `branches` of the company), read once. */
let branchList: Promise<unknown> | null = null;
function useKbOrg(): unknown {
  const [org, setOrg] = useState<unknown>(null);
  useEffect(() => {
    branchList ??= fetch(apiUrl('/api/kb/organizations')).then(r => r.ok ? r.json() : []).then((xs: unknown[]) => xs?.[0] ?? null).catch(() => null);
    let live = true;
    branchList.then(o => { if (live) setOrg(o); });
    return () => { live = false; };
  }, []);
  return org;
}

const UNIT_OF: Record<string, string> = { cash: 'USD', money: 'USD', spend: 'USD', arr: 'USD', load: 'ratio', incidents: 'count', progress: 'share', fte: 'FTE' };
const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ');

/** The tree's metrics in its order, named by the month file's metric list (case A: money, progress, spend, FTE). */
export const treeMetricDefs = (tree: EventTreeView, metrics: PreviewMetricDef[]): PreviewMetricDef[] =>
  tree.metrics.map(id => metrics.find(m => m.id === id) ?? { id, label: id === 'fte' ? 'FTE' : id === 'arr' ? 'ARR' : capital(id), unit: UNIT_OF[id] ?? 'count', higher: null, bars: id === 'incidents' });

export function EventTreeCard({ fork, tree, metrics }: { fork: string; tree: EventTreeView; metrics: PreviewMetricDef[] }) {
  const c = useCase(), cfg = c.cfg;
  const own = treeMetricDefs(tree, metrics);
  const [metric, setMetric] = useState(own[0]?.id ?? tree.metrics[0]);
  const m = own.find(x => x.id === metric) ?? own[0];
  // the chain of open sheets is in the URL (?sheet=<fork>~node~n5|path~p12, the last one shown): a step out of it (the
  // knowledge base, an actor), Back and a reload come back to the same node or scenario
  type Ref = { kind: 'node' | 'path'; id: string };
  const loc = useLocation(), go = useNavigate();
  const chainOf = (search: string): Ref[] => {
    const raw = new URLSearchParams(search).get('sheet') ?? '';
    if (!raw.startsWith(`${fork}~`)) return [];
    return raw.slice(fork.length + 1).split('|').map(x => { const [k, ...rest] = x.split('~'); return { kind: k as Ref['kind'], id: rest.join('~') }; }).filter(r => (r.kind === 'node' || r.kind === 'path') && r.id);
  };
  const chain = chainOf(loc.search), cur = chain.at(-1) ?? null;
  const node = cur?.kind === 'node' ? cur.id : null, path = cur?.kind === 'path' ? cur.id : null;
  // the item of the previous sheet that the user came from: Back scrolls to it and marks it
  const [focus, setFocus] = useState<string | null>(null);
  const hrefOf = (refs: Ref[]) => { const q = new URLSearchParams(loc.search); if (refs.length) q.set('sheet', `${fork}~${refs.map(r => `${r.kind}~${r.id}`).join('|')}`); else q.delete('sheet'); return `${loc.pathname}?${q}`; };
  const setChain = (refs: Ref[]) => go(hrefOf(refs), { replace: true, state: loc.state });
  // the tree's own P/M/O (by the outcome at the end of the year) and, apart, the pinned day examples of the KB branches
  const marks = useMemo(() => examplePaths(tree), [tree]), days = useMemo(() => dayPaths(tree), [tree]);
  const basis = useDayBasis(fork);
  const find = (id: string) => days.find(x => x.id === id) ?? marks.find(x => x.id === id) ?? tree.paths.find(x => x.id === id) ?? null;
  const n = node ? tree.byId.get(node) ?? null : null, p = path ? find(path) : null;
  const show = (r: Ref | null) => { setFocus(null); setChain(r ? [r] : []); };
  const step = (r: Ref) => { setFocus(null); setChain([...chain, r]); };
  const close = () => show(null);
  const prev = chain.at(-2), prevNode = prev?.kind === 'node' ? tree.byId.get(prev.id) : undefined, prevPath = prev?.kind === 'path' ? find(prev.id) : undefined;
  const back = prev ? { label: prevNode ? nodeTitle(prevNode) : prevPath ? pathTitle(prevPath) : 'the previous step', href: appHref(hrefOf(chain.slice(0, -1))),
    onBack: () => { setFocus(cur?.id ?? null); setChain(chain.slice(0, -1)); } } : undefined;
  const what = tree.outcome === 'money' ? 'money' : 'cash';
  return <>
    <div className="cs-card-head"><h3>Paths, week by week</h3><HelpTip entry={EVENT_TREE_HELP} label="Event tree" />
      {own.length > 1 && <span className="cs-card-tools cs-spread-pick"><span className="cs-tools-title">Show the spread of:</span><Segmented<string> size="sm" label="Metric in the ribbons" value={metric} options={own.map(x => ({ id: x.id, label: x.label }))} onChange={setMetric} /></span>}</div>
    <EventTreeChart tree={tree} marks={marks} days={days} basis={basis} metric={m?.id ?? metric} onNode={id => show({ kind: 'node', id })} onPath={id => show({ kind: 'path', id })} selected={node} />
    <p className="cs-legend"><i className="is-good" />ends better<i className="is-neutral" />about the same<i className="is-bad" />ends worse ({tree.outcome === 'money' ? 'money' : 'cash'} at the end, median) · band: middle 80% of {m?.label.toLowerCase() ?? 'the metric'}</p>
    {(marks.length > 0 || days.length > 0) && <p className="cs-tree-key cs-tree-key-one" data-testid="tree-examples">
      {marks.length > 0 && <span className="cs-key-grp"><span className="cs-key-what">P, M, O by {what} at the end of the {tree.unit === 'month' ? 'project' : 'year'}:</span>{marks.map(x => <button key={x.id} type="button" className="cs-key-chip" title={`${KIND_WORD[x.kind!]} path`} aria-label={`${KIND_WORD[x.kind!]} path`} onClick={() => show({ kind: 'path', id: x.id })} style={{ background: KIND_COLOR[x.kind!] }}>{x.kind![0].toUpperCase()}</button>)}</span>}
      {days.length > 0 && <span className="cs-key-grp" data-testid="tree-day-examples"><span className="cs-key-what"><i className="is-day" />Day examples by {basis.short}:</span>{days.map(x => <button key={x.id} type="button" className="cs-key-chip is-day" title={`Day example · ${x.kind} by ${basis.long}`} aria-label={`${KIND_WORD[x.kind!]} day example`} onClick={() => show({ kind: 'path', id: x.id })}>{x.kind![0].toUpperCase()}</button>)}<HelpTip size={13} label="Day examples" entry={{ title: 'Day examples', short: `The three example days of the Day level: scenarios of the knowledge base, ranked by ${basis.long}.`, full: 'They are replayed scenarios, drawn dashed, outside the shares of the tree. The P, M and O of the tree are its own scenarios, ranked by the outcome at the end of the year.' }} /></span>}
    </p>}
    <Sheet open={!!n} onClose={close} back={back} eyebrow={n ? `${Step(tree).toUpperCase()}S ${n.from}–${n.to} · ${forkLabel(cfg, fork).toUpperCase()}` : ''} title={n ? nodeTitle(n) : ''}
      foot={<><span><ForkDot fork={fork} /> Simulated on your company's actors</span><span>{n ? `${pct(n.share)} of ${tree.n ? tree.n.toLocaleString('en-US') : 'all'} scenarios` : ''}</span></>}>
      {n && <NodeSheet tree={tree} fork={fork} node={n} metrics={own} focus={focus} onNode={id => step({ kind: 'node', id })} onPath={id => step({ kind: 'path', id })} />}
    </Sheet>
    <Sheet open={!!p} onClose={close} back={back} eyebrow={`${p?.fixed ? 'DAY EXAMPLE' : 'SCENARIO'} · ${forkLabel(cfg, fork).toUpperCase()}`} title={p ? pathTitle(p) : ''}
      foot={<><span><ForkDot fork={fork} /> Simulated on your company's actors</span><span>{p ? `${pathWeeks(p).length} ${tree.unit}s with material events` : ''}</span></>}>
      {p && <PathSheet tree={tree} fork={fork} path={p} metrics={own} basis={basis} onNode={id => step({ kind: 'node', id })} onDone={close} />}
    </Sheet>
  </>;
}

const nodeTitle = (n: TreeNode) => splitWhat(n.split);
const pathTitle = (p: TreePathRaw) => p.fixed && p.kind ? `${KIND_WORD[p.kind]} day example` : p.kind ? `${KIND_WORD[p.kind]} path` : `Scenario ${p.scenario ?? p.id}`;

/** What the day examples of a fork are ranked by (the Day level): «cash on 26 Apr 2027», the end of the example days. */
function useDayBasis(fork: string): { short: string; long: string } {
  const a = useActors(), ex = a.view?.days[fork]?.[0], label = ex?.headline.label;
  const on = label && / on /.test(label) ? label : null;
  return on ? { short: on, long: `${on} (the end of the example days)` } : { short: 'cash at the end of the example days', long: 'cash at the end of the example days' };
}

/** The drawn width of a label and of a value label (the tree's fonts), measured once per text by the browser. */
let ctx2d: CanvasRenderingContext2D | null | undefined;
const widths = new Map<string, number>();
function measureWith(font: string, text: string): number {
  const key = `${font}|${text}`, hit = widths.get(key);
  if (hit != null) return hit;
  if (ctx2d === undefined) ctx2d = typeof document !== 'undefined' ? document.createElement('canvas').getContext('2d') : null;
  let w = text.length * 6.3 + 2;
  // a few px of room for the rounding of the glyphs
  if (ctx2d) { ctx2d.font = font; w = Math.ceil(ctx2d.measureText(text).width) + 5; }
  widths.set(key, w);
  return w;
}
const LABEL_FONT = '500 10.5px "DM Sans", sans-serif', VALUE_FONT = '600 10.5px "DM Sans", sans-serif';
const measureLabel = (t: string) => measureWith(LABEL_FONT, t);
const measureValue = (t: string) => measureWith(VALUE_FONT, t);

/** The tree's web font: the labels are measured again once it has loaded (a width measured with the fallback font
 *  would let two labels run into each other). Returns a number that changes when the measures do. */
function useLabelFonts(): number {
  const [gen, setGen] = useState(0);
  useEffect(() => {
    const fonts = typeof document !== 'undefined' ? document.fonts : undefined;
    if (!fonts?.load) return;
    let live = true;
    void Promise.all([fonts.load(LABEL_FONT), fonts.load(VALUE_FONT)]).then(() => { if (!live) return; widths.clear(); setGen(g => g + 1); }).catch(() => undefined);
    return () => { live = false; };
  }, []);
  return gen;
}

/** The space at the top of a ribbon kept for its label: the paths run below it when the ribbon is tall enough. */
const LABEL_ROOM = 14;

export function EventTreeChart({ tree, marks, days, basis, metric, onNode, onPath, selected }: { tree: EventTreeView; marks: TreePathRaw[]; days: TreePathRaw[]; basis: { short: string; long: string }; metric: string; onNode: (id: string) => void; onPath: (id: string) => void; selected: string | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(760);
  useLayoutEffect(() => { const el = ref.current; if (!el) return; const fit = () => setW(Math.max(300, Math.round(el.clientWidth))); const ro = new ResizeObserver(fit); ro.observe(el); fit(); return () => ro.disconnect(); }, []);
  const fontGen = useLabelFonts();
  const narrow = W < 520;
  const leaves = tree.nodes.filter(n => !n.children.length).length;
  const H = Math.max(narrow ? 300 : 340, Math.min(narrow ? 460 : 560, 60 + leaves * (narrow ? 16 : 22)));
  const L = useMemo(() => layoutEventTree(tree, { width: W, height: H, left: narrow ? 6 : 10, right: days.length ? (narrow ? 44 : 64) : (narrow ? 26 : 40), top: 6, bottom: 22, minH: narrow ? 5 : 7 }), [tree, W, H, narrow, days.length]);
  const [lo, hi] = useMemo(() => metricRange(tree, metric), [tree, metric]);
  const every = tree.unit === 'month' ? (narrow ? 6 : 3) : narrow ? 13 : 4;
  const ticks = Array.from({ length: Math.floor(tree.weeks / every) + 1 }, (_, i) => i * every).filter(w => w <= tree.weeks);
  const examples = marks;
  // example paths that share ribbons are drawn a little apart (P below, O above the median); the day examples below
  // them; in a ribbon tall enough they run under the row of its label
  const offset: Record<string, number> = { pessimistic: 3, median: 0, optimistic: -3 };
  const lanes = useMemo(() => ({
    examples: examples.map(p => ({ p, pts: pathPoints(tree, L, p, { labelRoom: LABEL_ROOM }).map(([px, py]) => [px, py + (offset[p.kind!] ?? 0)] as [number, number]) })),
    days: days.map((p, i) => ({ p, pts: pathPoints(tree, L, p, { labelRoom: LABEL_ROOM }).map(([px, py]) => [px, py + 6 + i * 1.5] as [number, number]) })),
  }), [tree, L, examples, days]); // eslint-disable-line react-hooks/exhaustive-deps
  // the labels: each inside its ribbon, never over another text, off the lines of the paths where a row allows (else on
  // a plate of the ribbon's colour); shortened or hidden when there is no room (the tooltip and the sheet name them)
  const labels = useMemo(() => {
    const ends = new Map(tree.nodes.filter(n => !n.children.length && n.outcomeEnd != null).map(n => [n.id, [money(n.outcomeEnd!), moneyShort(n.outcomeEnd!)]]));
    const lines = [...lanes.examples, ...lanes.days].flatMap(x => lineBoxes(x.pts, 2));
    return new Map(placeLabels(tree, L, ends, { measure: measureLabel, measureEnd: measureValue, lines, maxRows: narrow ? 2 : 4 }).map(x => [x.id, x]));
  }, [tree, L, narrow, lanes, fontGen]); // eslint-disable-line react-hooks/exhaustive-deps
  // the end marks stand in two columns right of the plot (P, M, O; then the day examples), stacked so that no two
  // marks overlap
  const markY = useMemo(() => {
    const out = new Map<string, number>();
    const column = (ps: { id: string; y: number }[]) => {
      const st = ps.filter(x => Number.isFinite(x.y)).sort((a, b) => a.y - b.y), gap = 17, lo = L.top + 8, hi = H - L.bottom - 8;
      for (let i = 0; i < st.length; i++) st[i].y = Math.max(st[i].y, lo, i ? st[i - 1].y + gap : -Infinity);
      for (let i = st.length - 1; i >= 0; i--) st[i].y = Math.min(st[i].y, hi, i < st.length - 1 ? st[i + 1].y - gap : Infinity);
      for (const x of st) out.set(x.id, x.y);
    };
    const lastY = (pts: [number, number][]) => pts.length ? pts[pts.length - 1][1] : NaN;
    column(lanes.examples.map(x => ({ id: `m:${x.p.id}`, y: lastY(x.pts) })));
    column(lanes.days.map(x => ({ id: `d:${x.p.id}`, y: lastY(x.pts) })));
    return out;
  }, [L, H, lanes]);
  // a plate under the drawn text only: a measured box has 5 px of room on the side away from its anchor
  const plate = (b: Box, tone: string, end: boolean) => <rect x={end ? b[0] + 3 : b[0] - 2} y={b[1] + 0.5} width={b[2] - b[0] - 1} height={b[3] - b[1]} rx={2} className="cs-enode-plate" style={{ fill: TONE_FILL[tone] }} />;
  return <div ref={ref} className="cs-tree cs-etree" data-testid="event-tree">
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="group" aria-label="Event tree: groups of scenarios week by week">
      {ticks.map(w => <g key={w}><line className="cs-tree-grid" x1={L.x(w)} x2={L.x(w)} y1={L.top} y2={H - L.bottom + 4} /><text className="cs-tree-week" x={L.x(w)} y={H - 6} textAnchor="middle">{w === 0 ? 'T' : `${tree.tick}${w}`}</text></g>)}
      {tree.nodes.map(n => {
        const b = L.boxes.get(n.id)!, h = b.y1 - b.y0, w = b.x1 - b.x0;
        const bands = n.bands[metric] ?? [];
        const y = (v: number) => b.y1 - 1.5 - (v - lo) / (hi - lo) * Math.max(0, h - 3);
        const xw = (wk: number) => L.x(wk);
        const area = h >= 8 && bands.length > 1 ? `M${bands.map(([wk, p10]) => `${xw(wk)},${y(p10)}`).join(' L')} L${[...bands].reverse().map(([wk, , , p90]) => `${xw(wk)},${y(p90)}`).join(' L')} Z` : '';
        const mid = h >= 5 && bands.length > 1 ? `M${bands.map(([wk, , p50]) => `${xw(wk)},${y(p50)}`).join(' L')}` : '';
        const text = n.label;
        return <g key={n.id} className={`cs-enode is-${n.tone} ${selected === n.id ? 'is-selected' : ''}`} role="button" tabIndex={0} data-node={n.id}
          aria-label={`${text}. ${Step(tree)}s ${n.from} to ${n.to}. Open the reasons`} onClick={() => onNode(n.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNode(n.id); } }}>
          <title>{text}</title>
          <rect x={b.x0} y={b.y0} width={Math.max(1, w)} height={Math.max(1, h)} style={{ fill: TONE_FILL[n.tone] }} className="cs-enode-box" />
          {area && <path d={area} style={{ fill: TONE_BAND[n.tone] }} className="cs-enode-band" />}
          {mid && <path d={mid} className="cs-enode-mid" />}
          {n.split && <line x1={b.x0} x2={b.x0} y1={b.y0} y2={b.y1} className="cs-enode-split" />}
        </g>;
      })}
      {lanes.examples.map(({ p, pts: drawn }) => {
        if (drawn.length < 2) return null;
        const pts = [...drawn], last = pts[pts.length - 1], my = markY.get(`m:${p.id}`) ?? last[1], end: [number, number] = [last[0], my];
        if (Math.abs(my - last[1]) > 0.5) pts.push([last[0] + 3, my]);
        return <g key={p.id} className="cs-epath" role="button" tabIndex={0} data-testid={`leaf-${p.kind}`} aria-label={`${KIND_WORD[p.kind!]} example: follow this scenario`}
          onClick={() => onPath(p.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPath(p.id); } }}>
          <title>{`${KIND_WORD[p.kind!]} example${p.fixed ? ' (the day example)' : ''}`}</title>
          <polyline points={pts.map(q => q.join(',')).join(' ')} className="cs-epath-hit" />
          <polyline points={pts.map(q => q.join(',')).join(' ')} className="cs-epath-line" style={{ stroke: KIND_COLOR[p.kind!] }} />
          <circle cx={end[0] + 9} cy={end[1]} r={8} style={{ fill: KIND_COLOR[p.kind!], stroke: '#fff', strokeWidth: 2 }} />
          <text x={end[0] + 9} y={end[1] + 3.5} textAnchor="middle" className="cs-epath-mark">{p.kind![0].toUpperCase()}</text>
        </g>;
      })}
      {lanes.days.map(({ p, pts: drawn }) => {
        if (drawn.length < 2) return null;
        const pts = [...drawn], last = pts[pts.length - 1], my = markY.get(`d:${p.id}`) ?? last[1], end: [number, number] = [last[0] + 17, my];
        pts.push([last[0] + 18, my]);
        return <g key={p.id} className="cs-dpath" role="button" tabIndex={0} data-testid={`day-${p.kind}`} aria-label={`Day example · ${p.kind} by ${basis.long}: follow it`}
          onClick={() => onPath(p.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPath(p.id); } }}>
          <title>{`Day example · ${p.kind} by ${basis.long}`}</title>
          <polyline points={pts.map(q => q.join(',')).join(' ')} className="cs-epath-hit" />
          <polyline points={pts.map(q => q.join(',')).join(' ')} className="cs-dpath-line" />
          <rect x={end[0] + 3} y={end[1] - 6} width={12} height={12} rx={2} className="cs-dpath-mark" />
          <text x={end[0] + 9} y={end[1] + 3} textAnchor="middle" className="cs-dpath-letter">{p.kind![0].toUpperCase()}</text>
        </g>;
      })}
      {/* the labels above the paths, each on a plate of its ribbon's colour: no line (a path, a band, its median) runs
          through a text; the placement keeps them off the path lines where a row of the ribbon allows */}
      <g className="cs-enode-labels" aria-hidden>
        {tree.nodes.map(n => {
          const lab = labels.get(n.id);
          if (!lab || (!lab.left && !lab.end)) return null;
          return <g key={n.id} className={`cs-enode-text is-${n.tone}`} data-label={n.id}>
            {lab.left && lab.leftBox && <>{plate(lab.leftBox, n.tone, false)}<text x={lab.leftBox[0]} y={baselineOf(lab.leftBox)} className="cs-enode-label" data-online={lab.leftOnLine || undefined}>{lab.left}</text></>}
            {lab.end && lab.endBox && <>{plate(lab.endBox, n.tone, true)}<text x={lab.endBox[2]} y={baselineOf(lab.endBox)} textAnchor="end" className="cs-enode-end" data-online={lab.endOnLine || undefined}>{lab.end}</text></>}
          </g>;
        })}
      </g>
    </svg>
  </div>;
}

/** A metric's weekly band (p10–p90 and the median) as a small chart, as wide as the sheet. */
function BandChart({ bands, metric, from, to, tick = 'w' }: { bands: Band[]; metric: PreviewMetricDef; from: number; to: number; tick?: string }) {
  if (bands.length < 2) return null;
  const W = 600, H = 60;
  const lo = Math.min(...bands.map(b => b[1])), hi = Math.max(...bands.map(b => b[3]));
  const x = (w: number) => (w - from) / Math.max(1, to - from) * W, y = (v: number) => 3 + (1 - (v - lo) / (hi - lo || 1)) * (H - 6);
  const area = `M${bands.map(b => `${x(b[0])},${y(b[1])}`).join(' L')} L${[...bands].reverse().map(b => `${x(b[0])},${y(b[3])}`).join(' L')} Z`;
  const last = bands[bands.length - 1], f = (v: number) => metricFmt(metric, v);
  return <figure className="cs-multiple cs-echart">
    <figcaption><span>{metric.label}</span><b>{f(last[2])}</b><small>{tick}{last[0]} median · {f(last[1])} – {f(last[3])}</small></figcaption>
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none" role="img" aria-label={`${metric.label}: week ${bands[0][0]} median ${f(bands[0][2])}, week ${last[0]} median ${f(last[2])}`}>
      <path d={area} className="cs-eband" /><path d={`M${bands.map(b => `${x(b[0])},${y(b[2])}`).join(' L')}`} className="cs-eband-mid" vectorEffect="non-scaling-stroke" />
    </svg>
    <span className="cs-echart-ticks"><span>{tick}{from}</span><span>{tick}{to}</span></span>
  </figure>;
}

/** A path's weekly values as a small chart, as wide as the sheet. */
function LineChart({ weeks, metric, tick = 'w' }: { weeks: Record<string, number>[]; metric: PreviewMetricDef; tick?: string }) {
  const pts = weeks.filter(w => Number.isFinite(w[metric.id]));
  if (pts.length < 2) return null;
  const W = 600, H = 50, lo = Math.min(...pts.map(p => p[metric.id])), hi = Math.max(...pts.map(p => p[metric.id])), first = pts[0].week, last = pts[pts.length - 1].week;
  const x = (w: number) => (w - first) / Math.max(1, last - first) * W, y = (v: number) => 3 + (1 - (v - lo) / (hi - lo || 1)) * (H - 6);
  const f = (v: number) => metricFmt(metric, v);
  return <figure className="cs-multiple cs-echart">
    <figcaption><span>{metric.label}</span><b>{f(pts[pts.length - 1][metric.id])}</b><small>{tick}{last} · from {f(pts[0][metric.id])}</small></figcaption>
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none" role="img" aria-label={`${metric.label}: from ${f(pts[0][metric.id])} to ${f(pts[pts.length - 1][metric.id])}`}>
      <path d={`M${pts.map(p => `${x(p.week)},${y(p[metric.id])}`).join(' L')}`} className="cs-eband-mid" vectorEffect="non-scaling-stroke" />
    </svg>
    <span className="cs-echart-ticks"><span>{tick}{first}</span><span>{tick}{last}</span></span>
  </figure>;
}

/** The actor card of an event's actor on «Actors and risks» (WP20): its own card, else its cohort (clients, prospects). */
function useCardOf(): (id: string | undefined) => string | null {
  const data = useActorData(undefined);
  const ids = data.status === 'ready' ? new Set(data.cards.cards.map(c => c.id)) : null;
  return id => {
    if (!id || !ids) return null;
    if (ids.has(id)) return id;
    if (/^CL-/.test(id) && ids.has('clients:standard')) return 'clients:standard';
    if (/^PR-/.test(id) && ids.has('prospects')) return 'prospects';
    return null;
  };
}

function EntityLinks({ e }: { e: { actor?: string; entityId?: string } }) {
  const nav = useNavigate(), loc = useLocation(), card = useCardOf()(actorOf(e)), actor = card ?? undefined;
  if (!actor && !e.entityId) return null;
  const actorPath = actor ? `/actors?actor=${encodeURIComponent(actor)}` : '';
  // the dossier over this page: its in-app path and parameters (the router's location, without the app's base)
  const entityPath = () => { const s = new URLSearchParams(loc.search); s.set('entity', e.entityId!); return `${loc.pathname}?${s}`; };
  return <span className="cs-elinks">
    {actor && <a className="cs-kb-link" href={appHref(actorPath)} onClick={ev => { ev.preventDefault(); nav(actorPath); }}><Users size={12} aria-hidden />Open the actor<ArrowUpRight size={12} aria-hidden /></a>}
    {e.entityId && <a className="cs-kb-link" href={`?entity=${encodeURIComponent(e.entityId)}`} onClick={ev => { ev.preventDefault(); nav(entityPath()); }}>In the knowledge base<ArrowUpRight size={12} aria-hidden /></a>}
  </span>;
}

function NodeSheet({ tree, fork, node: n, metrics, focus, onNode, onPath }: { tree: EventTreeView; fork: string; node: TreeNode; metrics: PreviewMetricDef[]; focus?: string | null; onNode: (id: string) => void; onPath: (id: string) => void }) {
  // Back from a scenario or a split: scroll to the item the user opened and mark it
  useEffect(() => { if (!focus) return; const el = document.querySelector<HTMLElement>(`[data-sheet-item="${CSS.escape(focus)}"]`); el?.scrollIntoView({ block: 'center' }); el?.focus({ preventScroll: true }); }, [focus, n.id]);
  const c = useCase(), cfg = c.cfg, parent = n.parent ? tree.byId.get(n.parent) : null;
  const s = n.split;
  const U = tree.unit, step = (w: number) => `${U} ${w} (${stepDate(cfg.forkDate, w, U)})`;
  const spread = s && s.weekP10 != null && s.weekP90 != null && s.weekP90 > s.weekP10 ? `, typically in ${U} ${s.week} (${stepDate(cfg.forkDate, s.week, U)}); in 8 of 10 of them between ${U} ${s.weekP10} and ${s.weekP90}` : '';
  const why = !s ? `All ${tree.n ? tree.n.toLocaleString('en-US') : ''} scenarios start together at the fork date; they split at the first material event that sets some of them apart.`
    : s.rest ? `The scenarios${parent?.split ? ` of «${nodeTitle(parent)}»` : ''} in which ${s.of ? `«${s.of.replace(/\.$/, '')}»` : 'the event'} does not happen: they go on together.`
    : s.other ? `Rarer events, each in too few scenarios for a branch of its own, grouped from ${step(s.week)} on.`
    : `${s.text.replace(/\.$/, '')}: it happens in ${pct(n.ofParent)} of the scenarios of the branch it leaves${spread || `, in ${step(s.week)}`}. Of the events of these weeks, it explains most of the spread of ${tree.outcome === 'money' ? 'the money' : 'the cash'} at the end.`;
  const paths = n.paths.map(id => tree.paths.find(p => p.id === id)).filter((p): p is TreePathRaw => !!p);
  return <div data-testid="tree-node-sheet">
    <p className="cs-sheet-lead"><b>Why it splits.</b> {why}</p>
    {s && !s.rest && !s.other && <EntityLinks e={s} />}
    <div className="cs-kv">
      <div><span>Share of all scenarios</span><strong>{pct(n.share)}</strong></div>
      <div><span>Share of its branch</span><strong>{pct(n.ofParent)}</strong></div>
      {n.n != null && <div><span>Scenarios</span><strong>{n.n.toLocaleString('en-US')}</strong></div>}
      <div><span>{Step(tree)}s</span><strong>{n.from}–{n.to}</strong></div>
      {n.outcomeEnd != null && <div><span>{tree.outcome === 'money' ? 'Money' : 'Cash'} at the end, median{n.children.length ? ' (its branches)' : ''}</span><strong>{money(n.outcomeEnd)}</strong>{tree.root.outcomeEnd != null && n !== tree.root && <small className={`cs-delta is-${n.tone}`}>{n.outcomeEnd >= tree.root.outcomeEnd ? '+' : '−'}{money(Math.abs(n.outcomeEnd - tree.root.outcomeEnd))} vs all</small>}</div>}
    </div>
    {s?.other && s.items?.length ? <div className="cs-sheet-section"><h3>The events grouped here</h3><ul className="cs-sheet-list">{s.items.map(i => <li key={i.text}>{i.text}<span className="cs-muted"> · {i.n} scenarios</span></li>)}</ul></div> : null}
    {n.depth > 0 && <NodeActions node={n} fork={fork} title={nodeTitle(n)} metrics={metrics} />}
    <div className="cs-sheet-section"><h3>How the scenarios vary inside it</h3>{metrics.map(m => <BandChart key={m.id} bands={n.bands[m.id] ?? []} metric={m} from={n.from} to={n.to} tick={tree.tick} />)}</div>
    {n.children.length > 0 && <div className="cs-sheet-section"><h3>Splits after it · {U} {tree.byId.get(n.children[0])!.from}</h3><ul className="cs-esplits">{n.children.map(id => { const k = tree.byId.get(id)!; return <li key={id}><button type="button" className={`cs-text-btn is-${k.tone}`} data-sheet-item={id} onClick={() => onNode(id)}><GitBranch size={13} aria-hidden />{k.label}</button></li>; })}</ul></div>}
    {paths.length > 0 && <div className="cs-sheet-section"><h3>Scenarios on this branch</h3><ul className="cs-esplits">{paths.map(p => <li key={p.id}><button type="button" className="cs-text-btn" data-sheet-item={p.id} onClick={() => onPath(p.id)}><Route size={13} aria-hidden style={p.kind ? { color: KIND_COLOR[p.kind] } : undefined} />{pathTitle(p)}</button></li>)}</ul></div>}
  </div>;
}

function PathSheet({ tree, fork, path: p, metrics, basis, onNode, onDone }: { tree: EventTreeView; fork: string; path: TreePathRaw; metrics: PreviewMetricDef[]; basis: { short: string; long: string }; onNode: (id: string) => void; onDone: () => void }) {
  const c = useCase(), cfg = c.cfg, a = useActors(), nav = useNavigate(), org = useKbOrg();
  const leaf = tree.byId.get(p.leaf);
  const weeks = pathWeeks(p);
  const ex = p.kind ? a.view?.days[fork]?.find(d => d.id === p.kind) : undefined;
  const branch = branchLink(ex?.tenant, org);
  const openDays = () => {
    onDone();
    const week = ((c.value('week') ?? []) as string[]), day = ((c.value('day') ?? []) as string[]);
    c.change({ week: week.includes(fork) ? week : [...week, fork], dayExample: p.kind!, day: day.includes(fork) ? day : [fork, ...day].slice(0, 2) }, `Day: ${KIND_WORD[p.kind!].toLowerCase()} example of ${forkLabel(cfg, fork)}`, 'quiet');
    window.setTimeout(() => document.getElementById('day')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80);
  };
  return <div data-testid="tree-path-sheet">
    <p className="cs-sheet-lead">{leaf ? <>Ends on the branch <button type="button" className="cs-text-btn" onClick={() => onNode(leaf.id)}>{nodeTitle(leaf)}</button>{p.scenario != null && <span className="cs-muted"> · scenario {p.scenario}</span>}{p.seed != null && <span className="cs-muted"> · seed {p.seed}</span>}</> : null}
      </p>
    {p.kind && <p className="cs-muted cs-epath-basis" data-testid="path-basis">{p.fixed ? `Day example · ${p.kind} by ${basis.long}: the scenario of its knowledge-base branch, replayed (outside the shares).` : `${KIND_WORD[p.kind]} path of this tree: by ${tree.outcome === 'money' ? 'money' : 'cash'} at the end of the ${tree.unit === 'month' ? 'project' : 'year'}.`}</p>}
    {p.fixed && p.kind && <div className="cs-epath-actions">
      <button type="button" className="button" data-testid="open-example-days" onClick={openDays}><ArrowDown size={14} aria-hidden />Open as example days</button>
      <a className="button cs-branch-link" data-testid="path-branch" data-tenant={ex?.tenant ?? ''} aria-disabled={!branch.open} title={branch.why || `Opens the brief on the branch «${ex?.tenant}», marked as a simulation`}
        href={branch.open ? appHref(`/brief?branch=${encodeURIComponent(ex!.tenant!)}`) : undefined} onClick={e => { e.preventDefault(); if (branch.open) nav(`/brief?branch=${encodeURIComponent(ex!.tenant!)}`); }}>
        <GitFork size={14} aria-hidden />Open in the knowledge base</a>
    </div>}
    {p.weeks.length > 1 && <div className="cs-sheet-section"><h3>{Step(tree)} by {tree.unit}</h3>{metrics.map(m => <LineChart key={m.id} weeks={p.weeks} metric={m} tick={tree.tick} />)}</div>}
    <div className="cs-sheet-section"><h3>What happened</h3>
      {weeks.length ? <ol className="cs-eweeks">{weeks.map(w => <li key={w.week}><span className="cs-eweek">{Step(tree)} {w.week} · {stepDate(cfg.forkDate, w.week, tree.unit)}</span>
        {w.events.map((e, i) => <div key={i} className="cs-eevent"><span>{e.text}</span><EntityLinks e={e} /></div>)}</li>)}</ol>
        : <p className="cs-muted">No material event: the scenario follows the habits of the company all year.</p>}
    </div>
  </div>;
}

/** The three example paths week by week, one small chart per metric (the right column of Week). */
export function ExampleMultiples({ tree, metrics: all }: { tree: EventTreeView; metrics: PreviewMetricDef[] }) {
  const ex = examplePaths(tree).filter(p => p.weeks.length > 1), metrics = treeMetricDefs(tree, all);
  if (!ex.length) return null;
  return <>{metrics.map(m => {
    const all = ex.flatMap(p => p.weeks.map(w => w[m.id]).filter(Number.isFinite));
    if (!all.length) return null;
    const W = 300, H = 70, lo = Math.min(...all), hi = Math.max(...all), last = tree.weeks;
    const x = (w: number) => 6 + w / Math.max(1, last) * (W - 12), y = (v: number) => 6 + (1 - (v - lo) / (hi - lo || 1)) * (H - 20);
    const med = ex.find(p => p.kind === 'median') ?? ex[0], end = med.weeks[med.weeks.length - 1]?.[m.id];
    return <figure key={m.id} className="cs-multiple">
      <figcaption><span><MetricLink id={m.id} label={m.label} /></span><b>{Number.isFinite(end) ? metricFmt(m, end) : '—'}</b><small>median path, {tree.unit} {med.weeks[med.weeks.length - 1]?.week}</small></figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={`${m.label} of the example paths`}>
        {ex.map(p => <path key={p.id} d={`M${p.weeks.filter(w => Number.isFinite(w[m.id])).map(w => `${x(w.week)},${y(w[m.id])}`).join(' L')}`} style={{ fill: 'none', stroke: KIND_COLOR[p.kind!], strokeWidth: 1.5, opacity: 0.9 }} />)}
        <text x={6} y={H - 2} className="cs-mult-tick">T</text><text x={W - 6} y={H - 2} textAnchor="end" className="cs-mult-tick">{tree.tick}{last}</text>
      </svg>
    </figure>;
  })}</>;
}

const attempted = new Set<string>();

/** The other forks kept for the week that have no tree yet: computed in the background once the shown one has its tree. */
export function usePrefetchWeek(missing: string[], stateKey: string, ready: boolean) {
  const c = useCase(), a = useActors();
  const key = `${c.cfg.id}|${stateKey}|${missing.join(',')}`;
  useEffect(() => {
    if (!ready || !missing.length || a.rerunAvailable !== true || a.weekRun.status === 'running' || attempted.has(key)) return;
    const t = window.setTimeout(() => { if (!attempted.has(key) && a.computeWeek(missing)) attempted.add(key); }, 700);
    return () => window.clearTimeout(t);
  }, [key, ready, a.rerunAvailable, a.weekRun.status]); // eslint-disable-line react-hooks/exhaustive-deps
}

/** A fork without a tree of the page's simulation state: the week is computed on actors when the card comes into view
 *  (once per state and forks), or with «Compute on actors»; its progress and a quiet failure show in place. */
export function WeekCompute({ forks, fork, stateKey }: { forks: string[]; fork: string; stateKey: string }) {
  const c = useCase(), a = useActors(), ref = useRef<HTMLDivElement>(null), r = a.weekRun, running = r.status === 'running';
  const key = `${c.cfg.id}|${stateKey}|${forks.join(',')}`;
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(t);
  }, [running]);
  useEffect(() => {
    const el = ref.current;
    if (!el || a.rerunAvailable !== true || running || attempted.has(key) || !forks.length) return;
    // in view for a moment with the same forks (the reader may still be choosing them at the review above)
    let timer: number | undefined;
    const io = new IntersectionObserver(es => {
      window.clearTimeout(timer);
      if (es.some(e => e.isIntersecting)) timer = window.setTimeout(() => { if (!attempted.has(key) && a.computeWeek(forks)) attempted.add(key); }, 700);
    }, { rootMargin: '100px' });
    io.observe(el);
    return () => { io.disconnect(); window.clearTimeout(timer); };
  }, [key, a.rerunAvailable, running]); // eslint-disable-line react-hooks/exhaustive-deps
  const label = forks.map(f => forkLabel(c.cfg, f)).join(', ');
  const failed = !running && r.forks.join(',') === forks.join(',') && (r.status === 'error' || !!r.message);
  return <div ref={ref} className="cs-week-missing" data-testid="week-compute" data-state={running ? 'running' : failed ? 'failed' : 'idle'}>
    {running ? <><strong>Simulating the week on actors…</strong><span><LoaderSpin />{label} · {((now - r.startedAt) / 1000).toFixed(1)} s</span></>
      : <><strong>The week of {forkLabel(c.cfg, fork)} is not computed for this state yet.</strong>
        {failed && <span className="cs-av-run is-error">{r.status === 'error' ? `The computation did not finish (${r.message}).` : r.message}</span>}
        <button type="button" className="button button-primary" data-testid="week-compute-run" aria-disabled={a.rerunAvailable !== true} aria-describedby={a.rerunAvailable !== true ? 'cs-week-compute-why' : undefined} title={a.rerunAvailable === true ? `Simulates the week tree of ${label} on your company's actors` : undefined}
          onClick={() => { if (a.rerunAvailable === true && a.computeWeek(forks)) attempted.add(key); }}><GitBranch size={14} aria-hidden />Compute on actors</button>
        {a.rerunAvailable !== true && <span id="cs-week-compute-why" className="cs-av-why" data-testid="week-compute-why">{a.rerunAvailable === false ? 'Not available here: the actor engine does not answer on this server.' : RERUN_WHY.checking}</span>}</>}
  </div>;
}
function LoaderSpin() { return <svg className="spin" width="13" height="13" viewBox="0 0 24 24" aria-hidden style={{ marginRight: 6, verticalAlign: -2 }}><path d="M21 12a9 9 0 1 1-6.219-8.56" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>; }
