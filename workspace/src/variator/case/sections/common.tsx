// Small shared states of the sections: loading, waiting for a choice.
import { LoaderCircle } from 'lucide-react';
import { useCase } from '../state';
import { scenarios } from '../ui/format';

export function Loading({ what }: { what: string }) {
  const c = useCase();
  const n = Number(c.value('sim|n') ?? 4096);
  return <div className="cs-loading" role="status">
    {c.status === 'error' ? <><strong>We could not compute {what}.</strong><p>{c.error}</p></>
      : <><LoaderCircle size={18} className="spin" /><strong>Simulating {scenarios(n)}…</strong><p>The first run of the case takes a second or two.</p></>}
    <div className="cs-skeleton" aria-hidden><i /><i /><i /></div>
  </div>;
}
