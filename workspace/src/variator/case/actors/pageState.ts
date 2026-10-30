// The page's edits as the engine's CaseState: the input of the formula engine (model/source.ts) and the body of a rerun
// on actors (WP18's month route takes this CaseState plus the forks that are on). Pure: no worker, no fetch.
import { defaultState, type CaseState } from '../../engine';
import type { CaseRuntime } from '../model/view';
import type { Dist, Edits } from '../model/types';

const isDist = (v: unknown): v is Dist => !!v && typeof v === 'object' && 'kind' in (v as object);
function distEdits(prefix: string, d: Dist, out: Record<string, number | boolean>) {
  if (d.kind === 'fixed') out[`${prefix}|value`] = d.value; else { out[`${prefix}|low`] = d.low; out[`${prefix}|mode`] = d.mode; out[`${prefix}|high`] = d.high; }
}

/** The page's edits as the engine's CaseState. */
export function toState(r: Pick<CaseRuntime, 'lc' | 'monthVars' | 'config'>, edits: Edits): CaseState {
  const s = defaultState(r.lc);
  const presets: string[] = [];
  for (const [path, v] of Object.entries(edits)) {
    const p = path.split('|');
    if (p[0] === 'hyp' && v === true) presets.push(p[1]);
    else if (p[0] === 'v' && isDist(v)) { if (r.monthVars.has(p[1]) && v.kind === 'fixed') s.timeline.moves[p[1]] = v.value; else distEdits(path, v, s.edits); }
    else if ((p[0] === 'o' || p[0] === 'rm') && isDist(v)) distEdits(path, v, s.edits);
    else if (p[0] === 'r' && typeof v === 'number') s.edits[path] = v;
    else if (p[0] === 'ev' && v === true) s.timeline.events.push(p[1]);
    else if (p[0] === 'goal' && v === true) s.timeline.off.push(p[1]);
    else if (p[0] === 'w' && typeof v === 'number') s.prefs.weights[p[1]] = v;
    else if (path === 'caution' && typeof v === 'string') s.prefs.riskAversion = r.config.caution.find(c => c.id === v)?.value ?? null;
    else if (path === 'priority' && typeof v === 'string') s.prefs.priority = v === 'custom' ? null : v;
    else if (path === 'sim|n' && typeof v === 'number') s.n = v;
    else if (path === 'sim|seed' && typeof v === 'number') s.seed = v;
    else if (path === 'week' && Array.isArray(v)) s.selectedForks = v;
    // a backup plan: bk|<id> = «<value>|<op>|<threshold>|<variable>|<change>» (see case/decision.tsx)
    else if (p[0] === 'bk' && typeof v === 'string') {
      const [value, op, threshold, variable, change] = v.split('|');
      if (/^[a-z_][a-z0-9_]*$/i.test(value) && (op === '<' || op === '>') && Number.isFinite(+threshold) && /^[a-z_][a-z0-9_]*$/i.test(variable) && Number.isFinite(+change))
        (s.backups ??= []).push({ id: p[1], if: `${value} ${op} ${+threshold}`, variable, change: +change, desc: `Backup plan: if ${value} ${op} ${threshold}, ${variable} + ${change}` });
    }
  }
  s.activePresets = r.config.scenarios.map(x => x.name).filter(n => presets.includes(n));
  return s;
}

/** The body of a rerun on actors: the page's CaseState, the forks that are on, and the actor run's own n and seed (those
 *  of the precomputed month file, so the defaults give the same result; the formula model's n stays the page's). */
export function monthBody(r: Pick<CaseRuntime, 'lc' | 'monthVars' | 'config'>, edits: Edits, forksOn: string[], n: number, seed: number): CaseState & { forks: string[] } {
  const s = toState(r, edits);
  return { ...s, forks: forksOn, n, seed, selectedForks: [] };
}
