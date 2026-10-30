// ② Month — Why: the causal chain (GPU shortage → infrastructure and latency → SLA → NPS → churn → revenue → cash)
// with the mean of each value for a fork, its delta to the baseline and a dot per fork; the main drivers of cash;
// rules and checks with their fire rates. A node opens a side sheet with its definition.
import { Fragment, useState } from 'react';
import { ArrowRight, ChevronDown, ChevronRight } from 'lucide-react';
import type { ChainNode } from '../model/types';
import { lowFirst } from '../model/view';
import { useCase } from '../state';
import { fmt, fmtDiff, forkColor, forkLabel, pct, toneOf } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { ForkName, SectionHeader, Sheet } from '../ui/parts';
import { Loading } from './common';
import { fireRate, NumbersBadge, onActors, sourceOf, useNumbers } from '../numbers';
import { useActors } from '../actors';

export function WhySection() {
  const c = useCase(), sec = c.cfg.sections.find(s => s.id === 'why')!, src = sourceOf(useActors());
  // the chain of causes has no actor equivalent: always the quick formula model, labelled, without ranking numbers
  return <section id="why" className="cs-section" aria-labelledby="why-title">
    <SectionHeader id="why" title="Why" purpose={sec.purpose} help={sec.help} badges={src === 'draft' ? <NumbersBadge source={src} /> : <NumbersBadge source={src} formulaOnly />} simulation={false} />
    {!c.run || !c.ranking ? <Loading what="the chain" /> : <WhyBody />}
  </section>;
}

function WhyBody() {
  const c = useCase(), cfg = c.cfg, run = c.run!;
  const base = cfg.forks.find(f => f.baseline)!.id;
  const [pick, setPick] = useState<string | null>(null);
  // the chain starts on the fork the answer names (the actor verdict while it is current)
  const nums = useNumbers(), a = useActors(), src = sourceOf(a);
  const fork = pick && run.forks.includes(pick) ? pick : nums?.order.find(f => run.forks.includes(f)) ?? c.ranking!.order[0];
  const [open, setOpen] = useState<ChainNode | null>(null);
  const node = (id: string) => cfg.chain.find(n => n.id === id)!;
  const drivers = run.drivers[fork] ?? [], dc = cfg.criteria.find(x => x.id === cfg.driverCriterion) ?? cfg.criteria[0];
  const maxR = Math.max(0.05, ...drivers.map(d => Math.abs(d.r)));
  // while the actors answer, the chain of causes (the quick formula model's) is collapsed and muted: its values can
  // differ from the actor numbers above (QA m3); the rules and checks read the actors
  const quick = onActors(src);
  const chain = <>
    <div className="cs-toolbar">
      <label className="cs-inline-select"><span>Values for</span><select className="cs-select" value={fork} onChange={e => setPick(e.target.value)} aria-label="Fork of the chain">{run.forks.map(f => <option key={f} value={f}>{forkLabel(cfg, f)}</option>)}</select></label>
      <HelpTip entry={cfg.help.chain} label="Chain of causes" more={[`Each card: the mean over the scenarios, the delta to ${forkLabel(cfg, base)}, and a dot per fork.`]} />
    </div>
    <div className="cs-chain-wrap" tabIndex={0} role="region" aria-label="Chain of causes">
      <ol className="cs-chain">
        {cfg.chainPath.map((col, i) => <Fragment key={i}>
          {i > 0 && <li className="cs-chain-arrow" aria-hidden><ArrowRight size={16} /></li>}
          <li className="cs-chain-col">{col.map(id => <ChainCard key={id} n={node(id)} fork={fork} base={base} onOpen={() => setOpen(node(id))} />)}</li>
        </Fragment>)}
      </ol>
    </div>
  </>;
  const driversCard = <article className={`card cs-card ${quick ? 'cs-span-12' : 'cs-span-5'}`}>
    <div className="cs-card-head"><h3>Main drivers of {lowFirst(dc.label)}</h3><HelpTip entry={cfg.help.drivers} label="Main drivers" more={[dc.higher ? `Left: higher values lower ${lowFirst(dc.label)}. Right: higher values raise it.` : `Left: higher values lower ${lowFirst(dc.label)} (better). Right: higher values raise it.`]} /><span className="cs-muted">{forkLabel(cfg, fork)}</span></div>
    <ul className="cs-drivers">{drivers.map(d => <li key={d.input}>
      <span className="cs-driver-label" title={d.label}>{d.label}</span>
      <span className="cs-driver-bar"><i className={d.r >= 0 ? 'is-pos' : 'is-neg'} style={{ width: `${Math.abs(d.r) / maxR * 50}%`, [d.r >= 0 ? 'left' : 'right']: '50%' } as React.CSSProperties} /></span>
      <span className="cs-num cs-driver-r">{d.r >= 0 ? '+' : '−'}{Math.abs(d.r).toFixed(2)}</span>
    </li>)}</ul>
  </article>;
  const rulesCard = <article className={`card cs-card ${quick ? 'cs-span-12' : 'cs-span-7'}`}>
    <div className="cs-card-head"><h3>Rules and checks</h3><HelpTip entry={cfg.help.rules} label="Rules and checks" more={['Each cell: how often the rule fired for the fork.']} />{quick && <NumbersBadge source={src} />}</div>
    <div className="cs-table-wrap" tabIndex={0} role="region" aria-label="Rules and checks">
      <table className="cs-table cs-rules"><thead><tr><th>Rule</th>{run.forks.map(f => <th key={f} className="cs-num" title={forkLabel(cfg, f)}><i className="cs-dot" style={{ background: forkColor(cfg, f), width: 8, height: 8 }} /> {forkLabel(cfg, f).split(' ')[0]}</th>)}</tr></thead>
        <tbody>{cfg.rules.map(r => <tr key={r.id} title={r.text}>
          <th scope="row"><span className="cs-rule-name">{r.label}</span><span className={`cs-kind is-${r.kind}`}>{r.kind}</span></th>
          {run.forks.map(f => { const v = fireRate(c, a, r.id, f); return <td key={f} className="cs-num">{v == null ? <span className="cs-muted" title="No actor check for this rule">—</span> : <span className="cs-fire"><b>{pct(v)}</b><i><s style={{ width: `${Math.min(1, v) * 100}%` }} /></i></span>}</td>; })}
        </tr>)}</tbody></table>
    </div>
  </article>;
  return <div className={`cs-body ${c.stale ? 'is-stale' : ''}`}>
    {quick ? <>
      <details className="cs-why-quick" open data-testid="why-quick" data-tour="why-chain" data-tour-open="">
        <summary><ChevronDown size={15} aria-hidden /><span>How the levers connect</span><small>(quick formula model)</small></summary>
        <p className="cs-why-note">The numbers above come from the simulation on your company's actors. These values are the quick formula model's: they show how the levers connect, and they can differ from the actors' numbers.</p>
        <div className="cs-why-muted">{chain}<div className="cs-grid">{driversCard}</div></div>
      </details>
      <div className="cs-grid">{rulesCard}</div>
    </> : <>
      <div className="cs-body" data-tour="why-chain">{chain}</div>
      <div className="cs-grid">{driversCard}{rulesCard}</div>
    </>}
    <Sheet open={!!open} onClose={() => setOpen(null)} eyebrow="CHAIN OF CAUSES · DEFINITION" title={open?.label ?? ''}>
      {open && <ChainSheet n={open} fork={fork} base={base} onNode={id => setOpen(node(id))} />}
    </Sheet>
  </div>;
}

function ChainCard({ n, fork, base, onOpen }: { n: ChainNode; fork: string; base: string; onOpen: () => void }) {
  const c = useCase(), run = c.run!, v = run.chain[fork]?.[n.id] ?? NaN, b = run.chain[base]?.[n.id] ?? NaN, d = v - b;
  const vals = run.forks.map(f => run.chain[f]?.[n.id] ?? NaN), lo = Math.min(...vals), hi = Math.max(...vals);
  const x = (u: number) => hi > lo ? (u - lo) / (hi - lo) * 100 : 50;
  return <button type="button" className="cs-chain-node" onClick={onOpen} aria-label={`${n.label}: ${fmt(v, n.unit)}. Open the definition.`}>
    <span className="cs-chain-label">{n.label}<ChevronRight size={13} aria-hidden /></span>
    <strong>{fmt(v, n.unit)}</strong>
    {fork !== base ? <span className={`cs-delta is-${toneOf(d, n.higher, 1e-9 * Math.max(1, Math.abs(b)))}`}>{fmtDiff(d, n.unit)}</span> : <span className="cs-delta is-neutral">baseline</span>}
    <span className="cs-chain-dots" aria-hidden>{run.forks.map(f => <i key={f} className={f === fork ? 'is-on' : ''} style={{ left: `${x(run.chain[f]?.[n.id] ?? lo)}%`, background: forkColor(c.cfg, f) }} />)}</span>
  </button>;
}

function ChainSheet({ n, fork, base, onNode }: { n: ChainNode; fork: string; base: string; onNode: (id: string) => void }) {
  const c = useCase(), cfg = c.cfg, run = c.run!;
  const users = cfg.chain.filter(x => x.dependsOn.includes(n.id));
  return <div>
    <p className="cs-sheet-lead">{n.definition}</p>
    <div className="cs-kv">
      <div><span>{forkLabel(cfg, fork)}</span><strong>{fmt(run.chain[fork]?.[n.id], n.unit)}</strong></div>
      <div><span>{forkLabel(cfg, base)} (baseline)</span><strong>{fmt(run.chain[base]?.[n.id], n.unit)}</strong></div>
      <div><span>Better when</span><strong>{n.higher == null ? 'context' : n.higher ? 'higher' : 'lower'}</strong></div>
      <div><span>Unit</span><strong>{n.unit}</strong></div>
    </div>
    <div className="cs-sheet-section"><h3>Per fork</h3>
      <table className="cs-table"><tbody>{run.forks.map(f => <tr key={f}><th scope="row"><ForkName fork={f} /></th><td className="cs-num">{fmt(run.chain[f]?.[n.id], n.unit)}</td><td className="cs-num">{f === base ? <span className="cs-muted">baseline</span> : <span className={`cs-delta is-${toneOf((run.chain[f]?.[n.id] ?? 0) - (run.chain[base]?.[n.id] ?? 0), n.higher, 1e-9)}`}>{fmtDiff((run.chain[f]?.[n.id] ?? 0) - (run.chain[base]?.[n.id] ?? 0), n.unit)}</span>}</td></tr>)}</tbody></table>
    </div>
    {n.dependsOn.length > 0 && <div className="cs-sheet-section"><h3>Depends on</h3><div className="cs-links">{n.dependsOn.map(id => <button key={id} type="button" className="cs-chip" onClick={() => onNode(id)}>{cfg.chain.find(x => x.id === id)?.label}<ChevronRight size={13} /></button>)}</div></div>}
    {users.length > 0 && <div className="cs-sheet-section"><h3>Drives</h3><div className="cs-links">{users.map(u => <button key={u.id} type="button" className="cs-chip" onClick={() => onNode(u.id)}>{u.label}<ChevronRight size={13} /></button>)}</div></div>}
  </div>;
}
