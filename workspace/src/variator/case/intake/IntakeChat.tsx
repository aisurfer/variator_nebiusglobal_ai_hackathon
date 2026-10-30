// The intake chat at the top of the cases page: the same question box as «Ask the knowledge base» (its classes), then a
// thread with the agent. The agent asks at most three questions for what is missing, then shows the draft of a case
// and what would happen next; running a new case is not in this version, so it points to the ready cases. The thread
// is kept in this browser (when storage works); «Start over» clears it.
import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Bot, FlaskConical, GitFork, KeyRound, Layers, ListChecks, Lock, MessagesSquare, RotateCcw, Send, Target, Telescope } from 'lucide-react';
import type { PageContext } from '../../../pages';
import type { CaseSummary } from '../model/types';
import { HelpTip } from '../ui/HelpTip';
import { buildFacts, nextMessage } from './engine';
import type { AgentMsg, Chip, DraftCase, IntakeConfig, IntakeFacts, IntakeMsg } from './types';

const KEY = 'variator:intake:v1';
const load = (): IntakeMsg[] => { try { const s = localStorage.getItem(KEY); const v = s ? JSON.parse(s) as unknown : null; return Array.isArray(v) ? v as IntakeMsg[] : []; } catch { return []; } };
const save = (t: IntakeMsg[]) => { try { if (t.length) localStorage.setItem(KEY, JSON.stringify(t)); else localStorage.removeItem(KEY); } catch { /* storage off: the thread lives in the page only */ } };

export function IntakeChat({ ctx, cfg, forkDate, cases }: { ctx: PageContext; cfg: IntakeConfig; forkDate: string; cases: CaseSummary[] }) {
  const facts: IntakeFacts = buildFacts(ctx.org.name, forkDate);
  const [thread, setThread] = useState<IntakeMsg[]>(load);
  const [text, setText] = useState(''), [typing, setTyping] = useState(false);
  const timer = useRef<number | undefined>(undefined), end = useRef<HTMLDivElement>(null), box = useRef<HTMLTextAreaElement>(null), again = useRef<HTMLButtonElement>(null);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => { save(thread); }, [thread]);
  useEffect(() => { if (thread.length) end.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, [thread.length, typing]);
  const last = thread[thread.length - 1];
  const done = last?.role === 'agent' && last.kind === 'draft';
  const send = (t: string, value?: string) => {
    const words = t.trim();
    if (!words || typing || done) return;
    const next: IntakeMsg[] = [...thread, { role: 'user', text: words, ...(value ? { value } : {}) }];
    setThread(next); setText(''); setTyping(true);
    // the agent answers at once; a short pause reads as a reply, not as a form
    timer.current = window.setTimeout(() => {
      const reply = nextMessage(next, cfg, facts);
      setThread([...next, reply]); setTyping(false);
      window.setTimeout(() => (reply.kind === 'draft' ? again.current : box.current)?.focus({ preventScroll: true }), 0);
    }, 380);
  };
  const restart = () => { window.clearTimeout(timer.current); setTyping(false); setThread([]); setText(''); box.current?.focus(); };
  return <section className="kb-ask cs-intake" aria-labelledby="intake-title">
    <div className="page-intro"><div>
      <div className="eyebrow intro-date">DECISIONS<span>·</span>{ctx.org.name.toUpperCase()}</div>
      <h1 id="intake-title" className="cs-intake-title">{cfg.title}<span className="lime-period">?</span><HelpTip variant="info" size={18} label="new cases" entry={{ title: 'A new case from your words', short: cfg.subtitle, full: '' }} /></h1>
    </div></div>
    {thread.length > 0 && <div className="card cs-thread" data-testid="intake-thread">
      <div className="cs-thread-head"><span className="cs-eyebrow"><Bot size={14} aria-hidden />New case · intake</span>
        <button ref={again} type="button" className="cs-text-btn" onClick={restart}><RotateCcw size={13} />Start over</button></div>
      <ol className="cs-msgs" aria-live="polite">
        {thread.map((m, i) => <li key={i} className={`cs-msg is-${m.role}`}>
          {m.role === 'user' ? <p>{m.text}</p> : <AgentBubble m={m} live={i === thread.length - 1 && !typing} picked={thread[i + 1]?.role === 'user' ? (thread[i + 1] as { value?: string }).value : undefined} onChip={c => send(c.label, c.value)} cases={cases} ctx={ctx} />}
        </li>)}
        {typing && <li className="cs-msg is-agent is-typing" aria-label="VARIATOR is writing"><span /><span /><span /></li>}
      </ol>
      <div ref={end} />
    </div>}
    {!done && <form className="card kb-ask-form cs-intake-form" onSubmit={e => { e.preventDefault(); send(text); }}>
      <MessagesSquare size={18} aria-hidden />
      <textarea ref={box} aria-label={thread.length ? 'Your answer' : 'Your decision'} rows={2} value={text} onChange={e => setText(e.target.value)}
        placeholder={thread.length ? 'Type your answer, or pick one above' : cfg.placeholder} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(text); } }} />
      <button className="button button-primary" disabled={!text.trim() || typing}><Send size={15} />{thread.length ? 'Send' : 'Ask'}</button>
    </form>}
    {!thread.length && <div className="kb-examples" aria-label="Examples">{cfg.examples.map(x => <button key={x} type="button" className="button" onClick={() => send(x)}>{x}</button>)}</div>}
  </section>;
}

function AgentBubble({ m, live, picked, onChip, cases, ctx }: { m: AgentMsg; live: boolean; picked?: string; onChip: (c: Chip) => void; cases: CaseSummary[]; ctx: PageContext }) {
  return <div className="cs-agent">
    <span className="cs-agent-mark" aria-hidden><Bot size={15} /></span>
    <div className="cs-agent-body">
      <p>{m.text}</p>
      {m.chips.length > 0 && <div className="cs-quick" role="group" aria-label="Quick replies">{m.chips.map(c => <button key={c.value} type="button" className={`cs-chip ${picked === c.value ? 'is-on' : ''}`} disabled={!live} aria-pressed={picked === c.value || undefined} onClick={() => onChip(c)}>{c.label}</button>)}</div>}
      {m.draft && <DraftCard d={m.draft} cases={cases} ctx={ctx} />}
    </div>
  </div>;
}

const NEXT_ICON = [KeyRound, ListChecks, Telescope, GitFork, FlaskConical];

function DraftCard({ d, cases, ctx }: { d: DraftCase; cases: CaseSummary[]; ctx: PageContext }) {
  return <article className="cs-draft" data-testid="intake-draft" aria-label="Draft case">
    <span className="cs-eyebrow">Draft case</span>
    <h3>{d.question}</h3>
    <div className="cs-draft-grid">
      <section><h4><GitFork size={14} aria-hidden />Forks</h4><ol className="cs-draft-forks">{d.forks.map(f => <li key={f.label} className={f.baseline ? 'is-base' : ''}>{f.label}{f.baseline && <span className="badge"><i />Baseline</span>}</li>)}</ol></section>
      <section><h4><Layers size={14} aria-hidden />World hypotheses</h4><ul className="cs-draft-chips">{d.hypotheses.map(h => <li key={h}>{h}</li>)}</ul></section>
      <section><h4><Target size={14} aria-hidden />Criteria</h4><ol className="cs-draft-chips">{d.criteria.map(c => <li key={c}>{c}</li>)}</ol></section>
    </div>
    <section className="cs-draft-shows"><h4>What this case would show</h4><ul>{d.shows.map(s => <li key={s}>{s}</li>)}</ul></section>
    <section><h4>What happens next</h4><ol className="cs-draft-next">{d.next.map((n, i) => { const Icon = NEXT_ICON[i] ?? ListChecks; return <li key={n.title}><Icon size={14} aria-hidden /><b>{n.title}</b><span>{n.text}</span></li>; })}</ol></section>
    <div className="cs-draft-notice" role="note">
      <p><Lock size={15} aria-hidden />{d.notice}</p>
      <div className="cs-draft-links">{d.links.map(id => { const c = cases.find(x => x.id === id); return c ? <a key={id} className="button" href={ctx.link(`/cases/${id}`)} onClick={e => { e.preventDefault(); ctx.go(`/cases/${id}`); }}><span className="cs-case-letter is-small">{c.letter}</span>{c.title}<ArrowUpRight size={14} /></a> : null; })}</div>
    </div>
  </article>;
}
