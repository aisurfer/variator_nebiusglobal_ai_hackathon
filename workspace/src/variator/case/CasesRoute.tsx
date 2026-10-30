// /cases → the list of cases; /cases/:id → the case page.
import { useEffect, useState } from 'react';
import { ArrowUpRight, CircleDashed, GitFork, Layers, LoaderCircle, Lock, Target, Waypoints } from 'lucide-react';
import { HelpTip } from './ui/HelpTip';
import { shortQuestion } from './ui/format';
import type { PageContext } from '../../pages';
import type { CaseConfig, CaseSummary } from './model/types';
import { source, warmEngine } from './model/source';
import { readCase, readIntake } from './model/cases';
import { IntakeChat } from './intake/IntakeChat';
import { setCaseChrome } from './chrome';
import { CasePage } from './CasePage';
import './case.css';

const FUNNEL = [{ id: 'forks', label: 'Forks' }, { id: 'month', label: 'Month' }, { id: 'week', label: 'Week' }, { id: 'day', label: 'Day' }] as const;

export function CasesRoute({ ctx }: { ctx: PageContext }) {
  const id = ctx.path.split('/')[2];
  return id ? <CaseLoader key={id} id={decodeURIComponent(id)} ctx={ctx} /> : <CasesList ctx={ctx} />;
}

function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [s, set] = useState<{ data: T | null; error: string; loading: boolean }>({ data: null, error: '', loading: true });
  useEffect(() => {
    let live = true;
    set(x => ({ ...x, loading: true }));
    fn().then(data => live && set({ data, error: '', loading: false })).catch(e => live && set({ data: null, error: e instanceof Error ? e.message : String(e), loading: false }));
    return () => { live = false; };
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  return s;
}

function CaseLoader({ id, ctx }: { id: string; ctx: PageContext }) {
  useEffect(warmEngine, []); // the worker loads while the case's files are read
  const cfg = useAsync<CaseConfig>(() => source.loadCase(id), [id]);
  useEffect(() => () => setCaseChrome({ caseTitle: null, step: null, forkDate: null, future: false, recomputing: false }), []);
  if (cfg.loading) return <div className="cs-page-state"><LoaderCircle className="spin" size={22} /><h1>Loading the case…</h1></div>;
  if (cfg.error || !cfg.data) return <div className="cs-page-state"><CircleDashed size={26} /><h1>We couldn’t open this case</h1><p>{cfg.error}</p><button className="button" onClick={() => ctx.go('/cases')}>Back to the cases</button></div>;
  return <CasePage cfg={cfg.data} ctx={ctx} />;
}

/** The intake (the config and the fork date it counts months from), loaded once for the cases page. */
async function loadIntake(list: CaseSummary[]) {
  const first = list.find(c => c.ready);
  const [cfg, bundle] = await Promise.all([readIntake(), first ? readCase(first.id) : Promise.resolve(null)]);
  return { cfg, forkDate: bundle?.config.forkDate ?? new Date().toISOString().slice(0, 10) };
}

function CasesList({ ctx }: { ctx: PageContext }) {
  const list = useAsync<CaseSummary[]>(() => source.listCases(), []);
  const intake = useAsync(() => list.data ? loadIntake(list.data) : new Promise<never>(() => {}), [list.data]);
  useEffect(() => { setCaseChrome({ caseTitle: null, step: null, forkDate: null, future: false }); }, []);
  return <div className="cs-cases">
    {intake.data && list.data ? <IntakeChat ctx={ctx} cfg={intake.data.cfg} forkDate={intake.data.forkDate} cases={list.data} />
      : intake.error ? <div className="cs-page-state is-inline"><p>{intake.error}</p></div>
        : <div className="cs-page-state is-inline"><LoaderCircle className="spin" size={20} /><p>Loading…</p></div>}
    <div className="cs-cases-head"><h2>Your cases</h2><HelpTip label="Your cases" entry={{ title: 'Your cases', short: 'Each case is one decision: the forks you can take, the world around them, and the simulated months that follow.', full: 'A card shows the question, the criteria, the forks and hypotheses, and how far the decision funnel is computed: forks, month, week and day. Open a card for the case page and its guided tour.' }} /></div>
    {list.loading ? <div className="cs-page-state is-inline"><LoaderCircle className="spin" size={20} /><p>Loading cases…</p></div>
      : list.error ? <div className="cs-page-state is-inline"><p>{list.error}</p></div>
        : <div className="cs-case-grid" data-testid="case-cards">
          {list.data!.map(c => <a key={c.id} href={c.ready ? ctx.link(`/cases/${c.id}`) : undefined} aria-disabled={!c.ready || undefined}
            className={`cs-case-card card ${c.ready ? '' : 'is-off'}`} onClick={e => { e.preventDefault(); if (c.ready) ctx.go(`/cases/${c.id}`); }}>
            <div className="cs-case-top"><span className="cs-case-letter">{c.letter}</span><span className={`badge ${c.ready ? 'badge-good' : ''}`}><i />{c.status}</span>{c.ready ? <ArrowUpRight size={16} className="cs-case-open" /> : <Lock size={14} className="cs-case-open" />}</div>
            <h2>{c.title}</h2>
            <p className="cs-case-question" title={c.question}>{shortQuestion(c.question)}</p>
            {c.criteria.length > 0 && <div className="cs-case-criteria" aria-label="Criteria"><Target size={14} aria-hidden />{c.criteria.map(x => <span key={x}>{x}</span>)}</div>}
            <div className="cs-case-meta">
              <span><GitFork size={14} />{c.forks} forks</span><span><Layers size={14} />{c.hypotheses} hypotheses</span><span><Waypoints size={14} />Owner {c.owner}</span>
            </div>
            <div className="cs-case-funnel" aria-label="Decision funnel">
              {FUNNEL.map(s => { const state = !c.ready ? '' : c.live.includes(s.id) ? 'is-done' : c.preview.includes(s.id) ? 'is-preview' : ''; return <span key={s.id} className={state} title={state === 'is-done' ? 'Live results' : state === 'is-preview' ? 'Preview data' : 'Not ready yet'}><i />{s.label}</span>; })}
            </div>
          </a>)}
        </div>}
  </div>;
}
