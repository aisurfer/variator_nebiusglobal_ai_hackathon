// The case page: one long decision page. Steps ① Forks → ② Month (Answer, Compare, Over time, Plan, Why, Details)
// → review → ③ Week → review → ④ Day, with a sticky progress bar, help popovers, change notices and resets.
// The header holds only the title, one short question and the tools; the lead, the request and every explanation sit
// behind «Know more», «Your request» and the «?» of each section.
import { DecisionProvider } from './decision';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { CloudOff } from 'lucide-react';
import type { PageContext } from '../../pages';
import type { CaseConfig, StepId } from './model/types';
import { CaseProvider, useCase } from './state';
import { useActorAuto } from './actorAuto';
import { setCaseChrome } from './chrome';
import { ProgressBar, useScrollSpy } from './Progress';
import { ToastStack } from './ui/Toasts';
import { HelpTip, TipBadge } from './ui/HelpTip';
import { longDate, scenarios, shortQuestion } from './ui/format';
import { AnswerSection } from './sections/Answer';
import { CompareSection } from './sections/Compare';
import { ForksSection, StaysSection, WorldSection } from './sections/Forks';
import { OriginDialog, RequestButton } from './sections/Origin';
import { OverTimeSection } from './sections/OverTime';
import { PlanSection } from './sections/Plan';
import { WhySection } from './sections/Why';
import { DetailsSection } from './sections/Details';
import { ReviewGate, WeekSection } from './sections/Week';
import { DaySection } from './sections/Day';
import type { CaseUi } from './ui/caseUi';
import { TourButton } from '../tour';
import { CaseTour, useCaseTour } from './tour';
import { ActorVerdict, useActors } from './actors';

export interface SlotProps { ctx: PageContext; jump: (id: string) => void; week: string[]; ui: CaseUi }
const SLOTS: Record<string, ((p: SlotProps) => ReactNode) | undefined> = {
  world: () => <WorldSection />,
  forks: p => <ForksSection ctx={p.ctx} onOrigin={p.ui.openOrigin} />,
  stays: () => <StaysSection />,
  // the verdict of the actor simulation (WP23, case/actors) at the top of the answer
  answer: () => <AnswerSection top={<ActorVerdict />} />,
  compare: () => <CompareSection />,
  overtime: () => <OverTimeSection />,
  plan: () => <PlanSection />,
  why: () => <WhySection />,
  details: () => <DetailsSection />,
  'week-review': p => <WeekGate jump={p.jump} />,
  week: () => <WeekSection />,
  'day-review': p => <ReviewGate id="day-review" level="Day" path="day" max={2} from={p.week} next={() => p.jump('day')} />,
  day: p => <DaySection ctx={p.ctx} />,
};
function WeekGate({ jump }: { jump: (id: string) => void }) {
  const c = useCase();
  return <ReviewGate id="week-review" level="Week" path="week" max={3} from={c.run?.forks ?? c.forksOn} next={() => jump('week')} />;
}

/** What the user gets in each step: nouns only, shown as a watermark beside the step title. */
const STEP_GAIN: Record<string, string> = {
  forks: 'Your options · What may happen around you · Your timeline',
  month: 'The best option · How often it wins · Risks by month',
  week: 'Week-by-week paths · Early warnings · Backup plans',
  day: 'Example days · Root causes · Fixes and key people',
};

export function CasePage({ cfg, ctx }: { cfg: CaseConfig; ctx: PageContext }) {
  return <CaseProvider cfg={cfg}><DecisionProvider caseId={cfg.id}><CaseBody ctx={ctx} /></DecisionProvider></CaseProvider>;
}

function CaseBody({ ctx }: { ctx: PageContext }) {
  const c = useCase(), cfg = c.cfg, actors = useActors();
  // after each edit the page reruns on the actors; meanwhile the actor numbers shown are dimmed (data-actors)
  useActorAuto();
  const ids = useMemo(() => cfg.sections.map(s => s.id), [cfg]);
  const { active, jump } = useScrollSpy(ids);
  const [viewed, setViewed] = useState<Set<StepId>>(new Set());
  const [origin, setOrigin] = useState<{ open: boolean; modal: boolean }>({ open: false, modal: true });
  const originFrom = useRef<Element | null>(null);
  const ui: CaseUi = useMemo(() => ({
    openOrigin: (opts?: { tour?: boolean }) => { if (!opts?.tour) originFrom.current = document.activeElement; setOrigin({ open: true, modal: !opts?.tour }); },
    closeOrigin: () => setOrigin(o => ({ ...o, open: false })),
    originOpen: origin.open,
  }), [origin.open]);
  const closeOrigin = useCallback(() => setOrigin(o => ({ ...o, open: false })), []);
  const tour = useCaseTour(cfg.id);
  const step = cfg.sections.find(s => s.id === active)?.step ?? 'forks';
  useEffect(() => { if (step !== 'forks') setViewed(v => v.has(step) ? v : new Set([...v, step])); }, [step]);
  useEffect(() => {
    const st = cfg.steps.find(s => s.id === step);
    setCaseChrome({ caseTitle: cfg.title, step: st ? st.label : null, forkDate: cfg.forkDate, future: step !== 'forks', recomputing: c.status === 'computing' || c.stale });
  }, [cfg, step, c.status, c.stale]);
  const week = (c.value('week') ?? []) as string[], day = (c.value('day') ?? []) as string[];
  const statusOf = (s: StepId) => {
    if (s === 'forks') return `${c.forksOn.length} forks · ${c.hypotheses.length} hypothes${c.hypotheses.length === 1 ? 'is' : 'es'} on`;
    if (s === 'month') return c.run ? `${scenarios(c.run.n)}${viewed.has('month') ? ' · viewed' : ''}` : 'Simulating…';
    if (s === 'week') return week.length ? `${week.length} fork${week.length === 1 ? '' : 's'}${c.run?.preview.paths ? ` · ${c.run.preview.paths} paths` : ''}` : 'Choose forks';
    return day.length ? `3 examples × ${day.length}` : 'Choose a fork';
  };
  return <div className="cs-page" data-actors={actors.pending ? 'pending' : actors.rankState === 'stale' ? 'unavailable' : undefined}>
    <header className="cs-head">
      <div className="eyebrow intro-date">DECISIONS<span>·</span>CASE {cfg.letter}<span>·</span>FORK DATE T {longDate(cfg.forkDate).toUpperCase()}<span>·</span>{cfg.horizonMonths} MONTHS</div>
      <div className="cs-head-row">
        <h1>{cfg.title}<span className="lime-period">.</span></h1>
        <HelpTip variant="info" size={18} label="this case" entry={{ title: `About «${cfg.title}»`, short: cfg.lead, full: cfg.question }} />
        <div className="cs-head-tools">
          {!c.storageOk && <TipBadge testId="storage-off" tone="warn" icon={<CloudOff size={12} aria-hidden />} text="Not saved" title="Edits are not saved" tip="This browser does not allow storage: your edits live in this page only and are lost on reload." />}
          {cfg.origin && <RequestButton onOpen={() => ui.openOrigin()} tour="origin-button" />}
          {tour.def && <TourButton onStart={() => tour.start(0)} />}
        </div>
      </div>
      <p className="cs-question" data-testid="case-question">{shortQuestion(cfg.question)}</p>
    </header>
    <ProgressBar active={active} jump={jump} statusOf={statusOf} viewed={viewed} />
    {cfg.steps.map(st => <div key={st.id} className="cs-step-block" data-step={st.id}>
      <div className="cs-step-divider"><span className="cs-step-numeral" aria-hidden>{String(st.number).padStart(2, '0')}</span><div><span className="cs-step-eyebrow">Step {st.number} of {cfg.steps.length}</span><h2>{st.label}</h2></div><HelpTip entry={cfg.help[st.help]} label={`Step ${st.number}`} />{STEP_GAIN[st.id] && <p className="cs-step-gain" aria-label={`In this step: ${STEP_GAIN[st.id]}`}>{STEP_GAIN[st.id]}</p>}</div>
      {cfg.sections.filter(s => s.step === st.id).map(s => {
        const slot = SLOTS[s.id];
        return <div key={s.id} className="cs-slot" data-tour={s.id}>{slot ? slot({ ctx, jump, week, ui }) : <section id={s.id} className="cs-section cs-placeholder"><header className="cs-section-head"><div className="cs-section-title"><h2>{s.label}</h2></div></header></section>}</div>;
      })}
    </div>)}
    {cfg.origin && <OriginDialog origin={cfg.origin} caseTitle={cfg.title} open={origin.open} modal={origin.modal} onClose={closeOrigin} returnTo={originFrom} />}
    <CaseTour tour={tour} ctx={ctx} ui={ui} />
    <ToastStack toasts={c.toasts} onDismiss={c.dismissToast} />
  </div>;
}
