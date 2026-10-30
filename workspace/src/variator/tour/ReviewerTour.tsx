// «Start from the company»: a short tour across pages before the case tour, for a reviewer who has a few minutes. Each
// step names its page (path and parameters); the host on that page shows it, Next navigates to the page of the next
// step, and the last step continues into the case tour (`next.to?tour=1`). Deep link: /workspace/brief?tour=reviewer
// [&step=N]. The pages of the tour file are in-app paths (/brief, /actors…): the router puts the app's base before them.
// The pages mount <ReviewerTour/> once; it renders nothing unless the address asks for the tour.
import { useEffect, useMemo } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Tour } from './Tour';
import type { TourStepDef, TourStepView } from './types';
import { preloadPage } from '../../routes';
import def from '../../../config/tours/reviewer.tour.json';

export interface PathTourDef {
  schema: 'variator-tour/1'; id: string; title: string; minutes: number;
  steps: (TourStepDef & { page: string })[];
  next?: { to: string; label: string };
}
export const REVIEWER_TOUR = def as PathTourDef;

/** The address of step i: its page with its own parameters, the tour's, and the view date kept. */
export function stepHref(t: PathTourDef, i: number, current: URLSearchParams): string {
  const [path, query] = t.steps[i].page.split('?');
  const n = new URLSearchParams(query ?? '');
  n.set('tour', t.id); n.set('step', String(i + 1));
  const asOf = current.get('asOf');
  if (asOf) n.set('asOf', asOf);
  return `${path}?${n}`;
}

export function ReviewerTour({ tour = REVIEWER_TOUR }: { tour?: PathTourDef }) {
  const loc = useLocation(), navigate = useNavigate();
  const params = new URLSearchParams(loc.search);
  const on = params.get('tour') === tour.id;
  const index = Math.min(tour.steps.length, Math.max(1, Math.round(Number(params.get('step'))) || 1)) - 1;
  const step = tour.steps[index];
  const [path, query] = step.page.split('?');
  // the page of the step with its own parameters (an actor, a tab): go there when the address lacks them
  const here = loc.pathname === path && [...new URLSearchParams(query ?? '')].every(([k, v]) => params.get(k) === v);
  useEffect(() => { if (on && !here) navigate(stepHref(tour, index, params), { replace: true }); }, [on, here, index]); // eslint-disable-line react-hooks/exhaustive-deps
  // the code of the page Next (or Finish) opens loads while the reviewer reads this step
  const nextPage = tour.steps[index + 1]?.page ?? tour.next?.to;
  useEffect(() => { if (on && nextPage) preloadPage(nextPage); }, [on, nextPage]);
  const views: TourStepView[] = useMemo(() => tour.steps.map(s => ({ id: s.id, anchor: s.anchor, focus: s.focus, title: s.title, body: s.body, tryText: s.try })), [tour]);
  if (!on || !here) return null;
  const clean = () => { const n = new URLSearchParams(loc.search); n.delete('tour'); n.delete('step'); navigate({ pathname: loc.pathname, search: n.toString() ? `?${n}` : '' }, { replace: true }); };
  return <Tour key={tour.id} label={tour.title} steps={views} index={index} barOffset={20}
    onIndex={i => navigate(stepHref(tour, i, params))}
    onAction={() => {}} actionDone={() => false} changes={0} onReset={() => {}}
    finishLabel={tour.next?.label}
    onClose={how => {
      if (how === 'finish' && tour.next) { const n = new URLSearchParams({ tour: '1' }); const asOf = params.get('asOf'); if (asOf) n.set('asOf', asOf); navigate(`${tour.next.to}?${n}`); }
      else clean();
    }} />;
}
