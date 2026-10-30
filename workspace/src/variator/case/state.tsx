// State of one case page: the config defaults, the presets of the active hypotheses, and the user's edits as a separate
// layer (saved in the browser when it can). Every change goes through `change`: it records an undo patch, recomputes
// (a new simulation, or only the instant ranking), and reports the effect as a toast. While the actors answer for the
// page (`noticeSource` «actors», WP25 R1), the edits are collected instead (`takeChanges`) and the notice comes from the
// actor rerun (case/actorAuto.ts): one source of numbers, the formula notice only when the actors cannot answer.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { BestPath, CaseConfig, CaseRun, Caution, ChangeReport, EditValue, Edits, Plan, Ranking, Snapshot } from './model/types';
import { source } from './model/source';
import { noticeFork } from './model/notice';
import { dropNoops } from './model/actorNotice';
import { forkLabel, forkNote, pBestScope, pct } from './ui/format';

export type ChangeKind = 'sim' | 'rank' | 'quiet';
export type Patch = Record<string, EditValue | null>;
export interface ToastItem { id: number; title: string; context: string; report: ChangeReport; planPending: boolean; undo: (() => void) | null; createdAt: number }
/** Edits made since the last notice on the actors: their titles, the patch that undoes them all, the fork they are
 *  about (null: none named) and whether one needed a new simulation. */
export interface ChangeBatch { what: string; undo: Patch; fork: string | null; kind: ChangeKind }

export interface CaseApi {
  cfg: CaseConfig;
  edits: Edits; presets: Edits;
  value: <T extends EditValue = EditValue>(path: string) => T;
  defaultOf: (path: string) => EditValue | undefined;
  /** The value before the user's edit (preset or default). */
  baseOf: (path: string) => EditValue | undefined;
  isEdited: (path: string) => boolean;
  fromPreset: (path: string) => boolean;
  editedPaths: (match: (path: string) => boolean) => string[];
  /** `fork`: the fork the change is about (its notice reports for it); default the plan's fork. */
  change: (patch: Patch, what: string, kind: ChangeKind, fork?: string) => void;
  resetPaths: (paths: string[], what: string) => void;
  resetAll: () => void;
  run: CaseRun | null; status: 'loading' | 'computing' | 'ready' | 'error'; error: string;
  /** Edits wait for a recompute: the results below are of the previous state. */
  stale: boolean;
  ranking: Ranking | null; best: BestPath | null;
  weights: Record<string, number>; caution: Caution;
  hypotheses: string[]; forksOn: string[];
  /** The fork of the plan; `timelineChosen`: picked by the user (else the formula leader, «best»: the page shows the
   *  leader of its source instead, see numbers.planFork). */
  timelineFork: string; timelineChosen: boolean; setTimelineFork: (fork: string) => void;
  compare: boolean; setCompare: (on: boolean) => void;
  original: { run: CaseRun; ranking: Ranking } | null;
  toasts: ToastItem[]; dismissToast: (id: number) => void;
  /** Who reports the edits: the formula model (its own notices) or the actors (the auto rerun, see takeChanges). */
  setNoticeSource: (s: 'formula' | 'actors') => void;
  /** The edits since the last notice on the actors (and forgets them); null: none. */
  takeChanges: () => ChangeBatch | null;
  pushNotice: (t: Omit<ToastItem, 'id' | 'createdAt'>) => number;
  storageOk: boolean;
  plans: Record<string, Plan | 'pending' | undefined>;
  requestPlan: (fork: string) => void;
}

const Ctx = createContext<CaseApi | null>(null);
export const useCase = () => { const c = useContext(Ctx); if (!c) throw new Error('useCase outside CaseProvider'); return c; };
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
let toastSeq = 0;
/** Steps of one control merge: «Engineers to hire: 5 → 6» then «… 6 → 7» reads «… 5 → 7». */
function mergeWhat(ws: string[], what: string) {
  const last = ws[ws.length - 1], m1 = last && /^(.*): (.+) → (.+)$/.exec(last), m2 = /^(.*): (.+) → (.+)$/.exec(what);
  if (m1 && m2 && m1[1] === m2[1] && m1[3] === m2[2]) ws[ws.length - 1] = `${m1[1]}: ${m1[2]} → ${m2[3]}`; else ws.push(what);
}

export function CaseProvider({ cfg, children }: { cfg: CaseConfig; children: ReactNode }) {
  const [edits, setEdits] = useState<Edits>(() => source.loadState(cfg.id) ?? {});
  const [storageOk, setStorageOk] = useState(() => { try { localStorage.setItem('variator:probe', '1'); localStorage.removeItem('variator:probe'); return true; } catch { return false; } });
  const [run, setRun] = useState<CaseRun | null>(null);
  const [status, setStatus] = useState<CaseApi['status']>('loading');
  const [error, setError] = useState('');
  const [stale, setStale] = useState(false);
  const [timelineFork, setTimelineForkState] = useState<string>('best');
  const [compare, setCompareState] = useState(false);
  const [original, setOriginal] = useState<CaseApi['original']>(null);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [plans, setPlans] = useState<CaseApi['plans']>({});
  const editsRef = useRef(edits), runRef = useRef<CaseRun | null>(null), reqRef = useRef(0), timer = useRef<number | undefined>(undefined);
  /** `fork`: the fork the change is about (null: none named); `snapFork`: the fork of the «before» snapshot. */
  const pending = useRef<{ before: Snapshot | null; what: string[]; undo: Patch; fork: string | null; snapFork: string | null } | null>(null);
  const rankPending = useRef<{ before: Edits; what: string[]; undo: Patch } | null>(null), rankTimer = useRef<number | undefined>(undefined);
  const noticeSource = useRef<'formula' | 'actors'>('formula'), batch = useRef<{ what: string[]; undo: Patch; fork: string | null; kind: ChangeKind } | null>(null);

  const hypotheses = useMemo(() => cfg.hypotheses.filter(h => (edits[`hyp|${h.id}`] ?? cfg.defaults[`hyp|${h.id}`]) === true).map(h => h.id), [cfg, edits]);
  const presets = useMemo(() => source.presetValues(cfg, hypotheses), [cfg, hypotheses]);
  const valueIn = useCallback((e: Edits, path: string) => (path in e ? e[path] : path in presets ? presets[path] : cfg.defaults[path]), [cfg, presets]);
  const value = useCallback(<T extends EditValue = EditValue>(path: string) => valueIn(edits, path) as T, [edits, valueIn]);
  const weightsOf = useCallback((e: Edits) => Object.fromEntries(cfg.criteria.map(c => [c.id, Number(valueIn(e, `w|${c.id}`) ?? 0)])), [cfg, valueIn]);
  const weights = useMemo(() => weightsOf(edits), [weightsOf, edits]);
  const caution = (value('caution') ?? 'margin') as Caution;
  const forksOn = useMemo(() => cfg.forks.filter(f => f.baseline || value(`fork|${f.id}`) !== false).map(f => f.id), [cfg, value]);

  const ranking = useMemo(() => run ? source.rank(run, cfg, weights, caution) : null, [run, cfg, weights, caution]);
  const best = useMemo(() => run && ranking ? source.bestPath(run, ranking, cfg, hypotheses) : null, [run, ranking, cfg, hypotheses]);
  const fork = (r: Ranking | null, f = timelineFork) => f === 'best' || !r?.order.includes(f) ? r?.order[0] ?? cfg.forks[0].id : f;
  runRef.current = run;

  const pushToast = useCallback((t: Omit<ToastItem, 'id' | 'createdAt'>) => {
    const id = ++toastSeq;
    setToasts(xs => [{ ...t, report: dropNoops(t.report), id, createdAt: Date.now() }, ...xs].slice(0, 3));
    return id;
  }, []);
  const dismissToast = useCallback((id: number) => setToasts(xs => xs.filter(t => t.id !== id)), []);

  const persist = useCallback((next: Edits) => { setStorageOk(source.saveState(cfg.id, next)); }, [cfg.id]);

  const recompute = useCallback((next: Edits) => {
    const req = ++reqRef.current;
    setStatus(s => s === 'loading' ? 'loading' : 'computing');
    source.simulate(cfg, next).then(r => {
      if (req !== reqRef.current) return;
      setRun(r); setStatus('ready'); setStale(false); setError('');
      const p = pending.current;
      pending.current = null;
      if (!p) return;
      const rk = source.rank(r, cfg, weightsOf(next), (valueIn(next, 'caution') ?? 'margin') as Caution);
      // the same fork before and after: the one the «before» snapshot describes, while it is still compared
      const nf = noticeFork(p.snapFork, rk.order, fork(rk)), f = nf.fork, after = source.snapshot(r, rk, cfg, f);
      const report = p.before && nf.comparable ? source.diff(p.before, after, r, cfg) : { effect: [], required: [], recommended: [], supporting: [], risks: [], tone: 'neutral' as const };
      const missed = report.pending ?? r.goals.some(g => (g.p[f] ?? 1) < 0.8);
      const undoPatch = p.undo, what = p.what.join('; ');
      const note = forkNote(cfg, f, path => valueIn(next, path), path => path in next);
      const id = pushToast({ title: what, context: `For ${forkLabel(cfg, f)}${note ? ` (${note})` : ''}`, report, planPending: missed, undo: () => changeRef.current(undoPatch, `Undo: ${what}`, 'sim', p.fork ?? undefined) });
      if (missed) source.solvePlan(cfg, next, f).then(plan => {
        setPlans(ps => ({ ...ps, [f]: plan }));
        const lines = source.requiredLines(cfg, plan);
        setToasts(xs => xs.map(t => t.id !== id ? t : { ...t, planPending: false, report: { ...t.report, required: lines.length ? lines : plan.met ? ['Apply the plan in «Plan»: then all milestones and goals are met.'] : ['No plan with these levers meets all of them. Move the dates (see Recommended) or choose another fork.'] } }));
      }).catch(() => setToasts(xs => xs.map(t => t.id !== id ? t : { ...t, planPending: false })));
      else if (!report.required.length) setToasts(xs => xs.map(t => t.id !== id ? t : { ...t, report: { ...t.report, required: ['Nothing: all milestones and goals are met.'] } }));
    }).catch(e => { if (req === reqRef.current) { setStatus('error'); setError(e instanceof Error ? e.message : String(e)); } });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg, weightsOf, valueIn, pushToast, timelineFork]);

  const change = useCallback((patch: Patch, what: string, kind: ChangeKind, about?: string) => {
    const prev = editsRef.current, next: Edits = { ...prev }, undo: Patch = {};
    for (const [path, v] of Object.entries(patch)) {
      undo[path] = path in prev ? prev[path] : null;
      const base = path in presets ? presets[path] : cfg.defaults[path];
      if (v === null || same(v, base)) delete next[path]; else next[path] = v;
    }
    if (same(prev, next)) return;
    editsRef.current = next;
    setEdits(next); persist(next);
    // the actors answer for the page: collect the edit for their notice; the formula model still recomputes (the blocks
    // it alone computes, and the fallback), without a notice of its own
    const onActors = noticeSource.current === 'actors' && kind !== 'quiet';
    if (onActors) {
      if (!batch.current) batch.current = { what: [], undo: {}, fork: about ?? null, kind };
      const b = batch.current;
      mergeWhat(b.what, what);
      for (const [k, v] of Object.entries(undo)) if (!(k in b.undo)) b.undo[k] = v;
      if (kind === 'sim') b.kind = 'sim';
      if (about && !b.fork) b.fork = about;
    }
    if (kind === 'sim' && onActors) {
      setStale(true); setPlans({});
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => recompute(editsRef.current), 320);
    } else if (kind === 'sim') {
      const r = runRef.current, rk = r ? source.rank(r, cfg, weightsOf(prev), (valueIn(prev, 'caution') ?? 'margin') as Caution) : null;
      const f0 = about && rk?.order.includes(about) ? about : fork(rk);
      if (!pending.current) pending.current = { before: r && rk ? source.snapshot(r, rk, cfg, f0) : null, what: [], undo: {}, fork: about && rk?.order.includes(about) ? about : null, snapFork: r && rk ? f0 : null };
      mergeWhat(pending.current.what, what);
      for (const [k, v] of Object.entries(undo)) if (!(k in pending.current.undo)) pending.current.undo[k] = v;
      setStale(true); setPlans({});
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => recompute(editsRef.current), 320);
    } else if (kind === 'rank' && runRef.current && !onActors) {
      // a slider moves in many small steps: one notice once it settles, from the first «before» to the last «after»
      if (!rankPending.current) rankPending.current = { before: prev, what: [], undo: {} };
      const rp = rankPending.current;
      rp.what = [...rp.what.filter(w => w.split(':')[0] !== what.split(':')[0]), what];
      for (const [k, v] of Object.entries(undo)) if (!(k in rp.undo)) rp.undo[k] = v;
      window.clearTimeout(rankTimer.current);
      rankTimer.current = window.setTimeout(() => {
        const r = runRef.current, p = rankPending.current;
        rankPending.current = null;
        if (!r || !p) return;
        const now = editsRef.current;
        const before = source.rank(r, cfg, weightsOf(p.before), (valueIn(p.before, 'caution') ?? 'margin') as Caution);
        const after = source.rank(r, cfg, weightsOf(now), (valueIn(now, 'caution') ?? 'margin') as Caution);
        const b0 = before.order[0], a0 = after.order[0], effect: string[] = [], of = `(${pBestScope(after.order.length)})`;
        if (b0 !== a0) effect.push(`Leader: ${forkLabel(cfg, b0)} → ${forkLabel(cfg, a0)}`);
        if (Math.abs(after.pBest[a0] - (before.pBest[a0] ?? 0)) >= 0.01) effect.push(`P(best) of ${forkLabel(cfg, a0)} ${of}: ${pct(before.pBest[a0] ?? 0)} → ${pct(after.pBest[a0])}`);
        if (b0 === a0 && after.order.some((f, i) => before.order[i] !== f)) effect.push(`New order below the leader: ${after.order.slice(1).map(f => forkLabel(cfg, f)).join(', ')}`);
        const risks = b0 !== a0 && after.pBest[a0] < 0.5 ? [`The new leader is best in only ${pct(after.pBest[a0])} of the scenarios ${of}: a close call`] : [];
        const what = p.what.join('; '), undoPatch = p.undo;
        pushToast({ title: what, context: 'Instant: the ranking only, no new simulation', report: { effect, required: [], recommended: b0 !== a0 ? [`Read the Answer again: the strategy now names ${forkLabel(cfg, a0)}`] : [], supporting: [], risks, tone: risks.length ? 'bad' : effect.length ? 'good' : 'neutral' }, planPending: false, undo: () => changeRef.current(undoPatch, `Undo: ${what}`, 'rank') });
      }, 450);
    }
  }, [cfg, presets, persist, recompute, weightsOf, valueIn, pushToast]);
  const changeRef = useRef(change);
  changeRef.current = change;

  // first run
  useEffect(() => { recompute(editsRef.current); return () => window.clearTimeout(timer.current); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const resetPaths = useCallback((paths: string[], what: string) => {
    const e = editsRef.current, edited = paths.filter(p => p in e);
    if (!edited.length) return;
    const kind: ChangeKind = edited.some(p => /^(v|o|r|rm|fork|hyp|goal|sim|bk)\|/.test(p)) ? 'sim' : edited.some(p => /^w\||^caution$|^priority$/.test(p)) ? 'rank' : 'quiet';
    change(Object.fromEntries(edited.map(p => [p, null])), what, kind);
  }, [change]);
  const resetAll = useCallback(() => {
    const prev = editsRef.current;
    if (!Object.keys(prev).length) return;
    change(Object.fromEntries(Object.keys(prev).map(p => [p, null])), 'Returned to the original', 'sim');
  }, [change]);

  const setCompare = useCallback((on: boolean) => {
    setCompareState(on);
    if (on && !original) source.simulate(cfg, {}).then(r => {
      const d = cfg.defaults, w = Object.fromEntries(cfg.criteria.map(c => [c.id, Number(d[`w|${c.id}`] ?? 0)]));
      setOriginal({ run: r, ranking: source.rank(r, cfg, w, (d.caution ?? 'margin') as Caution) });
    }).catch(() => setCompareState(false));
  }, [cfg, original]);

  const requestPlan = useCallback((f: string) => {
    setPlans(ps => ps[f] ? ps : { ...ps, [f]: 'pending' });
    source.solvePlan(cfg, editsRef.current, f).then(plan => setPlans(ps => ({ ...ps, [f]: plan }))).catch(() => setPlans(ps => ({ ...ps, [f]: undefined })));
  }, [cfg]);

  const api: CaseApi = {
    cfg, edits, presets, value,
    defaultOf: path => cfg.defaults[path],
    baseOf: path => path in presets ? presets[path] : cfg.defaults[path],
    isEdited: path => path in edits,
    fromPreset: path => !(path in edits) && path in presets,
    editedPaths: match => Object.keys(edits).filter(match),
    change, resetPaths, resetAll,
    run, status, error, stale, ranking, best, weights, caution, hypotheses, forksOn,
    timelineFork: fork(ranking), timelineChosen: timelineFork !== 'best' && !!ranking?.order.includes(timelineFork), setTimelineFork: f => setTimelineForkState(f),
    compare, setCompare, original, toasts, dismissToast, storageOk, plans, requestPlan,
    setNoticeSource: s => { noticeSource.current = s; if (s === 'formula') batch.current = null; },
    takeChanges: () => {
      const b = batch.current;
      batch.current = null;
      return b && b.what.length ? { what: b.what.join('; '), undo: b.undo, fork: b.fork, kind: b.kind } : null;
    },
    pushNotice: pushToast,
  };
  return <Ctx.Provider value={api}>{children}</Ctx.Provider>;
}
