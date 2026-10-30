// Metric drill-down: metric → operation/component → aggregate → records → source, for a side sheet or a page;
// simulation results add events and their declared causes as lineage nodes.
// The id is `metricId` or `metricId~nodeId`; every step is reached through onSelect('gmetric', id), so the host can
// make each step a navigation entry and Back works. Data comes from a DrilldownSource the host provides.
import { useEffect, useMemo, useState } from 'react';
import { ArrowUpRight, ChevronDown, ChevronRight, CircleAlert, ExternalLink, FileText, FlaskConical, LoaderCircle, RefreshCw } from 'lucide-react';
import type { LineageNode, MetricResult, MetricSpec, RecordRef } from '../contracts';
import { DeltaBadge, MiniBar, StatusPill, Value } from '../primitives/atoms';
import { ChangeList, FormulaLine } from '../primitives/molecules';
import { breakdownRows, componentsOf, TrendChart, unknownReason } from '../primitives/organisms';
import { barGeometry, breakdownUnit, compareLabel, constantFields, eventAmount, eventLine, formatNodeValue, formatValue, longDay, plainEngineText, plainLabel, recordPhrases, sinceLabel, stamp, STATUS_LABEL, weightShare } from '../primitives/format';
import { Block, KnowMore } from './shared';
import '../genui.css';

/** Where the drill-down reads a metric. Only `load` is required. */
export interface DrilldownSource {
  /** The definition and the full result (complete lineage) of a metric as of a date; null: the latest. */
  load: (metricId: string, asOf: string | null, compare: 'day' | 'week') => Promise<{ spec: MetricSpec; result: MetricResult }>;
  /** A source document named by RecordRef.evidenceIds; shown as a button that selects ('evidence', id). */
  evidence?: (id: string) => { id: string; title: string } | undefined;
  /** Plain steps generated from the formula, shown next to the spec's own howCalculated. */
  explain?: (spec: MetricSpec) => string[] | null;
  /** A note shown above the drill-down, e.g. that the numbers are sample data. */
  notice?: string | null;
  /** The parameters of the definition (target, bands, window, options), listed in the specification. */
  parameters?: (spec: MetricSpec) => { label: string; value: string }[] | null;
  /** The heading of spec.howCalculated (default: «As the analyst understood it»; a coded metric names its code). */
  howTitle?: string;
}
type OnSelect = (kind: string, id: string) => void;

export const splitMetricId = (id: string) => { const i = id.indexOf('~'); return i < 0 ? { metricId: id, nodeId: null } : { metricId: id.slice(0, i), nodeId: id.slice(i + 1) || null }; };

function findPath(node: LineageNode, id: string, path: LineageNode[] = []): LineageNode[] | null {
  const here = [...path, node];
  if (node.id === id) return here;
  for (const child of node.children) { const found = findPath(child, id, here); if (found) return found; }
  return null;
}
function firstAggregate(node: LineageNode): LineageNode | null {
  if (node.records?.length) return node;
  for (const c of node.children) { const f = firstAggregate(c); if (f) return f; }
  return null;
}
const KIND_LABEL: Record<LineageNode['kind'], string> = { metric: 'Metric', operation: 'Operation', component: 'Index component', aggregate: 'Aggregate', record: 'Record', event: 'Event', cause: 'Cause' };
/** The row style of a node kind: an event row is styled as a record row. */
const rowKind = (kind: LineageNode['kind']) => kind === 'event' ? 'record' : kind;
const causeType = (type: string) => type.replace(/_/g, ' ');
/** The caption of a node: its kind, and for a cause its type («Cause · life event»). */
const kindCaption = (n: LineageNode) => n.kind === 'cause' && n.cause ? `${KIND_LABEL.cause} · ${causeType(n.cause.type)}` : KIND_LABEL[n.kind];
const nodeValue = (n: LineageNode) => formatNodeValue(n.value, n.unit);
/** What a node's row shows on the right: its value; an event the amount its record moved; an event without one and a
 * cause nothing (they have no value of their own, so never «Unknown»). */
function rowValue(n: LineageNode): { text: string; caption?: string; unknown?: boolean } | null {
  if (n.value !== null) return { text: nodeValue(n) };
  if (n.kind === 'event') return eventAmount(n);
  if (n.kind === 'cause') return null;
  return { text: nodeValue(n), unknown: true };
}
/** The plain line under a node: an event says who, what and when; any other text loses its codes and ISO dates. */
const nodeText = (n: LineageNode) => n.value === null && n.unknownReason ? plainEngineText(n.unknownReason) : eventLine(n) ?? plainEngineText(n.explanation);

type Load = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; spec: MetricSpec; result: MetricResult };
function useMetric(source: DrilldownSource, metricId: string, asOf: string | null, compare: 'day' | 'week') {
  const [load, setLoad] = useState<Load>({ status: 'loading' });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoad({ status: 'loading' });
    source.load(metricId, asOf, compare)
      .then(({ spec, result }) => { if (live) setLoad({ status: 'ready', spec, result }); })
      .catch((e: unknown) => { if (live) setLoad({ status: 'error', message: e instanceof Error ? e.message : 'The metric could not be loaded.' }); });
    return () => { live = false; };
  }, [source, metricId, asOf, compare, tick]);
  return { load, retry: () => setTick(t => t + 1) };
}

// ---------------------------------------------------------------------------------------------
function RecordList({ records, onSelect, source, pageSize = 8, label }: { records: RecordRef[]; pageSize?: number; label: string; source: DrilldownSource; onSelect: OnSelect }) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(records.length / pageSize));
  const shown = records.slice(page * pageSize, page * pageSize + pageSize);
  const omit = constantFields(records);
  const phrases = (r: RecordRef) => recordPhrases(r.fields, omit, 6, r.dataset);
  if (!records.length) return <p className="muted">No records.</p>;
  return <div className="g-drill-records" aria-label={label}>
    {shown.map(r => {
      const sources = r.evidenceIds.map(id => source.evidence?.(id)).filter(Boolean);
      return <article key={`${r.dataset}-${r.id}`} className="g-drill-record">
        {r.detail ? <button type="button" className="g-drill-record-main" onClick={() => onSelect(r.detail!.kind, r.detail!.id)}><strong>{plainLabel(r.label)}</strong><ChevronRight size={16} aria-hidden /></button> : <div className="g-drill-record-main"><strong>{plainLabel(r.label)}</strong></div>}
        {phrases(r).length > 0 && <ul className="g-drill-facts">{phrases(r).map(p => <li key={p}>{p}</li>)}</ul>}
        {sources.length > 0 && <div className="g-drill-sources">{sources.slice(0, 3).map(e => { const { role, rest } = sourceRole(e!.title, r.label); return <button type="button" key={e!.id} className="g-drill-source" onClick={() => onSelect('evidence', e!.id)}><FileText size={13} aria-hidden />{role && rest ? <><b>{role}</b><span>{rest}</span></> : <span>{role ? role.charAt(0).toUpperCase() + role.slice(1) : rest}</span>}<ArrowUpRight size={12} aria-hidden /></button>; })}</div>}
      </article>;
    })}
    {pages > 1 && <div className="g-pager"><button type="button" className="button" disabled={page === 0} onClick={() => setPage(p => p - 1)}>Previous</button><span>{page * pageSize + 1}–{Math.min(records.length, (page + 1) * pageSize)} of {records.length}</span><button type="button" className="button" disabled={page >= pages - 1} onClick={() => setPage(p => p + 1)}>Next</button></div>}
  </div>;
}

function Excluded({ node, source, onSelect }: { node: LineageNode; source: DrilldownSource; onSelect: OnSelect }) {
  if (!node.excluded?.length) return null;
  return <div className="g-excluded">{node.excluded.map(x => <details key={x.reason}><summary><ChevronDown size={14} aria-hidden /><span><strong>Excluded {x.count}</strong> · {plainEngineText(x.reason)}</span></summary>{x.sample.length > 0 && <RecordList records={x.sample} source={source} onSelect={onSelect} label={`Excluded: ${x.reason}`} pageSize={5} />}{x.count > x.sample.length && <p className="muted">Showing {x.sample.length} examples of {x.count}.</p>}</details>)}</div>;
}

/** Expandable lineage tree; each node can also become the focus of the panel (a new detail step). */
function TreeNode({ node, metricId, depth, onSelect, focusId, totalWeight = 0 }: { node: LineageNode; metricId: string; depth: number; focusId: string | null; onSelect: OnSelect; totalWeight?: number }) {
  const childWeight = node.children.reduce((a, c) => a + (c.weight ?? 0), 0);
  const [open, setOpen] = useState(depth < 1);
  const hasChildren = node.children.length > 0;
  const isFocus = node.id === focusId;
  const text = nodeText(node);
  const value = rowValue(node);
  const label = plainLabel(node.label);
  return <li className={`g-tree-node is-${rowKind(node.kind)}${isFocus ? ' is-focus' : ''}`}>
    <div className="g-tree-row">
      {hasChildren ? <button type="button" className="g-tree-toggle" aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} ${label}`} onClick={() => setOpen(v => !v)}><ChevronRight size={15} /></button> : <span className="g-tree-toggle is-leaf" aria-hidden />}
      <button type="button" className="g-tree-main" onClick={() => onSelect('gmetric', `${metricId}~${node.id}`)}>
        <span className="g-tree-copy"><small>{kindCaption(node)}{node.weight !== undefined ? ` · ${weightShare(node.weight, totalWeight)} of weight` : ''}{node.records ? ` · ${node.recordsTotal ?? node.records.length} record${(node.recordsTotal ?? node.records.length) === 1 ? '' : 's'}` : ''}</small><strong>{label}</strong><span>{text}</span></span>
        <span className="g-tree-value">{node.normalized != null ? <><b>{Math.round(node.normalized)}</b><small>of 100</small></> : value && <><b className={value.unknown ? 'is-unknown' : ''}>{value.text}</b>{value.caption && <small>{value.caption}</small>}</>}</span>
      </button>
    </div>
    {open && hasChildren && <ul className="g-tree">{node.children.map(c => <TreeNode key={c.id} node={c} metricId={metricId} depth={depth + 1} onSelect={onSelect} focusId={focusId} totalWeight={childWeight} />)}</ul>}
  </li>;
}

function SpecBlock({ spec, params }: { spec: MetricSpec; params?: { label: string; value: string }[] | null }) {
  const window = spec.window.kind === 'rolling' ? `last ${spec.window.days} days` : spec.window.kind === 'periodToDate' ? `${spec.window.period} to date` : 'all history';
  const scope = spec.scope.kind === 'organization' ? 'Whole organization' : spec.scope.kind === 'reportingLine' ? 'Your reporting line' : `${spec.scope.projectIds.length} projects`;
  const list = (title: string, items: string[]) => items.length ? <div className="g-spec-list"><h4>{title}</h4><ul>{items.map(i => <li key={i}>{i}</li>)}</ul></div> : null;
  return <Block title="Specification" closed detail="The agreed definition behind this number">
    <dl className="g-spec-grid">
      <div><dt>Version</dt><dd>v{spec.version} · {spec.status}</dd></div>
      <div><dt>Recalculated</dt><dd>{spec.cadence === 'daily' ? 'Every morning' : 'Every Monday'}</dd></div>
      <div><dt>Compared with</dt><dd>{spec.comparison === 'both' ? 'Yesterday and last week' : spec.comparison === 'previousDay' ? 'Yesterday' : 'Last week'}</dd></div>
      <div><dt>Window · scope</dt><dd>{window} · {scope}</dd></div>
      <div><dt>Better when</dt><dd>{spec.direction === 'higher' ? 'Higher' : spec.direction === 'lower' ? 'Lower' : 'Context only'}{spec.bands ? ` · ${STATUS_LABEL.good.toLowerCase()} ${spec.direction === 'lower' ? '≤' : '≥'} ${formatValue(spec.bands.good, spec.unit)}, ${STATUS_LABEL.warn.toLowerCase()} ${spec.direction === 'lower' ? '≤' : '≥'} ${formatValue(spec.bands.warn, spec.unit)}` : ''}</dd></div>
      <div><dt>Target</dt><dd>{spec.target ? `${formatValue(spec.target.value, spec.unit)} · ${spec.target.basis}` : 'None'}</dd></div>
      <div><dt>Sources</dt><dd>{spec.dataSources.join(', ') || 'Not listed'}</dd></div>
      {spec.reference && <div><dt>Defined as in</dt><dd>{spec.reference.url ? <a href={spec.reference.url} target="_blank" rel="noreferrer">{spec.reference.name} <ExternalLink size={12} aria-hidden /></a> : <>Defined by {spec.reference.name}: {spec.reference.note}</>}</dd></div>}
      <div><dt>Interpretation confidence</dt><dd>{spec.confidence}</dd></div>
    </dl>
    {params?.length ? <div className="g-spec-list g-spec-params"><h4>Parameters</h4><dl className="g-spec-grid">{params.map(p => <div key={p.label}><dt>{p.label}</dt><dd>{p.value}</dd></div>)}</dl></div> : null}
    {list('Assumptions', spec.assumptions)}{list('Open questions', spec.openQuestions)}{list('Limitations', spec.limitations.map(plainEngineText))}
    {/* a metric computed by code (op «metric») has no DSL of its own: its formulas are in the breakdown */}
    {spec.formula.op !== 'metric' && <details className="g-dsl"><summary><ChevronDown size={14} aria-hidden />Technical definition (DSL)</summary><pre>{JSON.stringify(spec.formula, null, 2)}</pre></details>}
  </Block>;
}

// ---------------------------------------------------------------------------------------------
export function MetricDrilldown({ id, source, onSelect, asOf = null, compare = 'day' }: { id: string; source: DrilldownSource; onSelect: OnSelect; asOf?: string | null; compare?: 'day' | 'week' }) {
  const { metricId, nodeId } = splitMetricId(id);
  const { load, retry } = useMetric(source, metricId, asOf, compare);
  const steps = useMemo(() => {
    if (load.status !== 'ready' || !source.explain) return null;
    try { return source.explain(load.spec); } catch { return null; }
  }, [load, source]);
  if (load.status === 'loading') return <div className="g-drill g-drill-state"><LoaderCircle className="spin" size={22} /><p>Loading the full breakdown…</p></div>;
  if (load.status === 'error') return <div className="g-drill g-drill-state"><CircleAlert size={22} /><p>{load.message}</p><button className="button" onClick={retry}><RefreshCw size={15} />Retry</button></div>;
  const { spec, result } = load;
  const sample = source.notice && <p className="g-drill-sample"><FlaskConical size={14} aria-hidden />{source.notice}</p>;

  // Focused views: one node of the lineage, the highlight list, or the full breakdown.
  if (nodeId === 'highlight' || nodeId === 'breakdown') {
    return <div className="g-drill">{sample}
      <Crumbs spec={spec} path={[]} metricId={metricId} onSelect={onSelect} current={nodeId === 'highlight' ? spec.highlight?.label ?? 'Needs attention' : 'Breakdown'} />
      {nodeId === 'highlight' ? <Block title={`${spec.highlight?.label ?? 'Needs attention'} · ${result.highlight.length}`} detail={`Records on ${longDay(result.asOf)}`}><RecordList records={result.highlight} source={source} onSelect={onSelect} label="Highlighted records" /></Block>
        : <Block title={breakdownTitle(spec, result)} detail={`${result.breakdown.length} groups on ${longDay(result.asOf)}`}><BreakdownList result={result} spec={spec} onSelect={onSelect} /></Block>}
    </div>;
  }
  if (nodeId) {
    const path = findPath(result.lineage, nodeId);
    if (!path) return <div className="g-drill">{sample}<Crumbs spec={spec} path={[]} metricId={metricId} onSelect={onSelect} current="Missing step" /><p className="muted">This step of the breakdown is not part of the result for {longDay(result.asOf)}. The formula may have changed.</p><button className="button" onClick={() => onSelect('gmetric', metricId)}>Whole metric</button></div>;
    const node = path.at(-1)!;
    const components = node.kind === 'metric' || node.kind === 'operation' ? componentsOf(node) : [];
    const totalWeight = components.reduce((a, c) => a + (c.weight ?? 0), 0);
    const value = rowValue(node);
    return <div className="g-drill">{sample}
      <Crumbs spec={spec} path={path.slice(1, -1)} metricId={metricId} onSelect={onSelect} current={node.label} />
      <section className="g-drill-hero">
        <span className="eyebrow">{kindCaption(node)}</span>
        {node.normalized != null ? <div className="g-drill-hero-value"><Value value={node.normalized} unit="score" size="xl" /><span className="g-drill-raw">raw {nodeValue(node)}</span></div>
          : value && <div className="g-drill-hero-value"><span className="g-value g-value-xl">{value.text}</span>{value.caption && <span className="g-drill-raw">{value.caption}</span>}</div>}
        <p className="g-drill-expl">{nodeText(node)}</p>
        <FormulaLine node={node} label="With numbers" />
        {node.kind === 'component' && <dl className="g-spec-grid">
          <div><dt>Share of the index</dt><dd>{weightShare(node.weight ?? 0, (path.at(-2)?.children ?? []).reduce((a, c) => a + (c.weight ?? 0), 0))}</dd></div>
          <div><dt>Score 0–100</dt><dd>{node.normalized == null ? 'Unknown' : Math.round(node.normalized)}</dd></div>
          <div><dt>Records</dt><dd>{node.sample ?? '—'}{(() => { const c = spec.formula.op === 'index' ? spec.formula.components.find(x => x.label === node.label) : null; return c ? ` · at least ${c.minSample} needed · worst ${c.worst}, best ${c.best}` : ''; })()}</dd></div>
        </dl>}
        {node.kind === 'cause' && node.cause && <dl className="g-spec-grid">
          <div><dt>Cause type</dt><dd>{causeType(node.cause.type)}</dd></div>
        </dl>}
      </section>
      {node.children.length > 0 && <Block title="Made of" detail="Open a step to see its records">{components.length ? <div className="g-drill-comps">{components.map(c => <button type="button" key={c.id} className="g-drill-comp" onClick={() => onSelect('gmetric', `${metricId}~${c.id}`)}><span><strong>{plainLabel(c.label)}</strong><small>weight {c.weight} · {c.sample ?? 0} records</small></span><MiniBar value={c.normalized} label={c.label} tone={c.normalized == null ? 'muted' : c.normalized < 40 ? 'bad' : c.normalized < 60 ? 'warn' : 'neutral'} /><b>{c.normalized == null ? '—' : Math.round(c.normalized)}</b><ChevronRight size={15} aria-hidden /></button>)}</div>
        : <ul className="g-tree g-tree-root">{node.children.map(c => <TreeNode key={c.id} node={c} metricId={metricId} depth={1} onSelect={onSelect} focusId={null} totalWeight={node.children.reduce((a, x) => a + (x.weight ?? 0), 0)} />)}</ul>}</Block>}
      {node.records && <Block title={`Records · ${node.recordsTotal ?? node.records.length}`} detail={(node.recordsTotal ?? 0) > node.records.length ? `Showing ${node.records.length} of ${node.recordsTotal}; the engine truncated this list.` : 'The complete list behind this step, with the fields that made each one count'}><RecordList records={node.records} source={source} onSelect={onSelect} label={`Records of ${plainLabel(node.label)}`} /></Block>}
      {node.excluded?.length ? <Block title="Left out" detail="Records that exist but do not count here, and why"><Excluded node={node} source={source} onSelect={onSelect} /></Block> : null}
    </div>;
  }

  // Whole metric.
  const rootComponents = componentsOf(result.lineage);
  const example = rootComponents.length ? null : firstAggregate(result.lineage);
  const exampleRecords = example?.records ?? [];
  const exampleOmit = constantFields(exampleRecords);
  const rootWeight = rootComponents.reduce((a, c) => a + (c.weight ?? 0), 0);
  return <div className="g-drill">{sample}
    <section className="g-drill-hero">
      <div className="g-drill-hero-top"><StatusPill status={result.status} /><span className="g-muted">as of {longDay(result.asOf)}</span>
        <KnowMore label="About this number" align="end">{result.coverage.records} record{result.coverage.records === 1 ? '' : 's'}{result.coverage.historyAvailable ? '' : ' · snapshot without history'} · computed {stamp(result.computedAt) ?? result.computedAt} · engine {result.engineVersion}</KnowMore></div>
      <div className="g-drill-hero-value"><Value value={result.value} unit={spec.unit} size="xl" brief /></div>
      {result.value === null ? <p className="g-drill-expl"><strong>Unknown.</strong> {unknownReason(result)}</p> : <div className="g-valueblock-deltas"><DeltaBadge current={result.value} previous={result.previous.day} unit={spec.unit} direction={spec.direction} label={compareLabel('day')} /><DeltaBadge current={result.value} previous={result.previous.week} unit={spec.unit} direction={spec.direction} label={compareLabel('week')} />{spec.target && <span className="g-delta g-tone-neutral">Target {formatValue(spec.target.value, spec.unit)}</span>}</div>}
      {/* a warning stays visible: parts of the number are unknown */}
      {result.coverage.unknownComponents.length > 0 && <p className="g-drill-coverage">Unknown: {result.coverage.unknownComponents.join(', ')}</p>}
    </section>
    <Block title="The question" detail={spec.plainDefinition}><p className="g-question-quote">{spec.businessQuestion}</p>
      {/* the source of the definition stays visible: people check the number against it */}
      {spec.reference && <p className="g-metric-ref">Defined as in: {spec.reference.url ? <a href={spec.reference.url} target="_blank" rel="noreferrer">{spec.reference.name}<ExternalLink size={12} aria-hidden /></a> : <span title={spec.reference.note}>{spec.reference.name} (own definition: {spec.reference.note})</span>}</p>}</Block>
    <Block title="Worked example on this morning" detail="Today’s actual records, with the numbers substituted">
      <FormulaLine node={result.lineage} label={plainLabel(result.lineage.label)} />
      <p className="g-drill-text">{plainEngineText(result.lineage.explanation)}</p>
      {rootComponents.length > 0 && <div className="g-example">{rootComponents.map(c => <button key={c.id} type="button" className="g-example-row" onClick={() => onSelect('gmetric', `${metricId}~${c.id}`)}><span><strong>{plainLabel(c.label)}</strong><small>{c.normalized == null ? plainEngineText(c.unknownReason ?? 'Unknown this morning') : `${Math.round(c.normalized)} of 100 × ${weightShare(c.weight ?? 0, rootWeight)} of weight · ${c.sample ?? c.children[0]?.recordsTotal ?? 0} records`}</small></span><ChevronRight size={15} aria-hidden /></button>)}</div>}
      {exampleRecords.length > 0 && <div className="g-example">{exampleRecords.slice(0, 4).map(r => <button key={r.id} type="button" className="g-example-row" disabled={!r.detail} onClick={() => r.detail && onSelect(r.detail.kind, r.detail.id)}><span><strong>{plainLabel(r.label)}</strong><small>{recordPhrases(r.fields, exampleOmit, 3, r.dataset).join(' · ')}</small></span>{r.detail && <ChevronRight size={15} aria-hidden />}</button>)}{exampleRecords.length > 4 && <button type="button" className="g-text-button" onClick={() => onSelect('gmetric', `${metricId}~${example!.id}`)}>All {example!.recordsTotal ?? exampleRecords.length} records of “{plainLabel(example!.label)}” <ChevronRight size={14} /></button>}</div>}
      {!rootComponents.length && example?.excluded?.map(x => <p key={x.reason} className="g-drill-excluded">Excluded {x.count}: {plainEngineText(x.reason)}.</p>)}
    </Block>
    <Block title="How it’s calculated" closed detail={source.howTitle ? 'The plain steps of the definition, and the steps of this number’s breakdown' : 'The analyst’s plain steps, checked against steps generated deterministically from the formula'}>
      <div className="g-how">
        <div><h4>{source.howTitle ?? 'As the analyst understood it'}</h4><ol>{spec.howCalculated.map(s => <li key={s}>{plainEngineText(s)}</li>)}</ol></div>
        {steps?.length ? <details className="g-how-engine" open={steps.length <= 6}><summary><ChevronDown size={14} aria-hidden /><span><strong>From the formula</strong> · {steps.length} steps generated by the engine</span></summary><ol>{steps.map((s, i) => <li key={i}>{plainEngineText(s.replace(/^\s*\d+[.)]\s*/, ''))}</li>)}</ol></details>
          : <p className="muted">The engine’s step-by-step explanation is not available in this build.</p>}
      </div>
    </Block>
    <Block title="Breakdown of this number" detail="Expand a step, or open it to see its complete records and sources"><ul className="g-tree g-tree-root"><TreeNode node={result.lineage} metricId={metricId} depth={0} onSelect={onSelect} focusId={null} /></ul></Block>
    {result.coverage.historyAvailable && result.series.filter(p => p.value !== null).length > 1 && <Block title="History" detail={`One point per ${spec.cadence === 'weekly' ? 'week' : 'day'}`}><TrendChart spec={spec} result={result} showTarget height={170} /></Block>}
    {result.breakdown.length > 0 && <Block title={breakdownTitle(spec, result)}><BreakdownList result={result} spec={spec} onSelect={onSelect} limit={8} metricId={metricId} /></Block>}
    {spec.highlight && <Block title={`${spec.highlight.label} · ${result.highlight.length}`}>{result.highlight.length ? <RecordList records={result.highlight} source={source} onSelect={onSelect} label="Highlighted records" pageSize={5} /> : <p className="muted">Nothing on this morning.</p>}</Block>}
    <Block title={`What changed ${sinceLabel(compare)}`}><ChangeList changes={result.changes} onOpen={r => r.detail && onSelect(r.detail.kind, r.detail.id)} limit={8} /></Block>
    <SpecBlock spec={spec} params={(() => { try { return source.parameters?.(spec); } catch { return null; } })()} />
  </div>;
}

function Crumbs({ spec, path, metricId, onSelect, current }: { spec: MetricSpec; path: LineageNode[]; metricId: string; current: string; onSelect: OnSelect }) {
  return <nav className="g-crumbs" aria-label="Position in the breakdown">
    <button type="button" className="g-link" onClick={() => onSelect('gmetric', metricId)}>{spec.title}</button>
    {path.map(n => <span key={n.id}><ChevronRight size={13} aria-hidden /><button type="button" className="g-link" onClick={() => onSelect('gmetric', `${metricId}~${n.id}`)}>{plainLabel(n.label)}</button></span>)}
    <span><ChevronRight size={13} aria-hidden /><strong aria-current="page">{plainLabel(current)}</strong></span>
  </nav>;
}

/** The header of the breakdown names the unit once when the groups add up to another step («Group · ARR lost»). */
function breakdownTitle(spec: MetricSpec, result: MetricResult): string {
  const { of } = breakdownUnit(result.breakdown, result.lineage, spec.unit);
  return `${breakdownRows(spec, result, 0).label}${of ? ` · ${plainLabel(of)}` : ''}`;
}

function BreakdownList({ result, spec, onSelect, limit, metricId }: { result: MetricResult; spec: MetricSpec; onSelect: OnSelect; limit?: number; metricId?: string }) {
  const { person, rows } = breakdownRows(spec, result, limit ?? result.breakdown.length);
  const { unit } = breakdownUnit(result.breakdown, result.lineage, spec.unit);
  const geometry = barGeometry(rows.map(r => r.value));
  return <>{person && <p className="g-drill-excluded">Grouped by person for context only, alphabetical; not a rating of anyone.</p>}<ul className="g-bars g-drill-bars">{rows.map(r => {
    const g = geometry(r.value);
    const bar = <><span className="g-bar-label">{plainLabel(r.label)}</span><span className="g-bar-track"><i className={`g-bar-fill g-bar-neutral${g.negative ? ' is-negative' : ''}`} style={{ left: `${g.left}%`, width: `${g.width}%` }} /></span><span className="g-bar-value">{formatValue(r.value, unit)}</span></>;
    return <li key={r.key}>{r.detail ? <button type="button" className="g-bar" onClick={() => onSelect(r.detail!.kind, r.detail!.id)}>{bar}</button> : <div className="g-bar">{bar}</div>}</li>;
  })}</ul>{limit && metricId && result.breakdown.length > limit && <button type="button" className="g-text-button" onClick={() => onSelect('gmetric', `${metricId}~breakdown`)}>All {result.breakdown.length} groups <ChevronRight size={14} /></button>}</>;
}

/** "Orbit … · first signal" → badge "first signal" before the shared title, so source buttons differ at a glance. */
function sourceRole(title: string, recordLabel: string): { role: string | null; rest: string } {
  const i = title.lastIndexOf(' · ');
  if (i < 0) return { role: null, rest: title };
  const role = title.slice(i + 3), head = title.slice(0, i);
  return { role, rest: head === recordLabel || recordLabel.startsWith(head) || head.startsWith(recordLabel) ? '' : head };
}
