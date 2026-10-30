// The source of the future sections as one compact badge with a tooltip: «Actors» (simulated on the company's actors,
// with its state: for the defaults, current, or stale after your edits) or «Preview». The text lives in the tooltip.
import { FlaskConical, Hourglass, Users } from 'lucide-react';
import { HelpTip } from '../ui/HelpTip';
import type { ActorState } from './useActors';
import './actors.css';

const STATE_WORDS: Record<ActorState, string> = {
  defaults: 'For the case defaults.',
  current: 'Current: it follows your edits.',
  stale: 'Stale after your edits: rerun on actors (in «Answer») to update it.',
};

export function SourceBadge({ source, state = 'defaults', scope, testId = 'future-source', note, tip, text }: { source: 'actors' | 'preview'; state?: ActorState; scope?: string; testId?: string; note?: string; tip?: string; text?: string }) {
  if (source === 'preview') {
    return <span className="cs-tip-wrap" data-testid={testId} data-source="preview">
      <HelpTip variant="badge" className="cs-badge is-preview" label="Preview" entry={{ title: 'Preview', short: 'A stand-in made from the formula model of the case, not from your company\'s actors. It shows the original case and does not follow your edits.', full: note ?? '' }}>
        <FlaskConical size={12} aria-hidden />Preview
      </HelpTip>
    </span>;
  }
  const stale = state === 'stale';
  return <span className="cs-tip-wrap" data-testid={testId} data-source="actors" data-state={state}>
    <HelpTip variant="badge" className={`cs-badge ${stale ? 'is-warn' : 'is-actors'}`} label={text ?? 'Actors'}
      entry={{ title: `Simulated on your company's actors${scope ? ` · ${scope}` : ''}`, short: tip ?? STATE_WORDS[state], full: 'Each scenario steps your people, clients and vendors month by month with the habits learned from the company history up to the fork date. Nothing after the fork date is used.' }}>
      {stale ? <Hourglass size={12} aria-hidden /> : <Users size={12} aria-hidden />}{text ?? `Actors${stale ? ' · stale' : ''}`}
    </HelpTip>
  </span>;
}
