// The review gate before the week level, and ③ Week: the scenario tree of the year (weeks on the x-axis, branch
// width = share of paths, similar paths merged), small multiples per week, a side sheet with the numbers of a node, and
// the paths of the three day examples marked on the tree. The tree comes from the actor simulation (the two best forks
// of the actor ranking) when the API has it; other forks show the preview tree, with its badge.
import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, CheckSquare, GitBranch, Square, TriangleAlert } from 'lucide-react';
import type { HelpEntry, PreviewMetricDef, Ranking, WeekDist, WeekNode } from '../model/types';
import { lowFirst } from '../model/view';
import { useCase } from '../state';
import { fmt, fmtDiff, forkLabel, money, pBestScope, pct, toneOf } from '../ui/format';
import { useNumbers } from '../numbers';
import { HelpTip } from '../ui/HelpTip';
import { MetricLink } from '../ui/MetricLink';
import { Empty, ForkDot, ForkName, SectionHeader, Segmented, Sheet } from '../ui/parts';
import { EVENT_TREE_HELP, EventTreeCard, ExampleMultiples, simKey, SourceBadge, useActors, usePrefetchWeek, useRankOrder, WeekCompute, type MarkedLeaf } from '../actors';
import { actorRivals } from '../actors/scoreSpread';
import { FutureBadges } from './OverTime';
import { Loading } from './common';

/** Rivals the review should keep. On the actor result (current): the forks equal to the leader on every criterion, or
 *  whose score beats the leader's in at least 1 scenario in 10 (the engine's pairwise). On the formula ranking: the
 *  forks whose score range (10th–90th percentile of the per-scenario score, on the ranking's fixed anchors) overlaps
 *  the leader's. */
export function useRivals() {
  const c = useCase(), a = useActors(), { actors } = useRankOrder();
  return useMemo(() => {
    if (actors && a.verdict && a.month) {
      // the actor result: the engine's exact pairwise (the leader's score beaten in 1 scenario in 10), and the tie
      const lead = a.verdict.order[0].fork, tie = a.verdict.allTied ? a.verdict.runnerUp : null;
      return actorRivals(a.month, lead, a.verdict.order.map(o => o.fork), tie);
    }
    const rk = c.ranking;
    if (!rk) return new Set<string>();
    const lead = rk.order[0], [l10] = rk.scoreSpread[lead];
    return new Set(rk.order.slice(1).filter(f => rk.scoreSpread[f][2] >= l10));
  }, [c.ranking, actors, a.verdict, a.month]);
}

/** The forks the review suggests: the leader and the rivals that overlap it, those with week data first, up to max. */
export function suggestForks(rk: Ranking, rivals: Set<string>, from: string[], covered: string[], max: number): string[] {
  return [...new Set([rk.order[0], ...rk.order.slice(1).filter(f => rivals.has(f))])].filter(f => from.includes(f)).sort((a, b) => Number(covered.includes(b)) - Number(covered.includes(a))).slice(0, max);
}

/** The forks a review suggests, by the answer's ranking (the tour's «Show me» uses the same). */
export function useSuggestedForks(from: string[], max: number): string[] {
  const c = useCase(), rivals = useRivals(), { order } = useRankOrder(), { covered } = useCovered();
  return c.ranking ? suggestForks({ ...c.ranking, order }, rivals, from, covered, max) : [];
}

/** The forks with a week tree and day examples on actors (else those of the preview). */
export function useCovered() {
  const c = useCase(), a = useActors();
  // on actors every fork has (or gets) its event tree; the earlier tree covered two
  const trees = Object.keys(a.eventTrees);
  return { covered: a.view ? (trees.length ? trees : a.view.covers) : c.run?.preview.covers ?? [], actors: !!a.view };
}

/** A help entry without the «Preview data.» sentence the config texts carry (the data is the actor simulation). */
export const noPreviewWords = (e: HelpEntry | undefined, actors: boolean): HelpEntry | undefined => !e || !actors ? e : { ...e, short: e.short.replace(/\s*Preview data\.?/g, ''), full: e.full.replace(/\s*Preview data\.?/g, '') };

export function ReviewGate({ id, level, path, max, from, next }: { id: string; level: string; path: 'week' | 'day'; max: number; from: string[]; next: () => void }) {
  const c = useCase(), cfg = c.cfg, sec = cfg.sections.find(s => s.id === id)!;
  // the score range bars are the formula run's; P(best) is the page's (the actors' while they answer, as the fork cards
  // and Compare): a fork is best among all the forks of the ranking, not only those the gate lists
  const rivals = useRivals(), rk = c.ranking, nums = useNumbers();
  const sel = ((c.value(path) ?? []) as string[]).filter(f => from.includes(f));
  const set = (v: string[]) => {
    c.change({ [path]: v }, `${level}: ${v.map(f => forkLabel(cfg, f)).join(', ') || 'none'}`, 'quiet');
    if (!rk) return;
    // the notice says what the choice changes in essence: what the next level shows and what it can no longer tell
    const added = v.filter(f => !sel.includes(f)), dropped = sel.filter(f => !v.includes(f));
    const names = (fs: string[]) => fs.map(f => forkLabel(cfg, f)).join(', ');
    const f2 = (fs: string[]) => fs.length > 1 ? 'are' : 'is';
    const cover = v.reduce((x, f) => x + (nums?.pBest[f] ?? 0), 0), lead = order[0], of = nums?.pBestOf ?? rk.order.length;
    // one line of effect, one of risk at most
    const wait = added.some(f => actors && !a.eventTrees[f]) ? ' Wait for its simulation.' : '';
    const effect = [v.length ? `${level} level: ${v.length} of ${of} forks, ${v.length > 1 ? 'together best' : 'best'} in ${pct(Math.min(1, cover))} of the scenarios.${wait}` : `${level} level: no fork.`];
    const lost = dropped.filter(f => f === lead || rivals.has(f));
    const risks = lost.length ? [`${names(lost)} ${f2(lost)} out: the ${level.toLowerCase()} level can no longer show if ${lost.length > 1 ? 'they' : 'it'} would win.`] : [];
    c.pushNotice({ title: added.length ? `Added ${names(added)}` : dropped.length ? `Removed ${names(dropped)}` : `${level}: no change`, context: 'No new simulation', report: { effect, required: [], recommended: [], supporting: [], risks, tone: risks.length ? 'bad' : 'neutral' }, planPending: false, undo: () => c.change({ [path]: sel }, `Undo: ${level}`, 'quiet') });
  };
  const { covered, actors } = useCovered(), { order } = useRankOrder(), a = useActors();
  const suggestion = useSuggestedForks(from, max);
  return <section id={id} className="cs-section" aria-labelledby={`${id}-title`}>
    <SectionHeader id={id} title={sec.label === 'Review' ? `Review before the ${level.toLowerCase()} level` : sec.label} purpose={sec.purpose} help={sec.help} paths={p => p === path} simulation={false}
      more={[path === 'week' ? 'Rivals whose score range overlaps the leader are marked: the model cannot yet tell them apart, so better keep them.' : 'The day level shows three example scenarios of each fork.', 'The bar of a fork: its score range in 8 of 10 scenarios (0 to 1), with a tick at the median.', actors && 'Every fork you keep gets its week on your company\'s actors.']} />
    {!rk ? <Loading what="the ranking" /> : <article className="card cs-gate" data-testid={`gate-${path}`}>
      {!from.length ? <Empty icon={<ArrowDown size={22} />} title="Choose forks for the week level first" text="The day level shows forks you kept for the week level." action={<button type="button" className="button" onClick={() => document.getElementById('week-review')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>Go to the week review</button>} /> : <>
        <p className="cs-gate-lead">Pick up to {max} fork{max > 1 ? 's' : ''}.</p>
        <ul className="cs-gate-list">{order.filter(f => from.includes(f)).map((f, i) => {
          const on = sel.includes(f), full = !on && sel.length >= max, [s10, s50, s90] = rk.scoreSpread[f];
          return <li key={f} className={`${on ? 'is-on' : ''} ${rivals.has(f) && path === 'week' ? 'is-rival' : ''}`}>
            <button type="button" role="checkbox" aria-checked={on} disabled={full} onClick={() => set(on ? sel.filter(x => x !== f) : [...sel, f])}>
              {on ? <CheckSquare size={18} className="cs-check is-on" /> : <Square size={18} className="cs-check" />}
              <span className="cs-gate-name"><ForkName fork={f} strong={i === 0} />{i === 0 && <span className="badge badge-good"><i />Leader</span>}{rivals.has(f) && path === 'week' && <span className="badge badge-warn"><TriangleAlert size={11} />Overlaps the leader: better keep</span>}</span>
              <span className="cs-gate-range" title={`Score 10–90%: ${s10.toFixed(2)} – ${s90.toFixed(2)}`}><i style={{ left: `${s10 * 100}%`, width: `${Math.max(1, (s90 - s10) * 100)}%` }} /><b style={{ left: `${s50 * 100}%` }} /></span>
              <span className="cs-gate-p" data-testid="gate-pbest">best in {pct(nums?.pBest[f] ?? rk.pBest[f] ?? 0)}<span className="cs-pscope">{pBestScope(nums?.pBestOf ?? rk.order.length)}</span>{!actors && !covered.includes(f) && <small>no preview yet</small>}</span>
            </button>
          </li>;
        })}</ul>
        <div className="cs-gate-foot">
          <span className="cs-gate-spacer" />
          {suggestion.join() !== sel.join() && <button type="button" className="button" title={suggestion.map(f => forkLabel(cfg, f)).join(', ')} onClick={() => set(suggestion)}>Use the suggestion: {suggestion.length} fork{suggestion.length === 1 ? '' : 's'}</button>}
          <button type="button" className="button button-primary" disabled={!sel.length} onClick={next}>{path === 'week' ? 'View the week-by-week paths' : 'View the example days'}<ArrowDown size={14} /></button>
        </div>
      </>}
    </article>}
  </section>;
}

export function WeekSection() {
  const c = useCase(), sec = c.cfg.sections.find(s => s.id === 'week')!;
  const sel = ((c.value('week') ?? []) as string[]).filter(f => c.run?.forks.includes(f));
  const a = useActors(), treesCurrent = sel.length > 0 && sel.every(f => !!a.eventTrees[f]);
  return <section id="week" className="cs-section" aria-labelledby="week-title">
    <SectionHeader id="week" title="Week" purpose={sec.purpose} help={sec.help} badges={<FutureBadges level="week" treesCurrent={treesCurrent} />} />
    {!c.run ? <Loading what="the week tree" /> : !sel.length ? <Empty icon={<GitBranch size={22} />} title="Choose forks above" text="The week level shows the forks you keep at the review. Pick one or more forks first." action={<button type="button" className="button" onClick={() => document.getElementById('week-review')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>Go to the review</button>} /> : <WeekBody forks={sel} />}
  </section>;
}

const KIND_WORD: Record<MarkedLeaf['kind'], string> = { pessimistic: 'Pessimistic', median: 'Median', optimistic: 'Optimistic' };
const KIND_COLOR: Record<MarkedLeaf['kind'], string> = { pessimistic: '#b4533f', median: '#263138', optimistic: '#2f7a62' };

function WeekBody({ forks }: { forks: string[] }) {
  const c = useCase(), cfg = c.cfg, run = c.run!, a = useActors();
  const [pick, setPick] = useState(forks[0]);
  const fork = forks.includes(pick) ? pick : forks[0];
  const [node, setNode] = useState<WeekNode | null>(null);
  const base = cfg.forks.find(f => f.baseline)!.id, crit = cfg.criteria[0];
  // the fork's tree: the event tree of this simulation state (WP18c); the earlier week tree of the actor engine while
  // there is no event tree; nothing (it is computed on actors) while the actor set exists; the preview only without it
  const et = a.eventTrees[fork];
  // the earlier week tree (v1) only while the month file has no event trees and it is of this state
  const levels = !et && !!a.view?.tree[fork] && a.state !== 'stale' && !a.treeFromDefaults;
  const onActors = !!et || levels;
  const hasTree = (f: string) => !!a.eventTrees[f] || (!!a.view?.tree[f] && a.state !== 'stale' && !a.treeFromDefaults);
  const missing = a.view ? forks.filter(f => !hasTree(f)) : [];
  // the shown fork has its tree: the other kept forks are computed in the background
  usePrefetchWeek(missing, simKey(c.edits), !!a.view && !missing.includes(fork));
  const F = levels ? a.view! : run.preview;
  const nodes = F.tree[fork], weekly: WeekDist[] = F.weekly[fork] ?? [], marked = levels ? a.view!.marked[fork] ?? [] : [];
  // the median of the first criterion vs the baseline: the actor ranking's statistics, else the formula run's
  const stat = (f: string) => a.view ? a.month?.ranking?.options.find(o => o.option === f)?.stats?.[crit.id]?.p50 : run.stats[f]?.[crit.id]?.p50;
  const s1 = stat(fork), s0 = stat(base), d = s1 != null && s0 != null ? s1 - s0 : null;
  const dayForks = ((c.value('day') ?? []) as string[]);
  const openDay = (m: MarkedLeaf) => {
    const day = dayForks.includes(fork) ? dayForks : [fork, ...dayForks].slice(0, 2);
    c.change({ dayExample: m.kind, day }, `Day: ${KIND_WORD[m.kind].toLowerCase()} example of ${forkLabel(cfg, fork)}`, 'quiet');
    window.setTimeout(() => document.getElementById('day')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60);
  };
  const help = noPreviewWords(cfg.help.tree, onActors);
  const weekMetrics = a.view?.weekMetrics?.length ? a.view.weekMetrics : run.preview.weekMetrics;
  return <div className={`cs-body ${c.stale ? 'is-stale' : ''}`}>
    <div className="cs-toolbar">
      {forks.length > 1 ? <span className="cs-week-forks"><span className="cs-tools-title">Paths of your shortlist:</span><Segmented<string> label="Fork of the week level" value={fork} options={forks.map(f => ({ id: f, label: forkLabel(cfg, f) }))} onChange={setPick} /></span> : <ForkName fork={fork} strong />}
      {d != null && <span className="cs-toolbar-note">{fork === base ? 'This is the baseline.' : <>Median {lowFirst(crit.label)} vs {forkLabel(cfg, base)}: <span className={`cs-delta is-${toneOf(d, crit.higher)}`}>{fmtDiff(d, crit.unit)}</span></>}</span>}
    </div>
    <div className="cs-week">
      <article className="card cs-card cs-tree-card" data-tour="week-tree" data-testid="week-tree" data-source={onActors || a.view ? 'actors' : 'preview'} data-kind={et ? 'events' : levels ? 'levels' : a.view ? 'computing' : 'preview'}>
        {et ? <EventTreeCard key={fork} fork={fork} tree={et} metrics={weekMetrics} />
          : <>
            <div className="cs-card-head"><h3>Paths, week by week</h3><HelpTip entry={levels || !a.view ? help : EVENT_TREE_HELP} label="Scenario tree" more={['The width of a branch is its share of the paths; similar paths merge so a week keeps at most a few hundred branches. Select a split or an end to read its week.', marked.length > 0 && 'The coloured paths are the three day examples: select their end to open the example at the day level.']} />
              {!a.view && <span className="cs-card-tools"><SourceBadge source="preview" testId="tree-source" note="The actor simulation is not available here." /></span>}</div>
            {a.view && !levels ? <WeekCompute key={`${fork}|${missing.join(',')}`} forks={missing.includes(fork) ? missing : [fork]} fork={fork} stateKey={simKey(c.edits)} />
              : nodes ? <Tree nodes={nodes} marked={marked} onNode={setNode} onLeaf={openDay} selected={node?.id ?? null} />
              : <p className="cs-muted cs-nopreview">No week tree for {forkLabel(cfg, fork)} yet.</p>}
            {(levels || !a.view) && <p className="cs-legend"><i className="is-good" />better than typical<i className="is-neutral" />typical<i className="is-bad" />worse than typical ({F.weekMetrics[0]?.label.toLowerCase() ?? 'value'} of the branch)</p>}
            {marked.length > 0 && <p className="cs-tree-key" data-testid="tree-examples">{marked.map(m => <button key={m.kind} type="button" className="cs-text-btn" onClick={() => openDay(m)}><i style={{ background: KIND_COLOR[m.kind] }} />{KIND_WORD[m.kind]} path</button>)}</p>}
          </>}
      </article>
      <article className="card cs-card cs-weekly">
        <div className="cs-card-head"><h3>Week by week</h3><HelpTip entry={et ? { title: 'Week by week', short: 'The three example scenarios week by week: pessimistic, median and optimistic.', full: '' } : noPreviewWords(cfg.help.weekly, onActors)} label="Week by week" more={[et ? null : 'The bar: 8 of 10 branches of the week; the dot: the median.']} /></div>
        {et ? <ExampleMultiples tree={et} metrics={weekMetrics} /> : (levels || !a.view) ? F.weekMetrics.map(m => <WeekMultiple key={m.id} rows={weekly.filter(r => r.metric === m.id)} metric={m} />) : <p className="cs-muted">After the week is computed.</p>}
      </article>
    </div>
    <Sheet open={!!node} onClose={() => setNode(null)} eyebrow={`WEEK ${node?.week ?? ''} · ${forkLabel(cfg, fork).toUpperCase()}`} title={node?.label ?? ''} foot={<><span><ForkDot fork={fork} /> {onActors ? 'Simulated on your company\'s actors' : 'Preview data'}</span><span>Week {node?.week} of {Math.max(...(nodes ?? []).map(n => n.week), 0)}</span></>}>
      {node && <div data-testid="week-node">
        <p className="cs-sheet-lead">{node.summary}</p>
        <div className="cs-kv">{node.numbers.map(n => <div key={n.label}><span>{n.label}</span><strong>{n.value}</strong></div>)}</div>
        {node.events.length > 0 && <div className="cs-sheet-section"><h3>Key events</h3><ul className="cs-sheet-list">{node.events.map(e => <li key={e}>{e}</li>)}</ul></div>}
      </div>}
    </Sheet>
  </div>;
}

function Tree({ nodes, marked, onNode, onLeaf, selected }: { nodes: WeekNode[]; marked: MarkedLeaf[]; onNode: (n: WeekNode) => void; onLeaf: (m: MarkedLeaf) => void; selected: string | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(760);
  useLayoutEffect(() => { const el = ref.current; if (!el) return; const fit = () => setW(Math.max(300, Math.round(el.clientWidth))); const ro = new ResizeObserver(fit); ro.observe(el); fit(); return () => ro.disconnect(); }, []);
  const layout = useMemo(() => {
    const weeks = Math.max(1, ...nodes.map(n => n.week)), byWeek = new Map<number, WeekNode[]>(), byId = new Map(nodes.map(n => [n.id, n]));
    nodes.forEach(n => byWeek.set(n.week, [...(byWeek.get(n.week) ?? []), n]));
    const kids = new Map<string, number>();
    nodes.forEach(n => { if (n.parent) kids.set(n.parent, (kids.get(n.parent) ?? 0) + 1); });
    const y = new Map<string, number>(), top = 24, plot = Math.max(300, Math.min(520, 26 + 7 * Math.max(...[...byWeek.values()].map(l => l.length))));
    for (let w = 0; w <= weeks; w++) {
      const level = (byWeek.get(w) ?? []).slice().sort((a, b) => (y.get(a.parent ?? '') ?? 0) - (y.get(b.parent ?? '') ?? 0) || b.share - a.share);
      const total = level.reduce((t, n) => t + Math.max(n.share, 0.004), 0) || 1;
      let acc = 0;
      for (const n of level) { const h = Math.max(n.share, 0.004) / total * plot; y.set(n.id, top + acc + h / 2); acc += h; }
    }
    return { weeks, y, H: top + plot + 26, top, plot, byId, kids };
  }, [nodes]);
  if (!nodes.length) return <p className="cs-muted">No tree for this fork.</p>;
  // a phone: the whole year fits the width, only the example ends are labelled
  const narrow = W < 520, { weeks, y, H, top, plot, byId, kids } = layout, L = narrow ? 10 : 18, R = narrow ? 66 : 120;
  const x = (w: number) => L + w / weeks * (W - L - R);
  const tone = (t: string) => t === 'good' ? '#5f9c84' : t === 'bad' ? '#cf7f6f' : '#8a979e';
  const leafOf = new Map(marked.map(m => [m.leaf, m]));
  // labels of the largest end branches, only where they do not collide with each other or with an example's end
  const last: WeekNode[] = [];
  const taken = marked.map(m => y.get(m.leaf)).filter((v): v is number => v != null);
  for (const n of nodes.filter(k => k.week === weeks).sort((a, b) => b.share - a.share)) {
    const yy = y.get(n.id)!;
    if (!narrow && last.length < 6 && !leafOf.has(n.id) && last.every(l => Math.abs(y.get(l.id)! - yy) >= 14) && taken.every(t => Math.abs(t - yy) >= 14)) last.push(n);
  }
  const tickEvery = narrow ? 13 : 4, ticks = Array.from({ length: Math.floor(weeks / tickEvery) + 1 }, (_, i) => i * tickEvery).filter(w => w <= weeks);
  // the same value on every labelled branch (work done is 100% at the end) says nothing: the share alone then
  const uniform = new Set(last.map(n => n.end)).size <= 1;
  const interactive = (n: WeekNode) => !n.parent || n.week === weeks || (kids.get(n.id) ?? 0) > 1 || selected === n.id;
  return <div ref={ref} className="cs-tree">
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="group" aria-label="Scenario tree, week by week">
      {ticks.map(w => <g key={w}><line className="cs-tree-grid" x1={x(w)} x2={x(w)} y1={top - 6} y2={top + plot + 4} /><text className="cs-tree-week" x={x(w)} y={top + plot + 20} textAnchor="middle">{w === 0 ? 'T' : `w${w}`}</text></g>)}
      {nodes.map(n => {
        const p = n.parent ? byId.get(n.parent) : undefined;
        if (!p) return null;
        const x1 = x(p.week), y1 = y.get(p.id)!, x2 = x(n.week), y2 = y.get(n.id)!, mx = (x1 + x2) / 2;
        return <path key={`l-${n.id}`} className="cs-tree-link" d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} style={{ strokeWidth: Math.max(1, n.share * 18), stroke: tone(n.tone) }} />;
      })}
      {marked.map(m => {
        const pts = m.nodes.map(id => byId.get(id)).filter((n): n is WeekNode => !!n);
        if (pts.length < 2) return null;
        const d = pts.map((n, i) => { const xx = x(n.week), yy = y.get(n.id)!; if (!i) return `M${xx},${yy}`; const p = pts[i - 1], px = x(p.week), py = y.get(p.id)!, mx = (px + xx) / 2; return `C${mx},${py} ${mx},${yy} ${xx},${yy}`; }).join(' ');
        return <path key={`ex-${m.kind}`} className="cs-tree-example" d={d} style={{ stroke: KIND_COLOR[m.kind], strokeWidth: 2.2, opacity: 0.85 }} data-example={m.kind} />;
      })}
      {nodes.filter(interactive).map(n => {
        const cx = x(n.week), cy = y.get(n.id)!;
        return <g key={n.id} className={`cs-tree-node cs-atree-node ${selected === n.id ? 'is-selected' : ''}`} tabIndex={0} role="button" aria-label={`${n.label}: ${pct(n.share)} of the paths. ${n.summary}`} onClick={() => onNode(n)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNode(n); } }}>
          <title>{n.summary}</title>
          <circle cx={cx} cy={cy} r={7} className="cs-tree-hit" />
          {(!n.parent || (kids.get(n.id) ?? 0) > 1 || selected === n.id) && <circle cx={cx} cy={cy} r={n.parent ? 2.6 : 5} style={{ fill: n.parent ? tone(n.tone) : '#263830' }} />}
        </g>;
      })}
      {last.map(n => <text key={`t-${n.id}`} className="cs-tree-label" x={x(weeks) + 8} y={y.get(n.id)! + 4}>{uniform ? `${pct(n.share)} of paths` : `${n.end} · ${pct(n.share)}`}</text>)}
      {marked.map(m => {
        const n = byId.get(m.leaf);
        if (!n) return null;
        const cx = x(n.week), cy = y.get(n.id)!;
        return <g key={`leaf-${m.kind}`} className="cs-tree-leaf" role="button" tabIndex={0} data-testid={`leaf-${m.kind}`} aria-label={`${KIND_WORD[m.kind]} example: ${n.end}. Open it at the day level`} onClick={() => onLeaf(m)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onLeaf(m); } }}>
          <title>{`${KIND_WORD[m.kind]} example · ${n.summary}`}</title>
          <circle cx={cx} cy={cy} r={8} style={{ fill: KIND_COLOR[m.kind], stroke: '#fff', strokeWidth: 2 }} />
          <text x={cx} y={cy + 3.5} textAnchor="middle">{m.kind[0].toUpperCase()}</text>
          <text className="cs-tree-label" x={cx + 13} y={cy + 4} style={{ fill: KIND_COLOR[m.kind], fontWeight: 600 }}>{n.end}</text>
        </g>;
      })}
    </svg>
  </div>;
}

function WeekMultiple({ rows, metric: m }: { rows: WeekDist[]; metric: PreviewMetricDef }) {
  if (!rows.length) return null;
  const lo = Math.min(...rows.map(r => r.p10)), hi = Math.max(...rows.map(r => r.p90)), W = 300, H = 74, top = 6, bottom = 16, last = rows[rows.length - 1].week;
  const y = (v: number) => top + (1 - (v - lo) / (hi - lo || 1)) * (H - top - bottom), x = (w: number) => 8 + (w - 1) / Math.max(1, last - 1) * (W - 16);
  const f = (v: number) => m.unit === 'USD' ? money(v) : m.unit === 'share' ? `${Math.round(v * 100)}%` : m.bars ? v.toFixed(1) : fmt(v, m.unit);
  const title = m.bars ? `${m.label} per week` : m.label;
  return <figure className="cs-multiple">
    <figcaption><span><MetricLink id={m.id} label={title} /></span><b>{f(rows[rows.length - 1].p50)}</b><small>week {last} median</small></figcaption>
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label={`${title}: week 1 median ${f(rows[0].p50)}, week ${last} median ${f(rows[rows.length - 1].p50)}`}>
      {rows.map(r => <g key={r.week}><title>{`Week ${r.week}: ${f(r.p10)} – ${f(r.p90)}, median ${f(r.p50)}`}</title>
        <line x1={x(r.week)} x2={x(r.week)} y1={y(r.p10)} y2={y(r.p90)} className="cs-mult-bar" style={{ strokeWidth: Math.max(1.5, Math.min(5, (W - 16) / last * 0.6)) }} />
        <circle cx={x(r.week)} cy={y(r.p50)} r={last > 20 ? 1.6 : 2.6} className="cs-mult-dot" /></g>)}
      <text x={x(1)} y={H - 3} className="cs-mult-tick">w1</text><text x={x(last)} y={H - 3} textAnchor="end" className="cs-mult-tick">w{last}</text>
    </svg>
  </figure>;
}
