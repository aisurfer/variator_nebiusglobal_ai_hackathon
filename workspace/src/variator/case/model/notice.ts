// A change notice compares the SAME fork before and after a change. The fork is fixed when the change starts (the fork
// the change is about, else the leader then): the leader may change with the edit, and the medians of a new leader
// against those of the old one would read as the effect of the change on one fork (QA M4).
/** The fork the «after» snapshot of a notice describes, and whether the «before» snapshot is of the same fork. */
export function noticeFork(beforeFork: string | null, afterOrder: string[], fallback: string): { fork: string; comparable: boolean } {
  return beforeFork && afterOrder.includes(beforeFork) ? { fork: beforeFork, comparable: true } : { fork: fallback, comparable: false };
}
