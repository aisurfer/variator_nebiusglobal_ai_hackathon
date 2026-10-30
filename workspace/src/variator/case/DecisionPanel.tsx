// «Your decision»: the draft of the case (warning signs, backup plans, fixes, key-person risks) and «Decide and commit»,
// which freezes it with the chosen fork. The Brief then checks the warning signs against the real data.
import { useState } from 'react';
import { CheckCircle2, ClipboardCheck, Eye, LifeBuoy, RotateCcw, Trash2, UserRoundX, Wrench } from 'lucide-react';
import { useCase } from './state';
import { useDecision } from './decision';
import { useNumbers } from './numbers';
import { dateTime, forkLabel, pBestScope, pct } from './ui/format';
import { Sheet } from './ui/parts';

export function DecisionButton() {
  const d = useDecision(), [open, setOpen] = useState(false);
  const n = d.draft.warnings.length + d.draft.backups.length + d.draft.fixes.length;
  return <>
    <button type="button" className={`button cs-decision-btn ${d.committed ? 'is-committed' : ''}`} onClick={() => setOpen(true)} data-tour="decision" title="What you make of the analysis: warning signs, backup plans, fixes, and the commit">
      {d.committed ? <CheckCircle2 size={15} aria-hidden /> : <ClipboardCheck size={15} aria-hidden />}{d.committed ? 'Decided' : 'Your decision'}{n > 0 && <span className="cs-count">{n}</span>}
    </button>
    <Sheet open={open} onClose={() => setOpen(false)} eyebrow="YOUR DECISION" title={d.committed ? `Decided: ${d.committed.forkLabel}` : 'Decide and commit'}><DecisionBody onDone={() => setOpen(false)} /></Sheet>
  </>;
}

function DecisionBody({ onDone }: { onDone: () => void }) {
  const c = useCase(), d = useDecision(), cfg = c.cfg, nums = useNumbers();
  // the leader the Answer names, with its P(best) over all the forks of the ranking (the actors' while they answer)
  const src = d.committed ?? d.draft, lead = nums?.order[0] ?? c.best?.leader ?? null;
  const pBest = lead && nums ? nums.pBest[lead] ?? null : null, scope = (n: number | null | undefined) => n ? ` (${pBestScope(n)})` : '';
  const drop = (id: string, backup: boolean) => { d.remove(id); if (backup) c.change({ [`bk|${id}`]: null }, 'Backup plan removed', 'sim'); };
  const list = <T extends { id: string }>(title: string, Icon: typeof Eye, xs: T[], line: (x: T) => string, backup = false, empty = '') =>
    <section className="cs-sheet-section"><h3><Icon size={15} aria-hidden /> {title} <span className="cs-count">{xs.length}</span></h3>
      {xs.length ? <ul className="cs-decision-list">{xs.map(x => <li key={x.id}><span>{line(x)}</span>{!d.committed && <button type="button" className="cs-icon-btn" aria-label="Remove" onClick={() => drop(x.id, backup)}><Trash2 size={14} /></button>}</li>)}</ul> : <p className="cs-muted">{empty}</p>}</section>;
  return <div className="cs-decision">
    {!d.shared && <p className="cs-caveat">The server store is not reachable: this browser keeps the decision for now.</p>}
    <section className="cs-sheet-section"><h3>The fork</h3>
      <p>{d.committed ? <><b>{d.committed.forkLabel}</b>, committed {dateTime(d.committed.committedAt)}{d.committed.pBest != null ? ` · best in ${pct(d.committed.pBest)} of the scenarios${scope(d.committed.pBestOf)}` : ''}</> : lead ? <>The leader now: <b>{forkLabel(cfg, lead)}</b>{pBest != null ? ` · best in ${pct(pBest)} of the scenarios${scope(nums?.pBestOf)}` : ''}</> : 'Wait for the ranking.'}</p></section>
    {list('Warning signs', Eye, src.warnings, w => w.text, false, 'Add them from a split of «Paths, week by week»: «Watch for this branch».')}
    {list('Backup plans', LifeBuoy, src.backups, b => b.label, true, 'Add them from a split: «If this happens, do…».')}
    {list('Fixes', Wrench, src.fixes.filter(f => f.kind === 'task'), f => `${f.text}${f.owner ? ` · owner: ${f.owner}` : ''}`, false, 'Add them from a bad day of «Day»: «Fix it».')}
    {list('Key-person risks', UserRoundX, src.fixes.filter(f => f.kind === 'risk'), f => `${f.text}${f.mitigation ? ` · mitigation: ${f.mitigation}` : ''}`, false, 'Add them from a bad day: «Key-person risk».')}
    <div className="cs-decision-foot">
      {d.committed
        ? <button type="button" className="button" onClick={d.reopen}><RotateCcw size={14} />Reopen the decision</button>
        : <button type="button" className="button button-primary" disabled={!lead} onClick={() => { if (!lead) return; d.commit({ fork: lead, forkLabel: forkLabel(cfg, lead), pBest, pBestOf: nums?.pBestOf ?? null, why: c.best?.leader === lead ? c.best.strategy.map(x => x.text).join('') : '' }); onDone(); }}>
          <CheckCircle2 size={15} />Commit {lead ? forkLabel(cfg, lead) : ''}</button>}
      <span className="cs-muted">After the commit, the Brief checks the warning signs against the real data.</span>
    </div>
  </div>;
}
