// Change toasts: a port of the page's tlSnap / flushToast / toastPlan. A snapshot is taken before and after a change;
// diffReports says what changed, its effect, what is required (the plan), recommended and supporting, and the risks.
import { criterionLabel, optionLabel, pct, ppTxt, vfmt } from './format.js';
import { TARGET } from './path.js';
import type { CaseSnapshot, ChangeReport, Plan, Ranking, SimRun, TimelineResult } from './types.js';

/** The state of the results that a toast compares (for option j, the option the timeline shows). */
export function snapshot(R: SimRun, rk: Ranking, tl: TimelineResult | null, j: number): CaseSnapshot {
  const M = R.model;
  return {
    j,
    option: R.options[j],
    optionLabels: R.options.map((_o, i) => optionLabel(M, i)),
    decision: rk.order[0],
    goals: (tl ? tl.goals : []).map(g => ({
      name: g.name,
      label: g.label,
      emoji: g.emoji,
      p: g.p[j],
      p80: g.spread[j] ? g.spread[j][3] : null,
      month: g.month,
      movable: g.monthVar != null,
    })),
    conflicts: (tl ? tl.conflicts : []).filter(c => c.j === j).map(c => ({ goal: c.goal, goalLabel: c.goalLabel, variable: c.variable, variableLabel: c.variableLabel, r: c.r })),
    k0: R.stats[j][0].p50,
    crit0: { name: M.criteria[0].name, label: criterionLabel(M, 0), unit: M.criteria[0].unit },
  };
}

const halfUp = (x: number) => Math.ceil(x * 2) / 2;

/** The plan lines of the "Required" block (the page's toastPlan). */
export function requiredFromPlan(plan: Plan): string[] {
  return plan.met
    ? [...plan.steps.map(st => `${st.label} ${st.value}`), 'Then all milestones and goals are met (button “Apply the plan”).']
    : ['No plan with these levers meets all of them. Move the dates (see Recommended) or choose another option.'];
}

/**
 * The toast of one change. `before` and `after` are snapshots for the same timeline option; `change` says what was
 * done ("Hiring starts: month 2 → 4"); `plan` (optional) fills "Required" when goals are missed.
 */
export function diffReports(before: CaseSnapshot, after: CaseSnapshot, change: string, plan?: Plan | null): ChangeReport {
  const j = after.j, eff: string[] = [], rec: string[] = [], spare: string[] = [], sup: string[] = [], risks: string[] = [];
  const pMetChanges: ChangeReport['pMetChanges'] = [];
  const prev = Object.fromEntries(before.goals.map(g => [g.name, g]));
  let missed = 0;
  for (const g of after.goals) {
    if (g.p == null) continue;
    const b = prev[g.name], a = g.p, tag = `${g.emoji} ${g.label}`.trim();
    if (b && b.p != null && Math.abs(a - b.p) >= 0.01) {
      eff.push(`${tag}: ${pct(b.p)} → ${pct(a)} (${ppTxt(a - b.p)})`);
      pMetChanges.push({ goal: g.name, label: g.label, before: b.p, after: a, deltaPp: Math.round(100 * (a - b.p)) });
    }
    if (a < TARGET) {
      missed++;
      if (!b || b.p == null || b.p >= TARGET) risks.push(`${tag} now misses the ${pct(TARGET)} target`);
      if (g.p80 != null && g.movable) rec.push(`Set ${tag} to month ${halfUp(g.p80)} or later: then it is met in 8 of 10 futures`);
    } else if (b && b.p != null && a < b.p - 0.05) {
      risks.push(`${tag}: the chance falls by ${Math.round(100 * (b.p - a))} pp (still met)`);
    } else if (g.p80 != null && g.movable && a >= 0.97 && halfUp(g.p80) < g.month) {
      spare.push(`${tag} can come earlier: month ${halfUp(g.p80)} is still met in 8 of 10 futures`);
    }
  }
  if (!missed) rec.push(...spare.slice(0, 2));
  let leaderChanged: ChangeReport['leaderChanged'] = null;
  if (after.decision !== before.decision) {
    eff.push(`Best option: ${before.optionLabels[before.decision]} → ${after.optionLabels[after.decision]}`);
    leaderChanged = { from: before.optionLabels[before.decision], to: after.optionLabels[after.decision] };
  }
  const c0 = after.crit0, m0 = after.k0;
  if (Math.abs(m0 - before.k0) > 0.01 * Math.abs(before.k0)) eff.push(`Median ${c0.label}: ${vfmt(before.k0, c0.unit)} → ${vfmt(m0, c0.unit)}`);
  const known = new Set(before.conflicts.map(c => c.goal + '|' + c.variable));
  after.conflicts.forEach((c, i) => {
    const txt = `${c.variableLabel} (C${i + 1}) for ${c.goalLabel}`;
    sup.push(`${c.r < 0 ? 'Lower' : 'Raise'} ${txt}`);
    if (!known.has(c.goal + '|' + c.variable)) risks.push(`New input in conflict: ${txt}`);
  });
  const required = !missed ? ['Nothing: all milestones and goals are met.'] : plan ? requiredFromPlan(plan) : [];
  return {
    what: change,
    j,
    option: after.option,
    optionLabel: after.optionLabels[j],
    effect: eff,
    required,
    requiredPending: missed > 0 && !plan,
    recommended: rec,
    supporting: sup.slice(0, 3),
    risks,
    missed,
    tone: risks.length ? 'bad' : eff.length ? 'good' : '',
    leaderChanged,
    pMetChanges,
  };
}

/** Fill "Required" of a toast once the plan is known. */
export function withPlan(report: ChangeReport, plan: Plan): ChangeReport {
  if (!report.missed) return report;
  return { ...report, required: requiredFromPlan(plan), requiredPending: false };
}
