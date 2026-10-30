// The top bar of the case pages: the path (company / Decisions / case / current step), the fork date T and the
// «Simulation» badge while future content is on screen. The case page publishes its state to a tiny store; the shell
// renders these components through the page registry (pages.tsx → crumbs, tools).
import { useSyncExternalStore } from 'react';
import { CalendarClock, ChevronRight, Sparkles, Waypoints } from 'lucide-react';
import { longDate } from './ui/format';
import { appHref } from '../../appUrl';

export interface CaseChrome { caseTitle: string | null; step: string | null; forkDate: string | null; future: boolean; recomputing: boolean }
let state: CaseChrome = { caseTitle: null, step: null, forkDate: null, future: false, recomputing: false };
const listeners = new Set<() => void>();
export function setCaseChrome(patch: Partial<CaseChrome>) {
  const next = { ...state, ...patch };
  if (JSON.stringify(next) === JSON.stringify(state)) return;
  state = next;
  listeners.forEach(l => l());
}
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
const useChrome = () => useSyncExternalStore(subscribe, () => state, () => state);

export function CaseCrumbs({ onCases }: { onCases: () => void }) {
  const s = useChrome();
  return <span className="cs-crumbs">
    <span className="cs-crumb-group">Decisions</span><ChevronRight size={13} className="cs-crumb-sep" />
    {s.caseTitle ? <>
      <a href={appHref('/cases')} className="cs-crumb-link" onClick={e => { e.preventDefault(); onCases(); }}><Waypoints size={14} />Cases</a><ChevronRight size={13} className="cs-crumb-sep" />
      <span className="cs-crumb-case">{s.caseTitle}</span>
      {s.step && <><ChevronRight size={13} className="cs-crumb-sep" /><strong>{s.step}</strong></>}
    </> : <strong>Cases</strong>}
  </span>;
}

export function CaseTools() {
  const s = useChrome();
  if (!s.forkDate) return null;
  return <div className="cs-topbar-tools">
    <span className="cs-tdate" title="The fork date T: nothing after it is known; everything later is simulated"><CalendarClock size={15} /><span>Fork date T</span><strong>{longDate(s.forkDate)}</strong></span>
    {s.future && <span className="cs-badge is-sim cs-topbar-sim"><Sparkles size={12} />Simulation{s.recomputing ? ' · recomputing' : ''}</span>}
  </div>;
}
