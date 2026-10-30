// What the inspector of the timeline says about the selected marker, and where an event comes from. Pure: the unit
// test (tests/tour-inspector.test.ts) checks the texts per kind. Two kinds, never mixed up:
// - a decision (a lever): yours, in one fork (its date and size change only that fork) or in every fork alike;
// - an event: something in the world; placed on the timeline, it hits every fork the same way (a dated stress test).
// An event names its actor (from the actor cards) and the risk of the register that already fired in the history.
import type { ActorCard, RisksFile } from '../../actors/types';
import type { CaseConfig, Intervention } from './types';
import { forkLabel } from '../ui/format';

export const INSPECTOR_TITLE = 'Selected on the timeline';
export const INSPECTOR_HINT = 'Click any marker to see and change it.';

const list = (xs: string[]) => xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;

/** One line under the item's name: whose it is and what it changes. */
export function inspectorLine(cfg: CaseConfig, i: Intervention): string {
  if (i.kind === 'event') return 'An event in the world. Placed events hit every fork the same way.';
  const forks = i.forks ?? [];
  if (forks.length === 1) return `Your decision in the fork ${forkLabel(cfg, forks[0])}. Its date and size change only that fork.`;
  if (forks.length > 1) return `Your decision in the forks ${list(forks.map(f => forkLabel(cfg, f)))}. Its date and size change only those forks.`;
  return 'Your decision, the same in every fork. Its date and size change every fork alike.';
}

/** The headers of the two groups of the timeline. */
export function groupLabels(cfg: CaseConfig): { decisions: string; events: string } {
  const levers = cfg.interventions.filter(i => i.kind === 'lever');
  const perFork = levers.length > 0 && levers.every(i => (i.forks?.length ?? 0) === 1);
  const common = levers.length > 0 && levers.every(i => !i.forks?.length);
  return {
    decisions: perFork ? 'Your decisions · one per fork' : common ? 'Your decisions · the same in every fork' : 'Your decisions',
    events: 'Events you can place · the same for every fork',
  };
}

/** The actor card an item is about: its knowledge-base entity (org.VN-03, person.lw-e06), else its person. */
export function cardOf(i: Intervention, cards: ActorCard[]): ActorCard | undefined {
  const ids = [i.entityId?.replace(/^(org|person)\./, '').toUpperCase(), i.person?.toUpperCase()].filter((x): x is string => !!x);
  return ids.map(id => cards.find(c => c.id.toUpperCase() === id)).find(Boolean);
}

const arrOf = (c: ActorCard) => c.habits.find(h => h.key === 'arr')?.point ?? null;

/** «Nimbus Cloud is our cloud provider.», «Meridian Freight Group is our largest client, about 8% of ARR.»,
 *  «Daniel Kowalski is the lead backend engineer.» From the actor cards; null when the card is not known. */
export function actorLine(card: ActorCard | undefined, cards: ActorCard[]): string | null {
  if (!card) return null;
  // a title in running text: «Lead Backend Engineer» reads «lead backend engineer»; acronyms (QA, CEO) stay
  const role = (card.title.split(' · ')[0] || card.title).split(' ').map(w => /^[A-Z0-9]{2,}$/.test(w) ? w : w.toLowerCase()).join(' ');
  if (card.kind === 'client') {
    const all = cards.filter(c => c.kind === 'client').map(arrOf).filter((v): v is number => v != null && v > 0);
    // the share of the company's ARR (its card: ARR at T); without it, of the clients with a card
    const company = cards.find(c => c.kind === 'company')?.habits.find(h => h.key === 'arr_at_t' || h.key === 'arr')?.point ?? null;
    const own = arrOf(card), total = company && company > 0 ? company : all.reduce((a, b) => a + b, 0);
    const largest = own != null && all.length > 1 && own >= Math.max(...all);
    const share = own != null && total > 0 ? `, about ${Math.round(own / total * 100)}% of ARR` : '';
    return `${card.name} is our ${largest ? 'largest client' : role}${share}.`;
  }
  if (card.kind === 'person') return `${card.name} is the ${role}.`;
  return `${card.name} is our ${role}.`;
}

/** The risk of the register that models the same event in this case (by the event's model shock), with how often it
 *  already happened in the history. The first one when several do. */
export function riskOf(cfg: CaseConfig, i: Intervention, risks: RisksFile['risks']): { id: string; title: string; count: number } | null {
  if (!i.shock) return null;
  const r = risks.find(x => x.shocks.some(s => s.caseId === cfg.id && s.id === i.shock));
  return r ? { id: r.id, title: r.title, count: r.count } : null;
}

/** «Already happened in the history: 7 times (R4)». */
export const historyLine = (r: { id: string; count: number }) => `Already happened in the history: ${r.count} time${r.count === 1 ? '' : 's'} (${r.id})`;
