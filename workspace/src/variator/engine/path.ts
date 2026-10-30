// Best path (mirrors variator.mcda.path.best_path) and risks as consequences: a port of the page's whatBreaks,
// riskList, bestPath and strategy. Texts are plain (no HTML); the UI styles them.
import { criterionLabel, fmt, lbl, odds, optionLabel, pct, splitEmoji, variableLabel, vfmt } from './format.js';
import { anchored, beyondTie, feasibleKey, rankRun, resolvePrefs, tiedOn, type PrefsInput } from './rank.js';
import type { BestPath, BreakChange, ExportedModel, Prefs, Ranking, RiskItem, SimRun, TimelineResult } from './types.js';

/** A change counts only with a clear lead (as mcda.path.CLEAR_LEAD). */
export const CLEAR_LEAD = 0.02;
export const TARGET = 0.8;

/** score x feasible per option for raw weights wv and risk aversion a. */
export function keysFor(R: SimRun, wv: number[], a: number): number[] {
  const M = R.model, O = R.options.length, K = M.criteria.length, t = wv.reduce((s, x) => s + x, 0) || 1;
  const Mx = R.options.map((_o, j) => M.criteria.map((_c, k) => (1 - a) * R.stats[j][k].mean + a * R.stats[j][k].cvar));
  const keys: number[] = [], FK = feasibleKey(R);
  for (let j = 0; j < O; j++) {
    let sc = 0;
    for (let k = 0; k < K; k++) sc += anchored(R, M.criteria[k].higher, beyondTie(Mx[j][k], Mx[0][k], R.ties[k]), k) * wv[k] / t;
    keys.push(sc * FK[j]);
  }
  return keys;
}

const argmax = (a: number[]) => a.reduce((b, x, i) => (x > a[b] ? i : b), 0);

/** Single changes (a weight x0.5 or x2, risk aversion 0 or 1) after which another option leads clearly. */
export function whatBreaks(R: SimRun, prefs: Prefs): BreakChange[] {
  const M = R.model, wv = M.criteria.map(c => prefs.weights[c.name]), a = prefs.riskAversion;
  const base = argmax(keysFor(R, wv, a)), out: BreakChange[] = [];
  const clear = (keys: number[]) => {
    const j = argmax(keys);
    return keys[j] - keys[base] > CLEAR_LEAD ? j : base;
  };
  M.criteria.forEach((c, k) => [0.5, 2].forEach(f => {
    const w2 = wv.slice();
    w2[k] *= f;
    const j = clear(keysFor(R, w2, a));
    if (j !== base) out.push({ change: `weight of ${c.name} x${f}`, winner: M.options[j].name, label: `weight of ${criterionLabel(M, k)} ×${f}`, winnerLabel: optionLabel(M, j) });
  }));
  [0, 1].forEach(x => {
    if (x !== a) {
      const j = clear(keysFor(R, wv, x));
      if (j !== base) out.push({ change: `risk aversion ${x}`, winner: M.options[j].name, label: `risk aversion ${x}`, winnerLabel: optionLabel(M, j) });
    }
  });
  return out;
}

/** Pearson r; null when x has no spread (or y none). */
export function corr(x: ArrayLike<number>, y: ArrayLike<number>): number | null {
  const n = x.length;
  let mx = 0, my = 0;
  for (let i = 0; i < n; i++) {
    mx += x[i];
    my += y[i];
  }
  mx /= n;
  my /= n;
  let sxy = 0, sxx = 0, syy = 0;
  for (let i = 0; i < n; i++) {
    const a = x[i] - mx, b = y[i] - my;
    sxy += a * b;
    sxx += a * a;
    syy += b * b;
  }
  return sxx > 1e-12 * n && syy > 0 ? sxy / Math.sqrt(sxx * syy) : null;
}

function watchFor(R: SimRun, j: number, isBad: (s: number) => boolean | number): RiskItem['watch'] {
  const O = R.options.length, y = new Float64Array(R.n), x = new Float64Array(R.n);
  let best: { name: string; r: number } | null = null;
  for (let s = 0; s < R.n; s++) y[s] = isBad(s) ? 1 : 0;
  for (const [name, arr] of Object.entries(R.IN || {})) {
    for (let s = 0; s < R.n; s++) x[s] = arr[s * O + j];
    const r = corr(x, y);
    if (r != null && Math.abs(r) >= 0.1 && (!best || Math.abs(r) > Math.abs(best.r))) best = { name, r };
  }
  if (!best) return null;
  return { variable: best.name, label: variableLabel(R.model, best.name), high: best.r > 0 };
}

function choiceFor(R: SimRun, j: number, shareOf: (i: number) => number): RiskItem['choice'] {
  let bj = -1, bp = shareOf(j);
  R.options.forEach((_o, i) => {
    if (i !== j) {
      const p = shareOf(i);
      if (p < bp - 0.02) {
        bj = i;
        bp = p;
      }
    }
  });
  return bj < 0 ? null : { option: R.options[bj], label: optionLabel(R.model, bj), odds: odds(bp) };
}

/** Risks as consequences for option j, most important first (chance x weight). "Bad" = the red zone. */
export function riskList(R: SimRun, rk: Ranking, j: number, tl?: TimelineResult | null): RiskItem[] {
  const M = R.model, O = R.options.length, K = M.criteria.length, w = rk.w, RT = M.page.risks || {}, f = R.feasible[j];
  const out: RiskItem[] = [];
  const item = (kind: RiskItem['kind'], ref: string | null, head: string, p: number, ev: string, choice: RiskItem['choice'], watch: RiskItem['watch'], score: number) => {
    const { emoji, text } = splitEmoji(head);
    const line = `${text}. ${odds(p)} (${pct(p)}${ev ? '; ' + ev : ''})${choice ? ` · ${choice.label} makes it ${choice.odds}` : ''}${watch ? ` · Watch: ${watch.label} (${watch.high ? 'high' : 'low'} values)` : ''}`;
    out.push({ id: '', kind, ref, emoji, headline: text, p, odds: odds(p), evidence: ev, choice, watch, score, text: line });
  };
  if (f < 0.8) item('fragile', null, '🧱 The plan is fragile: a check fails', 1 - f, '', choiceFor(R, j, i => 1 - R.feasible[i]), watchFor(R, j, s => R.BAD[s * O + j]), 2);
  M.rules.filter(r => r.kind === 'check').forEach(r => {
    const p = R.fire[r.id][j] / R.n;
    if (p < 0.005) return;
    item('check', r.id, RT[r.id] || `⛔ ${lbl(M, r.id, r.desc)}: the plan is not acceptable then`, p, '', choiceFor(R, j, i => R.fire[r.id][i] / R.n), watchFor(R, j, s => R.BAD[s * O + j]), p);
  });
  M.criteria.forEach((c, k) => {
    const lim = R.limits[k].red, worse = (v: number) => (c.higher ? v < lim : v > lim);
    // no risk text when the red limit is the expected value (a capped result, for example runway at 60 months)
    if (!(w[k] > 0) || Math.abs(lim - R.stats[j][k].p50) <= 1e-9 * Math.max(1, Math.abs(lim))) return;
    const share = (i: number) => {
      let b = 0;
      for (let s = 0; s < R.n; s++) if (worse(R.X[(s * O + i) * K + k])) b++;
      return b / R.n;
    };
    const p = share(j);
    if (p < 0.01) return;
    // a check rule with the same share already names this risk (for example runway below its red limit)
    if (M.rules.some(r => r.kind === 'check' && R.fire[r.id][j] / R.n === p)) return;
    const cn = criterionLabel(M, k);
    item('criterion', c.name, RT[c.name] || `⚠️ ${cn} can end worse than planned`, p,
      `${cn} ${c.higher ? 'below' : 'above'} ${vfmt(lim, c.unit)}; expected ${vfmt(R.stats[j][k].p50, c.unit)}`,
      choiceFor(R, j, share), watchFor(R, j, s => worse(R.X[(s * O + j) * K + k])), p * w[k]);
  });
  (tl ? tl.goals : []).forEach(g => {
    const pj = g.p[j];
    if (pj == null) return;
    const miss = 1 - pj;
    if (miss < 0.01) return;
    const c = tl!.conflicts.find(x => x.j === j && x.goal === g.name);
    item('goal', g.name, `${g.emoji} You can miss “${g.label}”`, miss, `met in ${pct(pj)}, target ${pct(TARGET)}`,
      choiceFor(R, j, i => (g.p[i] == null ? 1 : 1 - (g.p[i] as number))),
      c ? { variable: c.variable, label: c.variableLabel, high: c.r < 0 } : null, miss * 1.5);
  });
  const red = rk.overall[j][2] / R.n;
  if (red >= 0.01) item('red', null, '🚦 You may need plan B: a key result ends in the red zone', red, `red = as bad as the worst 10% of “${optionLabel(M, 0)}”`, choiceFor(R, j, i => rk.overall[i][2] / R.n), null, red * 0.3);
  return out.sort((a, b) => b.score - a.score).map((x, i) => ({ ...x, id: `R${i + 1}` }));
}

/** Strategy in 1-2 sentences from the weights, the risk aversion, the hypotheses and the result (plain text). */
export function strategy(R: SimRun, prefs: Prefs, p: Pick<BestPath, 'j' | 'feasible' | 'breaks'>, activeLabels: string[]): string {
  const M = R.model, a = prefs.riskAversion;
  const top = M.criteria.map((c, k) => [c, prefs.weights[c.name], k] as const).filter(x => x[1] > 0).sort((x, y) => y[1] - x[1]).slice(0, 2);
  const aim = a < 0.34 ? 'the average result' : a < 0.67 ? 'the average and the bad cases' : 'the worst 5% of the cases';
  const situ = activeLabels.length ? ` in the situation “${activeLabels.join(' + ')}”` : '';
  const [c1, , k1] = top[0] || [M.criteria[0], 0, 0];
  const s = R.stats[p.j][k1];
  const first = `Your aim is ${top.map(x => criterionLabel(M, x[2])).join(' and ') || 'no criterion'} first, judged on ${aim}${situ}: choose ${optionLabel(M, p.j)}, with a median ${criterionLabel(M, k1)} of ${fmt(s.p50)} ${c1.unit}.`;
  const second = p.feasible < 0.8 ? `It is feasible in only ${pct(p.feasible)} of the scenarios, so remove the causes of the failed checks first.`
    : p.breaks.length ? `Examine the choice again if the ${p.breaks[0].label}: then ${p.breaks[0].winnerLabel} wins.`
    : 'The choice stays the same when one weight is halved or doubled.';
  return `${first} ${second}`;
}

/** Active hypotheses in definition order (the page's SCN.filter(ACTIVE)). */
export function activeScenarios(M: ExportedModel, active: Iterable<string>) {
  const on = new Set(active);
  return (M.scenarios || []).filter(s => on.has(s.name));
}

export interface BestPathOptions { ranking?: Ranking; timeline?: TimelineResult | null }

/** The best path card: decision, runner-up, "changes if", expected result, risks and the strategy sentence. */
export function bestPath(R: SimRun, prefsIn?: PrefsInput | null, active: Iterable<string> = [], opts: BestPathOptions = {}): BestPath {
  const M = R.model, prefs = resolvePrefs(M, prefsIn), rk = opts.ranking ?? rankRun(R, prefs);
  const j = rk.order[0], f = R.feasible[j];
  const breaks = whatBreaks(R, prefs);
  const risks = riskList(R, rk, j, opts.timeline);
  const expected: BestPath['expected'] = {};
  M.criteria.forEach((c, k) => {
    const s = R.stats[j][k];
    expected[c.name] = { p5: s.p5, median: s.p50, p95: s.p95, unit: c.unit };
  });
  const act = activeScenarios(M, active);
  const runner = rk.order.length > 1 ? rk.order[1] : null;
  const out: BestPath = {
    decision: M.options[j].name,
    j,
    runner: runner == null ? null : M.options[runner].name,
    runnerLabel: runner == null ? null : optionLabel(M, runner),
    feasible: f,
    pBest: rk.best[j] / Math.max(rk.valid, 1),
    breaks,
    ties: runner == null ? [] : tiedOn(R, rk.Mx, j, runner).map(k => criterionLabel(M, k)),
    risks,
    expected,
    situation: act.map(s => s.label),
    strategy: '',
  };
  out.strategy = strategy(R, prefs, out, act.map(s => s.label));
  return out;
}
