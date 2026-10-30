// The guided tour of a case page: its steps from config/cases/<case>.tour.json, its texts filled from the live state of
// the page (no number is written by hand), its «Show me» actions performed through the page's own state layer (so the
// usual notice appears), the first-visit offer, and the deep link `?tour=1&step=N`. The tour component is generic
// (../tour); what a tour file may name is in ./tourSpec.ts.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowUpRight, LoaderCircle, MessagesSquare, Waypoints } from 'lucide-react';
import type { PageContext } from '../../pages';
import { appPath } from '../../appUrl';
import { Tour, TourOffer, fillTemplate, type TourDef, type TourStepDef, type TourStepView } from '../tour';
import { readTour } from './model/cases';
import { goalViews, numbersOf, planFork } from './numbers';
import { closeCallOf, formulaInput, tiesOf, versusByMeans, versusDecision, type CloseCall, type Versus } from './model/closeCall';
import { actorInput, useActors, type ActorsApi } from './actors';
import { source } from './model/source';
import type { CaseConfig, Criterion, Dist, EditValue } from './model/types';
import { lowFirst } from './model/view';
import { BAR_OFFSET } from './Progress';
import { applyPreset } from './sections/Compare';
import { closeCallSentence } from './sections/Answer';
import { num } from './sections/Forks';
import { useSuggestedForks } from './sections/Week';
import { useCase, type CaseApi } from './state';
import { useDayData, type DayRecord } from './day/load';
import { exampleCount } from './day/model';
import type { CaseUi } from './ui/caseUi';
import { calMonth, fmt, fmtDiff, forkLabel, longDate, pBestScope, pct } from './ui/format';
import { HelpTip } from './ui/HelpTip';

const KEY = (id: string) => `variator:tour:${id}`;
const recall = (id: string) => { try { return localStorage.getItem(KEY(id)); } catch { return null; } };
const remember = (id: string, v: 'started' | 'dismissed' | 'done') => { try { localStorage.setItem(KEY(id), v); } catch { /* storage off: the offer comes again */ } };

export interface CaseTourState {
  def: TourDef | null; open: boolean; step: number; offer: boolean;
  start: (step?: number) => void; setStep: (i: number) => void; close: () => void; dismissOffer: () => void;
}

/** The tour of a case: its file, open or not, the step; the deep link starts it, the first visit offers it. */
export function useCaseTour(caseId: string): CaseTourState {
  const [def, setDef] = useState<TourDef | null>(null);
  const location = useLocation(), navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const [open, setOpen] = useState(false), [step, setStep] = useState(0), [offer, setOffer] = useState(false);
  const deep = useRef({ tour: params.get('tour'), step: params.get('step') });
  useEffect(() => { let live = true; readTour(caseId).then(d => { if (live) setDef(d); }).catch(() => { /* no tour */ }); return () => { live = false; }; }, [caseId]);
  useEffect(() => {
    if (!def) return;
    if (deep.current.tour === '1') {
      setStep(Math.min(def.steps.length, Math.max(1, Math.round(Number(deep.current.step)) || 1)) - 1);
      setOpen(true); remember(caseId, 'started');
    } else if (!recall(caseId)) setOffer(true);
  }, [def, caseId]);
  // the address follows the tour (?tour=1&step=N), so a link reproduces it; it is clean again once the tour closes
  const want = open ? `1|${step + 1}` : '|';
  useEffect(() => {
    // the address as it is now (not as it was at the last render): a step change and a close in a row both land
    const n = new URLSearchParams(window.location.search);
    if (!def || `${n.get('tour') ?? ''}|${n.get('step') ?? ''}` === want) return;
    if (open) { n.set('tour', '1'); n.set('step', String(step + 1)); } else { n.delete('tour'); n.delete('step'); }
    navigate({ pathname: appPath(window.location.pathname), search: n.toString() ? `?${n}` : '' }, { replace: true });
  }, [want, def]); // eslint-disable-line react-hooks/exhaustive-deps
  return {
    def, open, step, offer,
    start: (s = 0) => { setStep(s); setOpen(true); setOffer(false); remember(caseId, 'started'); },
    setStep,
    close: () => { setOpen(false); remember(caseId, 'done'); },
    dismissOffer: () => { setOffer(false); remember(caseId, 'dismissed'); },
  };
}

const fixed = (v: number): Dist => ({ kind: 'fixed', value: v });
const list = (xs: string[]) => xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** The month a «timeline.move» moves its item to: its value before your edits, plus `by`, inside the horizon. */
function moveOf(c: CaseApi, step: TourStepDef | undefined) {
  const a = step?.action;
  if (a?.id !== 'timeline.move') return null;
  const it = c.cfg.interventions.find(i => i.id === a.args?.item);
  if (!it) return null;
  const from = num(c.baseOf(it.monthPath)), to = Math.min(c.cfg.horizonMonths, Math.max(0, from + Number(a.args?.by ?? 0)));
  return { it, from, to };
}

const NUMBER_WORDS = ['no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'];
const upFirst = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const amount = (d: { crit: Criterion; diff: number }) => fmtDiff(Math.abs(d.diff), d.crit.unit).replace(/^[+−±]/, '');
/** The honest answer when the decision is the baseline or ties with the runner-up: lever by lever, what it does against
 *  the decision beyond the meaningful difference («Reduce tech debt is equal to it; Hire 5 engineers costs about $504k
 *  of cash at year end for gains within the noise; …»). Null when neither holds (the plain answer then). */
function honestAnswer(cfg: CaseConfig, order: string[], vs: Versus[], prefix = '', mode: 'short' | 'full' = 'full'): string | null {
  const d = order[0], D = forkLabel(cfg, d), base = cfg.forks.find(f => f.baseline)?.id;
  const runner = vs[0];
  if (d !== base) return runner && !runner.better.length && !runner.worse.length ? `${prefix}${prefix ? 'the' : 'The'} decision is ${D}: it is equal to ${forkLabel(cfg, runner.fork)} within the meaningful difference on every criterion and wins the tie` : null;
  if (!vs.length) return null;
  // short names only the first criterion of each side; full names them all
  const few = <T,>(xs: T[]) => mode === 'short' ? xs.slice(0, 1) : xs;
  const clause = (v: (typeof vs)[number]) => {
    const F = forkLabel(cfg, v.fork), money = v.worse.find(w => w.crit.unit.startsWith('USD')), others = v.worse.filter(w => w !== money).map(w => lowFirst(w.crit.label));
    if (!v.better.length && !v.worse.length) return `${F} is equal to it`;
    if (!v.better.length) return `${F} ${money ? `costs about ${amount(money)} of ${lowFirst(money.crit.label)}` : `is worse on ${list(few(others))}`}${money && others.length && mode === 'full' ? ` and is worse on ${list(others)}` : ''}${v.noise.length ? ' for gains within the noise' : ''}`;
    const gain = few(v.better.map(b => lowFirst(b.crit.label))), loss = few(v.worse.map(w => lowFirst(w.crit.label)));
    return `${F} is better on ${list(gain)}${loss.length ? ` but worse on ${list(loss)}` : ''}`;
  };
  // a lever better wherever it differs and still behind: the checks decide; the plain answer says it better
  if (vs.some(v => v.better.length && !v.worse.length)) return null;
  const n = vs.length, nWord = NUMBER_WORDS[n] ?? String(n);
  if (mode === 'full') return `${prefix}${prefix ? 'none' : 'None'} of the ${nWord} levers beats ${D} by a meaningful margin. ${upFirst(vs.map(clause).join('; '))}`;
  // short: the levers equal to it, then the costliest one and the one that gains the most (the rest is in «Know more»)
  const equal = vs.filter(v => !v.better.length && !v.worse.length).map(v => forkLabel(cfg, v.fork));
  const usd = (v: Versus) => Math.abs(v.worse.find(w => w.crit.unit.startsWith('USD'))?.diff ?? 0);
  const rest = vs.filter(v => v.better.length || v.worse.length);
  const costly = [...rest].filter(v => !v.better.length && usd(v) > 0).sort((a, b) => usd(b) - usd(a))[0];
  const gainer = [...rest].filter(v => v.better.length).sort((a, b) => b.better.length - a.better.length)[0];
  const picked = [costly, gainer].filter((v): v is Versus => !!v);
  const parts = [
    `${prefix}${prefix ? 'no' : 'No'} lever beats ${D} by a meaningful margin`,
    equal.length ? `${list(equal)} ${equal.length === 1 ? 'is' : 'are'} equal to it` : '',
    picked.length ? upFirst(picked.map(clause).join('; ')) : '',
  ].filter(Boolean);
  return parts.join('. ');
}

/** The value of a template variable from the live page (undefined while it is not known). See tourSpec.TOUR_VARS. */
function resolver(c: CaseApi, step: TourStepDef | undefined, actors: ActorsApi, day: DayRecord) {
  const cfg = c.cfg, rk = c.ranking, run = c.run, best = c.best, o = cfg.origin;
  const leader = rk?.order[0];
  const move = moveOf(c, step);
  const closeOf = (cc: CloseCall | null) => cc ? `, but it is a close call: ${forkLabel(cfg, cc.rival)} comes out on top in ${pct(cc.pRival)} of the scenarios (${pBestScope(cc.pBestOf)})` : '';
  const verdict = actors.month && actors.verdict && actors.rankState !== 'stale' ? actors.verdict : null;
  const nums = numbersOf(c, actors);
  return (name: string): string | undefined => {
    const [a, b, d] = name.split('.');
    switch (a) {
      case 'author': return o?.author.name;
      case 'requestedOn': return o ? longDate(o.requestedOn) : undefined;
      case 'frozenOn': return o ? longDate(o.frozenOn) : undefined;
      case 'drafted': return o ? String(o.drafted[b as 'forks' | 'hypotheses' | 'criteria']?.length ?? '') || undefined : undefined;
      case 'forkList': return list(cfg.forks.map(f => f.label));
      // the leader the screen shows: the actors' while their result is current, else the formula model's (one source)
      case 'leader': return nums?.order[0] ? forkLabel(cfg, nums.order[0]) : undefined;
      case 'runnerUp': return nums?.order[1] ? forkLabel(cfg, nums.order[1]) : undefined;
      case 'pBest': return nums?.order[0] ? pct(nums.pBest[nums.order[0]] ?? 0) : undefined;
      case 'pBestOf': return nums ? pBestScope(nums.pBestOf) : undefined;
      case 'feasible': return nums?.order[0] ? pct(nums.feasible[nums.order[0]] ?? 0) : best ? pct(best.feasible) : undefined;
      case 'closeCall': {
        if (!rk || !leader) return undefined;
        return closeOf(closeCallOf(cfg, formulaInput(run, rk))) || `, best in ${pct(rk.pBest[leader])} of the scenarios (${pBestScope(rk.order.length)})`;
      }
      case 'answer': case 'levers': {
        // the actor result is the answer while it is current (and while it reruns after a Show me of the tour); after
        // edits the formula model gives the draft. Honest about ties: lever by lever against the decision, beyond the
        // meaningful difference. `answer` is short (the levers that matter), `levers` names them all (Know more).
        const mode = a === 'answer' ? 'short' : 'full';
        const onActors = actors.month && actors.verdict && (actors.rankState !== 'stale' || actors.rerun.status === 'running') ? actors.verdict : null;
        if (onActors && actors.month) {
          const M = actors.month, order = onActors.order.map(o => o.fork);
          const mean = (f: string, k: string) => M.ranking?.options.find(o => o.option === f)?.stats?.[k]?.mean;
          const pre = "On your company's actors, ", cc = mode === 'short' ? closeCallOf(cfg, actorInput(cfg, M, c.forksOn)) : null;
          // a close call is the message (a third fork wins most scenarios): the trade-off in one sentence
          if (cc) return `${pre}${closeCallSentence(cfg, cc)}`;
          return honestAnswer(cfg, order, versusByMeans(cfg, order, mean, tiesOf(run)), pre, mode)
            ?? `${pre}${forkLabel(cfg, onActors.leader)} leads, best in ${pct(onActors.pBest)} of the scenarios (${pBestScope(onActors.pBestOf)})${closeOf(closeCallOf(cfg, actorInput(cfg, M, c.forksOn)))}`;
        }
        if (!rk || !leader) return undefined;
        const cc = mode === 'short' ? closeCallOf(cfg, formulaInput(run, rk)) : null;
        if (cc) return upFirst(closeCallSentence(cfg, cc));
        return honestAnswer(cfg, rk.order, versusDecision(cfg, run, rk), '', mode) ?? `The decision is ${forkLabel(cfg, leader)}${closeOf(closeCallOf(cfg, formulaInput(run, rk))) || `, best in ${pct(rk.pBest[leader])} of the scenarios (${pBestScope(rk.order.length)})`}`;
      }
      case 'from': return move ? calMonth(cfg, move.from) : undefined;
      case 'to': return move ? calMonth(cfg, move.to) : undefined;
      case 'examples': {
        // the example days the Day section shows for the leader: the actor day file (WP19) once the page is on actors,
        // the preview's only while the page has no actor set
        if (!leader) return undefined;
        if (actors.status === 'ready') return day.status === 'loading' ? undefined : String(exampleCount(day.file, leader));
        return actors.status === 'loading' || !run ? undefined : String(run.preview.days[leader]?.length ?? 0);
      }
      case 'hyp': return cfg.hypotheses.find(h => h.id === b)?.label;
      case 'fork': return cfg.forks.find(f => f.id === b)?.label;
      case 'preset': return cfg.priorityPresets.find(p => p.id === b)?.label;
      case 'item': return cfg.interventions.find(i => i.id === b)?.label;
      case 'milestone': return cfg.milestones.find(m => m.id === b)?.label;
      case 'crit': { const cr = cfg.criteria.find(x => x.id === b); return cr ? lowFirst(cr.label) : undefined; }
      case 'stat': case 'month': {
        // of the leader the answer names: on the actors while their verdict is current, else of the formula model
        const cr = cfg.criteria.find(x => x.id === b);
        const s = verdict ? actors.month?.ranking?.options.find(x => x.option === verdict.leader)?.stats?.[b] as Record<string, number> | undefined
          : run && leader ? run.stats[leader]?.[b] as unknown as Record<string, number> : undefined;
        const v = s?.[d];
        if (!cr || v == null || !Number.isFinite(v)) return undefined;
        return a === 'stat' ? fmt(v, cr.unit) : calMonth(cfg, Math.round(v * 2) / 2);
      }
      case 'pmet': {
        // the number the Plan shows: of the actors while their result is current, of the formula model after edits
        const f = d === 'plan' ? planFork(c, nums) : d === 'leader' ? nums?.order[0] : d;
        const p = f ? goalViews(c, actors, f).find(x => x.id === b)?.p : undefined;
        return p == null ? undefined : pct(p);
      }
      case 'risk': {
        const r = best?.risks[Number(b) - 1];
        if (!r) return undefined;
        return d === 'head' ? r.head : d === 'odds' ? r.odds : r.month?.label;
      }
      case 'cal': {
        if (!run || !leader) return undefined;
        const top = source.calendar(run, cfg, leader).filter(r => r.total >= 0.005).sort((x, y) => y.total - x.total)[0];
        if (!top) return undefined;
        return d === 'label' ? lowFirst(top.label) : d === 'share' ? pct(top.total) : top.medianMonth ? calMonth(cfg, top.medianMonth) : undefined;
      }
      case 'chain': {
        const ids = cfg.chainPath.flat(), id = b === 'first' ? ids[0] : ids[ids.length - 1];
        const n = cfg.chain.find(x => x.id === id);
        return n ? lowFirst(n.label) : undefined;
      }
      default: return undefined;
    }
  };
}

/** The tour over a case page (or its first-visit offer). */
export function CaseTour({ tour, ctx, ui }: { tour: CaseTourState; ctx: PageContext; ui: CaseUi }) {
  const c = useCase(), cfg = c.cfg, def = tour.def;
  const actors = useActors(), suggested = useSuggestedForks(c.run?.forks ?? [], 3), dayData = useDayData(c.cfg.id);
  const startEdits = useRef<Record<string, EditValue>>({});
  const wasOpen = useRef(false);
  if (tour.open && !wasOpen.current) startEdits.current = { ...c.edits };
  wasOpen.current = tour.open;
  const changes = useMemo(() => {
    const before = startEdits.current, keys = new Set([...Object.keys(before), ...Object.keys(c.edits)]);
    return [...keys].filter(k => !same(before[k], c.edits[k])).length;
  }, [c.edits, tour.open]); // eslint-disable-line react-hooks/exhaustive-deps

  const steps = def?.steps ?? [];
  // a notice of the step before that arrives after Next (its simulation was still running) is not this step's: it goes
  const lastAct = useRef(0), quietUntil = useRef(0);
  useEffect(() => {
    const now = Date.now();
    c.toasts.filter(t => t.createdAt < quietUntil.current && now < quietUntil.current).forEach(t => c.dismissToast(t.id));
  }, [c.toasts]); // eslint-disable-line react-hooks/exhaustive-deps
  // the forks of the week gate: by the actor ranking while it is current, else by the formula ranking
  const suggestion = useCallback(() => suggested, [suggested]);
  const done = useCallback((i: number): boolean => {
    const a = steps[i]?.action;
    if (!a) return false;
    // an edit is done once the page answers for it: the actors rerun after each edit (wait while it is pending)
    if (a.id !== 'origin.open' && actors.pending) return false;
    switch (a.id) {
      case 'origin.open': return ui.originOpen;
      case 'hypotheses.on': return ((a.args?.ids ?? []) as string[]).every(id => c.value(`hyp|${id}`) === true);
      case 'timeline.move': { const m = moveOf(c, steps[i]); return !!m && num(c.value(m.it.monthPath)) === m.to && (!m.it.forks?.length || c.timelineFork === m.it.forks[0]); }
      case 'priority.apply': return c.value('priority') === a.args?.preset;
      case 'forks.keep': return ((c.value('week') ?? []) as string[]).length > 0 && ((c.value('day') ?? []) as string[]).length > 0;
      default: return false;
    }
  }, [steps, c, ui.originOpen, actors.pending]);
  const act = useCallback((i: number) => {
    const a = steps[i]?.action;
    if (!a || done(i)) return;
    lastAct.current = Date.now(); quietUntil.current = 0; // from now on the notices are this step's
    switch (a.id) {
      case 'origin.open': ui.openOrigin({ tour: true }); break;
      case 'hypotheses.on': {
        const off = ((a.args?.ids ?? []) as string[]).filter(id => c.value(`hyp|${id}`) !== true);
        const names = off.map(id => `«${cfg.hypotheses.find(h => h.id === id)?.label ?? id}»`);
        if (off.length) c.change(Object.fromEntries(off.map(id => [`hyp|${id}`, true])), `${off.length === 1 ? 'Hypothesis' : 'Hypotheses'} ${list(names)} on`, 'sim');
        break;
      }
      case 'timeline.move': {
        const m = moveOf(c, steps[i]);
        if (!m) break;
        if (m.it.forks?.length === 1) c.setTimelineFork(m.it.forks[0]);
        const now = num(c.value(m.it.monthPath));
        if (now !== m.to) c.change({ [m.it.monthPath]: fixed(m.to) }, `${m.it.label}: ${calMonth(cfg, now)} → ${calMonth(cfg, m.to)}`, 'sim', m.it.forks?.[0]);
        break;
      }
      case 'priority.apply':
        // the page reruns on the actors by itself (WP25 R1): the verdict stays the answer, its notice follows
        applyPreset(c, String(a.args?.preset));
        break;
      case 'forks.keep': {
        const sel = suggestion();
        if (sel.length) c.change({ week: sel, day: [sel[0]] }, `Week: ${sel.map(f => forkLabel(cfg, f)).join(', ')}; day: ${forkLabel(cfg, sel[0])}`, 'quiet');
        break;
      }
    }
  }, [steps, c, cfg, ui, done, suggestion]);

  // a step that opened the origin modal closes it when the tour moves on
  const leave = useCallback((i: number) => { if (steps[i]?.action?.id === 'origin.open' && ui.originOpen) ui.closeOrigin(); }, [steps, ui]);

  const recomputing = (s: TourStepDef) => !!s.action && s.action.id !== 'origin.open' && actors.pending;
  const views: TourStepView[] = steps.map(s => {
    const fill = (t: string) => fillTemplate(t, resolver(c, s, actors, dayData));
    const help = (s.help ?? []).filter(k => cfg.help[k]);
    const more = s.more ? { title: fill(s.more.label), short: fill(s.more.text), full: '' } : null;
    const links = s.links ?? [];
    return {
      id: s.id, anchor: s.anchor, focus: s.focus, title: fill(s.title), body: fill(s.body), tryText: s.try ? fill(s.try) : undefined,
      action: s.action ? { label: fill(s.action.label ?? 'Show me'), then: s.action.then } : undefined,
      see: s.see ? { ...s.see, label: fill(s.see.label) } : undefined,
      extra: help.length || links.length || more || recomputing(s) ? <>
        {recomputing(s) && <p className="vt-status" role="status" data-testid="tour-recomputing"><LoaderCircle size={14} className="spin" aria-hidden />Recomputing on your company's actors…</p>}
        {(help.length > 0 || more) && <div className="vt-more"><span>Know more</span>
          {more && <HelpTip variant="badge" entry={more} label={more.title}>{more.title}</HelpTip>}
          {help.map(k => <HelpTip key={k} variant="badge" entry={cfg.help[k]} label={cfg.help[k].title}>{cfg.help[k].title}</HelpTip>)}</div>}
        {links.length > 0 && <div className="vt-links">{links.map(l => <button key={l.to} type="button" className="vt-link" onClick={() => {
          remember(cfg.id, 'done');
          ctx.go(l.to, { tour: l.tour ? '1' : null, step: null, entity: null, metric: null });
        }}>{l.to.startsWith('/cases/') ? <Waypoints size={15} aria-hidden /> : <MessagesSquare size={15} aria-hidden />}<span>{fill(l.label)}</span><ArrowUpRight size={14} aria-hidden /></button>)}</div>}
      </> : undefined,
    };
  });

  if (!def || !steps.length) return null;
  if (!tour.open) return tour.offer ? <TourOffer minutes={def.minutes} onStart={() => tour.start(0)} onLater={tour.dismissOffer} /> : null;
  return <Tour label={`Guided tour of ${cfg.title}`} steps={views} index={Math.min(tour.step, steps.length - 1)} barOffset={BAR_OFFSET}
    onIndex={i => {
      leave(tour.step);
      // each step starts clean: the notices of the step before have been read
      c.toasts.forEach(t => c.dismissToast(t.id));
      if (Date.now() - lastAct.current < 10_000) quietUntil.current = Date.now() + 4000;
      tour.setStep(Math.max(0, Math.min(steps.length - 1, i)));
    }}
    onAction={act} actionDone={done} changes={changes}
    onReset={() => { c.resetAll(); c.setTimelineFork('best'); }}
    onClose={() => { leave(tour.step); tour.close(); }}
    fallbackFocus={() => document.querySelector<HTMLElement>('[data-tour="tour-button"]')} reserveRight={420} />;
}
