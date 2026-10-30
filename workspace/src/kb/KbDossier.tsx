// Side sheets of the knowledge-base workspace: a metric with its definition and every record behind it, and the
// dossier of one artefact (fields, people, source text, why it exists, what relies on it, its version history).
import * as Dialog from '@radix-ui/react-dialog';
import { useMemo, useState } from 'react';
import { ArrowLeft, ChevronDown, ChevronRight, ExternalLink, GitBranch, History, Link2, Lock, LoaderCircle, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { MetricDrilldown, type DrilldownSource } from '../genui/detail/MetricDrilldown';
import { Block, knowMoreOpen } from '../genui/detail/shared';
import { formatValue as formatGenValue } from '../genui/primitives/format';
import { catalogueApi, drilldownOf, lineageSteps, parameterRows, type CatalogueSet } from './catalogue';
import { useBriefSet } from './KbBrief';
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { KbTrailStep, KbDossier, KbDossierGroup, KbDossierLink, KbMetricResult, KbOrganization, KbRole } from './types';
import { formatDate, formatDay, formatValue, isMoney, kbApi, kbBranch, KIND_LABEL, useKb } from './api';
import { peopleWords, sourceLabel } from './words';

// Predicate labels from the cause's side («why it exists») and from the dependent's side («what relies on it»).
const CAUSE_LABEL: Record<string, string> = { replies_to: 'Reply to', informed_by: 'Considers', decided_in: 'Decided at', implements: 'Implements', refers_to: 'Refers to', supersedes: 'Replaces', caused_by: 'Caused by', based_on: 'Based on', motivated_by: 'Motivated by', depends_on: 'Depends on', closes: 'Closes', blocked_by: 'Blocked by', part_of: 'Part of' };
const EFFECT_LABEL: Record<string, string> = { replies_to: 'Replied by', informed_by: 'Considered by', decided_in: 'Decision made here', implements: 'Implemented by', refers_to: 'Referred to by', supersedes: 'Replaced by', caused_by: 'Causes', based_on: 'Basis for', motivated_by: 'Motivates', depends_on: 'Needed by', closes: 'Closed by', blocked_by: 'Blocks', part_of: 'Includes' };
const isoish = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
import { RecordButton } from './KbBrief';
import { GroupedList } from './GroupedList';
import { highlightTerms } from './highlight';
import '../domain.css';

interface SheetProps {
  org: KbOrganization; role: KbRole; asOf?: string; metric: string | null; entity: string | null;
  /** The last day of data (the view date without asOf) and whether it is still loading: they choose the catalogue set. */
  defaultAsOf?: string | null; datesLoading?: boolean;
  /** The dossiers opened before the current one; `from` adds the current dossier to them. */
  trail: KbTrailStep[];
  onEntity: (id: string, from?: KbTrailStep) => void; onMetric: (id: string, fromTitle?: string) => void;
  /** The title of the sheet that Back returns to, when the history knows it. */
  backTo?: string; onClose: (key: 'metric' | 'entity') => void; onBack: () => void;
  onCrumb: (index: number) => void;
}

export function KbSheets(p: SheetProps) {
  const open = !!(p.metric || p.entity);
  const showEntity = !!p.entity;
  return <Dialog.Root open={open} onOpenChange={o => { if (!o) p.onClose(showEntity && !p.metric ? 'entity' : 'metric'); }}><Dialog.Portal><Dialog.Overlay className="detail-overlay" />
    <Dialog.Content className="detail-panel kb-sheet" aria-describedby="kb-sheet-description"
      onEscapeKeyDown={e => { if (knowMoreOpen(document.querySelector('.kb-sheet'))) e.preventDefault(); }}>
      {showEntity ? <EntitySheet {...p} id={p.entity!} /> : p.metric ? <AnyMetricSheet {...p} id={p.metric} /> : null}
    </Dialog.Content></Dialog.Portal></Dialog.Root>;
}

/** A metric of the catalogue set of the view date opens its drill-down; any other id is a knowledge-base metric. */
function AnyMetricSheet(p: SheetProps & { id: string }) {
  const { loading, set } = useBriefSet(p.asOf, p.defaultAsOf, !!p.datesLoading);
  const metricId = p.id.split('~')[0];
  if (loading) return <><Head eyebrow="METRIC" title="Metric" back={false} onBack={p.onBack} /><div className="detail-scroll-body"><Loading /></div></>;
  return set?.metrics[metricId] ? <CatalogueMetricSheet {...p} set={set} /> : <MetricSheet {...p} />;
}

function CatalogueMetricSheet({ org, id, set, backTo, onEntity, onMetric, onBack }: SheetProps & { id: string; set: CatalogueSet }) {
  const metricId = id.split('~')[0];
  const head = useKb(() => catalogueApi.metric(set, metricId), [set.id, metricId]);
  const m = head.data?.metric;
  const source = useMemo<DrilldownSource>(() => ({
    load: async mid => drilldownOf(await catalogueApi.metric(set, mid), org.id),
    explain: () => head.data ? lineageSteps(head.data.result) : null,
    parameters: spec => head.data ? parameterRows(head.data.params, v => formatGenValue(v, spec.unit)) : null,
    howTitle: 'As the metric catalogue computes it',
  }), [set, org.id, head.data]);
  // records open their knowledge-base dossier; steps of the breakdown are steps of the sheet (Back returns)
  const select = (kind: string, target: string) => {
    const from = m?.title ?? 'Metric';
    if (kind === 'gmetric') onMetric(target, from);
    else if (kind === 'kbEntity' || kind === 'evidence') onEntity(target, { id, title: from });
  };
  return <>
    <Head eyebrow={m?.area ? `METRIC · ${AREA_LABEL[m.area]}` : 'METRIC'} title={m?.title ?? 'Metric'} back={id.includes('~')} backTo={backTo ?? m?.title} onBack={onBack} crumb="VARIATOR metric catalogue" />
    <div className="detail-scroll-body kb-catalogue-sheet">{head.error ? <p className="kb-empty">The metric could not be read from the catalogue: {head.error}</p> : !head.data ? <Loading /> : <>
      {/* the drill-down keeps the exact number; the note says how to read it (a capped runway, a negative burn) */}
      {m?.display?.note && !id.includes('~') && <p className="kb-caveat kb-display-note">{m.display.note}</p>}
      <MetricDrilldown id={id} source={source} onSelect={select} compare="week" /></>}</div>
    <div className="detail-bottom-bar"><span><span className="domain-live-dot" />As of {formatDate(set.asOf)}</span><span>Computed by the VARIATOR metric catalogue</span></div>
  </>;
}

function Head({ eyebrow, title, back, backTo, onBack, crumb = kbBranch() ? 'Simulated future · not real history' : 'Live knowledge base' }: { eyebrow: string; title: string; back: boolean; backTo?: string; onBack: () => void; crumb?: string }) {
  return <div className="detail-sticky-head"><div className="detail-topbar">{back ? <button className="detail-back" onClick={onBack} aria-label={backTo ? `Back to ${backTo}` : 'Back'} title={backTo ? `Back to ${backTo}` : undefined}><ArrowLeft size={16} />{backTo ? <>Back to <b className="detail-back-to">{backTo}</b></> : 'Back'}</button> : <span className="detail-breadcrumb"><Link2 size={14} />{crumb}</span>}<Dialog.Close className="domain-icon-button detail-close" aria-label="Close details"><X size={20} /></Dialog.Close></div>
    <div className="detail-title-wrap"><span className="eyebrow">{eyebrow}</span><Dialog.Title>{title}</Dialog.Title><Dialog.Description id="kb-sheet-description" className="sr-only">Details read from the knowledge base. Use Back or Close to return.</Dialog.Description></div></div>;
}

const AREA_LABEL: Record<string, string> = { money: 'MONEY', clients: 'CLIENTS AND GROWTH', delivery: 'DELIVERY AND SERVICE' };

function MetricSheet({ org, asOf, id, onEntity, onBack }: SheetProps & { id: string }) {
  const r = useKb(() => kbApi.metric(org.id, id, asOf), [org.id, id, asOf]);
  const m = r.data;
  return <>
    <Head eyebrow={m?.area ? `METRIC · ${AREA_LABEL[m.area]}` : 'METRIC'} title={m?.title ?? 'Metric'} back={false} onBack={onBack} />
    <div className="detail-scroll-body">{r.loading ? <Loading /> : r.error || !m ? <p className="kb-empty">{r.error}</p> : <MetricBody m={m} onEntity={onEntity} />}</div>
    <div className="detail-bottom-bar"><span><span className="domain-live-dot" />As of {m ? formatDate(m.asOf) : '…'}</span><span>Computed by formula from the knowledge base</span></div>
  </>;
}

/** The target line is the top of the chart (above every point, and not also its bottom). */
function targetAtTop(m: KbMetricResult): boolean {
  const values = m.series.map(p => p.value).filter((v): v is number => v !== null);
  return m.target !== null && values.length > 0 && m.target > Math.min(...values) && m.target >= Math.max(...values);
}

export function MetricBody({ m, onEntity }: { m: KbMetricResult; onEntity: (id: string) => void }) {
  return <div className="kb-metric-body">
    <div className="kb-metric-hero"><strong>{formatValue(m.value, m.unit)}</strong><span>{m.previous !== null ? `a week earlier: ${formatValue(m.previous, m.unit)}` : ''}{m.baseline != null ? ` · baseline ${formatValue(m.baseline, m.unit)}` : ''}{m.sample !== null ? ` · n = ${m.sample}` : ''}</span></div>
    {/* visible: the question and the caveats (they change how to read the number); the definition, the steps and the
        sources are a disclosure closed by default */}
    <section className="detail-section"><p className="detail-lead">{m.question}</p>{m.caveats.map((c, i) => <p key={i} className="kb-caveat">{c}</p>)}</section>
    <Block title="How it is calculated" closed><p className="kb-definition">{m.definition}</p><ol className="kb-steps">{m.howCalculated.map((s, i) => <li key={i}>{s}</li>)}</ol>
      <p className="kb-meta">Sources: {m.sources.join(', ')}{m.target !== null ? ` · target ${formatValue(m.target, m.unit)}` : ''}{m.bands ? ` · on track ${m.direction === 'lower' ? '≤' : '≥'} ${formatValue(m.bands.good, m.unit)}, watch ${m.direction === 'lower' ? '≤' : '≥'} ${formatValue(m.bands.warn, m.unit)}` : ''}</p></Block>
    <section className="detail-section"><h3>Weekly trend</h3><div className="kb-chart"><ResponsiveContainer width="100%" height={180}><LineChart data={m.series} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
      <CartesianGrid stroke="#eef1f2" vertical={false} /><XAxis dataKey="date" tickFormatter={d => formatDate(d)} tick={{ fontSize: 11, fill: '#7f8992' }} /><YAxis width={64} tick={{ fontSize: 11, fill: '#7f8992' }} tickFormatter={v => isMoney(m.unit) ? `${Math.round(v / 1000)}k` : String(v)} />
      <Tooltip formatter={v => formatValue(Number(v), m.unit)} labelFormatter={d => `As of ${formatDate(String(d))}`} />{m.baseline != null && <ReferenceLine y={m.baseline} stroke="#9aa5ad" strokeDasharray="4 4" label={{ value: `baseline ${formatValue(m.baseline, m.unit)}`, position: 'insideBottomRight', fontSize: 11, fill: '#7f8992' }} />}
      {m.target !== null && m.direction !== 'context' && m.series.some(p => p.value !== null && Math.abs(p.value - m.target!) <= Math.max(...m.series.map(q => q.value ?? 0)) * 0.6) && <ReferenceLine y={m.target} stroke="#c9a227" strokeDasharray="2 4"
        // above the line (insideBottom…), on the left: clear of the date axis when the target is 0 and of the baseline label on the right; a target at the top of the chart goes below its line
        label={{ value: `target ${formatValue(m.target, m.unit)}`, position: targetAtTop(m) ? 'insideTopLeft' : 'insideBottomLeft', fontSize: 11, fill: '#8a7320' }} />}
      <Line type="monotone" dataKey="value" stroke="#78a147" strokeWidth={2} dot={{ r: 3 }} connectNulls isAnimationActive={false} /></LineChart></ResponsiveContainer></div></section>
    {m.breakdown.length > 0 && <section className="detail-section"><h3>Breakdown</h3><div className="kb-bars">{m.breakdown.map(b => <div key={b.key} className="kb-bar"><span>{b.label}</span><i style={{ width: `${Math.max(4, (b.value / Math.max(...m.breakdown.map(x => x.value), 1)) * 100)}%` }} /><b>{formatValue(b.value, isMoney(m.unit) ? m.unit : m.unit === 'hours' ? 'hours' : m.unit === 'score' ? 'score' : 'count')}</b></div>)}</div></section>}
    <section className="detail-section"><h3>{m.itemsLabel ?? 'Behind the number'} <span className="count-pill">{m.itemsTotal}</span></h3>
      {m.restrictedTotal > 0 && <p className="kb-meta"><Lock size={12} /> {m.restrictedTotal} of them you may not open; they are counted for the organisation.</p>}
      <ul className="kb-items">{m.items.map((r, i) => <li key={`${r.id}-${i}`}><RecordButton r={r} onOpen={onEntity} /></li>)}</ul>
      {!m.items.length && <p className="kb-empty">No records as of this date.</p>}</section>
  </div>;
}

function EntitySheet({ org, role, id, metric, asOf, trail, backTo, onEntity, onBack, onCrumb }: SheetProps & { id: string }) {
  const r = useKb(() => kbApi.entity(org.id, id, asOf), [org.id, id, asOf]);
  const d = r.data;
  // a step from this dossier puts it on the trail, so that Back and the breadcrumbs return to it
  const open = (next: string) => onEntity(next, { id, title: d ? peopleWords(d.title) : id });
  const notYet = /not known yet/i.test(r.error);
  const hidden = !notYet && /\b404\b|not found|not visible/i.test(r.error);
  // a dossier of a simulated future says so in its head and footer, over the page's Simulation banner
  const branch = kbBranch();
  return <>
    <Head eyebrow={`${branch ? 'SIMULATION · ' : ''}${d ? [(KIND_LABEL[d.kind] ?? d.kind).toUpperCase(), sourceLabel(d.source, !!branch).toUpperCase()].filter(Boolean).join(' · ') : 'DOSSIER'}`} title={d ? peopleWords(d.title) : hidden ? 'Restricted' : 'Loading…'} back={!!metric || trail.length > 0} backTo={trail.at(-1)?.title ?? backTo ?? (metric ? 'the metric' : undefined)} onBack={onBack} />
    {trail.length > 0 && <nav className="kb-trail" aria-label="Dossiers opened before">{trail.map((t, i) => <span key={`${t.id}-${i}`}><button onClick={() => onCrumb(i)} title={t.title}>{t.title}</button><ChevronRight size={12} aria-hidden /></span>)}<b title={d ? peopleWords(d.title) : undefined}>{d ? peopleWords(d.title) : '…'}</b></nav>}
    {d && d.effects.length > 1 && <button className="kb-effects-tag" onClick={() => document.getElementById('kb-effects')?.scrollIntoView({ behavior: 'smooth' })}><Link2 size={13} />Leads to {d.effects.length} records</button>}
    <div className="detail-scroll-body" key={id}>{r.loading ? <Loading /> : notYet ? <div className="detail-invalid"><Lock size={30} /><h3>Not known yet on {formatDate(asOf)}</h3><p>{r.error}. Move the view date later to see it.</p></div> : hidden ? <div className="detail-invalid"><Lock size={30} /><h3>You can see that this exists, not its content</h3><p>{role.name} has no access to it in the source system. The brief counts it for the organisation without showing it.</p></div> : r.error || !d ? <p className="kb-empty">{r.error}</p> : <Dossier d={d} onEntity={open} />}</div>
    <div className="detail-bottom-bar">{branch ? <span className="kb-sheet-branch"><span className="branch-chip">SIMULATION</span>{branch.label}</span> : <span><span className="domain-live-dot" />Read as {role.name}</span>}<span>{d?.asOf ? `As known on ${formatDate(d.asOf)}` : 'As known now'}</span></div>
  </>;
}

/** «Assigned: tasks 266»: the newest 5 objects, each opening its own dossier; «All N» shows the rest (up to 50). */
function RelatedGroup({ g, onEntity }: { g: KbDossierGroup; onEntity: (id: string) => void }) {
  const [all, setAll] = useState(false);
  const shown = all ? g.items : g.items.slice(0, 5);
  return <section className="detail-section kb-related-group" data-group={g.key}>
    <h3>{g.label} <span className="count-pill">{g.total}</span></h3>
    <ul className="kb-source-list kb-source-columns">{shown.map(x => <li key={x.id}><button onClick={() => onEntity(x.id)} title={[peopleWords(x.title), [KIND_LABEL[x.kind] ?? x.kind, g.key === 'out' ? x.via : '', x.status, x.date ? formatDate(x.date, true) : ''].filter(Boolean).join(' · ')].join('\n')}>
      {peopleWords(x.title)}{x.status && <small> · {x.status}</small>}</button></li>)}</ul>
    {g.total > 5 && <button className="text-button kb-related-more" onClick={() => setAll(v => !v)}>{all ? 'Show the newest 5' : `All ${g.total}`}</button>}
    {all && g.total > g.items.length && <p className="kb-meta">The newest {g.items.length} of {g.total}.</p>}
  </section>;
}

function Dossier({ d, onEntity }: { d: KbDossier; onEntity: (id: string) => void }) {
  return <div className="kb-dossier">
    <div className="detail-property-grid kb-props">
      {/* a client's record in the directory is only the day the directory was loaded: show since when it is a client */}
      {d.since ? <div data-since><span>{d.since.label}</span><strong>{formatDay(d.since.date)}</strong></div> : <div><span>Created</span><strong>{formatDate(d.created, true)}</strong></div>}
      <div><span>Last change</span><strong>{formatDate(d.updated, true)}</strong></div>
      {d.facts.map(f => <div key={f.label}><span>{peopleWords(f.label).replace(/^\w/, c => c.toUpperCase())}</span><strong title={/amount/.test(f.label) && Number(f.value) ? `$${Number(f.value).toLocaleString('en-US', { maximumFractionDigits: 2 })}` : undefined}>{factText(f)}</strong></div>)}
    </div>
    {d.people.length > 0 && <section className="detail-section"><h3>People</h3><p className="kb-people">{d.people.map((p, i) => <span key={i}><small>{p.role}</small>{p.name}</span>)}</p></section>}
    {(d.related ?? []).map(g => <RelatedGroup key={g.key} g={g} onEntity={onEntity} />)}
    <LinkSection title="Why it exists" icon={<GitBranch size={15} />} empty="No recorded causes." links={d.why} labels={CAUSE_LABEL} onEntity={onEntity} />
    {d.chains.length > 0 && <details className="kb-chains" open><summary>Chains to the root cause</summary>{d.chains.map((c, i) => <p key={i}>{peopleWords(c)}</p>)}</details>}
    <LinkSection anchor="kb-effects" title="What relies on it" icon={<Link2 size={15} />} empty="Nothing recorded depends on it." links={d.effects} labels={EFFECT_LABEL} onEntity={onEntity} />
    <section className="detail-section"><h3>Source text</h3><div className="detail-markdown kb-excerpt"><ReactMarkdown>{highlightTerms(peopleWords(d.excerpt)) || '_No text._'}</ReactMarkdown></div>
      {d.url && <a className="detail-source-link" href={d.url} target="_blank" rel="noreferrer">Open in the source system <ExternalLink size={13} /></a>}</section>
    {/* a warning: the dossier shows an older version than the current one */}
    {d.versionsLater > 0 && <p className="kb-caveat">{d.versionsLater} later version{d.versionsLater === 1 ? ' was' : 's were'} recorded after {formatDate(d.asOf)} and {d.versionsLater === 1 ? 'is' : 'are'} not shown.</p>}
    <details className="detail-section know-more-details kb-history-details"><summary><ChevronDown size={15} aria-hidden /><h3><History size={15} /> Version history <span className="count-pill">{d.history.length}</span></h3></summary>
      <ol className="kb-history">{d.history.slice(0, 20).map(h => <li key={h.seq}><b>v{h.seq}</b> {formatDate(h.at, true)}{h.actor ? ` · ${h.actor}` : ''}{h.changed.length ? <small> · {h.changed.map(c => c.replace(/_/g, ' ')).join(', ')}</small> : null}</li>)}</ol></details>
  </div>;
}

function LinkSection({ anchor, title, icon, empty, links, labels, onEntity }: { anchor?: string; title: string; icon: React.ReactNode; empty: string; links: KbDossierLink[]; labels: Record<string, string>; onEntity: (id: string) => void }) {
  return <section className="detail-section" id={anchor}><h3>{icon} {title} <span className="count-pill">{links.length}</span></h3>
    {/* one bullet per link, grouped by its relation; the quote and the date are in the hint */}
    {links.length ? <GroupedList items={links.map(l => ({ id: l.other.id, group: labels[l.predicate] ?? l.label, title: peopleWords(l.other.title),
      note: [l.inferred ? 'proposed' : '', l.status && !['CURRENT', 'CURRENT_COMPATIBLE'].includes(l.status) ? (l.status === 'STALE' ? 'source changed since' : l.status.toLowerCase()) : ''].filter(Boolean).join(' · '),
      hint: [peopleWords(l.other.title), `${KIND_LABEL[l.other.kind] ?? l.other.kind} · ${formatDate(l.other.date)}`, l.quote ? `«${peopleWords(l.quote)}»` : ''].filter(Boolean).join('\n') }))} onOpen={onEntity} /> : <p className="kb-empty">{empty}</p>}</section>;
}

/** A fact of a dossier for people: an amount in dollars, a time as a date, any other value in people's words. */
export function factText(f: { label: string; value: string }): string {
  if (/amount/.test(f.label) && Number(f.value)) return formatValue(Number(f.value), 'money');
  if (isoish.test(f.value)) return formatDate(f.value, true);
  return peopleWords(f.value);
}

function Loading() { return <div className="kb-loading"><LoaderCircle className="spin" size={22} />Reading the knowledge base…</div>; }
