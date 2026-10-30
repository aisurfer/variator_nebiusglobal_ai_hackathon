// What the user MAKES of a bad day: «Fix it» turns the cause into a task with an owner; «Key-person risk» records the
// person the bad days depend on, with a mitigation. Both go into the decision draft (case/decision.tsx).
import { useState } from 'react';
import { UserRoundX, Wrench } from 'lucide-react';
import { newId, useDecision } from '../decision';
import type { DayItem } from './model';

export function DayFix({ e, fork, day }: { e: DayItem; fork: string; day: string }) {
  const d = useDecision();
  const [open, setOpen] = useState<'task' | 'risk' | null>(null);
  const [text, setText] = useState(''), [owner, setOwner] = useState(e.person ?? '');
  if (e.tone !== 'bad') return null;
  const done = d.draft.fixes.filter(f => f.day === day && f.text.includes(e.title));
  const start = (k: 'task' | 'risk') => {
    setOpen(k);
    setText(k === 'task' ? `Prevent: ${e.title}${e.cause ? ` (cause: ${e.cause})` : ''}` : `${e.person ?? 'One person'} carries the bad days (${e.title})`);
  };
  const add = () => {
    if (!open || !text.trim()) return;
    d.addFix({ id: newId('f'), kind: open, text: text.trim(), owner: open === 'task' ? owner.trim() || null : null, person: e.person ?? null, entityId: e.personEntity ?? e.entityId ?? null, fork, day,
      mitigation: open === 'risk' ? 'A second person learns the work; on-call shared' : undefined });
    setOpen(null);
  };
  return <span className="cs-dayfix">
    {done.length > 0 && <span className="badge"><i />{done.length} in the decision</span>}
    <button type="button" className="cs-text-btn" onClick={() => start('task')}><Wrench size={12} aria-hidden />Fix it</button>
    {e.person && <button type="button" className="cs-text-btn" onClick={() => start('risk')}><UserRoundX size={12} aria-hidden />Key-person risk</button>}
    {open && <span className="cs-dayfix-form">
      <input aria-label={open === 'task' ? 'The task' : 'The risk'} value={text} onChange={x => setText(x.target.value)} />
      {open === 'task' && <input aria-label="Owner" placeholder="Owner" value={owner} onChange={x => setOwner(x.target.value)} />}
      <button type="button" className="button button-primary" onClick={add}>Add to the decision</button>
    </span>}
  </span>;
}
