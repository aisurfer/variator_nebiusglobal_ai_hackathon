// ② Month — Answer: the strategy in one sentence, the decision, what would change it, the expected result per
// criterion, and the three main risks as consequences with the month they can come.
import { useMemo, type ReactNode } from 'react';
import { ArrowRight, CalendarClock, ChevronDown, Compass, Equal, Eye, Scale, ShieldCheck, Shuffle, TriangleAlert } from 'lucide-react';
import { closeCallOf, equalChoicesOf, formulaInput, type CloseCall, type EqualChoices } from '../model/closeCall';
import { source } from '../model/source';
import type { BestPath, CaseConfig } from '../model/types';
import { lowFirst } from '../model/view';
import { applyPreset } from './Compare';
import { actorInput, useActors } from '../actors';
import { onActors, sourceOf, type NumbersSource } from '../numbers';
import { useCase } from '../state';
import { fmt, fmtDiff, forkLabel, pBestScope, pct, toneOf } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { ForkDot, ForkName, ForkNote, SectionHeader, useForkNote } from '../ui/parts';
import { Loading } from './common';

/** The verdict of the actor simulation: the tour's answer region while its ranking is current (a stale one, or the slim
 *  «Preview» line without actor data, leaves the tour on the formula decision below). */
function VerdictSlot({ children }: { children: ReactNode }) {
  const a = useActors();
  // after an edit the page reruns on the actors: the previous verdict stays, dimmed, until the new one is back (the
  // verdict itself says «Recomputing on your company's actors…»)
  return <div className={`cs-answer-top ${a.pending ? 'is-recomputing' : ''}`} data-slot="actor-verdict" data-tour={a.month && a.verdict && a.rankState !== 'stale' ? 'answer-verdict' : undefined}>
    {children}
  </div>;
}

/** The close call of the actor verdict, when the verdict shows one (a current actor ranking). */
export function useActorCloseCall() {
  const c = useCase(), a = useActors();
  return a.month && a.verdict && a.rankState !== 'stale' ? closeCallOf(c.cfg, actorInput(c.cfg, a.month, c.forksOn)) : null;
}

/** `top`: the slot at the top of the Answer for the verdict of the actor simulation (WP23). */
export function AnswerSection({ top }: { top?: ReactNode }) {
  const c = useCase();
  const sec = c.cfg.sections.find(s => s.id === 'answer')!;
  return <section id="answer" className="cs-section" aria-labelledby="answer-title">
    <SectionHeader id="answer" title="Answer" purpose={sec.purpose} help={sec.help} />
    {top && <VerdictSlot>{top}</VerdictSlot>}
    {!c.run || !c.best || !c.ranking ? <Loading what="the answer" /> : <FormulaAnswer />}
  </section>;
}

/** Whose answer the page gives. `actors` (and `pending`, while the page reruns on the actors after an edit): the verdict
 *  above is the answer and the formula model's block is a collapsed quick estimate. `draft`: the actors cannot answer for
 *  this state (no route, or the rerun failed): the formula block is a quick estimate, said so. `formula`: no actor data. */
export function useAnswerSource(): NumbersSource {
  return sourceOf(useActors());
}

function FormulaAnswer() {
  const c = useCase(), a = useActors(), src = useAnswerSource(), cfg = c.cfg, leader = c.ranking!.order[0];
  if (src === 'formula') return <AnswerBody />;
  if (src === 'draft') return <div className="cs-answer-draft">
    <p className="cs-draft-strip" data-testid="answer-draft"><TriangleAlert size={14} aria-hidden /><span><b>Quick estimate (formula model) — actors unavailable</b> · {a.rerunAvailable === false ? 'the actor engine does not answer on this server' : `the rerun did not finish${a.rerun.message ? ` (${a.rerun.message})` : ''}`}. Rerun on actors above to try again.</span></p>
    <AnswerBody />
  </div>;
  const actorLeader = a.verdict!.leader;
  return <>
    {actorLeader !== leader && <p className="cs-quick-note" data-testid="quick-disagree">The quick formula model ranks {forkLabel(cfg, leader)} first; the answer above comes from the simulation on your company's actors.</p>}
    <details className="cs-quick" data-testid="quick-estimate">
      <summary><ChevronDown size={15} aria-hidden /><span>Quick estimate</span><small>formula model</small><span className="cs-quick-leader"><ForkDot fork={leader} /><span data-testid="leader">{forkLabel(cfg, leader)}</span></span></summary>
      <AnswerBody quick />
    </details>
  </>;
}

/** A criterion's mean difference in words, seen from the rival (`diff` is decision − rival): «cash at year end about
 *  $534k higher». */
const rivalSide = (x: { crit: CloseCall['strong'][number]['crit']; diff: number }) =>
  `${lowFirst(x.crit.label)} about ${fmtDiff(Math.abs(x.diff), x.crit.unit).replace(/^[+−±]/, '')} ${-x.diff > 0 ? 'higher' : 'lower'}`;
const withIs = (xs: string[]) => xs.map((x, i) => i === 0 ? x.replace(/ about /, ' is about ') : x);

/** Plain words for a close call, all from the result: [what the decision keeps, the trade-off (what the rival wins and
 *  how often, what it costs, the average), the choice]. The cards show the last two (the decision is named above them). */
export function closeCallLines(cfg: CaseConfig, cc: CloseCall): [string, string, string] {
  const D = forkLabel(cfg, cc.decision), R = forkLabel(cfg, cc.rival), T = cc.tied.map(f => forkLabel(cfg, f));
  const keeps = cc.strong.map(x => rivalSide({ crit: x.crit, diff: -x.diff }));
  const strong = cc.strong.map(s => lowFirst(s.crit.label)), wins = cc.rivalWins.map(w => lowFirst(w.crit.label));
  return [
    `With ${D}${T.length ? ` (equal to ${listOf(T)})` : ''}, ${listOf(withIs(keeps))} than with ${R}.`,
    `${tradeOff(cfg, cc)}.`,
    `The choice depends on what matters more to you: ${wins.length ? `${listOf(wins)}, or ` : ''}${listOf(strong)}. Try a priority preset.`,
  ];
}

/** «R comes out on top in 66% of the scenarios (of 5 forks), with cash … higher, but churn … higher and NPS … lower; on
 *  average it scores about the same as D». */
function tradeOff(cfg: CaseConfig, cc: CloseCall): string {
  const D = forkLabel(cfg, cc.decision), R = forkLabel(cfg, cc.rival);
  const gains = cc.rivalWins.map(rivalSide), costs = cc.strong.map(x => rivalSide({ crit: x.crit, diff: x.diff }));
  return `${R} comes out on top in ${pct(cc.pRival)} of the scenarios (${pBestScope(cc.pBestOf)})${gains.length ? `, with ${listOf(gains)}${cc.small ? ' by small margins' : ''}` : ''}${costs.length ? `, but ${listOf(costs)}` : ''}; `
    + (cc.even ? `on average it scores about the same as ${D}` : `on average ${D} scores higher`);
}

/** The close call in one sentence (the tour's answer): «a real trade-off: <the trade-off>». */
export function closeCallSentence(cfg: CaseConfig, cc: CloseCall): string {
  return `a real trade-off: ${tradeOff(cfg, cc)}`;
}
const listOf = (xs: string[]) => xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;

/** Plain words for equal choices: equal on what, why the decision wins the tie, and what would break it. */
export function equalChoicesLines(cfg: CaseConfig, eq: EqualChoices, breaks: BestPath['breaks']): [string, string, string] {
  const D = forkLabel(cfg, eq.decision), R = forkLabel(cfg, eq.runnerUp), b = breaks[0];
  return [
    `${D} and ${R} are equal within the meaningful difference on ${listOf(eq.on.map(c => lowFirst(c.label)))}.`,
    eq.why === 'baseline' ? `${D} wins the tie because it changes nothing.` : eq.why === 'feasible' && eq.feasible ? `${D} wins the tie: it passes the checks more often (${pct(eq.feasible[0])} against ${pct(eq.feasible[1])}).` : `${D} wins the tie on the score.`,
    b ? `It changes if ${b.change}: then ${forkLabel(cfg, b.winner)} wins.` : 'No single change of priorities breaks the tie.',
  ];
}

/** The presets that give another leader, at once (the instant ranking of the formula run): what breaks a tie. */
function usePresetsThatChange() {
  const c = useCase(), cfg = c.cfg, run = c.run, leader = c.ranking?.order[0], current = String(c.value('priority') ?? 'custom');
  return useMemo(() => !run || !leader ? [] : cfg.priorityPresets.filter(p => p.id !== current).map(p => {
    const rk = source.rank(run, cfg, Object.fromEntries(cfg.criteria.map(cr => [cr.id, p.weights[cr.id] ?? 0])), p.caution ?? c.caution);
    return { preset: p, winner: rk.order[0] };
  }).filter(x => x.winner !== leader).slice(0, 2), [run, leader, current, cfg, c.caution]); // eslint-disable-line react-hooks/exhaustive-deps
}

function EqualChoicesBody({ eq }: { eq: EqualChoices }) {
  const c = useCase(), cfg = c.cfg, lines = equalChoicesLines(cfg, eq, c.best?.breaks ?? []), change = usePresetsThatChange();
  return <div className="cs-close is-equal">
    <div className="cs-card-head"><span className="cs-eyebrow cs-close-eyebrow"><Equal size={13} aria-hidden />Equal choices</span>
      <HelpTip label="Equal choices" entry={{ title: 'Equal choices', short: 'The decision and the runner-up differ by less than the meaningful difference on every criterion.', full: 'Each criterion has a meaningful difference (for example $100k of cash or one NPS point): smaller differences count as ties, in the score and in each scenario. When two forks tie on everything, the baseline wins the tie, because it changes nothing; otherwise the fork that passes the checks more often. A change of priorities can break the tie.' }} /></div>
    <ul className="cs-close-lines">{lines.map(l => <li key={l}>{l}</li>)}</ul>
    {change.length > 0 && <div className="cs-close-presets" role="group" aria-label="Priority presets that break the tie">
      {change.map(x => <button key={x.preset.id} type="button" className="button" title={x.preset.line} onClick={() => applyPreset(c, x.preset.id)}>{x.preset.label}<span className="cs-close-then">→ {forkLabel(cfg, x.winner)}</span></button>)}
    </div>}
  </div>;
}

function CloseCallBody({ cc }: { cc: CloseCall }) {
  const c = useCase(), cfg = c.cfg, preset = String(c.value('priority') ?? 'custom');
  const lines = closeCallLines(cfg, cc);
  return <div className="cs-close">
    <div className="cs-card-head"><span className="cs-eyebrow cs-close-eyebrow"><Scale size={13} aria-hidden />Close call</span>
      <HelpTip label="Close call" entry={{ title: 'A close call', short: 'The decision has the best score on average, but another fork wins more of the scenarios.', full: 'The ranking orders the forks by their mean score times the chance they are feasible. P(best) counts in how many scenarios a fork comes out on top of all the forks compared. When the two disagree, or the decision wins fewer than half of the scenarios, the choice depends on your priorities: a preset shows which fork wins when one side matters more.' }} /></div>
    <ul className="cs-close-lines">{lines.slice(1).map(l => <li key={l}>{l}</li>)}</ul>
    <div className="cs-close-presets" role="group" aria-label="Priority presets that settle it">
      {cc.presets.map(p => <button key={p.id} type="button" className={`button ${preset === p.id ? 'is-on' : ''}`} aria-pressed={preset === p.id} title={p.line} onClick={() => applyPreset(c, p.id)}>{p.label}</button>)}
    </div>
  </div>;
}

function AnswerBody({ quick = false }: { quick?: boolean }) {
  const c = useCase();
  const best = c.best!, run = c.run!, rk = c.ranking!, cfg = c.cfg;
  const base = cfg.forks.find(f => f.baseline)!.id, leader = best.leader;
  const orig = c.compare ? c.original : null;
  const note = useForkNote(leader);
  const feasibleTone = best.feasible >= 0.9 ? 'good' : best.feasible >= 0.8 ? 'warn' : 'danger';
  // one close call on the page: the verdict's when it shows one, else the formula model's
  const actorClose = useActorCloseCall();
  const input = formulaInput(run, rk), equal = actorClose ? null : equalChoicesOf(cfg, input);
  const close = actorClose || equal ? null : closeCallOf(cfg, input);
  return <div className={`cs-body ${c.stale ? 'is-stale' : ''}`}>
    <div className="briefing-strip cs-strategy">
      <span className="briefing-icon"><Compass size={18} /></span>
      <p>{best.strategy.map((s, i) => s.strong ? <span key={i}><strong>{s.text}</strong>{note && <span className="cs-muted"> ({note})</span>}</span> : <span key={i}>{s.text}</span>)}</p>
    </div>
    <div className="cs-body" data-tour="answer-main">
    <div className="cs-grid" data-tour="answer-decision">
      <article className="card cs-decision cs-span-5" data-testid="decision">
        <div className="cs-card-head"><span className="cs-eyebrow">Decision</span><HelpTip entry={cfg.help.pBest} label="P(best)" /></div>
        <div className="cs-decision-name"><ForkDot fork={leader} size={12} /><h3 data-testid={quick ? undefined : 'leader'}>{forkLabel(cfg, leader)}</h3>{note && <ForkNote note={note} />}</div>
        <div className="cs-decision-facts">
          <div><span>Best in</span><strong>{pct(best.pBest)}</strong><small>of the scenarios</small><small className="cs-pscope" data-testid="decision-pbest-scope">{pBestScope(rk.order.length)}</small><i className="cs-pbar"><b style={{ width: `${best.pBest * 100}%` }} /></i></div>
          <div><span>Feasible</span><strong>{pct(best.feasible)}</strong><small><span className={`badge badge-${feasibleTone}`}><i />{best.feasible >= 0.8 ? 'Holds' : 'Fragile'}</span></small></div>
          <div><span>Score × feasible</span><strong>{rk.key[leader].toFixed(2)}</strong><small>of 1.00</small></div>
        </div>
        {best.runnerUp && <p className="cs-decision-next"><span>Runner-up</span><ForkName fork={best.runnerUp} /><span className="cs-muted">best in {pct(rk.pBest[best.runnerUp])} · {pBestScope(rk.order.length)} · key {rk.key[best.runnerUp].toFixed(2)}</span></p>}
        {orig && <p className="cs-orig-note">Original run: {forkLabel(cfg, orig.ranking.order[0])} led{orig.ranking.order[0] === leader ? ' too' : ''}, best in {pct(orig.ranking.pBest[orig.ranking.order[0]])} · {pBestScope(orig.ranking.order.length)}.</p>}
      </article>
      <article className={`card cs-changes cs-span-7 ${close || equal ? 'cs-close-call' : ''}`} data-testid={close ? 'close-call' : equal ? 'equal-choices' : undefined}>
        {close && <CloseCallBody cc={close} />}
        {equal && <EqualChoicesBody eq={equal} />}
        <div className="cs-card-head"><span className="cs-eyebrow">Changes if</span><HelpTip entry={cfg.help.changesIf} label="Changes if" more={['Set the priorities in «Compare» → Priorities. Every line is one change at a time.']} /></div>
        {best.breaks.length ? <ul className="cs-breaks">{best.breaks.slice(0, close || equal ? 2 : 5).map((b, i) => <li key={i}><Shuffle size={14} aria-hidden /><span>{b.change.charAt(0).toUpperCase() + b.change.slice(1)}</span><ArrowRight size={13} className="cs-muted" aria-hidden /><ForkName fork={b.winner} strong /></li>)}</ul>
          : <p className="cs-holds"><ShieldCheck size={16} />The choice holds under every single change of priorities.</p>}
      </article>
    </div>
    <div className="cs-sub-head"><h3>Expected result of {forkLabel(cfg, leader)}</h3><HelpTip entry={cfg.help.expected} label="Expected result" more={[`Each card: the median, the bar of 90% of the outcomes (5% to 95%), and the difference to ${forkLabel(cfg, base)}.`]} /></div>
    <div className="cs-kpis" style={{ '--cols': cfg.criteria.length } as React.CSSProperties}>
      {cfg.criteria.map(cr => {
        const s = run.stats[leader][cr.id], b = run.stats[base][cr.id], d = s.p50 - b.p50, tone = toneOf(d, cr.higher, 1e-9 * Math.max(1, Math.abs(b.p50)));
        const o = orig?.run.stats[leader]?.[cr.id];
        const lo = Math.min(s.p5, b.p5), hi = Math.max(s.p95, b.p95), x = (v: number) => hi > lo ? (v - lo) / (hi - lo) * 100 : 50;
        return <article key={cr.id} className="metric-card cs-kpi" data-testid={`kpi-${cr.id}`}>
          <div className="metric-top"><span>{cr.label}</span><HelpTip entry={{ title: cr.label, short: cr.description, full: cr.help }} /></div>
          <div className="metric-value">{fmt(s.p50, cr.unit)}</div>
          <div className="cs-range" role="img" aria-label={`90% between ${fmt(s.p5, cr.unit)} and ${fmt(s.p95, cr.unit)}; baseline median ${fmt(b.p50, cr.unit)}`}>
            <i className="cs-range-bar" style={{ left: `${x(s.p5)}%`, width: `${Math.max(1, x(s.p95) - x(s.p5))}%` }} />
            <i className="cs-range-mid" style={{ left: `${x(s.p50)}%` }} />
            <i className="cs-range-base" style={{ left: `${x(b.p50)}%` }} title={`${forkLabel(cfg, base)}: ${fmt(b.p50, cr.unit)}`} />
          </div>
          <div className="cs-kpi-range">{fmt(s.p5, cr.unit, { compact: true })} – {fmt(s.p95, cr.unit, { compact: true })}</div>
          <div className="metric-bottom">
            {leader === base ? <span className="cs-muted">This is the baseline</span> : <span className={`cs-delta is-${tone}`}>{fmtDiff(d, cr.unit)}<em>vs baseline</em></span>}
            {o && <span className={`cs-delta is-${toneOf(s.p50 - o.p50, cr.higher, 1e-9 * Math.max(1, Math.abs(o.p50)))}`}>{fmtDiff(s.p50 - o.p50, cr.unit)}<em>vs original</em></span>}
          </div>
        </article>;
      })}
    </div>
    </div>
    <div className="cs-sub-head"><h3>Risks to watch</h3><HelpTip entry={cfg.help.risks} label="Risks to watch" more={['Consequence first. The month comes from the risk calendar in «Over time».', best.risks.length > 3 && `${best.risks.length - 3} more risk${best.risks.length - 3 === 1 ? '' : 's'} (R4–R${best.risks.length}) are in the details of the ranking.`]} /></div>
    {best.risks.length ? <ol className="cs-risks">
      {best.risks.slice(0, 3).map(r => <li key={r.id} className="card cs-risk" data-testid="risk">
        <span className="cs-rid">{r.id}</span>
        <strong className="cs-risk-head">{r.head}</strong>
        <p className="cs-risk-odds"><b>{r.odds}</b><span>{pct(r.p)} · {r.detail}</span></p>
        <ul className="cs-risk-facts">
          <li><CalendarClock size={14} />{r.month ? <>Most likely by <b>{r.month.label}</b></> : <span className="cs-muted">No month: a result {cfg.horizonMonths === 12 ? 'at year end' : `at month ${cfg.horizonMonths}`}</span>}</li>
          {r.choice && <li><Shuffle size={14} /><span><ForkName fork={r.choice.fork} /> makes it {r.choice.odds}</span></li>}
          {r.watch && <li><Eye size={14} /><span>Watch: {r.watch}</span></li>}
        </ul>
      </li>)}
    </ol> : <p className="cs-holds"><ShieldCheck size={16} />No risk above 1 in 100 for {forkLabel(cfg, leader)}.</p>}
  </div>;
}
