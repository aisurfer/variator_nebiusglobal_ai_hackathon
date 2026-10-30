// The actor results of a case page, shared by the verdict and the future sections without a provider: one small store
// per case (loaded once from the VARIATOR API), read with useSyncExternalStore. A rerun posts the page state to WP18's
// month route; its result is shown while the page state still matches it (simKey / rankKey), else it is marked stale.
// Auto mode (WP25 R1, the case page reruns after each edit): while a rerun for the page state is due or running, the
// previous result stays the answer (`pending`, the states read «current» or «defaults»); a newer request aborts an older
// one. Only when the route is missing or the rerun for this state failed is the result «stale».
import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react';
import { useCase } from '../state';
import type { CaseConfig, Preview } from '../model/types';
import { monthBodyOf } from '../model/source';
import { DEFAULT_KEY, forkKey, futureView, rankKey, simKey, verdictOf } from './adapt';
import { loadActors, probeRerun, rerunMonth, type LoadedActors } from './load';
import type { ActorMonth, DayFile, FutureView, VerdictView } from './types';
import { eventTreeView, isEventTree, type EventTreeRaw, type EventTreeView } from './eventTreeModel';

export type RerunStatus = 'idle' | 'running' | 'done' | 'error';
/** Paths a fork for a live week tree. */
export const WEEK_N = 300;
interface ActorRecord {
  status: 'loading' | 'ready' | 'missing' | 'error';
  error: string;
  loaded: LoadedActors | null;
  /** The month route exists (null: not probed yet). */
  rerunAvailable: boolean | null;
  /** `scope`: «month» (the verdict's rerun) or «week» (the week for your edits: the month plus the week trees). */
  /** `keys`: the page state the rerun was for (an error is about that state only). */
  rerun: { status: RerunStatus; startedAt: number; ms: number | null; message: string; scope: 'month' | 'week'; keys?: { simKey: string; rankKey: string } };
  result: { month: ActorMonth; simKey: string; rankKey: string; ms: number } | null;
  /** Event trees computed on demand (live `weekForks`), by fork and by the fork's own simulation inputs (forkKey). */
  weekCache: Record<string, Record<string, EventTreeRaw>> | null;
  /** The on-demand week computation (Week asks for the forks it shows that have no tree yet). */
  weekRun: { status: RerunStatus; startedAt: number; ms: number | null; message: string; forks: string[]; simKey: string };
}

const records = new Map<string, ActorRecord>();
/** The rerun in flight per case: a newer page state aborts it (its answer would be stale on arrival). */
const inflight = new Map<string, { ctrl: AbortController; simKey: string; rankKey: string }>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach(l => l());
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
const EMPTY: ActorRecord = { status: 'loading', error: '', loaded: null, rerunAvailable: null, rerun: { status: 'idle', startedAt: 0, ms: null, message: '', scope: 'month' }, result: null,
  weekCache: null, weekRun: { status: 'idle', startedAt: 0, ms: null, message: '', forks: [], simKey: '' } };
const set = (id: string, patch: Partial<ActorRecord>) => { records.set(id, { ...(records.get(id) ?? EMPTY), ...patch }); emit(); };

function ensure(id: string) {
  if (records.has(id)) return;
  records.set(id, EMPTY);
  loadActors(id)
    .then(loaded => set(id, loaded ? { status: 'ready', loaded } : { status: 'missing' }))
    .catch(e => set(id, { status: 'error', error: e instanceof Error ? e.message : String(e) }));
  probeRerun(id).then(ok => set(id, { rerunAvailable: ok }));
}

/** Test hook: forget every loaded case (e2e and unit tests start clean). */
export function resetActorStore() { records.clear(); emit(); }

/** «for the defaults»: the precomputed file of the case; «current»: a rerun for the page state as it is; «stale»: the page
 *  changed after the result shown was made. */
export type ActorState = 'defaults' | 'current' | 'stale';

export interface ActorsApi {
  status: ActorRecord['status'];
  /** The month result shown (rerun or defaults), null when there is no actor set. */
  month: ActorMonth | null;
  /** The future sections' data from the actors (null: none), and from the preview. */
  view: FutureView | null;
  /** State of the fans, the calendar, the tree and the examples; of the ranking (priorities count too). While a rerun
   *  for the page state is due or running (`pending`), the previous result is shown as the answer, so these read
   *  «current» or «defaults»; «stale» only when the actors cannot answer for this state (no route, or it failed). */
  state: ActorState; rankState: ActorState;
  /** The result shown is not of the page state yet and a rerun on the actors is due or running: show it dimmed. */
  pending: boolean;
  /** The result shown is not of the page state (pending or stale): the auto rerun's trigger. */
  behind: boolean;
  /** The tree and the day examples are of the defaults (the rerun did not return them). */
  treeFromDefaults: boolean;
  verdict: VerdictView | null;
  rerunAvailable: boolean | null;
  rerun: ActorRecord['rerun'];
  run: () => void;
  /** Rerun with the week trees (Week and Day for your edits). */
  runWeek: () => void;
  /** The week tree and the day examples shown are not those of the page state. */
  weekStale: boolean;
  /** «1,000 scenarios · learned from 12 months of history». */
  scope: string;
  /** The event trees of the current simulation state, by fork (the defaults' file, the rerun, or computed on demand). */
  eventTrees: Record<string, EventTreeView>;
  /** Computes the event trees of these forks for the page state (the live route's `weekForks`). */
  /** False when it did not start (another computation runs). */
  computeWeek: (forks: string[]) => boolean;
  weekRun: ActorRecord['weekRun'];
}

// The view of a month file is built once for the page (WP29): about thirty components read useActors, and each used to
// build its own on the main thread during the first view (≈0.7 s in all for case B's month file). The view is read-only.
let lastView: { cfg: CaseConfig; month: ActorMonth; base: ActorMonth | null; fromDefaults: boolean; day: DayFile | null; view: FutureView } | null = null;
function sharedView(cfg: CaseConfig, month: ActorMonth, base: ActorMonth | null, fromDefaults: boolean, day: DayFile | null): FutureView {
  const l = lastView;
  if (l && l.cfg === cfg && l.month === month && l.base === base && l.fromDefaults === fromDefaults && l.day === day) return l.view;
  // a rerun without trees or examples keeps those of the defaults (marked as such in their sections)
  const merged = fromDefaults && base ? { ...month, weekTrees: base.weekTrees, dayExamples: base.dayExamples, weekMetrics: base.weekMetrics, dayMetrics: base.dayMetrics } : month;
  const view = futureView(cfg, merged, day);
  lastView = { cfg, month, base, fromDefaults, day, view };
  return view;
}

export function useActors(): ActorsApi {
  const c = useCase(), id = c.cfg.id;
  useEffect(() => ensure(id), [id]);
  const rec = useSyncExternalStore(subscribe, () => records.get(id) ?? EMPTY, () => EMPTY);
  const sk = simKey(c.edits), rk = rankKey(c.edits);
  const base = rec.loaded?.month ?? null, res = rec.result;
  let month: ActorMonth | null = null, state: ActorState = 'defaults';
  if (res && res.simKey === sk) { month = res.month; state = 'current'; }
  else if (base && sk === DEFAULT_KEY) { month = base; state = 'defaults'; }
  else if (res || base) { month = res?.month ?? base; state = 'stale'; }
  let rankState: ActorState = !month ? 'defaults' : res && month === res.month && res.rankKey === rk ? 'current' : month === base && rk === DEFAULT_KEY ? 'defaults' : 'stale';
  // auto mode: a state the actors can still answer is pending, not stale (the previous result stays the answer)
  const behind = !!month && (state === 'stale' || rankState === 'stale');
  const failedHere = rec.rerun.status === 'error' && rec.rerun.keys?.simKey === sk && rec.rerun.keys?.rankKey === rk;
  const pending = behind && !!rec.loaded && rec.rerunAvailable !== false && !failedHere;
  if (pending) { const shown: ActorState = month === base ? 'defaults' : 'current'; if (state === 'stale') state = shown; if (rankState === 'stale') rankState = shown; }
  // a rerun without trees or examples keeps those of the defaults (marked as such in their sections)
  const treeFromDefaults = !!month && month !== base && !!base && !Object.keys(month.weekTrees ?? {}).length;
  const day = month === base || treeFromDefaults ? rec.loaded?.day ?? null : null;
  const view = useMemo(() => month ? sharedView(c.cfg, month, base, treeFromDefaults, day) : null, [c.cfg, month, base, treeFromDefaults, day]);
  const verdict = useMemo(() => month ? verdictOf(c.cfg, month, c.forksOn) : null, [c.cfg, month, c.forksOn]);
  const n = month?.n ?? 0, months = month?.provenance?.history?.months ?? 12;
  const scope = `${n.toLocaleString('en-US')} scenarios · learned from ${months} months of history`;

  /** A rerun for the page state as it is: the month (the verdict), or with `week` also the week trees and the bridge
   *  examples of the two best forks (WP18 `include: ["week"]`, a few seconds more: only when the week is asked for). */
  const rerun = useCallback((scope: 'month' | 'week') => {
    const r = records.get(id);
    if (!r || !r.loaded) return;
    const edits = c.edits, keys = { simKey: simKey(edits), rankKey: rankKey(edits) };
    // the same state already running: nothing to do; an older state: abort it, this one replaces it
    const prev = inflight.get(id);
    if (r.rerun.status === 'running' && prev) {
      if (prev.simKey === keys.simKey && prev.rankKey === keys.rankKey) return;
      prev.ctrl.abort();
    } else if (r.rerun.status === 'running') return;
    const ctrl = new AbortController(), mine = () => inflight.get(id)?.ctrl === ctrl;
    inflight.set(id, { ctrl, ...keys });
    const body = { ...monthBodyOf(id, edits, c.forksOn, r.loaded.month.n, r.loaded.month.seed), ...(scope === 'week' ? { include: ['week'] } : {}) };
    set(id, { rerun: { status: 'running', startedAt: performance.now(), ms: null, message: '', scope, keys } });
    rerunMonth(id, body, ctrl.signal)
      .then(({ month: m, ms }) => { if (!mine()) return; inflight.delete(id); set(id, { result: { month: m, ...keys, ms }, rerun: { status: 'done', startedAt: 0, ms, message: '', scope, keys } }); })
      .catch(e => { if (!mine()) return; inflight.delete(id); set(id, { rerun: { status: 'error', startedAt: 0, ms: null, message: e instanceof Error ? e.message : String(e), scope, keys } }); });
  }, [id, c.edits, c.forksOn]);
  const run = useCallback(() => rerun('month'), [rerun]);
  const runWeek = useCallback(() => rerun('week'), [rerun]);
  // the week tree and the examples shown are not those of the page state: the defaults' after a rerun of the month, or
  // the page changed since (the defaults themselves are precomputed: nothing to rerun there)
  const weekStale = !!month && simKey(c.edits) !== DEFAULT_KEY && (treeFromDefaults || state === 'stale');

  // event trees of the simulation state the page is in: the month shown when it is current (or the defaults' file), and
  // the trees computed on demand for this state
  // the event tree of each fork for ITS simulation inputs (its levers and the world): the precomputed one (n = 1,000)
  // while they are the defaults, else the one computed for them (an unrelated edit keeps a fork's tree)
  const keys = useMemo(() => Object.fromEntries(c.cfg.forks.map(f => [f.id, forkKey(c.cfg, c.edits, f.id)])), [c.cfg, c.edits]);
  const keyText = JSON.stringify(keys);
  const baseTrees = base?.eventTrees, cacheAll = rec.weekCache, live = res && res.simKey === sk ? res.month.eventTrees : undefined;
  const eventTrees = useMemo(() => {
    const out: Record<string, EventTreeView> = {};
    for (const f of c.cfg.forks.map(x => x.id)) {
      const k = keys[f];
      const t = k === DEFAULT_KEY ? baseTrees?.[f] : cacheAll?.[f]?.[k] ?? live?.[f];
      const v = t && isEventTree(t) ? eventTreeView(t) : null;
      if (v) out[f] = v;
    }
    return out;
  }, [keyText, baseTrees, cacheAll, live]); // eslint-disable-line react-hooks/exhaustive-deps
  const computeWeek = useCallback((forks: string[]): boolean => {
    const r = records.get(id);
    if (!r || !r.loaded || !forks.length || r.weekRun.status === 'running') return false;
    const edits = c.edits, keys = { simKey: simKey(edits), rankKey: rankKey(edits) };
    const forkKeys = Object.fromEntries(forks.map(f => [f, forkKey(c.cfg, edits, f)]));
    // 300 paths a fork keep the live tree within the route's 15 s on a busy machine (the precomputed trees have 1,000)
    const body = { ...monthBodyOf(id, edits, c.forksOn, r.loaded.month.n, r.loaded.month.seed), include: ['week'], weekForks: forks.slice(0, 3), weekN: WEEK_N };
    const t0 = performance.now();
    set(id, { weekRun: { status: 'running', startedAt: t0, ms: null, message: '', forks, simKey: keys.simKey } });
    // a run that outlasts the route's limit still finishes in the background and is cached: ask once more a bit later
    const ask = () => rerunMonth(id, body).catch(async (e: unknown) => {
      if (!/longer than|504|timed? ?out/i.test(e instanceof Error ? e.message : String(e))) throw e;
      await new Promise(res => window.setTimeout(res, 2500));
      return rerunMonth(id, body);
    });
    ask().then(({ month: m }) => {
      const ms = performance.now() - t0;
      const now = records.get(id)!, got = m.eventTrees ?? {}, cache = { ...(now.weekCache ?? {}) };
      for (const [f, t] of Object.entries(got)) cache[f] = { ...(cache[f] ?? {}), [forkKeys[f] ?? forkKey(c.cfg, edits, f)]: t };
      set(id, {
        weekCache: cache,
        // the answer is the month of this state too: the verdict and the months follow it (at the defaults the
        // precomputed file stays the answer; only its trees are added)
        ...(keys.simKey === DEFAULT_KEY ? {} : { result: { month: m, ...keys, ms } }),
        weekRun: { status: 'done', startedAt: 0, ms, message: Object.keys(got).length ? '' : 'The actor engine returned no event tree for these forks yet.', forks, simKey: keys.simKey },
      });
    }).catch(e => set(id, { weekRun: { status: 'error', startedAt: 0, ms: null, message: e instanceof Error ? e.message : String(e), forks, simKey: keys.simKey } }));
    return true;
  }, [id, c.cfg, c.edits, c.forksOn]);

  return { status: rec.status, month, view, state, rankState, pending, behind, treeFromDefaults, weekStale, verdict, rerunAvailable: rec.rerunAvailable, rerun: rec.rerun, run, runWeek, scope, eventTrees, computeWeek, weekRun: rec.weekRun };
}

/** The preview of the page (run.preview) in the same view type as the actor data. */
export function previewFuture(p: Preview, criteria: string[]): FutureView {
  return { ...p, preview: true, source: 'preview', n: 0, marked: {}, daySource: 'preview', criteria: p.monthMetrics.map(m => m.id).filter(m => criteria.includes(m)) };
}

/** The forks by rank, for every default picked by rank (charts, calendar, review gates): the actor ranking while it is
 *  current for the page state (the answer of the page), else the formula ranking. */
export function useRankOrder(): { order: string[]; actors: boolean } {
  const c = useCase(), a = useActors();
  const actorOrder = a.verdict && a.rankState !== 'stale' ? a.verdict.order.map(o => o.fork) : null;
  const formula = c.ranking?.order;
  return useMemo(() => actorOrder ? { order: actorOrder, actors: true } : { order: formula ?? [], actors: false },
    [actorOrder?.join('|'), formula?.join('|')]); // eslint-disable-line react-hooks/exhaustive-deps
}
