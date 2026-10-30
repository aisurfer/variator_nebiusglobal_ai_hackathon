// What the user MAKES of a split of the event tree: a warning sign («if by week 12 cash is below $1.4M, we are on
// this branch», checked against the real data in the Brief) and a backup plan («if team load > 1.05, hire 2 more»: an
// effect rule of the formula model, so the case recomputes with it). Both go into the decision draft.
import { useState } from 'react';
import { Eye, LifeBuoy } from 'lucide-react';
import type { PreviewMetricDef } from '../model/types';
import { useCase } from '../state';
import { forkLabel } from '../ui/format';
import { newId, useDecision } from '../decision';
import type { TreeNode } from './eventTreeModel';

/** Week metric → the formula value a backup plan tests, and the catalogue metric the Brief measures it with. */
const FORMULA: Record<string, string> = { cash: 'cash_end', load: 'load', incidents: 'incidents' };
const CATALOGUE: Record<string, string> = { cash: 'fin.cash', load: 'ops.team_load', incidents: 'ops.incidents', arr: 'rev.arr' };

const fmt = (v: number, unit: string) => unit === 'USD' ? `$${Math.abs(v) >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : `${Math.round(v / 1e3)}k`}` : unit === 'ratio' ? v.toFixed(2) : `${Math.round(v * 10) / 10}`;

export function NodeActions({ node: n, fork, title, metrics }: { node: TreeNode; fork: string; title: string; metrics: PreviewMetricDef[] }) {
  const c = useCase(), d = useDecision(), cfg = c.cfg;
  const [open, setOpen] = useState<'watch' | 'backup' | null>(null);
  const usable = metrics.filter(m => (n.bands[m.id] ?? []).length);
  const [metric, setMetric] = useState(usable[0]?.id ?? '');
  const m = usable.find(x => x.id === metric);
  const end = m ? (n.bands[m.id] ?? []).at(-1) : undefined;
  // the bad side of a metric: below for «higher is better», above otherwise
  const op: '<' | '>' = m?.higher === false ? '>' : '<';
  const [threshold, setThreshold] = useState<string>('');
  const value = threshold !== '' ? +threshold : end ? end[2] : NaN;
  const levers = cfg.interventions.filter(i => i.kind === 'lever').flatMap(i => i.values.flatMap(v => v.kind === 'number' && v.path.startsWith('v|') ? [{ id: v.path.split('|')[1], label: `${i.label}: ${v.label}`, unit: v.unit, step: v.step }] : []));
  const [lever, setLever] = useState(levers[0]?.id ?? ''), [change, setChange] = useState(String(levers[0]?.step ?? 1));
  const watched = d.draft.warnings.some(w => w.node === n.id && w.fork === fork);
  const backed = d.draft.backups.some(b => b.node === n.id && b.fork === fork);
  if (!usable.length) return null;

  const addWatch = () => {
    if (!m || !end || !Number.isFinite(value)) return;
    d.addWarning({ id: newId('w'), metric: m.id, label: m.label, unit: m.unit, op, threshold: value, week: end[0], catalogue: CATALOGUE[m.id] ?? null, fork, node: n.id, nodeTitle: title,
      text: `If by week ${end[0]} ${m.label.toLowerCase()} is ${op === '<' ? 'below' : 'above'} ${fmt(value, m.unit)}, we are on the branch «${title}»` });
    setOpen(null);
  };
  const addBackup = () => {
    const f = m ? FORMULA[m.id] : undefined, l = levers.find(x => x.id === lever);
    if (!m || !f || !l || !Number.isFinite(value) || !Number.isFinite(+change)) return;
    const id = newId('b');
    d.addBackup({ id, label: `If ${m.label.toLowerCase()} ${op} ${fmt(value, m.unit)}: ${l.label} ${+change >= 0 ? '+' : ''}${change}`, fork, node: n.id,
      when: { value: f, label: m.label, op, threshold: value }, then: { variable: l.id, label: l.label, change: +change, unit: l.unit } });
    // the formula model runs it at once: the case recomputes, the notice reports the effect
    c.change({ [`bk|${id}`]: `${f}|${op}|${value}|${l.id}|${+change}` }, `Backup plan: ${l.label} ${+change >= 0 ? '+' : ''}${change} if ${m.label.toLowerCase()} ${op} ${fmt(value, m.unit)}`, 'sim', fork);
    setOpen(null);
  };

  return <div className="cs-sheet-section cs-node-actions">
    <h3>Act on this branch</h3>
    <div className="cs-node-action-buttons">
      <button type="button" className={`button ${open === 'watch' ? 'is-on' : ''}`} onClick={() => setOpen(open === 'watch' ? null : 'watch')}><Eye size={14} aria-hidden />{watched ? 'Watch one more sign' : 'Watch for this branch'}</button>
      <button type="button" className={`button ${open === 'backup' ? 'is-on' : ''}`} onClick={() => setOpen(open === 'backup' ? null : 'backup')}><LifeBuoy size={14} aria-hidden />{backed ? 'Add another backup plan' : 'If this happens, do…'}</button>
    </div>
    {open && <div className="cs-node-form">
      <label>Sign<select value={metric} onChange={e => { setMetric(e.target.value); setThreshold(''); }}>{usable.filter(x => open === 'watch' || FORMULA[x.id]).map(x => <option key={x.id} value={x.id}>{x.label}</option>)}</select></label>
      <label>{op === '<' ? 'Below' : 'Above'}<input type="number" step="any" value={threshold !== '' ? threshold : end ? String(+end[2].toPrecision(3)) : ''} onChange={e => setThreshold(e.target.value)} /></label>
      {open === 'watch' && end && <span className="cs-muted">by week {end[0]} · the median of this branch is {fmt(end[2], m!.unit)}</span>}
      {open === 'backup' && <>
        <label>Then<select value={lever} onChange={e => { setLever(e.target.value); setChange(String(levers.find(l => l.id === e.target.value)?.step ?? 1)); }}>{levers.map(l => <option key={l.id} value={l.id}>{l.label}</option>)}</select></label>
        <label>Change by<input type="number" step="any" value={change} onChange={e => setChange(e.target.value)} /></label>
      </>}
      <button type="button" className="button button-primary" onClick={open === 'watch' ? addWatch : addBackup}>{open === 'watch' ? 'Add the warning sign' : 'Add and recompute'}</button>
      {open === 'backup' && <p className="cs-muted cs-node-note">The formula model applies it in every fork, in the scenarios where the sign holds (noted for {forkLabel(cfg, fork)}). The actor simulation does not run backup plans yet.</p>}
    </div>}
  </div>;
}
