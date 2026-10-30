// The verdict of the actor simulation at the top of «Answer» (WP21's slot): the leader by the actor ranking, its
// P(best), the runner-up, the tradeoff line of the engine (with ties), the state of the result, and «Rerun on actors»
// (posts the page state to WP18's month route; Over time, Week and Day follow the new result).
import { useEffect, useState } from 'react';
import { Equal, LoaderCircle, RefreshCw, Scale, Users } from 'lucide-react';
import { closeCallOf, equalChoicesOf } from '../model/closeCall';
import { closeCallLines, equalChoicesLines } from '../sections/Answer';
import { applyPreset } from '../sections/Compare';
import { useCase } from '../state';
import { forkLabel, pBestScope, pct } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { ForkDot, ForkName } from '../ui/parts';
import { actorInput } from './adapt';
import { rerunWhy } from './rerun';
import { SourceBadge } from './SourceBadge';
import { useActors, type ActorsApi } from './useActors';
import './actors.css';

const STATE_LINE: Record<string, string> = { defaults: 'For the defaults', current: 'Current', stale: 'Stale after your edits' };
const secs = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

export function ActorVerdict() {
  const c = useCase(), a = useActors(), cfg = c.cfg;
  if (a.status === 'loading') return <article className="card cs-av is-loading" data-testid="actor-verdict" data-status="loading" aria-busy="true"><span className="cs-eyebrow"><Users size={13} aria-hidden />On your company's actors</span><span className="cs-muted"><LoaderCircle size={14} className="spin" aria-hidden />Loading the actor simulation…</span></article>;
  if (!a.month || !a.verdict) return <article className="card cs-av is-missing" data-testid="actor-verdict" data-status={a.status}>
    <span className="cs-eyebrow"><Users size={13} aria-hidden />On your company's actors</span>
    <SourceBadge source="preview" testId="verdict-source" />
    <span className="cs-muted cs-av-quiet">The actor simulation is not available here: the months, weeks and days below are the preview.</span>
  </article>;
  const v = a.verdict, stale = a.rankState === 'stale';
  // the same cards as the formula answer: «Equal choices» for a full tie, else a «Close call» when there is one
  const input = !stale ? actorInput(cfg, a.month, c.forksOn) : null;
  const equal = equalChoicesOf(cfg, input), close = equal ? null : closeCallOf(cfg, input);
  const lines = close ? closeCallLines(cfg, close) : null;
  const breaksIf = a.month.ranking?.bestPath?.breaks_if ?? [];
  const eqLines = equal ? equalChoicesLines(cfg, equal, []).slice(0, breaksIf.length ? 2 : 3) : null;
  // formula inputs you edited that the actor engine does not take (it learns them): named, not hidden
  const ignored = (a.month.provenance?.ignoredEdits ?? []).map(name => cfg.inputs.find(i => i.path === `v|${name}`)?.label ?? name);
  // after an edit the page reruns on the actors (WP25 W3): the verdict shown is the previous one until it is back
  const recomputing = a.pending;
  return <article className={`card cs-av ${stale ? 'is-outdated' : ''} ${recomputing ? 'is-recomputing' : ''}`} data-testid="actor-verdict" data-status="ready" data-state={recomputing ? 'recomputing' : a.rankState}>
    <div className="cs-card-head">
      <span className="cs-eyebrow"><Users size={13} aria-hidden />On your company's actors</span>
      <SourceBadge source="actors" state={a.rankState} scope={a.scope} testId="verdict-source" />
      {recomputing ? <span className="cs-av-state is-recomputing" data-testid="actor-state"><LoaderCircle size={12} className="spin" aria-hidden /><span data-testid="actor-recomputing">Recomputing on your company's actors…</span></span>
        : <span className={`cs-av-state is-${a.rankState === 'stale' ? 'outdated' : a.rankState}`} data-testid="actor-state">{STATE_LINE[a.rankState]}</span>}
      <HelpTip label="The actor verdict" entry={{ title: 'The verdict on your company\'s actors', short: 'The same forks, stepped month by month on your people, clients and vendors with the habits learned from the history.', full: 'The ranking below the verdict comes from the formula model of the case and recomputes at once. This verdict comes from the actor simulation: it is slower, so it reruns a moment after each edit, and the previous verdict stays, dimmed, until the new one is back. When the two disagree, read the tradeoff: it names what each fork is better at.' }}
        more={[`P(best): the share of the ${a.month.n.toLocaleString('en-US')} scenarios in which the fork has the best score of all ${v.pBestOf} forks with your priorities. Two forks equal on everything share a scenario, so the verdict adds theirs up («together best in»).`]} />
      <span className="cs-card-tools"><RerunButton a={a} /></span>
    </div>
    <div className="cs-av-main">
      {v.allTied && v.runnerUp ? <div className="cs-av-leader" data-testid="actor-tie">
        <ForkDot fork={v.leader} size={12} />
        <h3 data-testid="actor-leader">{forkLabel(cfg, v.leader)}</h3>
        <span className="cs-av-eq">· equal to <ForkName fork={v.runnerUp} /></span>
        <span className="cs-av-p" title={`${forkLabel(cfg, v.leader)} best in ${pct(v.pBest)}, ${forkLabel(cfg, v.runnerUp)} in ${pct(v.runnerPBest ?? 0)}, ${pBestScope(v.pBestOf)}: a tie shares the scenario`}>together best in <b data-testid="actor-pbest">{pct(v.pBest + (v.runnerPBest ?? 0))}</b><span className="cs-pscope" data-testid="actor-pbest-scope">· {pBestScope(v.pBestOf)}</span></span>
        <i className="cs-pbar cs-av-bar" aria-hidden><b style={{ width: `${Math.min(1, v.pBest + (v.runnerPBest ?? 0)) * 100}%` }} /></i>
      </div> : <>
        <div className="cs-av-leader">
          <ForkDot fork={v.leader} size={12} />
          <h3 data-testid="actor-leader">{forkLabel(cfg, v.leader)}</h3>
          <span className="cs-av-p"><b data-testid="actor-pbest">{pct(v.pBest)}</b> best <span className="cs-pscope" data-testid="actor-pbest-scope">· {pBestScope(v.pBestOf)}</span></span>
          <i className="cs-pbar cs-av-bar" aria-hidden><b style={{ width: `${v.pBest * 100}%` }} /></i>
        </div>
        {v.runnerUp && <p className="cs-av-runner" data-testid="actor-runner"><span>Runner-up</span><ForkName fork={v.runnerUp} /><span className="cs-muted">{pct(v.runnerPBest ?? 0)} best · {pBestScope(v.pBestOf)}</span></p>}
      </>}
    </div>
    {v.tradeoff && <p className="cs-av-trade" data-testid="actor-tradeoff"><Scale size={14} aria-hidden />{v.tradeoff}</p>}
    {v.tieLine && !eqLines && <p className="cs-av-trade" data-testid="actor-ties"><Equal size={14} aria-hidden />{v.tieLine}</p>}
    {eqLines && <div className="cs-av-close is-equal" data-testid="actor-equal-choices">
      <span className="cs-eyebrow"><Equal size={12} aria-hidden /> Equal choices</span>
      <ul>{eqLines.map(l => <li key={l}>{l}</li>)}</ul>
    </div>}
    {ignored.length > 0 && a.state === 'current' && <p className="cs-av-run" data-testid="actor-ignored">Not used on actors: {ignored.join(', ')}. The actors learn {ignored.length === 1 ? 'it' : 'them'} from the history.</p>}
    {lines && close && <div className="cs-av-close" data-testid="actor-close-call">
      <span className="cs-eyebrow">Close call</span>
      <ul>{lines.slice(1).map(l => <li key={l}>{l}</li>)}</ul>
      <div className="cs-close-presets" role="group" aria-label="Priority presets that settle it">{close.presets.map(p => <button key={p.id} type="button" className="button" title={p.line} onClick={() => applyPreset(c, p.id)}>{p.label}</button>)}</div>
    </div>}
    <RerunStatus a={a} />
  </article>;
}

/** «Rerun on actors» with the reason next to it when it cannot run: checking the engine, no engine on this server,
 *  running, or already up to date (after an edit the page reruns on its own; the button retries a failed rerun). */
function RerunButton({ a }: { a: ActorsApi }) {
  const running = a.rerun.status === 'running';
  const r = rerunWhy({ rerunAvailable: a.rerunAvailable, pending: a.pending, running, stale: a.state === 'stale' || a.rankState === 'stale' });
  return <span className="cs-av-rerun" data-state={r.state}>
    {r.why && <span id="cs-av-why" className="cs-av-why" data-testid="actor-rerun-why">{r.why}</span>}
    <button type="button" className="button button-primary" aria-disabled={r.off} data-testid="actor-rerun" aria-describedby={r.why ? 'cs-av-why' : undefined}
      onClick={() => { if (!r.off) a.run(); }}>
      {r.state === 'running' ? <LoaderCircle size={14} className="spin" aria-hidden /> : <RefreshCw size={14} aria-hidden />}{r.state === 'running' ? 'Running on actors…' : 'Rerun on actors'}
    </button>
  </span>;
}

function RerunStatus({ a }: { a: ActorsApi }) {
  const r = a.rerun, [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (r.status !== 'running') return;
    const t = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(t);
  }, [r.status]);
  if (r.status === 'running') return <p className="cs-av-run" role="status" data-testid="actor-progress"><LoaderCircle size={13} className="spin" aria-hidden />Simulating {a.month?.n.toLocaleString('en-US') ?? ''} scenarios on actors · {secs(Math.max(0, now - r.startedAt))}</p>;
  if (r.status === 'error') return <p className="cs-av-run is-error" role="status" data-testid="actor-error">The rerun did not finish ({r.message}). The result shown is unchanged.</p>;
  if (r.status === 'done' && r.ms != null) return <p className="cs-av-run" role="status" data-testid="actor-done">Rerun on actors in {secs(r.ms)}.</p>;
  return null;
}
