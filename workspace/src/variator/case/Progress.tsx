// The sticky progress bar of the decision funnel: four steps with a status line each, the sections of the current
// step under it, the current section highlighted while you scroll (IntersectionObserver), the done part in lime.
// A click scrolls smoothly to a step or a section.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Anchor, CalendarDays, CalendarRange, Check, GitCompareArrows, GitFork, Globe2, Hourglass, ListChecks, LoaderCircle, Microscope, RotateCcw, Route, SlidersHorizontal, Sun, Target, TriangleAlert, Waypoints, type LucideIcon } from 'lucide-react';
import type { StepId } from './model/types';
import { useCase } from './state';
import { DecisionButton } from './DecisionPanel';
import { useActors } from './actors';
import { HelpTip } from './ui/HelpTip';
import { scenarios } from './ui/format';

export const BAR_OFFSET = 72;

/** The section whose part is at the top of the reading zone (just under the sticky bar). */
/** The lead of the row of section links: what the user gets in that step. */
/** The recompute bar: it fills towards 90% in the time the last recompute took (an estimate, so it moves smoothly),
 *  then completes to 100% and fades when the numbers are ready. The page stays usable under it. */
function RecomputeBar({ run, text }: { run: string; text: string }) {
  const active = run === 'busy' || run === 'stale';
  const [phase, setPhase] = useState<'off' | 'run' | 'done'>('off');
  const started = useRef(0), last = useRef(2500);
  useEffect(() => {
    if (active) { if (phase !== 'run') { started.current = performance.now(); setPhase('run'); } return; }
    if (phase !== 'run') return;
    last.current = Math.min(20000, Math.max(800, performance.now() - started.current));
    setPhase('done');
    const t = window.setTimeout(() => setPhase('off'), 450);
    return () => window.clearTimeout(t);
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps
  // a frame after the start the width goes to 90%: the transition then fills it over the estimated time
  const [armed, setArmed] = useState(false);
  useEffect(() => { if (phase !== 'run') { setArmed(false); return; } const f = window.requestAnimationFrame(() => setArmed(true)); return () => window.cancelAnimationFrame(f); }, [phase]);
  if (phase === 'off') return null;
  return <span className={`cs-recompute-bar is-${phase} ${run === 'stale' ? 'is-stale' : ''}`} role="progressbar" aria-label={text} aria-busy={phase === 'run'} title={text}>
    <i style={{ width: phase === 'done' ? '100%' : armed ? '90%' : '0%', transitionDuration: phase === 'run' ? `${last.current}ms` : '250ms' }} /></span>;
}

/** The icon of a section link (by its slot); a slot without one shows only its name. */
const SUB_ICON: Record<string, LucideIcon> = { world: Globe2, forks: GitFork, stays: Anchor, answer: Target, why: Waypoints, compare: GitCompareArrows, overtime: CalendarRange, details: Microscope, week: Route, plan: ListChecks, day: Sun, month: CalendarDays };
const SUB_TITLE: Record<string, string> = { forks: 'Set up the future:', month: 'See the months:', week: 'Check the weeks:', day: 'Live a day:' };

export function useScrollSpy(ids: string[], offset = BAR_OFFSET) {
  const [active, setActive] = useState(ids[0]);
  const visible = useRef(new Set<string>()), lock = useRef(0), pickRef = useRef<() => void>(() => {}), relock = useRef<number | undefined>(undefined);
  const key = ids.join('|');
  useEffect(() => {
    const pick = () => {
      if (Date.now() < lock.current) return;
      const atEnd = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
      if (atEnd) { setActive(ids[ids.length - 1]); return; }
      const first = ids.find(id => visible.current.has(id));
      if (first) setActive(first);
    };
    pickRef.current = pick;
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) visible.current.add(e.target.id); else visible.current.delete(e.target.id); });
      pick();
    }, { rootMargin: `-${offset}px 0px -55% 0px`, threshold: 0 });
    ids.forEach(id => { const el = document.getElementById(id); if (el) io.observe(el); });
    // a jump locks the bar on its target while the smooth scroll runs; once the page stops scrolling, the lock ends and
    // what is on screen counts again (a scroll of your own right after the click included)
    let idle: number | undefined;
    const onScroll = () => {
      if (Date.now() < lock.current) { window.clearTimeout(idle); idle = window.setTimeout(() => { lock.current = 0; pick(); }, 180); return; }
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) pick();
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => { io.disconnect(); window.removeEventListener('scroll', onScroll); window.clearTimeout(relock.current); window.clearTimeout(idle); };
  }, [key, offset]); // eslint-disable-line react-hooks/exhaustive-deps
  const jump = useCallback((id: string) => {
    const el = document.getElementById(id);
    if (!el) return;
    lock.current = Date.now() + 2500;
    setActive(id);
    // the page did not need to scroll (or stopped early): the lock ends anyway
    window.clearTimeout(relock.current);
    relock.current = window.setTimeout(() => { if (lock.current) { lock.current = 0; pickRef.current(); } }, 2550);
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - offset + 8, behavior: reduce ? 'auto' : 'smooth' });
  }, [offset]);
  return { active, jump };
}

export function ProgressBar({ active, jump, statusOf, viewed }: { active: string; jump: (id: string) => void; statusOf: (step: StepId) => string; viewed: Set<StepId> }) {
  const c = useCase();
  const sections = c.cfg.sections, current = sections.find(s => s.id === active) ?? sections[0];
  const stepIndex = c.cfg.steps.findIndex(s => s.id === current.step);
  const [menu, setMenu] = useState(false), [confirm, setConfirm] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null), subRef = useRef<HTMLOListElement>(null);
  // keep the current section visible in the (scrollable) list of sections, without scrolling the page
  useEffect(() => {
    const ol = subRef.current, el = ol?.querySelector<HTMLElement>('.is-active');
    if (ol && el && ol.scrollWidth > ol.clientWidth) ol.scrollTo({ left: el.offsetLeft - ol.offsetLeft - ol.clientWidth / 2 + el.clientWidth / 2, behavior: 'smooth' });
  }, [active]);
  const editCount = Object.keys(c.edits).length;
  const hyp = c.hypotheses.length;
  // up to date once the formula run and, where they answer, the actors have the page state (the auto rerun)
  const actors = useActors(), onActors = actors.pending && c.status === 'ready' && !c.stale;
  const run = c.status === 'error' ? 'error' : c.status === 'loading' || c.status === 'computing' || onActors ? 'busy' : c.stale ? 'stale' : 'ok';
  const runText = run === 'busy' ? (c.status === 'loading' ? 'Simulating…' : onActors ? 'Recomputing on your company\'s actors…' : 'Recomputing…') : run === 'stale' ? 'Stale: waiting for the recompute' : run === 'error' ? 'Not computed' : 'Up to date';
  useEffect(() => {
    // the options menu and its in-page confirm sit where the notices are: the notices step aside while it is open
    document.body.classList.toggle('cs-confirming', menu);
    return () => document.body.classList.remove('cs-confirming');
  }, [menu]);
  useEffect(() => {
    if (!menu) { setConfirm(false); return; }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { if (confirm) setConfirm(false); else setMenu(false); } };
    const out = (e: MouseEvent) => { if (!menuRef.current?.contains(e.target as Node)) setMenu(false); };
    document.addEventListener('keydown', esc); document.addEventListener('mousedown', out);
    return () => { document.removeEventListener('keydown', esc); document.removeEventListener('mousedown', out); };
  }, [menu, confirm]);
  return <nav className="cs-progress" aria-label="Decision steps" data-step={current.step}>
    <ol className="cs-steps">
      {c.cfg.steps.map((s, i) => {
        const own = sections.filter(x => x.step === s.id), at = own.findIndex(x => x.id === active);
        const fill = i < stepIndex ? 1 : i > stepIndex ? 0 : (at + 1) / own.length;
        const state = i < stepIndex ? 'done' : i === stepIndex ? 'current' : 'next';
        return <li key={s.id} className={`cs-step is-${state}`} data-step={s.id}>
          <button type="button" onClick={() => jump(own[0].id)} aria-current={state === 'current' ? 'step' : undefined} title={`Step ${s.number} · ${s.label}: ${statusOf(s.id)}`}>
            <span className="cs-step-num">{state === 'done' || viewed.has(s.id) && state !== 'current' ? <Check size={11} strokeWidth={3} /> : s.number}</span>
            <span className="cs-step-label">{s.label}</span>
            <span className="cs-step-track" aria-hidden><i style={{ width: `${fill * 100}%` }} /></span>
          </button>
        </li>;
      })}
    </ol>
    <ol ref={subRef} className="cs-sub-sections" aria-label={`Sections of step ${c.cfg.steps[stepIndex].number}`}>
      <li className="cs-sub-title" aria-hidden>{SUB_TITLE[current.step] ?? 'Go to:'}</li>
      {sections.filter(s => s.step === current.step).map(s => <li key={s.id}><button type="button" className={s.id === active ? 'is-active' : ''} aria-current={s.id === active ? 'location' : undefined} onClick={() => jump(s.id)}>{(() => { const I = SUB_ICON[s.id]; return I ? <I size={13} aria-hidden /> : null; })()}{s.label}</button></li>)}
    </ol>
    <div className="cs-progress-tools" ref={menuRef}>
      <DecisionButton />
      <span className={`cs-run-dot is-${run}`} data-state={run} role="status" aria-label={runText} title={runText} />
      {c.compare && <span className="cs-compare-flag" title="Compare with original is on"><GitCompareArrows size={13} /></span>}
      <button type="button" className={`cs-menu-btn ${menu ? 'is-open' : ''}`} aria-haspopup="dialog" aria-expanded={menu} aria-label="Case options" title="Case options: run, compare, reset" onClick={() => setMenu(v => !v)}>
        <SlidersHorizontal size={16} />{editCount > 0 && <span className="cs-menu-count" aria-label={`${editCount} edits`}>{editCount}</span>}
      </button>
      {menu && <div className="cs-menu" role="dialog" aria-label="Case options">
        {!confirm ? <>
          <div className="cs-menu-run">
            <span className={`cs-run-state is-${run}`}>{run === 'busy' ? <LoaderCircle size={14} className="spin" /> : run === 'stale' ? <Hourglass size={14} /> : run === 'error' ? <TriangleAlert size={14} /> : <Check size={14} />}{runText}</span>
            {c.run && <small>{scenarios(c.run.n)} · seed {c.run.seed}{c.run.ms ? ` · ${(c.run.ms / 1000).toFixed(1)} s` : ''}</small>}
          </div>
          <ol className="cs-menu-steps">{c.cfg.steps.map(s => <li key={s.id} data-step={s.id}><b>{s.number}</b><span>{s.label}</span><small>{statusOf(s.id)}</small></li>)}</ol>
          <label className={`cs-switch ${c.compare ? 'is-on' : ''}`}>
            <input type="checkbox" checked={c.compare} onChange={e => c.setCompare(e.target.checked)} />
            <span className="cs-switch-track" aria-hidden><i /></span><span>Compare with original</span><HelpTip entry={c.cfg.help.compareOriginal} size={13} />
          </label>
          <button type="button" className="cs-text-btn cs-reset-all" disabled={!editCount} onClick={() => setConfirm(true)} title={c.cfg.help.returnOriginal?.short}>
            <RotateCcw size={14} />Return to original{editCount > 0 && <span className="cs-count">{editCount}</span>}
          </button>
          {!c.storageOk && <p className="cs-menu-note">Edits are not saved in this browser.</p>}
        </> : <div className="cs-confirm" role="alertdialog" aria-label="Return to the original">
          <strong>Return to the original?</strong>
          <p>This clears {editCount} edit{editCount === 1 ? '' : 's'}{hyp ? `, including ${hyp} hypothes${hyp === 1 ? 'is' : 'es'}` : ''}, and recomputes the case from its defaults. You can undo it from the notice.</p>
          <div><button type="button" className="button button-primary" onClick={() => { setConfirm(false); setMenu(false); c.resetAll(); }}>Return to original</button><button type="button" className="button" onClick={() => setConfirm(false)}>Cancel</button></div>
        </div>}
      </div>}
    </div>
    {/* a thin bar under the steps while numbers recompute: the page stays usable (no overlay, no blocked clicks) */}
    <RecomputeBar run={run} text={runText} />
  </nav>;
}
