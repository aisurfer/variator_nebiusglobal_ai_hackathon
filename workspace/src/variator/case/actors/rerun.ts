// «Rerun on actors» in the verdict: whether it can run now, and the reason in words when it cannot (shown next to the
// button, not only in a tooltip). Pure (unit-tested): the same words on a server whose actor engine answers and on one
// where it does not.
export interface RerunInput {
  /** The month route of the actor engine answers (null: not probed yet). */
  rerunAvailable: boolean | null;
  /** A rerun for the page state is due or running (auto mode after an edit). */
  pending: boolean;
  /** A rerun asked for with the button is running. */
  running: boolean;
  /** The result shown is not of the page state (its simulation or its priorities). */
  stale: boolean;
}
export type RerunState = 'checking' | 'unavailable' | 'running' | 'current' | 'ready';
export interface RerunWhy { state: RerunState; off: boolean; why: string }

export const RERUN_WHY: Record<Exclude<RerunState, 'ready'>, string> = {
  checking: 'Checking whether the actor engine answers on this server…',
  unavailable: 'Not available here: the actor engine does not answer on this server. The verdict shown is for the default inputs.',
  running: 'Running on your company\'s actors: the new result is on its way.',
  current: 'Up to date: the actors already answered for this page. Edit a fork, an event or a priority to rerun.',
};

export function rerunWhy(a: RerunInput): RerunWhy {
  const state: RerunState = a.rerunAvailable === false ? 'unavailable' : a.rerunAvailable == null ? 'checking'
    : a.running || a.pending ? 'running' : !a.stale ? 'current' : 'ready';
  return { state, off: state !== 'ready', why: state === 'ready' ? '' : RERUN_WHY[state] };
}
