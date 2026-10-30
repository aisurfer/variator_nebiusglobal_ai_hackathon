// ② Month — Compare: the ranking (score × P(feasible), P(best), feasible, RAG), the criteria table with deltas,
// the trade-off line, and the Priorities panel (presets, fine weights, «How cautious?»): instant re-ranking.
import { Fragment } from 'react';
import { Scale } from 'lucide-react';
import { noEmoji, peopleText, type Ranking as EngineRanking } from '../../engine';
import type { Caution } from '../model/types';
import { useCase, type CaseApi } from '../state';
import { NumbersBadge, onActors, useNumbers } from '../numbers';
import { useActors } from '../actors';
import { lowFirst } from '../model/view';
import { churnBreakdown, churnCriterion, churnLine, churnMore } from '../model/churn';
import { fmt, fmtDiff, forkLabel, pBestScope, pct, toneOf } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { ForkName, ModifiedMark, SectionHeader, Segmented } from '../ui/parts';
import { Loading } from './common';


const isPriority = (p: string) => p.startsWith('w|') || p === 'caution' || p === 'priority';

export function CompareSection() {
  const c = useCase(), nums = useNumbers();
  const sec = c.cfg.sections.find(s => s.id === 'compare')!;
  return <section id="compare" className="cs-section" aria-labelledby="compare-title">
    <SectionHeader id="compare" title="Compare" purpose={sec.purpose} help={sec.help} paths={isPriority} badges={nums && <NumbersBadge source={nums.source} />} simulation={!onActors(nums?.source)} />
    {!c.run || !c.ranking ? <Loading what="the ranking" /> : <div className={`cs-compare ${c.stale ? 'is-stale' : ''}`}>
      <div className="cs-compare-main"><Ranking /><Criteria /><Tradeoff /></div>
      <Priorities />
    </div>}
  </section>;
}

export function RagBar({ shares, label }: { shares: [number, number, number]; label: string }) {
  return <span className="cs-rag" role="img" aria-label={`${label}: green ${pct(shares[0])}, amber ${pct(shares[1])}, red ${pct(shares[2])}`} title={`Green ${pct(shares[0])} · amber ${pct(shares[1])} · red ${pct(shares[2])}`}>
    {shares.map((s, i) => s > 0.004 && <i key={i} className={`is-${['g', 'a', 'r'][i]}`} style={{ flexGrow: s }} />)}
  </span>;
}

function Ranking() {
  const c = useCase(), cfg = c.cfg, rk = useNumbers()!;
  // «compare with the original» is a formula run: only next to formula numbers
  const orig = c.compare && !onActors(rk.source) ? c.original : null;
  const maxKey = Math.max(...rk.order.map(f => rk.key[f]), 0.0001);
  const off = cfg.forks.filter(f => !rk.order.includes(f.id));
  return <article className="card cs-card" data-tour="ranking">
    <div className="cs-card-head"><h3>Ranking</h3><HelpTip entry={cfg.help.score} label="Score" more={['Sorted by score × P(feasible).']} /></div>
    <div className="cs-table-wrap" tabIndex={0} role="region" aria-label="Ranking of the forks">
      <table className="cs-table cs-rank-table" data-testid="ranking">
        <thead><tr><th>#</th><th>Fork</th><th className="cs-num">Score × feasible <HelpTip entry={cfg.help.score} size={13} /></th><th>P(best) <HelpTip entry={cfg.help.pBest} size={13} /><small className="cs-pscope cs-th-scope" data-testid="pbest-scope">{pBestScope(rk.pBestOf)}</small></th><th>Feasible <HelpTip entry={cfg.help.feasible} size={13} /></th><th>Outcomes <HelpTip entry={cfg.help.rag} size={13} /></th></tr></thead>
        <tbody>
          {rk.order.map((f, i) => {
            const feas = rk.feasible[f], tone = feas >= 0.9 ? 'good' : feas >= 0.8 ? 'warn' : 'danger';
            const op = orig ? orig.ranking.order.indexOf(f) : -1;
            return <tr key={f} className={i === 0 ? 'is-leader' : ''}>
              <td className="cs-num cs-rank-n">{i + 1}{orig && op >= 0 && op !== i && <small className={`cs-delta is-${op > i ? 'good' : 'bad'}`} title="Place in the original run">{op > i ? '▲' : '▼'}{Math.abs(op - i)}</small>}</td>
              <th scope="row"><ForkName fork={f} strong={i === 0} /></th>
              <td className="cs-num"><span className="cs-keycell"><i className="cs-hbar"><b style={{ width: `${rk.key[f] / maxKey * 100}%` }} /></i><span>{rk.key[f].toFixed(2)}</span></span></td>
              <td><span className="cs-keycell"><i className="cs-pbar"><b style={{ width: `${rk.pBest[f] * 100}%` }} /></i><span>{pct(rk.pBest[f])}</span></span></td>
              <td><span className={`badge badge-${tone}`}><i />{pct(feas)}</span></td>
              <td><RagBar shares={rk.rag[f]} label={forkLabel(cfg, f)} /></td>
            </tr>;
          })}
          {off.map(f => <tr key={f.id} className="is-off"><td className="cs-num">—</td><th scope="row"><ForkName fork={f.id} /></th><td colSpan={4} className="cs-muted">Turned off in step 1</td></tr>)}
        </tbody>
      </table>
    </div>
  </article>;
}

function Criteria() {
  const c = useCase(), cfg = c.cfg, rk = useNumbers()!, base = cfg.forks.find(f => f.baseline)!.id;
  const orig = c.compare && !onActors(rk.source) ? c.original : null;
  return <article className="card cs-card">
    <div className="cs-card-head"><h3>Criteria</h3><HelpTip entry={cfg.help.criteriaTable} label="Criteria" more={[`Each cell: the median, [5%, 95%] of the outcomes, and the delta to ${forkLabel(cfg, base)}.`]} /></div>
    <div className="cs-table-wrap" tabIndex={0} role="region" aria-label="Criteria per fork">
      <table className="cs-table cs-crit-table">
        <thead><tr><th>Fork</th>{cfg.criteria.map(cr => <th key={cr.id} className="cs-num">{cr.label}<small>{cr.higher ? 'higher is better' : 'lower is better'} · weight {pct(rk.weights[cr.id])}</small></th>)}</tr></thead>
        <tbody>{rk.order.map(f => <tr key={f}>
          <th scope="row"><ForkName fork={f} /></th>
          {cfg.criteria.map(cr => {
            const s = rk.stat(f, cr.id), b = rk.stat(base, cr.id), o = orig?.run.stats[f]?.[cr.id];
            if (!s || !b) return <td key={cr.id} className="cs-num cs-muted">—</td>;
            const d = s.p50 - b.p50;
            return <td key={cr.id} className="cs-num">
              <span className="cs-cell-main">{fmt(s.p50, cr.unit)}</span>
              <span className="cs-cell-range">[{fmt(s.p5, cr.unit, { compact: true })}, {fmt(s.p95, cr.unit, { compact: true })}]</span>
              {f !== base && <span className={`cs-delta is-${toneOf(d, cr.higher, 1e-9 * Math.max(1, Math.abs(b.p50)))}`}>{fmtDiff(d, cr.unit)}</span>}
              {o && <span className={`cs-delta is-quiet is-${toneOf(s.p50 - o.p50, cr.higher, 1e-9 * Math.max(1, Math.abs(o.p50)))}`} title="vs the original run">{fmtDiff(s.p50 - o.p50, cr.unit)} orig.</span>}
            </td>;
          })}
        </tr>)}</tbody>
      </table>
    </div>
    <ChurnNote fork={rk.order[0]} on={onActors(rk.source)} />
  </article>;
}

/** What churn counts here against the brief (QA m2): one line from the actors' breakdown of the answer's fork, the
 *  definitions and the clients who had already given notice behind «?». */
function ChurnNote({ fork, on }: { fork: string | undefined; on: boolean }) {
  const c = useCase(), a = useActors(), b = on && fork ? churnBreakdown(a.month, fork) : null;
  if (!b || !a.month?.anchor || !churnCriterion(c.cfg)) return null;
  const more = churnMore(b, a.month.anchor, c.cfg.horizonMonths);
  return <p className="cs-churn-note" data-testid="churn-note"><span>{churnLine(b, a.month.anchor, c.cfg.horizonMonths, forkLabel(c.cfg, fork!))}</span>
    <HelpTip label="What churn counts" entry={{ title: 'What churn counts', short: more[0], full: '' }} more={more.slice(1)} size={13} /></p>;
}

/** The engine's trade-off between the leader and the runner-up: by how much each is better, and where they are equal
 *  within the meaningful difference (or who wins a full tie). */
function Tradeoff() {
  const c = useCase();
  const a = useActors(), nums = useNumbers();
  const t = (c.ranking?.raw as { rk?: EngineRanking } | undefined)?.rk?.tradeoff;
  // on the actors: the verdict's trade-off of the leader and the runner-up, or the tie when they are equal
  const v = a.verdict, tie = v?.tieLine ? `${forkLabel(c.cfg, v.leader)}: ${lowFirst(v.tieLine)}` : undefined;
  const text = onActors(nums?.source) ? v?.tradeoff || tie : t?.text ? peopleText(noEmoji(t.text)) : c.best?.tradeoff;
  if (!text) return null;
  return <p className="cs-tradeoff" data-testid="tradeoff"><Scale size={16} aria-hidden /><span>{text}</span><HelpTip entry={c.cfg.help.tradeoff} size={13} /></p>;
}

/** Applies a priority preset: its weights (and its caution), an instant re-ranking with its notice. */
export function applyPreset(c: CaseApi, id: string, kind: 'rank' | 'quiet' = 'rank') {
  const cfg = c.cfg, p = cfg.priorityPresets.find(x => x.id === id);
  if (!p) return;
  c.change({ ...Object.fromEntries(cfg.criteria.map(cr => [`w|${cr.id}`, p.weights[cr.id] ?? 0])), ...(p.caution ? { caution: p.caution } : {}), priority: p.id }, `Priorities: ${p.label}`, kind);
}

export function Priorities() {
  const c = useCase();
  const cfg = c.cfg, preset = String(c.value('priority') ?? 'custom');
  const CAUTION_OPTIONS = cfg.caution.map(x => ({ id: x.id, label: x.label, title: x.description }));
  const apply = (id: string) => applyPreset(c, id);
  return <aside className="card cs-priorities" aria-labelledby="priorities-title" data-testid="priorities" data-tour="priorities">
    <div className="cs-card-head"><h3 id="priorities-title">Priorities</h3><HelpTip entry={cfg.help.priorities} label="Priorities" more={['Instant: the ranking changes without a new simulation.']} /></div>
    <div className="cs-presets" role="radiogroup" aria-label="Priority presets">
      {cfg.priorityPresets.map(p => <button key={p.id} type="button" role="radio" aria-checked={preset === p.id} className={`cs-chip ${preset === p.id ? 'is-on' : ''}`} title={p.line} onClick={() => apply(p.id)}>{p.label}</button>)}
      {preset === 'custom' && <span className="cs-chip is-custom" aria-live="polite">Custom</span>}
    </div>
    <div className="cs-weights">
      {cfg.criteria.map(cr => {
        const path = `w|${cr.id}`, v = Number(c.value(path) ?? 0);
        return <Fragment key={cr.id}><label className="cs-weight">
          <span className="cs-weight-top"><span>{cr.label}</span><ModifiedMark path={path} unit="x" label={`Weight of ${cr.label}`} inline format={x => Number(x ?? 0).toFixed(2)}><b>{v.toFixed(2)}</b></ModifiedMark></span>
          <input type="range" min={0} max={3} step={0.25} value={v} aria-label={`Weight of ${cr.label}`} style={{ '--fill': `${v / 3 * 100}%` } as React.CSSProperties}
            onChange={e => c.change({ [path]: Number(e.target.value), priority: 'custom' }, `Weight of ${cr.label}: ${v.toFixed(2)} → ${Number(e.target.value).toFixed(2)}`, 'rank')} />
          <span className="cs-weight-share">{pct(c.ranking?.weights[cr.id] ?? 0)} of the decision</span>
        </label></Fragment>;
      })}
    </div>
    <div className="cs-caution">
      <span className="cs-caution-label">How cautious?<HelpTip entry={cfg.help.caution} size={13} /></span>
      <ModifiedMark path="caution" unit="" label="How cautious" format={v => CAUTION_OPTIONS.find(o => o.id === v)?.label ?? String(v)}>
        <Segmented<Caution> label="How cautious" value={c.caution} options={CAUTION_OPTIONS} onChange={v => c.change({ caution: v, priority: 'custom' }, `How cautious: ${CAUTION_OPTIONS.find(o => o.id === v)!.label}`, 'rank')} />
      </ModifiedMark>
    </div>
  </aside>;
}
