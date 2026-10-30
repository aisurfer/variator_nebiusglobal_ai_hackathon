// The decision of a case page: what the user MAKES of the analysis. A draft collects warning signs (from a split of the
// event tree), backup plans (a lever that starts only if a condition holds), fixes (from a bad day) and key-person
// risks; «Decide and commit» freezes it with the chosen fork. Stored by the workspace server (/api/decisions, shared);
// while that is unreachable, the browser keeps it (localStorage) and says so.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { apiUrl } from '../../appUrl';

/** «If by week 12 cash is below $1.4M, we are on the bad branch»: checked against the real data in the Brief. */
export interface WarningSign {
  id: string; metric: string; label: string; unit: string; op: '<' | '>'; threshold: number; week: number;
  /** The catalogue metric of the Brief that measures it (null: not measured live yet). */
  catalogue: string | null;
  fork: string; node: string; nodeTitle: string; text: string;
}
/** «If team load > 1.05, then hire 2 more»: an effect rule of the formula model (the actors do not run it yet). */
export interface BackupPlan {
  id: string; label: string; fork: string; node: string;
  when: { value: string; label: string; op: '<' | '>'; threshold: number };
  then: { variable: string; label: string; change: number; unit: string };
}
/** A fix of a cause seen on a bad day: a task with an owner, or a key-person risk with its mitigation. */
export interface Fix {
  id: string; kind: 'task' | 'risk'; text: string; owner: string | null; person: string | null; entityId: string | null;
  fork: string; day: string; mitigation?: string;
}
export interface DecisionDraft { warnings: WarningSign[]; backups: BackupPlan[]; fixes: Fix[] }
export interface CommittedDecision extends DecisionDraft {
  fork: string; forkLabel: string; committedAt: string; pBest: number | null;
  /** The number of forks its P(best) was computed over (absent in decisions committed before it was kept). */
  pBestOf?: number | null;
  why: string;
}
interface Stored { draft: DecisionDraft | null; committed: CommittedDecision | null }

const EMPTY: DecisionDraft = { warnings: [], backups: [], fixes: [] };
const local = (caseId: string) => `variator.decision.${caseId}`;
export const newId = (p: string) => `${p}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

async function load(caseId: string): Promise<{ data: Stored; shared: boolean }> {
  try {
    const r = await fetch(apiUrl(`/api/decisions/${encodeURIComponent(caseId)}`));
    if (r.ok) return { data: await r.json() as Stored, shared: true };
  } catch { /* the browser copy below */ }
  try { return { data: JSON.parse(localStorage.getItem(local(caseId)) ?? 'null') ?? { draft: null, committed: null }, shared: false }; } catch { return { data: { draft: null, committed: null }, shared: false }; }
}

async function save(caseId: string, data: Stored): Promise<boolean> {
  try { localStorage.setItem(local(caseId), JSON.stringify(data)); } catch { /* private mode */ }
  try {
    const r = await fetch(apiUrl(`/api/decisions/${encodeURIComponent(caseId)}`), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    return r.ok;
  } catch { return false; }
}

export interface DecisionApi {
  draft: DecisionDraft; committed: CommittedDecision | null; shared: boolean; loaded: boolean;
  addWarning: (w: WarningSign) => void; addBackup: (b: BackupPlan) => void; addFix: (f: Fix) => void;
  remove: (id: string) => void; commit: (c: Omit<CommittedDecision, keyof DecisionDraft | 'committedAt'>) => void; reopen: () => void;
}
const Ctx = createContext<DecisionApi | null>(null);
export const useDecision = () => { const c = useContext(Ctx); if (!c) throw new Error('useDecision outside DecisionProvider'); return c; };

export function DecisionProvider({ caseId, children }: { caseId: string; children: ReactNode }) {
  const [data, setData] = useState<Stored>({ draft: null, committed: null });
  const [shared, setShared] = useState(true), [loaded, setLoaded] = useState(false);
  const latest = useRef(data);
  useEffect(() => { let live = true; void load(caseId).then(x => { if (!live) return; latest.current = x.data; setData(x.data); setShared(x.shared); setLoaded(true); }); return () => { live = false; }; }, [caseId]);
  const put = useCallback((next: Stored) => { latest.current = next; setData(next); void save(caseId, next).then(setShared); }, [caseId]);
  const draft = data.draft ?? EMPTY;
  const edit = (f: (d: DecisionDraft) => DecisionDraft) => put({ ...latest.current, draft: f(latest.current.draft ?? EMPTY) });
  const api: DecisionApi = {
    draft, committed: data.committed, shared, loaded,
    addWarning: w => edit(d => ({ ...d, warnings: [...d.warnings, w] })),
    addBackup: b => edit(d => ({ ...d, backups: [...d.backups, b] })),
    addFix: f => edit(d => ({ ...d, fixes: [...d.fixes, f] })),
    remove: id => edit(d => ({ warnings: d.warnings.filter(x => x.id !== id), backups: d.backups.filter(x => x.id !== id), fixes: d.fixes.filter(x => x.id !== id) })),
    commit: c => put({ ...latest.current, committed: { ...(latest.current.draft ?? EMPTY), ...c, committedAt: new Date().toISOString() } }),
    reopen: () => put({ ...latest.current, committed: null }),
  };
  return <Ctx.Provider value={api}>{children}</Ctx.Provider>;
}

/** The committed decision of a case, read once (the Brief shows its warning signs). */
export async function readCommitted(caseId: string): Promise<CommittedDecision | null> { return (await load(caseId)).data.committed; }
