// Monte Carlo of the formula model (mirrors variator.simulation.simulate_tensor + rules), and the statistics.
// A port of model-simulator.html "simulate" and "stats": the same mulberry32 stream, one row of uniforms per
// scenario (variables, then two per risk), shared by all options (common random numbers).
import { compileModel, withDerived, type CompiledModel } from './compile.js';
import { clearQuantileCache, mulberry32, quant, sampler } from './dist.js';
import type { CritStats, ExportedModel, SimRun } from './types.js';

const now = () => (globalThis.performance ? globalThis.performance.now() : Date.now());
/** Anchors: the baseline's 1st and 99th percentile; at least 4 meaningful differences wide (as Python mcda). */
export const ANCHOR_TAIL = 0.01, ANCHOR_WIDTH = 4;

export interface SimulateOptions {
  n: number;
  seed: number;
  /** Record the varying inputs per scenario (needed for "what to watch" and the timeline conflicts). Default true. */
  recordInputs?: boolean;
  /** Compute the statistics (stats, feasible, limits). Default true; a plan trial needs only the timeline. */
  withStats?: boolean;
  caseId?: string;
  compiled?: CompiledModel;
}

/** Linear-interpolated quantile of a sorted array. */
export function q(srt: ArrayLike<number>, p: number): number {
  const i = (srt.length - 1) * p, lo = Math.floor(i), hi = Math.ceil(i);
  return srt[lo] + (srt[hi] - srt[lo]) * (i - lo);
}

/** Simulate an (effective) model. The heavy step: run it in the worker for the UI. */
export function simulateModel(M: ExportedModel, opts: SimulateOptions): SimRun {
  const t0 = now();
  clearQuantileCache();
  const C = opts.compiled ?? compileModel(M);
  const n = opts.n, rng = mulberry32(opts.seed | 0);
  const V = M.variables, nv = V.length, nr = M.risks.length, O = C.opts.length, K = M.criteria.length;
  const X = new Float64Array(n * O * K), FLAG = new Int8Array(n * O * K).fill(-1), BAD = new Uint8Array(n * O);
  const fire: Record<string, number[]> = {};
  M.rules.forEach(r => (fire[r.id] = new Array(O).fill(0)));
  const chainNames = (M.page.chain || []).filter(x => C.dmap[x]);
  const chainSum = C.opts.map(() => Object.fromEntries(chainNames.map(x => [x, 0])) as Record<string, number>);
  const critIdx: Record<string, number> = Object.fromEntries(M.criteria.map((c, k) => [c.name, k]));
  // per-scenario inputs (after the shocks) for the "watch" signal of a risk: only inputs that vary
  const varying = opts.recordInputs === false ? [] : V.filter(v => v.dist.kind !== 'fixed' || M.risks.some(r => r.variable === v.name)
    || C.opts.some(o => o.overrides[v.name] && o.overrides[v.name].kind !== 'fixed')).map(v => v.name);
  const IN: Record<string, Float64Array> = Object.fromEntries(varying.map(name => [name, new Float64Array(n * O)]));
  // timeline: per scenario and option, "met" of each milestone or goal and the value of each forecast formula
  const TL = C.tl || [], TM: Record<string, Float64Array> = {}, TS: Record<string, Float64Array> = {};
  for (const it of TL) {
    if (it.cf) TM[it.name] = new Float64Array(n * O);
    if (it.sf) TS[it.name] = new Float64Array(n * O);
  }
  // Slot layout (compileModel): the page's env object as a Float64Array. Samplers hold the PERT tables.
  const S = C.slots, vSlot = V.map(v => S[v.name]), cSlot = M.criteria.map(c => S[c.name]);
  const samplers = C.opts.map(o => V.map(v => sampler(o.overrides[v.name] || v.dist)));
  const risks = M.risks.map(r => ({ r, slot: S[r.variable], mag: sampler(r.magnitude), off: C.opts.map(o => !!(r.options && !r.options.includes(o.name))) }));
  const inSlot = varying.map(name => S[name]), inArr = varying.map(name => IN[name]);
  const chainSlot = chainNames.map(x => S[x]);
  const applies = C.rules.map(r => C.opts.map(o => !(r.options && !r.options.includes(o.name))));
  const flagKs = C.rules.map(r => r.acts.map(a => (a && a.flag ? (a.criterion ? [critIdx[a.criterion]] : M.criteria.map((_c, k) => k)) : [])));
  const COLOR = { green: 0, amber: 1, red: 2 };
  const u = new Float64Array(nv + 2 * nr);
  for (let s = 0; s < n; s++) {
    for (let i = 0; i < u.length; i++) u[i] = Math.min(Math.max(rng(), 1e-12), 1 - 1e-12);
    for (let j = 0; j < O; j++) {
      const o = C.opts[j], env = C.blank.slice(), smp = samplers[j];
      for (let i = 0; i < nv; i++) env[vSlot[i]] = smp[i](u[i]);
      for (let i = 0; i < nr; i++) {
        const rk = risks[i];
        if (rk.off[j]) continue;
        if (u[nv + 2 * i] < rk.r.p) {
          const m = rk.mag(u[nv + 2 * i + 1]);
          env[rk.slot] = rk.r.effect === 'multiply' ? env[rk.slot] * (1 + m) : env[rk.slot] + m;
        }
      }
      for (let i = 0; i < inSlot.length; i++) inArr[i][s * O + j] = env[inSlot[i]];
      for (let ri = 0; ri < C.rules.length; ri++) {
        const r = C.rules[ri];
        if (r.kind !== 'effect' || !applies[ri][j]) continue;
        const before = withDerived(C, env);
        let b = r.conds.findIndex(c => c(before));
        if (b >= 0) fire[r.id][j]++;
        else if (r.acts[r.acts.length - 1]) b = r.acts.length - 1;
        else continue;
        const act = r.acts[b];
        if (act) for (const [k, f] of act.setSlots) env[k] = f(before);
      }
      const fin = withDerived(C, env), base = (s * O + j) * K;
      for (let ri = 0; ri < C.rules.length; ri++) {
        const r = C.rules[ri];
        if (r.kind === 'effect' || !applies[ri][j]) continue;
        let b = r.conds.findIndex(c => c(fin));
        if (b >= 0) fire[r.id][j]++;
        else if (r.acts[r.acts.length - 1]) b = r.acts.length - 1;
        else continue;
        const act = r.acts[b];
        if (!act) continue;
        if (act.fail) BAD[s * O + j] = 1;
        if (act.flag) {
          const color = COLOR[act.flag];
          for (const k of flagKs[ri][b]) FLAG[base + k] = Math.max(FLAG[base + k], color);
        }
      }
      for (let k = 0; k < K; k++) X[base + k] = o.fns[k](fin);
      for (let x = 0; x < chainSlot.length; x++) chainSum[j][chainNames[x]] += fin[chainSlot[x]];
      if (TL.length) {
        const e2 = fin.slice();
        for (let k = 0; k < K; k++) e2[cSlot[k]] = X[base + k]; // goals may name criteria
        for (const it of TL) {
          if (it.cf) TM[it.name][s * O + j] = it.cf(e2) ? 1 : 0;
          if (it.sf) TS[it.name][s * O + j] = it.sf(e2);
        }
      }
    }
  }
  const run: SimRun = {
    caseId: opts.caseId ?? '',
    model: M,
    n,
    seed: opts.seed,
    ms: 0,
    options: M.options.map(o => o.name),
    criteria: M.criteria.map(c => c.name),
    higher: M.criteria.map(c => c.higher),
    X, FLAG, BAD, fire, IN, TM, TS,
    chainNames,
    chain: chainSum.map(cs => Object.fromEntries(Object.entries(cs).map(([k, v]) => [k, v / n]))),
    stats: [],
    feasible: [],
    limits: [],
    ties: [],
    anchors: [],
  };
  if (opts.withStats !== false) computeStats(run);
  run.ms = Math.round(now() - t0);
  return run;
}

/** Mean, quantiles and the worst-5% mean per option and criterion; feasible shares; RAG limits (fixed, else from option 0). */
export function computeStats(R: SimRun): void {
  const M = R.model, O = R.options.length, K = M.criteria.length;
  R.stats = [];
  R.feasible = [];
  for (let j = 0; j < O; j++) {
    R.stats[j] = [];
    for (let k = 0; k < K; k++) {
      const v = new Float64Array(R.n);
      for (let s = 0; s < R.n; s++) v[s] = R.X[(s * O + j) * K + k];
      const srt = Float64Array.from(v).sort(), t = Math.max(1, Math.ceil(0.05 * R.n));
      let mean = 0, lo = 0, hi = 0;
      for (const x of v) mean += x;
      mean /= R.n;
      for (let i = 0; i < t; i++) {
        lo += srt[i];
        hi += srt[R.n - 1 - i];
      }
      const st: CritStats = {
        mean, p5: q(srt, 0.05), p10: q(srt, 0.1), p25: q(srt, 0.25), p50: q(srt, 0.5), p75: q(srt, 0.75), p90: q(srt, 0.9), p95: q(srt, 0.95),
        cvar: M.criteria[k].higher ? lo / t : hi / t, srt,
      };
      R.stats[j][k] = st;
    }
    let bad = 0;
    for (let s = 0; s < R.n; s++) bad += R.BAD[s * O + j];
    R.feasible[j] = 1 - bad / R.n;
  }
  // fixed limits (criterion.rag): a number, or a variable whose median sets it (as simulation.tensor.resolved_criteria)
  const ragVal = (x: number | string) => (typeof x === 'number' ? x : quant(M.variables.find(v => v.name === x)!.dist, 0.5));
  R.limits = M.criteria.map((c, k) => c.rag ? { green: ragVal(c.rag.green), red: ragVal(c.rag.red), fixed: true }
    : { green: R.stats[0][k].p50, red: c.higher ? R.stats[0][k].p10 : R.stats[0][k].p90, fixed: false });
  // meaningful difference per criterion (criterion.tie: a number or a variable whose median sets it; none = 0)
  R.ties = M.criteria.map(c => (c.tie == null ? 0 : Math.max(ragVal(c.tie), 0)));
  // fixed anchors (as variator.mcda.anchors): the baseline's 1st-99th percentile of each criterion, at least
  // ANCHOR_WIDTH meaningful differences wide; the score of a fork does not depend on the other forks
  R.anchors = M.criteria.map((_c, k) => {
    const s = R.stats[0][k].srt, pl = q(s, ANCHOR_TAIL), ph = q(s, 1 - ANCHOR_TAIL), mid = (pl + ph) / 2;
    let span = Math.max(ph - pl, ANCHOR_WIDTH * R.ties[k]);
    if (!(span > 0)) span = Math.max(Math.abs(mid), 1) * 1e-9;
    return { lo: mid - span / 2, span };
  });
}

/** Share of scenarios where arr[s * O + j] is 1 (the page's shareOf). */
export function shareOf(R: SimRun, arr: Float64Array, j: number): number {
  const O = R.options.length;
  let m = 0;
  for (let s = 0; s < R.n; s++) m += arr[s * O + j];
  return m / R.n;
}

/** The typed arrays of a run, for a transfer from the worker. */
export function runBuffers(R: SimRun): ArrayBuffer[] {
  const out = new Set<ArrayBuffer>();
  const add = (a: ArrayBufferView) => out.add(a.buffer as ArrayBuffer);
  [R.X, R.FLAG, R.BAD].forEach(add);
  for (const rec of [R.IN, R.TM, R.TS]) Object.values(rec).forEach(add);
  for (const row of R.stats) for (const st of row) add(st.srt);
  return [...out];
}
