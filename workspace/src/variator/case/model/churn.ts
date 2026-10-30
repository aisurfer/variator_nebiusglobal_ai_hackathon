// What «churn» counts on the case page against the brief (QA m2, WP18's `options[fork].churnBreakdown`): the case
// criterion counts notices given after T (and downgrades); the brief counts contracts that ended. Counted like the
// brief, year 1 includes the clients who had already given notice before T. One line, the rest behind «?». Pure.
import type { ActorMonth } from '../actors/types';
import type { CaseConfig } from './types';
import { dayMonth } from '../ui/format';

interface Band { p10?: number; p50: number; p90?: number; definition?: string }
export interface ChurnBreakdown {
  alreadyNoticed?: { arrShare: number; clients: { id: string; name: string; arr: number; endDate: string }[]; definition?: string };
  newNotices?: Band; downgrades?: Band; criterion?: Band; terminations?: Band; total?: Band;
}

export function churnBreakdown(m: ActorMonth | null, fork: string): ChurnBreakdown | null {
  const b = (m?.options?.[fork] as unknown as { churnBreakdown?: ChurnBreakdown } | undefined)?.churnBreakdown;
  return b?.criterion && b.terminations ? b : null;
}

/** The churn criterion of a case (by id, else by its label). */
export const churnCriterion = (cfg: CaseConfig) => cfg.criteria.find(c => c.id === 'churn') ?? cfg.criteria.find(c => /churn/i.test(c.label));

const share = (x: number) => `${(100 * x).toFixed(Math.abs(x) < 0.1 ? 1 : 0)}%`;
const day = dayMonth;
const NUMBER_WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten'];

/** «Churn here counts notices given after 26 Oct (18%). Counted like the brief (contracts ending): about 17% in year 1,
 *  including 3.6% from 5 clients who had already given notice.» */
export function churnLine(b: ChurnBreakdown, anchor: string, horizonMonths: number, forkName?: string): string {
  const when = horizonMonths === 12 ? 'in year 1' : `over ${horizonMonths} months`;
  const a = b.alreadyNoticed, n = a?.clients.length ?? 0;
  const already = a && n ? `, including ${share(a.arrShare)} from ${n} client${n === 1 ? '' : 's'} who had already given notice` : '';
  return `Churn here counts notices given after ${day(anchor)} (${share(b.criterion!.p50)}${forkName ? ` for ${forkName}` : ''}). Counted like the brief (contracts ending): about ${share(b.terminations!.p50)} ${when}${already}.`;
}

/** The «?» of the line, in plain words: what each count is, and the clients who had given notice before T with the
 *  dates their contracts end. */
export function churnMore(b: ChurnBreakdown, anchor: string, horizonMonths: number): string[] {
  const T = day(anchor), when = horizonMonths === 12 ? 'within the year' : `within ${horizonMonths} months`;
  const out = [
    `Churn here: the share of yearly revenue on ${T} whose clients give notice after ${T}, plus downgrades. The case ranks the forks on it.`,
    `Like the brief: the share of that revenue whose contracts end ${when}, plus downgrades. It includes the clients who gave notice before ${T}.`,
  ];
  const cs = b.alreadyNoticed?.clients ?? [];
  if (cs.length) out.push(`${upFirst(NUMBER_WORDS[cs.length] ?? String(cs.length))} client${cs.length === 1 ? '' : 's'} had already given notice: ${cs.map(c => `${c.name} (ends ${day(c.endDate)} ${c.endDate.slice(0, 4)})`).join(', ')}.`);
  return out;
}
const upFirst = (x: string) => x.charAt(0).toUpperCase() + x.slice(1);
