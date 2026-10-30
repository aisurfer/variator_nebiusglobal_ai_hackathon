// Questions in natural language to the knowledge base (/api/v1/ask behind /api/kb/:org/ask), with the cited
// artefacts opening as dossiers. The answer is composed by the knowledge base; the page shows it for people: a «why»
// from its structure (the subject, the chains of causes, the root cause), any other answer in people's words
// (src/kb/words.ts). Everything technical (the match, ids, times, the knowledge cut, the interpretation, the raw
// text) stays in the closed disclosure «How the question was understood».
import { useEffect, useRef, useState } from 'react';
import { GroupedList } from './GroupedList';
import { setAskTerms } from './highlight';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowUpRight, ChevronDown, LoaderCircle, MessagesSquare, RefreshCw, Send } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import type { KbAskAnswer, KbAskPlan, KbOrganization, KbRole, KbWhy, KbWhyNode } from './types';
import { formatDate, formatDay, kbApi, kbBranch, KIND_LABEL } from './api';
import { peopleWords, splitAnswer } from './words';
import { loadBranchExamples } from './branchExamples';
import { KnowMore } from '../genui/detail/shared';

// The company's own questions (askExamples of its configuration, for the role of the user), else general ones about
// money, clients and delivery.
const EXAMPLES = ['Which invoices are still unpaid, and why?', 'Which deals went quiet, and what did we promise last?', 'Which decisions did we reverse this quarter, and why?', 'What caused the latest production incident?', 'Which work is blocked, and by what?'];
const PLACEHOLDER = 'For example: why did we move the release date?';

/** Search highlights of the knowledge base («<mark>word</mark>») as markdown links to `#mark`, rendered as <mark>:
 * the answer is never parsed as HTML. Brackets inside a highlight are escaped; a stray tag is dropped. */
export function markHighlights(text: string): string {
  return text.replace(/<mark>([\s\S]*?)<\/mark>/gi, (_, t: string) => t.trim() ? `[${t.replace(/([[\]\\])/g, '\\$1')}](#mark)` : t)
    .replace(/<\/?mark>/gi, '');
}

/** The knowledge base cites versions inline («[mail.msg-000101@1]»): number them and make them open the dossier. */
export function withCitations(answer: Pick<KbAskAnswer, 'answer'>): { text: string; order: string[] } {
  const order: string[] = [];
  const text = markHighlights(answer.answer).replace(/\[([\w.:-]+@\d+)\]/g, (_, vid: string) => {
    if (!order.includes(vid)) order.push(vid);
    return ` [${order.indexOf(vid) + 1}](#cite-${encodeURIComponent(vid)})`;
  }).replace(/\s*•\s+/g, '\n\n- ').replace(/\n(?!\n)/g, '\n\n');
  return { text, order };
}

/** An answer without a «why» structure in people's words: the lines a person reads (dollars, readable dates, no ids,
 * version suffixes or Python lists; the citations stay for the numbered links), and the technical lines (the time
 * and knowledge cut, the interpretation, the search fragments) for the disclosure. */
export function peopleAnswer(raw: string): { text: string; technical: string } {
  const unmarkedDigits = raw.replace(/<mark>(\d+)<\/mark>/gi, '$1'); // «<mark>2027</mark>-02-26» is a date
  const { main, technical } = splitAnswer(unmarkedDigits);
  const lines = (main || unmarkedDigits.split('\n').filter(l => !/^\s*(Time|Knowledge cut|Interpretation|Fragments found):/.test(l)).join('\n')).split('\n');
  return { text: lines.map(l => peopleWords(l)).join('\n').trim(), technical };
}

const CHOSEN = new Set(['decision', 'rule', 'policy', 'hypothesis', 'habit', 'process']);
/** One object of a «why» for people: «decision “Use 15% …”» for what someone chose, else its title. */
function nodeName(n: KbWhyNode): string { return CHOSEN.has(n.kind) ? `${n.kind} “${peopleWords(n.title)}”` : peopleWords(n.title); }
/** Its date and, for a decision, who made it: «27 Oct 2026, Avery Collins». */
function nodeMeta(n: KbWhyNode): string { return [formatDay(n.date), CHOSEN.has(n.kind) ? n.actor : null].filter(Boolean).join(', '); }

function WhyNode({ n, onOpen, meta = true }: { n: KbWhyNode; onOpen: (id: string) => void; meta?: boolean }) {
  const m = meta ? nodeMeta(n) : '';
  return <span className="kb-why-node"><button className="kb-why-link" onClick={() => onOpen(n.id)}>{nodeName(n)}</button>{m && <span className="kb-why-meta"> ({m})</span>}</span>;
}

/** A «why» for people: the subject and its date, each chain of declared causes as linked titles from the effect to the
 * root («Incidents fell in February 2027 ← Debt work shows an effect ← decision “Use 15% …” (27 Oct 2026, Avery
 * Collins)»), and the root cause in one line. */
export function WhyView({ why, onOpen }: { why: KbWhy; onOpen: (id: string) => void }) {
  const day = formatDay(why.subject.date);
  return <div className="kb-answer-text kb-why" data-why={why.chains.length}>
    <p className="kb-why-lead"><button className="kb-why-link" onClick={() => onOpen(why.subject.id)}>{peopleWords(why.subject.title)}</button>{day && <span className="kb-why-meta"> · {day}</span>}</p>
    {why.chains.length > 0 && <><h3 className="kb-sub">{why.chains.length > 1 ? 'Chains of causes' : 'Chain of causes'}</h3>
      <ol className="kb-why-chains">{why.chains.map((c, i) => <li key={i}>{c.map((n, k) => <span key={k}>{k > 0 && <span className="kb-why-arrow" title={n.via ?? 'caused by'} aria-label={n.via ?? 'caused by'}> ← </span>}<WhyNode n={n} onOpen={onOpen} meta={k > 0} /></span>)}</li>)}</ol></>}
    {why.roots.length > 0 && <p className="kb-why-root"><strong>Root cause:</strong> {why.roots.slice(0, 3).map((r, i) => <span key={r.id}>{i > 0 && '; '}<WhyNode n={r} onOpen={onOpen} /></span>)}
      {why.roots[0].statement && <span className="kb-why-statement">{peopleWords(why.roots[0].statement)}</span>}</p>}
  </div>;
}

/** What the knowledge base understood: kinds, the entities names were matched to, relations, the window, and the
 * technical lines of its answer (the time and knowledge cut, the interpretation, the raw text with its ids). */
function PlanView({ a, technical }: { a: KbAskAnswer; technical: string }) {
  const p = a.plan;
  const rel = (r: KbAskPlan['relations'][number]) => `${r.negate ? 'no ' : ''}${r.predicate.replace(/_/g, ' ')} → ${r.to}`;
  // a disclosure closed by default: the answer and its sources come first
  return <details className="kb-plan know-more-details" aria-label="How the question was understood"><summary><ChevronDown size={14} aria-hidden />How the question was understood · {a.mode === 'agent' ? 'researched by the knowledge-base agent' : a.mode === 'llm' ? 'composed from retrieved evidence' : 'deterministic answer'} · {(a.latencyMs / 1000).toFixed(1)} s</summary><dl>
    <dt>Asked as of</dt><dd>{formatDate(a.asOf)}, end of day — only what was known by then</dd>
    {p && p.kinds.length > 0 && <><dt>Looking for</dt><dd>{p.kinds.map(k => KIND_LABEL[k] ?? k).join(', ')}{p.operation === 'count' ? ' (count)' : p.operation === 'list' ? ' (full list)' : ''}</dd></>}
    {p && !['list', 'count'].includes(p.operation) && <><dt>Question type</dt><dd>{p.operation}</dd></>}
    {p && p.entities.length > 0 && <><dt>Matched</dt><dd>{p.entities.map(e => `${e.text || e.label} → ${e.label}`).join('; ')}</dd></>}
    {p && p.relations.length > 0 && <><dt>Relations</dt><dd>{p.relations.map(rel).join('; ')}</dd></>}
    {p?.time.interpretation && <><dt>Period</dt><dd>{p.time.interpretation}</dd></>}
    {a.total !== null && <><dt>Result</dt><dd className="kb-plan-total">{a.total === 0 ? 'nothing matches' : a.shown !== null && a.shown < a.total ? `showing ${a.shown} of ${a.total}` : `${a.total} found`}</dd></>}
    {p && <><dt>Parsed by</dt><dd>{p.planner === 'llm' ? 'the language model (query plan)' : 'rules'}</dd></>}
    <dt>Knowledge cut</dt><dd>as known at {/^\d{4}-\d{2}-\d{2}/.test(a.knownAt) ? formatDate(a.knownAt, true) : a.knownAt}</dd>
    {a.unknowns.length > 0 && <><dt>Unknown</dt><dd>{a.unknowns.join('; ')}</dd></>}
    {technical && <><dt>Details</dt><dd className="kb-plan-raw">{technical}</dd></>}
    <dt>Full answer</dt><dd className="kb-plan-raw">{a.answer.replace(/<\/?mark>/gi, '')}</dd>
  </dl></details>;
}

export function KbAskPage({ org, role, asOf, defaultAsOf, onOpen }: { org: KbOrganization; role: KbRole; asOf?: string; defaultAsOf?: string; onOpen: (id: string) => void }) {
  const [question, setQuestion] = useState('');
  const [state, setState] = useState<{ loading: boolean; error: string; answer: KbAskAnswer | null; asked: string }>({ loading: false, error: '', answer: null, asked: '' });
  const viewDate = asOf ?? defaultAsOf;
  const stale = !!state.answer && !!viewDate && state.answer.asOf !== viewDate;
  // example questions: the company's for its history; a simulated future's own (or none), never the company's
  const branch = kbBranch();
  const [branchExamples, setBranchExamples] = useState<string[]>([]);
  useEffect(() => {
    if (!branch) return;
    let live = true;
    loadBranchExamples(branch.tenant).then(x => { if (live) setBranchExamples(x); });
    return () => { live = false; };
  }, [branch?.tenant]); // eslint-disable-line react-hooks/exhaustive-deps
  const examples = branch ? branchExamples : org.askExamples?.[role.id] ?? EXAMPLES;
  const ask = async (q: string) => {
    const text = q.trim();
    if (!text) return;
    setQuestion(text);
    setState({ loading: true, error: '', answer: null, asked: text });
    try { const answer = await kbApi.ask(org.id, text, asOf); setAskTerms(text, answer.answer); setState({ loading: false, error: '', answer, asked: text }); }
    catch (e) { setState({ loading: false, error: e instanceof Error ? e.message : 'The knowledge base did not answer', answer: null, asked: text }); }
  };
  // ?q=<question> (the «Ask this future» chips of the Day section): asked once on arrival; then q leaves the URL
  // through the router (a reload does not ask again, and the shell's links never carry it); branch and asOf stay
  const location = useLocation(), navigate = useNavigate();
  const once = useRef(false);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const q = params.get('q');
    if (!q || once.current) return;
    once.current = true;
    params.delete('q');
    navigate(`${location.pathname}${params.size ? `?${params}` : ''}`, { replace: true });
    void ask(q);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="kb-ask">
    <section className="page-intro"><div><div className="eyebrow intro-date">ASK THE KNOWLEDGE BASE</div><h1>What do you want to know<span className="lime-period">?</span>
      <KnowMore label="About the answers">Answers cite the letters, tickets, meeting notes and documents they rest on, within what {role.name} may read. A question is asked as of the view date: only what was known by the end of that day counts.</KnowMore></h1></div></section>
    <form className="card kb-ask-form" onSubmit={e => { e.preventDefault(); void ask(question); }}>
      <MessagesSquare size={18} /><textarea aria-label="Question" rows={2} value={question} onChange={e => setQuestion(e.target.value)} placeholder={branch ? branchExamples[0] ? `For example: ${branchExamples[0]}` : 'Ask why something happened in this simulated future' : PLACEHOLDER} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void ask(question); } }} />
      <button className="button button-primary" disabled={state.loading || !question.trim()}><Send size={15} />Ask</button>
    </form>
    {examples.length > 0 && <div className="kb-examples" data-examples={branch ? 'branch' : 'company'} aria-label="Sample questions"><span className="kb-examples-label">Or try a sample question:</span>{examples.map(x => <button key={x} className="button" onClick={() => void ask(x)}>{x}</button>)}</div>}
    {state.loading && <div className="kb-loading"><LoaderCircle className="spin" size={22} />Searching mail, Jira, Confluence, GitLab and XWiki…</div>}
    {state.error && <p className="kb-caveat">{state.error}</p>}
    {state.answer && <Answer a={state.answer} asked={state.asked} stale={stale ? viewDate ?? null : null} onAgain={() => void ask(state.asked)} onOpen={onOpen} />}
  </div>;
}

/** The answer for people: a «why» from its structure, any other answer in people's words with numbered citations;
 * the sources; the technical part in the closed disclosure. */
function Answer({ a, asked, stale, onAgain, onOpen }: { a: KbAskAnswer; asked: string; stale: string | null; onAgain: () => void; onOpen: (id: string) => void }) {
  const why = a.why && a.why.chains.length > 0 ? a.why : null;
  const people = peopleAnswer(a.answer);
  const cited = withCitations({ answer: people.text });
  return <section className="card kb-answer" data-answer={why ? 'why' : 'text'}><div className="section-heading"><div><h2>{asked}</h2><p>As of {formatDate(a.asOf)}</p></div>
    {stale && <button className="button" onClick={onAgain}><RefreshCw size={14} />Ask again as of {formatDate(stale)}</button>}</div>
    {why ? <WhyView why={why} onOpen={onOpen} />
      : <div className="detail-markdown kb-answer-text"><ReactMarkdown components={{ a: ({ href, children }) => href === '#mark' ? <mark className="kb-mark">{children}</mark> : href?.startsWith('#cite-') ? <button className="kb-cite" onClick={() => onOpen(decodeURIComponent(href.slice(6)).split('@')[0])}>{children}</button> : <a href={href} target="_blank" rel="noreferrer">{children}</a> }}>{cited.text}</ReactMarkdown></div>}
    {a.citations.length > 0 && <><h3 className="kb-sub">Sources</h3><GroupedList items={[...new Map(a.citations.map(c => [c.id, c])).values()].slice(0, 12).map(c => { const n = cited.order.indexOf(c.versionId); return { id: c.id, group: KIND_LABEL[c.kind] ?? (peopleWords(c.kind) || 'Source'), title: peopleWords(c.title), mark: !why && n >= 0 ? `[${n + 1}]` : '', level: influence(a.answer, c.versionId) }; })} onOpen={onOpen} /></>}
    <PlanView a={a} technical={people.technical} />
  </section>;
}

/** How much a source bears on the question, read from the answer text (the knowledge base stores no link strength):
 *  3 — on the causal chain or in the short answer; 2 — a search hit that matches the question; 1 — only a loose hit
 *  (for example, another topic of the same meeting). */
export function influence(answer: string, versionId: string): 1 | 2 | 3 {
  const lines = answer.split('\n').filter(l => l.includes(versionId));
  // the short answer, a chain (—…→) and the root causes are the causal path; the «Fragments found» are search hits
  if (lines.some(l => l.startsWith('In short:') || l.includes('→') || l.includes('effective '))) return 3;
  return lines.some(l => l.includes('<mark>')) ? 2 : 1;
}
