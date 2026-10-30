// «Model settings»: the analyst's drawer with every input of the model by block, the shocks and the simulation size.
// A value commits on Enter or when the field loses focus; each changed value is marked and has ↺.
import { useState } from 'react';
import { RotateCcw } from 'lucide-react';
import type { Dist } from '../model/types';
import { useCase } from '../state';
import { displayScale, displaySuffix } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { ModifiedMark, Segmented, Sheet } from '../ui/parts';

function NumInput({ value, onCommit, label, width = 76 }: { value: number; onCommit: (v: number) => void; label: string; width?: number }) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? String(+value.toFixed(4));
  const commit = () => { if (draft == null) return; const v = Number(draft.replace(',', '.')); setDraft(null); if (Number.isFinite(v) && v !== value) onCommit(v); };
  return <input className="cs-num-input" style={{ width }} inputMode="decimal" aria-label={label} value={shown} onChange={e => setDraft(e.target.value)} onBlur={commit} onKeyDown={e => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') setDraft(null); }} />;
}

function DistEditor({ path, unit, label }: { path: string; unit: string; label: string }) {
  const c = useCase(), d = c.value(path) as Dist, k = displayScale(unit);
  if (!d || typeof d !== 'object') return null;
  const set = (patch: Partial<{ value: number; low: number; mode: number; high: number }>) => {
    const next = { ...d, ...Object.fromEntries(Object.entries(patch).map(([key, v]) => [key, (v as number) / k])) } as Dist;
    c.change({ [path]: next }, `${label}: changed`, 'sim');
  };
  return <span className="cs-dist">
    {d.kind === 'fixed' ? <NumInput value={d.value * k} label={label} onCommit={v => set({ value: v })} />
      : <><NumInput value={d.low * k} label={`${label}, low`} onCommit={v => set({ low: v })} /><NumInput value={d.mode * k} label={`${label}, likely`} onCommit={v => set({ mode: v })} /><NumInput value={d.high * k} label={`${label}, high`} onCommit={v => set({ high: v })} /></>}
    <small>{displaySuffix(unit)}</small>
  </span>;
}

export function ModelSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const c = useCase(), cfg = c.cfg;
  const edited = c.editedPaths(p => /^(v|r|rm|o|sim)\|/.test(p));
  const n = Number(c.value('sim|n')), seed = Number(c.value('sim|seed'));
  return <Sheet open={open} onClose={onClose} eyebrow="FOR THE ANALYST · ALL INPUTS" title="Model settings" width={760}
    foot={<><span>{edited.length} changed value{edited.length === 1 ? '' : 's'} · amber: yours, lime: from a hypothesis</span>{edited.length > 0 && <button type="button" className="cs-text-btn" onClick={() => c.resetPaths(edited, 'Model settings: reset')}><RotateCcw size={13} />Reset all settings</button>}</>}>
    <p className="cs-sheet-lead">Every input of the model, the links between factors and the random shocks. A range is low – likely – high; each scenario draws one value from it. {cfg.help.modelSettings && <HelpTip entry={cfg.help.modelSettings} />}</p>
    <div className="cs-sheet-section">
      <h3>Simulation</h3>
      <div className="cs-settings-row"><span>Scenarios</span>
        <ModifiedMark path="sim|n" unit="count" label="Scenarios" format={v => Number(v).toLocaleString('en-US')}>
          <Segmented<string> size="sm" label="Scenarios" value={String(n)} options={['1024', '2048', '4096', '8192'].map(x => ({ id: x, label: Number(x).toLocaleString('en-US') }))} onChange={v => c.change({ 'sim|n': Number(v) }, `Scenarios: ${n.toLocaleString('en-US')} → ${Number(v).toLocaleString('en-US')}`, 'sim')} />
        </ModifiedMark></div>
      <div className="cs-settings-row"><span>Random seed</span><ModifiedMark path="sim|seed" unit="count" label="Seed" format={v => String(v)}><NumInput value={seed} label="Seed" onCommit={v => c.change({ 'sim|seed': Math.round(v) }, `Seed: ${seed} → ${Math.round(v)}`, 'sim')} /></ModifiedMark></div>
    </div>
    {cfg.blocks.map(b => {
      const inputs = cfg.inputs.filter(i => i.block === b.id);
      if (!inputs.length) return null;
      return <div key={b.id} className="cs-sheet-section">
        <h3>{b.label}<span className="cs-count">{inputs.length}</span></h3>
        <div className="cs-settings">{inputs.map(i => <div key={i.path} className="cs-settings-row">
          <span>{i.label}<small>{i.source}</small></span>
          <ModifiedMark path={i.path} unit={i.unit} label={i.label}><DistEditor path={i.path} unit={i.unit} label={i.label} /></ModifiedMark>
        </div>)}</div>
      </div>;
    })}
    <div className="cs-sheet-section">
      <h3>Shocks<span className="cs-count">{cfg.shocks.length}</span></h3>
      <div className="cs-settings">{cfg.shocks.map(s => {
        const p = Number(c.value(s.probabilityPath));
        return <div key={s.id} className="cs-settings-row">
          <span>{s.label}<small>chance a year · size when it happens</small></span>
          <span className="cs-dist">
            <ModifiedMark path={s.probabilityPath} unit="share" label={`${s.label}, chance`}><NumInput value={p * 100} label={`${s.label}, chance in percent`} width={60} onCommit={v => c.change({ [s.probabilityPath]: Math.min(1, Math.max(0, v / 100)) }, `${s.label}: chance ${Math.round(p * 100)}% → ${Math.round(v)}%`, 'sim')} /></ModifiedMark>
            <small>%</small>
            <ModifiedMark path={s.magnitudePath} unit={s.unit} label={`${s.label}, size`}><DistEditor path={s.magnitudePath} unit={s.unit} label={`${s.label}, size`} /></ModifiedMark>
          </span>
        </div>;
      })}</div>
    </div>
  </Sheet>;
}
