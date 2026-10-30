// «Your decisions»: the warning signs of the committed case decisions, checked against the numbers of this brief.
// A sign that holds says the company is on the branch the user feared, so it is time for the backup plan.
import { useEffect, useState } from 'react';
import { Eye, ShieldAlert, ShieldCheck } from 'lucide-react';
import { readIndex } from '../variator/case/model/cases';
import { readCommitted, type CommittedDecision } from '../variator/case/decision';
import type { KbMetricResult } from './types';
import { formatValue } from './api';

interface Row { caseId: string; title: string; d: CommittedDecision }

export function BriefDecisions({ metrics, onMetric }: { metrics: Map<string, KbMetricResult>; onMetric: (id: string) => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => {
    let live = true;
    void readIndex().then(cases => Promise.all(cases.map(async c => ({ caseId: c.id, title: c.title, d: await readCommitted(c.id) }))))
      .then(xs => { if (live) setRows(xs.filter((x): x is Row => !!x.d && x.d.warnings.length > 0)); }).catch(() => {});
    return () => { live = false; };
  }, []);
  if (!rows.length) return null;
  return <section className="kb-area kb-decisions" aria-labelledby="kb-decisions-title">
    <div className="kb-area-head"><h2 id="kb-decisions-title"><Eye size={17} aria-hidden /> Warning signs of your decisions</h2></div>
    {rows.map(r => <div key={r.caseId} className="kb-decision">
      <p className="kb-decision-head"><b>{r.title}</b> · decided: {r.d.forkLabel}{r.d.backups.length ? ` · ${r.d.backups.length} backup plan${r.d.backups.length > 1 ? 's' : ''} ready` : ''}</p>
      <ul>{r.d.warnings.map(w => {
        const m = w.catalogue ? metrics.get(w.catalogue) : undefined;
        // the simulation counts load as a ratio, the catalogue as a percent
        const limit = m && w.unit === 'ratio' && m.unit === 'percent' ? w.threshold * 100 : w.threshold;
        const hit = m?.value != null ? (w.op === '<' ? m.value < limit : m.value > limit) : null;
        return <li key={w.id} className={hit ? 'is-hit' : hit === false ? 'is-ok' : ''}>
          {hit ? <ShieldAlert size={14} aria-hidden /> : <ShieldCheck size={14} aria-hidden />}
          <span>{w.text}.</span>
          {m ? <button type="button" className="text-button" onClick={() => onMetric(m.id)}>Now: {formatValue(m.value, m.unit)} — {hit ? 'the sign holds: time for the backup plan' : 'not yet'}</button> : <small>Not measured in this brief.</small>}
        </li>;
      })}</ul>
    </div>)}
  </section>;
}
