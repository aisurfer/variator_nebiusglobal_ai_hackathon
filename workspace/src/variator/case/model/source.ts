// The data source of the case pages: the WP13a engine (workspace/src/variator/engine) over a case config
// (workspace/config/cases/<id>.json, listed in config/cases/index.json) and its preview file. The page keeps the
// user's edits as one flat layer of paths (see `Edits`); this module turns them into the engine's CaseState, runs the
// engine (Web Worker), and maps its results to the view types in ./types. ./view builds the page's view of a case.
import {
  bestPath as engineBestPath, breakText, cautionWords, corr, defaultState, diffReports, effectiveModel, evaluateTimeline, loadState as engineLoadState,
  noEmoji, peopleText, peopleValue, rankRun, saveState as engineSaveState, snapshot as engineSnapshot, suggestDates as engineSuggestDates, variableLabel,
  type CaseSnapshot, type CaseState, type CritStats, type ExportedModel, type Plan as EnginePlan, type Ranking as EngineRanking, type SimRun, type TimelineResult,
} from '../../engine';
import { createEngineClient, type EngineClient } from '../../engine/client';
import { readCase, readIndex } from './cases';
import { buildRuntime, lowFirst, MODE_WORDS, type CaseRuntime } from './view';
import { monthBody, toState } from '../actors/pageState';
import { formulaScoreSpread } from '../actors/scoreSpread';
import type {
  BestPath, CaseConfig, CaseRun, CaseSummary, Caution, ChangeReport, Dist, Edits, EditValue, Plan, Ranking, Risk, RiskCalendarRow, Snapshot, Stat, StepId, Unit,
} from './types';


/** The best path with the criteria on which the decision and the runner-up are equal (a superset of model/types BestPath). */
export type BestPathView = BestPath & { ties: string[] };
const listWords = (xs: string[]) => xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;

export interface CaseSource {
  kind: 'mock' | 'engine';
  listCases(): Promise<CaseSummary[]>;
  loadCase(id: string): Promise<CaseConfig>;
  /** A full recompute (off the main thread). */
  simulate(cfg: CaseConfig, edits: Edits): Promise<CaseRun>;
  /** Instant: priorities only, no new simulation. */
  rank(run: CaseRun, cfg: CaseConfig, weights: Record<string, number>, caution: Caution): Ranking;
  /** `ties`: criteria (labels) where the decision and the runner-up are equal within the meaningful difference. */
  bestPath(run: CaseRun, ranking: Ranking, cfg: CaseConfig, hypotheses: string[]): BestPathView;
  solvePlan(cfg: CaseConfig, edits: Edits, fork: string): Promise<Plan>;
  /** The «Required» lines of a notice once the plan is known. */
  requiredLines(cfg: CaseConfig, plan: Plan): string[];
  suggestDates(run: CaseRun, cfg: CaseConfig, fork: string): Edits;
  snapshot(run: CaseRun, ranking: Ranking, cfg: CaseConfig, fork: string): Snapshot;
  diff(before: Snapshot, after: Snapshot, run: CaseRun, cfg: CaseConfig): ChangeReport;
  presetValues(cfg: CaseConfig, hypotheses: string[]): Edits;
  /** Rows of the risk calendar of a fork: the preview's checks, then the milestones of the live run missed at their month. */
  calendar(run: CaseRun, cfg: CaseConfig, fork: string): RiskCalendarRow[];
  /** Saved edits of a case; false or null when the browser storage is not available. */
  saveState(id: string, edits: Edits): boolean;
  loadState(id: string): Edits | null;
}

const runtimes = new Map<string, CaseRuntime>();
/** Status words of the cases list (index `status`). */
const STATUS_WORDS: Record<string, string> = { ready: 'Month level ready', preparing: 'In preparation' };
const rt = (id: string) => { const r = runtimes.get(id); if (!r) throw new Error(`The case «${id}» is not loaded.`); return r; };

// ---------------------------------------------------------------- text helpers (engine/format.ts writes the texts for people)
const pct = (p: number) => `${Math.round(p * 100)}%`;
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
export function monthLabel(cfg: CaseConfig, m: number): string {
  const t = new Date(`${cfg.forkDate}T12:00:00Z`), d = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + Math.round(m), 1));
  return `${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
/** A plan step's value for people: a month, a count, a share. */
function stepValue(cfg: CaseConfig, unit: string, v: number) {
  if (unit === 'month') return monthLabel(cfg, v);
  if (unit === 'FTE' || unit === 'people') return `${Math.round(v)} ${Math.round(v) === 1 ? 'person' : 'people'}`;
  if (unit === 'count') return `${Math.round(v)}`;
  return peopleValue(v, unit);
}

// ---------------------------------------------------------------- edits <-> engine state
// toState lives in ../actors/pageState (pure: the rerun on actors posts the same CaseState).
const isDist = (v: unknown): v is Dist => !!v && typeof v === 'object' && 'kind' in (v as object);
export { toState };
/** The page's edits as the CaseState of a loaded case, with the forks on, for a rerun on actors (WP18's month route). */
export function monthBodyOf(caseId: string, edits: Edits, forksOn: string[], n: number, seed: number) { return monthBody(rt(caseId), edits, forksOn, n, seed); }
/** The engine's CaseState as the page's edits (to read a saved state). */
function fromState(r: CaseRuntime, s: CaseState): Edits {
  const e: Edits = {}, D = r.view.defaults;
  for (const h of s.activePresets) e[`hyp|${h}`] = true;
  const groups = new Map<string, Record<string, number>>();
  for (const [path, v] of Object.entries(s.edits)) {
    if (typeof v !== 'number') continue;
    const parts = path.split('|');
    if (parts[0] === 'r') { e[path] = v; continue; }
    const key = parts.slice(0, -1).join('|'), g = groups.get(key) ?? {};
    g[parts[parts.length - 1]] = v; groups.set(key, g);
  }
  for (const [key, g] of groups) {
    const base = D[key];
    if (!isDist(base)) continue;
    e[key] = base.kind === 'fixed' ? { kind: 'fixed', value: g.value ?? base.value } : { kind: 'pert', low: g.low ?? base.low, mode: g.mode ?? base.mode, high: g.high ?? base.high };
  }
  for (const [name, m] of Object.entries(s.timeline.moves)) e[`v|${name}`] = { kind: 'fixed', value: m };
  for (const ev of s.timeline.events) e[`ev|${ev}`] = true;
  for (const off of s.timeline.off) e[`goal|${off}|off`] = true;
  for (const [k, w] of Object.entries(s.prefs.weights)) e[`w|${k}`] = w;
  const cautionOf = (a: number) => r.view.caution.reduce((best, c) => Math.abs(c.value - a) < Math.abs(best.value - a) ? c : best, r.view.caution[0]).id;
  if (s.prefs.riskAversion != null && r.view.caution.length) e.caution = cautionOf(s.prefs.riskAversion);
  if (s.prefs.priority) e.priority = s.prefs.priority; else if (Object.keys(s.prefs.weights).length) e.priority = 'custom';
  if (s.n !== r.config.defaults.n) e['sim|n'] = s.n;
  if (s.seed !== r.config.defaults.seed) e['sim|seed'] = s.seed;
  if (s.selectedForks.length) e.week = s.selectedForks;
  for (const [k, v] of Object.entries(e)) if (JSON.stringify(v) === JSON.stringify(D[k])) delete e[k];
  return e;
}
const UI_KEYS = (path: string) => path.startsWith('fork|') || path === 'day' || path === 'dayExample';
const uiKey = (id: string) => `variator:case:${id}:ui:v1`;

// ---------------------------------------------------------------- runs
interface Raw { full: SimRun; run: SimRun; state: CaseState; tl: TimelineResult }
const raw = (run: CaseRun) => run.raw as Raw;
let client: EngineClient | null = null;
const engine = () => (client ??= createEngineClient());
/** Starts the engine's worker before the first run asks for it (WP29: a case page loads its code while its files
 *  arrive). */
export function warmEngine(): void { engine(); }

/** The run restricted to some forks (fork on/off is the page's own; the engine simulates all of them). */
function subRun(R: SimRun, keep: number[]): SimRun {
  if (keep.length === R.options.length) return R;
  const O = R.options.length, K = R.criteria.length, n = R.n, O2 = keep.length;
  const X = new Float64Array(n * O2 * K), FLAG = new Int8Array(n * O2 * K), BAD = new Uint8Array(n * O2);
  const per = (a: Float64Array) => { const out = new Float64Array(n * O2); for (let s = 0; s < n; s++) keep.forEach((j, i) => { out[s * O2 + i] = a[s * O + j]; }); return out; };
  for (let s = 0; s < n; s++) keep.forEach((j, i) => {
    BAD[s * O2 + i] = R.BAD[s * O + j];
    for (let k = 0; k < K; k++) { X[(s * O2 + i) * K + k] = R.X[(s * O + j) * K + k]; FLAG[(s * O2 + i) * K + k] = R.FLAG[(s * O + j) * K + k]; }
  });
  const keepNames = keep.map(j => R.options[j]);
  const model: ExportedModel = { ...R.model, options: R.model.options.filter(o => keepNames.includes(o.name)) };
  const map = <T,>(o: Record<string, T>, f: (v: T) => T) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, f(v)]));
  return {
    ...R, model, options: keepNames, X, FLAG, BAD,
    fire: map(R.fire, v => keep.map(j => v[j])), IN: map(R.IN, per), TM: map(R.TM, per), TS: map(R.TS, per),
    chain: keep.map(j => R.chain[j]), stats: keep.map(j => R.stats[j]), feasible: keep.map(j => R.feasible[j]),
  };
}
const q = (srt: ArrayLike<number>, p: number) => { const i = (srt.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i); return srt[lo] + (srt[hi] - srt[lo]) * (i - lo); };
const stat = (s: CritStats): Stat => ({ mean: s.mean, p1: q(s.srt, 0.01), p5: s.p5, p10: s.p10, p25: s.p25, p50: s.p50, p75: s.p75, p90: s.p90, p95: s.p95, p99: q(s.srt, 0.99), min: s.srt[0], max: s.srt[s.srt.length - 1], cvar: s.cvar });
const column = (R: SimRun, a: ArrayLike<number>, j: number) => { const O = R.options.length, out = new Float64Array(R.n); for (let s = 0; s < R.n; s++) out[s] = a[s * O + j]; return out; };

/** Inputs that move the first criterion most for option j (correlation over the scenarios). */
function drivers(R: SimRun, j: number) {
  const K = R.criteria.length, O = R.options.length, first = new Float64Array(R.n);
  for (let s = 0; s < R.n; s++) first[s] = R.X[(s * O + j) * K];
  return Object.entries(R.IN).map(([name, a]) => ({ input: name, label: noEmoji(variableLabel(R.model, name)), r: corr(column(R, a, j), first) }))
    .filter((d): d is { input: string; label: string; r: number } => d.r != null && Math.abs(d.r) >= 0.05).sort((a, b) => Math.abs(b.r) - Math.abs(a.r)).slice(0, 6);
}

/** The variable on the left of a condition («cash >= g_cash» → cash). */
const condSubject = (c: string) => /^\s*(\w+)\s*(<=|>=|<|>)/.exec(c)?.[1] ?? null;

function calendarRows(run: CaseRun, fork: string): RiskCalendarRow[] {
  const H = run.preview.horizon, own = run.preview.calendar[fork] ?? [];
  // the milestones of the fork from the live run, missed at their target month: they follow your edits, so they take
  // the place of the preview's rows about the same milestone (the preview shows the original case)
  const late = run.goals.filter(g => g.kind === 'milestone' && g.p[fork] != null).map((g): RiskCalendarRow => {
    const miss = 1 - (g.p[fork] ?? 1), at = Math.min(H, Math.max(1, Math.ceil(g.month))), was = own.find(o => o.ref === g.id && (o.kind === 'milestone' || o.id.startsWith('late')));
    return { id: `late-${g.id}`, label: was?.label ?? `${g.label}: late`, consequence: was?.consequence || 'Missed at its target month', byMonth: Array.from({ length: H }, (_, k) => k + 1 >= at ? miss : 0), medianMonth: miss > 0.005 ? at : null, total: miss, ref: g.id, kind: 'milestone', live: true };
  });
  return [...own.filter(o => !late.some(l => l.ref === o.ref && (o.kind === 'milestone' || o.id.startsWith('late')))), ...late];
}

// ---------------------------------------------------------------- the source
export const engineSource: CaseSource = {
  kind: 'engine',
  async listCases() {
    return Promise.all((await readIndex()).map(async (entry): Promise<CaseSummary> => {
      const base: CaseSummary = {
        id: entry.id, letter: entry.letter, title: entry.title, question: entry.question, forks: entry.forks, hypotheses: entry.hypotheses, criteria: [],
        status: STATUS_WORDS[entry.status] ?? entry.status, lastRun: null, ready: false, owner: entry.owner, live: [], preview: [],
      };
      if (entry.status !== 'ready' || !entry.config) return base;
      try {
        const b = await readCase(entry.id), r = runtimes.get(entry.id) ?? buildRuntime(b);
        runtimes.set(entry.id, r);
        // a step is live when one of its sections (the review gates aside) runs on the engine, not on the preview
        const own = (step: string) => b.config.sections.filter(s => s.step === step && !s.id.endsWith('-review'));
        const live = r.view.steps.map(s => s.id).filter(s => own(s).some(x => !x.preview)), preview = r.view.steps.map(s => s.id).filter(s => !live.includes(s));
        return { ...base, ready: true, criteria: r.view.criteria.map(c => c.label), live, preview: b.preview ? preview : [], request: r.view.origin?.request };
      } catch (e) {
        return { ...base, question: `${entry.question} (${e instanceof Error ? e.message : String(e)})` };
      }
    }));
  },
  async loadCase(id) {
    if (!runtimes.has(id)) runtimes.set(id, buildRuntime(await readCase(id)));
    return rt(id).view;
  },
  async simulate(cfg, edits) {
    const r = rt(cfg.id), state = toState(r, edits);
    const full = await engine().simulate(r.config, state).result;
    const on = cfg.forks.filter(f => f.baseline || edits[`fork|${f.id}`] !== false).map(f => f.id);
    const keep = full.options.map((o: string, j: number) => on.includes(o) ? j : -1).filter((j: number) => j >= 0);
    const R = subRun(full, keep), tl = evaluateTimeline(R, state.timeline.off);
    const by = <T,>(f: (j: number, fork: string) => T) => Object.fromEntries(R.options.map((o, j) => [o, f(j, o)]));
    return {
      n: R.n, seed: R.seed, ms: R.ms, forks: R.options,
      stats: by(j => Object.fromEntries(R.criteria.map((c, k) => [c, stat(R.stats[j][k])]))),
      feasible: by(j => R.feasible[j]),
      limits: Object.fromEntries(R.criteria.map((c, k) => [c, R.limits[k]])),
      fire: Object.fromEntries(Object.entries(R.fire).map(([id, v]) => [id, by(j => v[j] / R.n)])),
      chain: by(j => R.chain[j]),
      goals: tl.goals.map(g => ({ id: g.name, label: noEmoji(g.label), kind: g.kind as 'milestone' | 'goal', month: g.month, movable: g.monthVar != null, p: by(j => g.p[j]), spread: by(j => g.spread[j]?.length ? g.spread[j] : null) })),
      conflicts: tl.conflicts.map(c => ({ goal: c.goal, fork: c.option, input: c.variable, inputLabel: noEmoji(c.variableLabel), r: c.r })),
      drivers: by(j => drivers(R, j)),
      preview: r.previewView,
      raw: { full, run: R, state, tl } satisfies Raw,
    };
  },
  rank(run, cfg, weights, caution) {
    const { run: R } = raw(run), a = cfg.caution.find(c => c.id === caution)?.value ?? rt(cfg.id).config.defaults.riskAversion;
    const rk = rankRun(R, { weights, riskAversion: a }), n = R.n;
    // spread of the per-scenario score (the review gate's «overlaps the leader»): the ranking's own scale, with the
    // fixed anchors and the ties of the meaningful differences (not 0..1 across the forks of the scenario)
    const spread = formulaScoreSpread(R, rk.w);
    const by = <T,>(f: (j: number) => T) => Object.fromEntries(R.options.map((o, j) => [o, f(j)]));
    return {
      raw: { rk, prefs: { weights, riskAversion: a } }, caution,
      weights: Object.fromEntries(R.criteria.map((c, k) => [c, rk.w[k]])),
      score: by(j => rk.score[j]), key: by(j => rk.key[j]), pBest: by(j => rk.pBest[j]), order: rk.order.map(j => R.options[j]),
      rag: by(j => Object.fromEntries(R.criteria.map((c, k) => [c, rk.rag[j][k].map(x => x / n) as [number, number, number]]))),
      overall: by(j => rk.overall[j].map(x => x / n) as [number, number, number]),
      scoreSpread: by(j => spread[j]),
    };
  },
  bestPath(run, ranking, cfg, hypotheses) {
    const r = rt(cfg.id), { run: R, tl } = raw(run), { rk, prefs } = ranking.raw as { rk: EngineRanking; prefs: { weights: Record<string, number>; riskAversion: number } };
    const bp = engineBestPath(R, prefs, hypotheses, { ranking: rk, timeline: tl });
    const fl = (id: string) => cfg.forks.find(f => f.id === id)?.label ?? id;
    const critLabel = (name: string) => cfg.criteria.find(c => c.id === name)?.label ?? name;
    const breaks = bp.breaks.map(b => ({ change: breakText(R.model, b), winner: b.winner }));
    // the month of a risk: the calendar row about the same criterion, check, goal or milestone
    const rows = calendarRows(run, bp.decision);
    const checks = new Set(r.config.rules.filter(x => x.kind === 'check').map(x => x.id));
    const subjectOf = (ref: string | null) => { const it = r.config.timeline?.items.find(i => i.name === ref); return it?.condition ? condSubject(it.condition) : null; };
    const rowFor = (kind: string, ref: string | null) => {
      const mapped = cfg.riskRows[kind === 'fragile' ? 'fragile' : ref ?? ''];
      const hit = mapped ? rows.find(row => row.id === mapped) ?? rows.find(row => row.ref === ref && row.live) : undefined;
      if (hit) return hit;
      const keys = kind === 'fragile' ? [...checks] : [ref, subjectOf(ref)].filter((x): x is string => !!x);
      return rows.find(row => keys.some(k => row.ref === k || row.id === k)) ?? rows.find(row => keys.some(k => row.id.startsWith(`${k}_`)));
    };
    const risks: Risk[] = bp.risks.map(x => {
      const row = rowFor(x.kind, x.ref);
      return {
        id: x.id, head: peopleText(x.headline), p: x.p, odds: x.odds, detail: peopleText(x.evidence),
        choice: x.choice ? { fork: x.choice.option, odds: x.choice.odds } : null, watch: x.watch ? `${peopleText(x.watch.label)} (${x.watch.high ? 'high' : 'low'} values)` : null,
        month: row?.medianMonth ? { median: row.medianMonth, label: monthLabel(cfg, row.medianMonth) } : null, kind: x.kind === 'fragile' ? 'check' : x.kind,
      };
    });
    const top = cfg.criteria.map(c => ({ c, w: prefs.weights[c.id] ?? 0 })).filter(x => x.w > 0).sort((a, b) => b.w - a.w).slice(0, 2);
    const c1 = top[0]?.c ?? cfg.criteria[0], s1 = run.stats[bp.decision][c1.id];
    const situ = hypotheses.length ? ` in a world of ${hypotheses.map(h => lowFirst(cfg.hypotheses.find(x => x.id === h)?.label ?? h)).join(' and ')}` : '';
    const strategy: BestPath['strategy'] = [
      { text: `Your aim is ${top.map(x => lowFirst(x.c.label)).join(' and ') || 'no criterion'} first, judged on ${cautionWords(prefs.riskAversion)}${situ}: choose ` },
      { text: fl(bp.decision), strong: true },
      { text: `, with a median ${lowFirst(c1.label)} of ${peopleValue(s1.p50, c1.unit)}. ` },
      { text: bp.feasible < 0.8 ? `It is feasible in only ${pct(bp.feasible)} of the scenarios, so remove the causes of the failed checks first.` : breaks.length ? `Look again if ${breaks[0].change}: then ${fl(breaks[0].winner)} wins.` : 'The choice holds when any one weight is halved or doubled.' },
    ];
    // the tradeoff of the leader and the runner-up: what each is better at, and where they are equal within the
    // meaningful difference of the criterion (the engine's `equal`; the best path's `ties` name the same criteria)
    const t = rk.tradeoff, words = (xs: string[]) => listWords(xs.map(c => lowFirst(critLabel(c))));
    const equal = t.equal?.length ? t.equal : bp.ties ?? [];
    const better = t.runner ? [
      t.leaderBetter.length ? `${fl(t.leader)} is better than ${fl(t.runner)} on ${words(t.leaderBetter)}.` : '',
      t.runnerBetter.length ? `${fl(t.runner)} is better on ${words(t.runnerBetter)}.` : '',
    ].filter(Boolean).join(' ') : '';
    const tie = t.runner && equal.length ? `${better ? 'Equal' : `${fl(t.leader)} and ${fl(t.runner)} are equal`} within the meaningful difference on ${words(equal)}.` : '';
    const tradeoff = [better, tie].filter(Boolean).join(' ');
    return { leader: bp.decision, runnerUp: bp.runner, pBest: bp.pBest, feasible: bp.feasible, breaks, risks, strategy, tradeoff, ties: t.runner ? equal.map(c => lowFirst(critLabel(c))) : [] };
  },
  async solvePlan(cfg, edits, fork) {
    const r = rt(cfg.id), j = r.config.options.findIndex(o => o.name === fork);
    const p = await engine().solve(r.config, toState(r, edits), j).result;
    return planView(r, p);
  },
  requiredLines(cfg, plan) {
    if (!plan.raw) return [];
    if (!plan.met) return ['No plan with these levers meets all of them. Move the dates (see Recommended) or choose another fork.'];
    return [...plan.steps.map(s => `${s.label}${s.unit === 'month' ? ' ' : ': '}${stepValue(cfg, s.unit, s.value)}${s.mode ? `, ${s.mode.label}` : ''}`), 'Then all milestones and goals are met («Apply the plan» in «Plan»).'];
  },
  suggestDates(run, _cfg, fork) {
    const { run: R, tl } = raw(run), j = R.options.indexOf(fork);
    if (j < 0) return {};
    return Object.fromEntries(Object.entries(engineSuggestDates(tl, R.model, j)).map(([v, m]) => [`v|${v}`, { kind: 'fixed', value: m } as Dist]));
  },
  snapshot(run, ranking, _cfg, fork) {
    const { run: R, tl } = raw(run), j = Math.max(0, R.options.indexOf(fork));
    return engineSnapshot(R, (ranking.raw as { rk: EngineRanking }).rk, tl, j);
  },
  diff(before, after) {
    const d = diffReports(before as CaseSnapshot, after as CaseSnapshot, '');
    const clean = (xs: string[]) => xs.map(peopleText);
    return { effect: clean(d.effect), required: clean(d.required), recommended: clean(d.recommended), supporting: clean(d.supporting), risks: clean(d.risks), tone: d.tone === '' ? 'neutral' : d.tone, pending: d.requiredPending };
  },
  presetValues(cfg, hypotheses) {
    if (!hypotheses.length) return {};
    const r = rt(cfg.id);
    const { model } = effectiveModel(r.lc, { ...defaultState(r.lc), activePresets: r.config.scenarios.map(s => s.name).filter(n => hypotheses.includes(n)) });
    const out: Edits = {};
    const toDist = (d: { kind: string; value?: number; low?: number; mode?: number; high?: number }): Dist => d.kind === 'fixed' ? { kind: 'fixed', value: d.value ?? 0 } : { kind: 'pert', low: d.low ?? 0, mode: d.mode ?? 0, high: d.high ?? 0 };
    const put = (path: string, v: EditValue | undefined) => { if (v !== undefined && JSON.stringify(v) !== JSON.stringify(cfg.defaults[path])) out[path] = v; };
    for (const v of model.variables) put(`v|${v.name}`, toDist(v.dist));
    for (const x of model.risks) { put(`r|${x.name}|p`, x.p); put(`rm|${x.name}`, toDist(x.magnitude)); }
    for (const o of model.options) for (const [k, d] of Object.entries(o.overrides)) put(`o|${o.name}|${k}`, toDist(d));
    return out;
  },
  calendar(run, _cfg, fork) { return calendarRows(run, fork); },
  saveState(id, edits) {
    const engineOk = engineSaveState(toState(rt(id), edits));
    try { localStorage.setItem(uiKey(id), JSON.stringify(Object.fromEntries(Object.entries(edits).filter(([k]) => UI_KEYS(k))))); return engineOk; } catch { return false; }
  },
  loadState(id) {
    try {
      const r = rt(id), e = fromState(r, engineLoadState(r.lc));
      const ui = localStorage.getItem(uiKey(id));
      return { ...e, ...(ui ? JSON.parse(ui) as Edits : {}) };
    } catch { return null; }
  },
};

function planView(r: CaseRuntime, p: EnginePlan): Plan {
  const lever = (v: string) => r.config.timeline?.levers.find(l => l.variable === v);
  return {
    fork: p.option, met: p.met, raw: p, p: Object.fromEntries(p.names.map((n, i) => [n, p.p[i]])),
    steps: p.steps.map(s => {
      const lv = lever(s.variable), modeVar = lv?.mode ?? null;
      const cur = modeVar ? Object.entries(lv!.modes).find(([, v]) => v === (r.lc.base.variables.find(x => x.name === modeVar)?.dist.value ?? 0))?.[1] ?? 0 : 0;
      return {
        // «Start hiring by month» + «Apr '27» reads «Start hiring by Apr '27»
        path: `v|${s.variable}`, label: noEmoji(s.label).replace(/ by month$/, s.unit === 'month' ? ' by' : ' by month'), unit: s.unit as Unit, value: s.value, current: s.current,
        mode: modeVar && s.mode != null && s.modeValue != null ? { label: MODE_WORDS[s.mode]?.phrase ?? s.mode, path: `v|${modeVar}`, value: s.modeValue, current: cur } : null,
      };
    }),
  };
}

export const source: CaseSource = engineSource;
