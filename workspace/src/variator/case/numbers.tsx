// One source of numbers per page state (QA M1/M2). While the actor result is current, every ranking number on the page
// (fork cards, Compare, the tour's texts) comes from the actor simulation; after an edit, until a rerun on actors, the
// formula model gives them all, as the «draft after your edits»; without actor data, the formula model alone. The
// milestones, goals and checks come from month.json too (WP18); blocks with no actor equivalent (the chain of causes,
// the inputs in conflict, the levers plan) are labelled «Quick formula model» or closed as a «Formula estimate».
import type { ReactNode } from 'react';
import { FlaskConical, LoaderCircle, TriangleAlert, Users } from 'lucide-react';
import { useActors } from './actors';
import { useCase } from './state';
import { HelpTip } from './ui/HelpTip';
import { numbersOf, onActors, type Numbers, type NumbersSource } from './model/numbers';

export * from './model/numbers';

export function useNumbers(): Numbers | null {
  const c = useCase(), a = useActors();
  return numbersOf(c, a);
}

/** A small badge that says where the numbers of a block come from. */
export function NumbersBadge({ source, formulaOnly = false }: { source: NumbersSource; formulaOnly?: boolean }) {
  if (formulaOnly) return <HelpTip variant="badge" className="cs-badge is-formula" label="Quick formula model" entry={{ title: 'Quick formula model', short: 'These numbers come from the quick formula model of the case, not from the simulation on your company\'s actors.', full: 'The actor simulation gives the answer, the ranking and the months. Blocks it does not compute yet are shown from the formula model, labelled so.' }}><FlaskConical size={12} aria-hidden />Quick formula model</HelpTip>;
  if (source === 'actors') return <HelpTip variant="badge" className="cs-badge is-actors" label="On your company's actors" entry={{ title: 'On your company\'s actors', short: 'These numbers come from the simulation on your company\'s actors, the same as the answer above.', full: '' }}><Users size={12} aria-hidden />On your company's actors</HelpTip>;
  if (source === 'pending') return <HelpTip variant="badge" className="cs-badge is-actors is-pending" label="Recomputing on your company's actors" entry={{ title: 'Recomputing on your company\'s actors', short: 'Your edit is being simulated on your company\'s actors. Until it is back, the previous actor numbers stay, dimmed.', full: '' }}><LoaderCircle size={12} className="spin" aria-hidden />Recomputing on your company's actors…</HelpTip>;
  if (source === 'draft') return <HelpTip variant="badge" className="cs-badge is-warn" label="Quick estimate, actors unavailable" entry={{ title: 'Quick estimate (formula model): actors unavailable', short: 'The actors could not answer for this state, so the quick formula model gives these numbers. Rerun on actors in «Answer» to try again.', full: '' }}><TriangleAlert size={12} aria-hidden />Quick estimate · actors unavailable</HelpTip>;
  return null;
}

/** Formula numbers of a block that has no actor numbers yet, while the actor result is current: closed, labelled. */
export function FormulaEstimate({ source, what, children }: { source: NumbersSource; what: string; children: ReactNode }) {
  if (!onActors(source)) return <>{children}</>;
  return <details className="cs-formula-estimate" data-testid="formula-estimate">
    <summary><FlaskConical size={14} aria-hidden /><span>Formula estimate</span><small>{what}</small></summary>
    <div className="cs-formula-estimate-body">{children}</div>
  </details>;
}
