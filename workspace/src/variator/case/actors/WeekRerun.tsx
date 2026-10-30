// «Rerun the week for your edits»: the week tree and the day examples of the page state, asked for when the reader
// reaches Week or Day after edits (the month rerun of the verdict returns the months only; WP18 `include: ["week"]`).
import { useEffect, useState } from 'react';
import { LoaderCircle, RefreshCw } from 'lucide-react';
import { useCase } from '../state';
import { useActors } from './useActors';
import './actors.css';

export function WeekRerun({ forks: own }: { forks?: string[] } = {}) {
  const c = useCase(), a = useActors(), r = a.weekRun, running = r.status === 'running';
  // the forks of the day level (else those kept for the week)
  const day = (c.value('day') ?? []) as string[], forks = own ?? (day.length ? day : (c.value('week') ?? []) as string[]);
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(t);
  }, [running]);
  if (!a.weekStale || a.rerunAvailable !== true) return null;
  return <span className="cs-week-rerun">
    <button type="button" className="button" data-testid="week-rerun" aria-disabled={running} onClick={() => { if (!running) a.computeWeek(forks); }}
      title="Simulates the week tree and the example days of the two best forks for the page as it is now">
      {running ? <LoaderCircle size={14} className="spin" aria-hidden /> : <RefreshCw size={14} aria-hidden />}
      {running ? `Rerunning the week… ${((now - r.startedAt) / 1000).toFixed(1)} s` : 'Rerun the week for your edits'}
    </button>
    {r.status === 'error' && <span className="cs-av-run is-error" role="status">The week rerun did not finish ({r.message}).</span>}
  </span>;
}
