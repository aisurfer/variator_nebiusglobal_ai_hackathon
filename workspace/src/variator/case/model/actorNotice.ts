// The change notice on the actors (WP25 R1): the edits rerun on the actors automatically, and the notice compares the
// previous actor result with the new one, for the same fork (by id, QA M4), never with the formula model. Pure: a unit
// test reads it.
import type { ActorMonth } from '../actors/types';
import { fmt, forkLabel, pBestScope, pct } from '../ui/format';
import { lowFirst } from './view';
import type { CaseConfig, ChangeReport } from './types';

interface Row { id: string; label?: string; pMet?: number; pEver?: number; rule?: string | null }
interface Rows { goals?: Row[]; milestones?: Row[]; checks?: Row[]; riskCalendar?: { id: string; label: string }[] }
const rowsOf = (m: ActorMonth, f: string) => m.options?.[f] as unknown as Rows | undefined;

/** The forks of a month by the decision rule, the decision first (only the forks compared on the page). */
export function actorOrder(m: ActorMonth, forksOn?: string[]): string[] {
  return [...(m.ranking?.options ?? [])].filter(o => !forksOn || forksOn.includes(o.option)).sort((a, b) => a.rank - b.rank).map(o => o.option);
}

/** A line «X: a → b» says nothing when a and b read the same: such lines are dropped (QA p2). */
export const isNoop = (line: string) => { const m = /^(.*): (.+) → (.+)$/.exec(line); return !!m && m[2].trim() === m[3].trim(); };
export function dropNoops(r: ChangeReport): ChangeReport {
  const keep = (xs: string[]) => xs.filter(x => !isNoop(x));
  return { ...r, effect: keep(r.effect), recommended: keep(r.recommended), supporting: keep(r.supporting), risks: keep(r.risks) };
}

const MIN = 0.01, BIG = 0.05;

/** The notice of a batch of edits on the actors: `before` is the result the page showed before the edits, `after` the
 *  rerun for the page state as it is now; `about` the fork the edit is about (else the fork that led before). */
export function actorNotice(cfg: CaseConfig, before: ActorMonth, after: ActorMonth, forksOn: string[], about: string | null): { fork: string; comparable: boolean; report: ChangeReport } {
  const ob = actorOrder(before, forksOn), oa = actorOrder(after, forksOn);
  const want = about ?? ob[0] ?? oa[0];
  const comparable = !!want && ob.includes(want) && oa.includes(want), fork = comparable ? want : oa[0] ?? want;
  const L = (f: string) => forkLabel(cfg, f);
  const effect: string[] = [], risks: string[] = [], recommended: string[] = [];
  let good = 0, bad = 0;
  if (ob[0] && oa[0] && ob[0] !== oa[0]) {
    effect.push(`Answer: ${L(ob[0])} → ${L(oa[0])}`);
    recommended.push(`Read the Answer again: it now names ${L(oa[0])}`);
  }
  if (comparable) {
    const rb = before.ranking?.options.find(o => o.option === fork), ra = after.ranking?.options.find(o => o.option === fork);
    if (rb && ra && Math.abs(ra.pBest - rb.pBest) >= MIN) { effect.push(`P(best) of ${L(fork)} (${pBestScope(after.ranking?.options.length ?? oa.length)}): ${pct(rb.pBest)} → ${pct(ra.pBest)}`); (ra.pBest > rb.pBest ? good++ : bad++); }
    // its milestones and goals
    const items = (m: ActorMonth) => { const r = rowsOf(m, fork); const out = new Map<string, Row>(); for (const x of [...(r?.milestones ?? []), ...(r?.goals ?? [])]) if (!out.has(x.id)) out.set(x.id, x); return out; };
    const gb = items(before), ga = items(after);
    for (const [id, a] of ga) {
      const b = gb.get(id);
      if (!b || a.pMet == null || b.pMet == null || Math.abs(a.pMet - b.pMet) < MIN) continue;
      effect.push(`${a.label ?? id}: ${pct(b.pMet)} → ${pct(a.pMet)}`);
      if (a.pMet - b.pMet <= -BIG) bad++; else if (a.pMet - b.pMet >= BIG) good++;
    }
    // the median of the criterion the drivers explain (cash at year end in case B)
    const cr = cfg.criteria.find(x => x.id === cfg.driverCriterion) ?? cfg.criteria[0];
    const sb = rb?.stats?.[cr.id]?.p50, sa = ra?.stats?.[cr.id]?.p50;
    if (cr && sb != null && sa != null && fmt(sb, cr.unit) !== fmt(sa, cr.unit)) effect.push(`Median ${lowFirst(cr.label)}: ${fmt(sb, cr.unit)} → ${fmt(sa, cr.unit)}`);
    // the checks that fire more often
    const cb = rowsOf(before, fork)?.checks ?? [], ca = rowsOf(after, fork)?.checks ?? [];
    const label = (x: Row) => cfg.rules.find(r => r.id === x.rule)?.label ?? rowsOf(after, fork)?.riskCalendar?.find(r => r.id === x.id)?.label ?? x.id;
    for (const a of ca) {
      const b = cb.find(x => x.id === a.id);
      if (!b || a.pEver == null || b.pEver == null || a.pEver - b.pEver < 0.02) continue;
      risks.push(`${label(a)}: ${pct(b.pEver)} → ${pct(a.pEver)}`);
      bad++;
    }
  }
  const tone: ChangeReport['tone'] = bad > good ? 'bad' : good > 0 ? 'good' : 'neutral';
  return { fork, comparable, report: dropNoops({ effect, required: [], recommended, supporting: [], risks, tone }) };
}
