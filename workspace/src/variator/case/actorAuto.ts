// The case page reruns on the actors after each edit (WP25 R1): about 0.6 s after the last change (a drag or a slider
// counts as one edit), through WP18's month route (a weight, caution or tie edit takes its fast path, ~0.1 s; a
// simulation edit 1–4 s). Meanwhile the previous actor result stays on screen, dimmed («Recomputing on your company's
// actors…»); a newer edit aborts an older request. When the result returns, the notice compares the previous actor
// result with the new one for the same fork. The formula model reports only when the actors cannot answer.
import { useEffect, useRef } from 'react';
import { useActors } from './actors';
import type { ActorMonth } from './actors/types';
import { actorNotice } from './model/actorNotice';
import { useCase } from './state';
import { forkLabel, forkNote } from './ui/format';

export const AUTO_DEBOUNCE_MS = 600;

export function useActorAuto() {
  const c = useCase(), a = useActors();
  const usable = a.status === 'ready' && !!a.month && a.rerunAvailable !== false;
  const api = useRef(c);
  api.current = c;
  useEffect(() => { api.current.setNoticeSource(usable ? 'actors' : 'formula'); }, [usable]);

  // the result the page showed before the edits of this batch (the previous current result)
  const before = useRef<ActorMonth | null>(null);
  useEffect(() => { if (a.behind && !before.current && a.month) before.current = a.month; }, [a.behind, a.month]);

  // the rerun, once the edits settle (each newer page state restarts the wait; run() aborts an older request)
  const key = JSON.stringify(c.edits), run = useRef(a.run);
  run.current = a.run;
  useEffect(() => {
    if (!a.pending) return;
    const t = window.setTimeout(() => run.current(), AUTO_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [key, a.pending]);

  // the result is back for the page state: one notice for the batch, actor result against actor result
  const failed = a.behind && !a.pending;
  useEffect(() => {
    const c = api.current;
    if (a.behind && !failed) return;
    const prev = before.current;
    before.current = null;
    const b = c.takeChanges();
    if (!b) return;
    const undo = () => c.change(b.undo, `Undo: ${b.what}`, b.kind, b.fork ?? undefined);
    if (failed) {
      c.pushNotice({ title: b.what, context: 'The actors could not answer for this state', report: { effect: ['The page shows the quick estimate of the formula model until a rerun on actors succeeds.'], required: [], recommended: ['Rerun on actors in «Answer» to try again.'], supporting: [], risks: [], tone: 'bad' }, planPending: false, undo });
      return;
    }
    if (!a.month) return;
    const n = prev && prev !== a.month ? actorNotice(c.cfg, prev, a.month, c.forksOn, b.fork) : null;
    const fork = n?.fork ?? b.fork ?? a.verdict?.leader;
    const report = n && (n.report.effect.length || n.report.risks.length) ? n.report
      : { effect: [n ? 'No measurable change on the actors (below 1 pp).' : 'Back to the result shown: no change on the actors.'], required: [], recommended: [], supporting: [], risks: [], tone: 'neutral' as const };
    // the fork with its edited size, as the formula notice said it: «For Hire 5 engineers (edited: 6)»
    const note = fork ? forkNote(c.cfg, fork, p => c.value(p), p => c.isEdited(p)) : null;
    c.pushNotice({ title: b.what, context: `${fork ? `For ${forkLabel(c.cfg, fork)}${note ? ` (${note})` : ''} · ` : ''}on your company's actors`, report, planPending: false, undo });
  }, [a.behind, failed, a.month, key]); // eslint-disable-line react-hooks/exhaustive-deps
}
