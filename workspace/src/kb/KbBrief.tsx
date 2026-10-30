// The morning brief of the CEO: every metric of the company as of a week, as a short top row of key numbers and three
// areas (money, clients and growth, delivery and service). The brief of the VARIATOR metric catalogue is read through
// the VARIATOR API when a set is computed for the view date; otherwise the brief is computed from the live knowledge
// base, with a quiet note that says why.
import { BriefDecisions } from './BriefDecisions';
import { useEffect } from 'react';
import { ArrowUpRight, ChevronLeft, ChevronRight, Handshake, Info, Lock, LoaderCircle, PackageCheck, RefreshCw, ShieldAlert, Sparkles, Wallet, type LucideIcon } from 'lucide-react';
import type { KbAreaId, KbBrief, KbBriefArea, KbMetricResult, KbOrganization, KbRecord, KbRole } from './types';
import { formatDate, formatDay, formatFact, formatValue, isMoney, kbApi, KIND_LABEL, useKb } from './api';
import { catalogueApi, fallbackNote, onBranch, pickSet, useCatalogue, type CatalogueSet } from './catalogue';
import { KnowMore } from '../genui/detail/shared';
import { STATUS_LABEL } from '../genui/primitives/format';
import { ReviewerTour } from '../variator/tour/ReviewerTour';
import { preloadSheetsWhenIdle } from '../routes';
import { statusAgrees, tileOf } from './tiles';

export { statusAgrees, tileOf, type Tile } from './tiles';

const AREA_ICON: Record<KbAreaId, LucideIcon> = { money: Wallet, clients: Handshake, delivery: PackageCheck };

interface BriefProps { org: KbOrganization; role: KbRole; asOf?: string; onAsOf: (d: string) => void; onMetric: (id: string) => void; onOpen: (id: string) => void }

/** The catalogue set of the view date (no asOf: the last day of data; the knowledge base down: the newest set). */
export function useBriefSet(asOf: string | undefined, defaultAsOf: string | null | undefined, datesLoading: boolean) {
  const cat = useCatalogue();
  const sets = cat.status === 'ready' ? cat.sets : null;
  const viewDate = asOf ?? defaultAsOf ?? null;
  // no view date yet (the knowledge base has not answered, or is down): the newest catalogue brief shows at once; when
  // the last day of data arrives and differs from it, the page moves to the brief of that date
  const newest = sets && !viewDate ? [...sets].sort((a, b) => b.asOf.localeCompare(a.asOf) || b.builtAt.localeCompare(a.builtAt))[0] ?? null : null;
  const set: CatalogueSet | null = sets ? pickSet(sets, viewDate) ?? newest : null;
  return { loading: cat.status === 'loading' || (!asOf && datesLoading && !newest), sets, set, viewDate };
}

export function KbBriefPage(p: BriefProps & { defaultAsOf?: string | null; datesLoading?: boolean }) {
  const { loading, sets, set, viewDate } = useBriefSet(p.asOf, p.defaultAsOf, !!p.datesLoading);
  const art = useKb(() => set ? catalogueApi.brief(set) : Promise.resolve(null), [set?.id ?? '', set?.briefFile ?? '']);
  // a set and no answer for it yet (the render before its read starts included): still loading, not the fallback brief
  if (loading || (set && !art.data && !art.error)) return <BriefLoading />;
  if (set && art.data) return <BriefView {...p} b={art.data} kind="catalogue" set={set.id} source={set.tenant ? 'Computed by the VARIATOR metric catalogue from the records of this simulated future' : 'Computed by the VARIATOR metric catalogue'} />;
  return <KbComputedBrief {...p} note={fallbackNote(sets, viewDate, set ? art.error : '')} />;
}

/** The brief the workspace computes from the live knowledge base (server/kb). */
function KbComputedBrief(p: BriefProps & { note?: string | null }) {
  const brief = useKb(() => kbApi.brief(p.org.id, p.asOf), [p.org.id, p.asOf]);
  if (brief.loading && !brief.data) return <BriefLoading />;
  if (brief.error) return <div className="workspace-state"><ShieldAlert size={30} /><h1>The brief is unavailable</h1><p>{brief.error}</p><button className="button button-primary" onClick={brief.reload}><RefreshCw size={16} />Try again</button></div>;
  return <BriefView {...p} b={brief.data!} kind="kb" refreshing={brief.loading} source="Computed from the live knowledge base" />;
}

export function BriefLoading() {
  return <div className="workspace-state"><LoaderCircle className="spin" size={28} /><h1>Reading the knowledge base…</h1><p>Mail, Jira, Confluence, GitLab and XWiki are combined into the brief.</p></div>;
}

/** The page of a brief, whatever computed it. `source` names where the numbers come from; `note` is a quiet line
 * under the headline (for example, why this brief is not the catalogue brief). */
export function BriefView({ org, role, b: raw, kind, set, refreshing = false, source, note, onAsOf, onMetric, onOpen }: Omit<BriefProps, 'asOf'> & { b: KbBrief; kind: 'catalogue' | 'kb'; set?: string; refreshing?: boolean; source: string; note?: string | null }) {
  const b = onBranch(raw, org);
  const idx = b.weeks.indexOf(b.asOf);
  const byId = new Map(b.metrics.map(m => [m.id, m]));
  const pick = (ids: string[]) => ids.map(id => byId.get(id)).filter((m): m is KbMetricResult => !!m);
  const top = pick(b.top);
  const first = role.name.split(' ').slice(0, 2).join(' ');
  useEffect(preloadSheetsWhenIdle, []); // the numbers are on screen: the sheets they open load next (src/routes.ts)
  // secondary texts (where the numbers come from, why, how to read the page) are behind «Know more»
  const about = <KnowMore label="About this brief"><span className="kb-about">
    {note && <span className="kb-about-note">{note}</span>}
    <span>{org.name} · {source} ({formatDay(b.range.start)} — {formatDay(b.range.end)}). Numbers by formula, never by a model.</span>
    <span>Each number opens its definition and the records behind it. Items you may not open are counted and marked with a lock; their content follows the knowledge-base permissions of {role.name}.</span>
  </span></KnowMore>;
  return <div className={`kb-brief ${refreshing ? 'kb-refreshing' : ''}`} data-source={kind} data-set={set} data-branch={b.branch?.tenant}>
    <ReviewerTour />
    <section className="page-intro"><div><div className="eyebrow intro-date">MORNING BRIEF{kind === 'kb' && <><span>·</span>KNOWLEDGE BASE</>}<span>·</span>AS OF {formatDate(b.asOf).toUpperCase()}</div>
      <h1>Good morning, {first}<span className="lime-period">.</span>{about}</h1></div>
      <div className="kb-stepper" role="group" aria-label="As-of week">
        <button className="icon-button" disabled={idx <= 0} onClick={() => onAsOf(b.weeks[idx - 1])} aria-label="Previous week"><ChevronLeft size={18} /></button>
        <select aria-label="As of" value={b.asOf} onChange={e => onAsOf(e.target.value)}>{(b.weeks.includes(b.asOf) ? b.weeks : [...b.weeks, b.asOf]).map(w => <option key={w} value={w}>{formatDate(w)}</option>)}</select>
        <button className="icon-button" disabled={idx < 0 || idx >= b.weeks.length - 1} onClick={() => onAsOf(b.weeks[idx + 1])} aria-label="Next week"><ChevronRight size={18} /></button>
      </div>
    </section>
    <div className="briefing-strip kb-headline"><span className="briefing-icon"><Sparkles size={18} /></span><div>{b.headline.map((h, i) => <Headline key={i} text={h} />)}</div></div>
    {top.length > 0 && <section className="metric-grid kb-kpis kb-top" aria-label="Key numbers" data-tour="brief-top" style={{ '--top': top.length } as React.CSSProperties}>{top.map(m => <KpiCard key={m.id} m={m} onOpen={() => onMetric(m.id)} />)}</section>}
    {!b.branch && <BriefDecisions metrics={byId} onMetric={onMetric} />}
    {b.areas.map(a => <AreaSection key={a.id} area={a} metrics={pick(a.metrics)} all={b.metrics} onMetric={onMetric} onOpen={onOpen} />)}
  </div>;
}

/** «Money: invoiced …» — the area name of a headline sentence is set in bold. */
function Headline({ text }: { text: string }) {
  const m = /^([A-Z][A-Za-z ]{1,24}): (.+)$/.exec(text);
  return m ? <p><strong>{m[1]}:</strong> {m[2]}</p> : <p>{text}</p>;
}

function AreaSection({ area, metrics, all, onMetric, onOpen }: { area: KbBriefArea; metrics: KbMetricResult[]; all: KbMetricResult[]; onMetric: (id: string) => void; onOpen: (id: string) => void }) {
  const Icon = AREA_ICON[area.id] ?? Sparkles;
  const kpis = metrics.filter(m => m.view === 'kpi'), lists = metrics.filter(m => m.view !== 'kpi');
  // the state of the whole area, the key numbers of the top row included
  const mine = all.filter(m => m.area === area.id || area.metrics.includes(m.id));
  const bad = mine.filter(m => m.status === 'bad').length, warn = mine.filter(m => m.status === 'warn').length, good = mine.some(m => m.status === 'good');
  const badge = bad ? <span className="badge badge-danger"><i />{bad} {bad === 1 ? 'needs' : 'need'} attention</span>
    : warn ? <span className="badge badge-warn"><i />{warn} to watch</span> : good ? <span className="badge badge-good"><i />On track</span> : null;
  return <section className="kb-area" aria-labelledby={`kb-area-${area.id}`} data-area={area.id}>
    <div className="kb-area-head"><span className="kb-area-icon"><Icon size={17} strokeWidth={1.8} /></span><div className="kb-title-row"><h2 id={`kb-area-${area.id}`}>{area.title}</h2><KnowMore label={`About ${area.title.toLowerCase()}`}>{area.question}</KnowMore></div>{badge}</div>
    {/* a few tiles sit in a column beside the list cards; more tiles get their own rows above them */}
    <div className={`kb-area-body ${kpis.length && kpis.length <= 3 && lists.length ? 'is-split' : ''}`}>
      {kpis.length > 0 && <div className="kb-area-kpis">{kpis.map(m => <KpiCard key={m.id} m={m} compact onOpen={() => onMetric(m.id)} />)}</div>}
      {lists.length > 0 && <div className="kb-lists">{lists.map(m => <ListCard key={m.id} m={m} onMetric={() => onMetric(m.id)} onOpen={onOpen} />)}</div>}
    </div>
    {!metrics.length && <p className="kb-empty">The key numbers of this area are in the top row.</p>}
  </section>;
}

// what a tile shows (value text, change, target, note): src/kb/tiles.ts

function KpiCard({ m, onOpen, compact = false }: { m: KbMetricResult; onOpen: () => void; compact?: boolean }) {
  const t = tileOf(m);
  return <button className={`metric-card kb-kpi ${compact ? 'kb-kpi-compact' : ''}`} data-metric={m.id} onClick={onOpen} aria-label={`${t.title}: ${t.value}.${t.note ? ` ${t.note}` : ''} Open details`}>
    <div className="metric-top"><span>{t.title}</span><ArrowUpRight size={16} className="metric-open" /></div>
    {/* the note is a tooltip: the tile is a button, so it holds no second control */}
    <div className="metric-value">{t.value}{t.note && <span className="kb-kpi-note" title={t.note} role="img" aria-label={t.note}><Info size={14} /></span>}</div>
    <Spark series={t.series} status={m.status} height={compact ? 26 : 34} />
    {/* the baseline, the sample and the caveats are in the metric sheet */}
    <div className="metric-bottom"><StatusTag m={m} target={t.target} />{t.delta && <span>{t.delta}</span>}</div>
  </button>;
}

function StatusTag({ m, target }: { m: KbMetricResult; target: string | null }) {
  const cls = m.status === 'good' ? 'badge-good' : m.status === 'warn' ? 'badge-warn' : m.status === 'bad' ? 'badge-danger' : '';
  return <span className={`badge ${cls}`}><i />{STATUS_LABEL[m.status]}{target ? ` · target ${target}` : ''}</span>;
}

export function Spark({ series, status, height = 34 }: { series: { date: string; value: number | null }[]; status: KbMetricResult['status']; height?: number }) {
  const pts = series.filter(p => p.value !== null) as { date: string; value: number }[];
  if (pts.length < 2) return <div className="kb-spark-empty" style={{ height }} />;
  const max = Math.max(...pts.map(p => p.value)), min = Math.min(...pts.map(p => p.value)), span = max - min || 1, w = 160;
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${(i / (pts.length - 1)) * w},${height - 3 - ((p.value - min) / span) * (height - 6)}`).join(' ');
  return <svg className={`kb-spark kb-spark-${status}`} viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" style={{ height }} role="img" aria-label={`Weekly trend from ${pts[0].value} to ${pts[pts.length - 1].value}`}><path d={d} /></svg>;
}

function ListCard({ m, onMetric, onOpen }: { m: KbMetricResult; onMetric: () => void; onOpen: (id: string) => void }) {
  const t = tileOf(m);
  return <section className="card kb-list-card" data-metric={m.id}>
    <div className="section-heading"><div><h2>{t.title} <span className="count-pill">{t.value}</span><KnowMore label={`About ${t.title}`}>{t.note ? <>{m.question} {t.note}</> : m.question}</KnowMore></h2></div><StatusTag m={m} target={t.target} /></div>
    {m.view === 'breakdown' && m.breakdown.length > 0 && <div className="kb-bars">{m.breakdown.slice(0, 5).map(b => <div key={b.key} className="kb-bar"><span>{b.label}</span><i style={{ width: `${Math.max(6, (b.value / Math.max(...m.breakdown.map(x => x.value))) * 100)}%` }} /><b>{formatValue(b.value, isMoney(m.unit) ? m.unit : 'count')}</b></div>)}</div>}
    <ul className="kb-items">{m.items.slice(0, 3).map((r, i) => <li key={`${r.id}-${i}`}><RecordButton r={r} onOpen={onOpen} facts={1} /></li>)}</ul>
    {!m.items.length && <p className="kb-empty">Nothing here as of this date.</p>}
    <button className="text-button kb-more" onClick={onMetric}>{m.itemsTotal > 3 ? `All ${m.itemsTotal}` : 'Details'} <ArrowUpRight size={14} /></button>
  </section>;
}

/** A record behind a number; `facts`: how many of its facts show (the brief shows one, the sheet three). */
export function RecordButton({ r, onOpen, facts: shown = 3 }: { r: KbRecord; onOpen: (id: string) => void; facts?: number }) {
  const facts = Object.entries(r.facts).filter(([, v]) => v !== null && v !== '').slice(0, shown);
  const locked = r.restricted;
  const body = <><span className="kb-item-kind">{locked && <Lock size={11} />}{KIND_LABEL[r.kind] ?? r.kind} · {formatDate(r.date)}</span><strong>{r.title}</strong>{facts.length > 0 && <small>{facts.map(([k, v]) => `${k}: ${formatFact(k, v)}`).join(' · ')}</small>}{r.relatedTitle && <small className="kb-related">{r.relatedLabel ?? 'related'}: {r.relatedTitle}</small>}</>;
  if (!r.id) return <div className="kb-item kb-item-hidden">{body}</div>;
  return <button className={`kb-item ${locked ? 'kb-item-locked' : ''}`} onClick={() => onOpen(r.id)} title={locked ? 'You can see that it exists, not its content' : 'Open the dossier'}>{body}</button>;
}
