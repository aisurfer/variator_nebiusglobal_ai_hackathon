// Organisms: the 11 dashboard primitives of the fixed catalog (descriptors.ts), one props interface.
import { useMemo, useState, type ReactNode } from 'react';
import { ArrowUpRight, CheckCircle2, ChevronRight, CircleHelp, History } from 'lucide-react';
import { Area, CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { LineageNode, MetricResult, MetricSpec, MetricStatus, PrimitiveId, RecordRef, WidgetSpec } from '../contracts';
import { comparedMorning, orderBreakdown, surpriseCounts } from './brief';
import { DeltaBadge, Sparkline, StatusPill, Value } from './atoms';
import { ChangeList, ComponentRow, MetricHeader, RecordRow, TargetBar, ValueBlock } from './molecules';
import { barGeometry, compareLabel, constantFields, sharedPhrases, formatDelta, formatValue, longDay, plainEngineText, shortDay, statusFor, STATUS_LABEL, weekday } from './format';
import { useComparedResult, usePrimitiveRuntime } from './runtime';

export interface PrimitiveProps {
  widget: WidgetSpec;
  specs: MetricSpec[];
  results: Record<string, MetricResult>;
  compare: 'day' | 'week';
  onOpen(metricId: string, nodeId?: string): void;
}

// ---------------------------------------------------------------------------------------------
// Shared pieces
// ---------------------------------------------------------------------------------------------
function resolve(props: PrimitiveProps, index = 0) {
  const id = props.widget.metricIds[index];
  const spec = props.specs.find(s => s.id === id) ?? null;
  const result = id ? props.results[id] ?? null : null;
  return { id, spec, result, title: props.widget.title ?? spec?.title ?? 'Metric' };
}
const toneOf = (status: MetricStatus): 'neutral' | 'warn' | 'bad' => status === 'bad' ? 'bad' : status === 'warn' ? 'warn' : 'neutral';
export const unknownReason = (r: MetricResult) => plainEngineText(r.lineage.unknownReason ?? (r.coverage.unknownComponents.length ? `Not enough data for ${r.coverage.unknownComponents.join(', ')}.` : r.lineage.explanation) ?? 'The engine could not compute this value for this morning.');

function Card({ widget, children, className = '' }: { widget: WidgetSpec; children: ReactNode; className?: string }) {
  return <article className={`g-card g-p-${widget.primitive} ${className}`} data-widget={widget.id} data-primitive={widget.primitive}>{children}</article>;
}
/** A widget whose metric or result is missing renders as a neutral tile, never as a crash. */
function Missing({ widget, title, reason, onOpen }: { widget: WidgetSpec; title: string; reason: string; onOpen?: () => void }) {
  return <Card widget={widget} className="g-card-missing"><MetricHeader title={title} onOpen={onOpen} /><p className="g-state"><CircleHelp size={16} aria-hidden />{reason}</p></Card>;
}
function UnknownState({ result }: { result: MetricResult }) {
  return <p className="g-state g-state-unknown"><CircleHelp size={16} aria-hidden /><span><strong>Unknown for this morning.</strong> {unknownReason(result)}</span></p>;
}
function NoHistoryNote({ result }: { result: MetricResult }) {
  return <p className="g-state"><History size={16} aria-hidden /><span><strong>No history.</strong> This source is a snapshot, so only {shortDay(result.asOf)} is known; earlier mornings are not invented.</span></p>;
}
function contextLine(spec: MetricSpec): string | null {
  const parts: string[] = [];
  if (spec.target) parts.push(`Target ${formatValue(spec.target.value, spec.unit)}`);
  if (spec.bands && spec.direction !== 'context') parts.push(`Good ${spec.direction === 'higher' ? '≥' : '≤'} ${formatValue(spec.bands.good, spec.unit)}`);
  return parts.join(' · ') || null;
}
/** The requested window of the series, without leading unknown days (history may start later). */
export function seriesFor(r: MetricResult, days: number | null | undefined) {
  const window = days ? r.series.slice(-days) : r.series;
  const first = window.findIndex(p => p.value !== null);
  return first <= 0 ? window : window.slice(first);
}

/**
 * Absolute surprise numbers beside an index (T06.7). Counts come from the engine's records, and
 * "from clients or outside parties" from the records or a companion metric; never an invented zero.
 * Loads the full lineage when the dashboard view truncated it.
 */
function SurpriseFacts({ spec, result, props }: { spec: MetricSpec; result: MetricResult; props: PrimitiveProps }) {
  const isIndex = spec.formula.op === 'index';
  const companions = { specs: props.specs, results: props.results };
  const direct = isIndex ? surpriseCounts(result, companions) : null;
  const full = useComparedResult(spec.id, isIndex && !direct ? result.asOf : null);
  const counts = direct ?? (full ? surpriseCounts(full, companions) : null);
  if (!counts) return null;
  return <p className="g-facts"><strong>{counts.surprises}</strong> {counts.surprises === 1 ? 'surprise' : 'surprises'}{counts.outside !== null && <> <span>·</span> <strong>{counts.outside}</strong> from {counts.outsideLabel}</>} <span>·</span> of {counts.issues} {counts.issues === 1 ? 'issue' : 'issues'} that reached you</p>;
}

// ---------------------------------------------------------------------------------------------
// kpi, kpiTarget, scoreGauge: compact tiles, the whole tile opens the drilldown.
// ---------------------------------------------------------------------------------------------
function Tile({ widget, title, status, onOpen, children }: { widget: WidgetSpec; title: string; status: MetricStatus; onOpen: () => void; children: ReactNode }) {
  return <Card widget={widget} className={`g-tile g-tile-${status}`}>
    <button type="button" className="g-tile-button" onClick={onOpen}>
      <span className="g-tile-top"><span className="g-tile-title">{title}</span><StatusPill status={status} showQuiet={false} /><ArrowUpRight size={15} className="g-tile-open" aria-hidden /></span>
      {children}
      <span className="sr-only">Open breakdown</span>
    </button>
  </Card>;
}

export function KpiTile(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const context = contextLine(spec);
  return <Tile widget={props.widget} title={title} status={result.status} onOpen={() => props.onOpen(id)}>
    {result.value === null ? <UnknownState result={result} /> : <ValueBlock spec={spec} result={result} compare={props.compare} />}
    {spec.formula.op === 'index' && result.value !== null && <SurpriseFacts spec={spec} result={result} props={props} />}
    <span className="g-tile-foot">
      {result.coverage.historyAvailable && <Sparkline series={seriesFor(result, props.widget.options.seriesDays ?? 28)} unit={spec.unit} label={`${title} trend`} tone={toneOf(result.status)} target={props.widget.options.showTarget ? spec.target?.value : null} />}
      {context && <span className="g-tile-context">{context}</span>}
    </span>
  </Tile>;
}

export function KpiTargetTile(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const target = spec.target?.value ?? null;
  const share = target && result.value !== null ? result.value / target * 100 : null;
  return <Tile widget={props.widget} title={title} status={result.status} onOpen={() => props.onOpen(id)}>
    {result.value === null ? <UnknownState result={result} /> : <ValueBlock spec={spec} result={result} compare={props.compare} />}
    <TargetBar value={result.value} target={target} bands={spec.bands} direction={spec.direction} unit={spec.unit} status={result.status} previous={result.previous[props.compare]} />
    <span className="g-tile-context">{target === null ? 'No target in the spec' : share === null ? `Target ${formatValue(target, spec.unit)}` : `${Math.round(share)}% of ${formatValue(target, spec.unit)} target · ${spec.target!.basis}`}</span>
  </Tile>;
}

export function ScoreGauge(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  return <Tile widget={props.widget} title={title} status={result.status} onOpen={() => props.onOpen(id)}>
    {result.value === null ? <UnknownState result={result} /> : <ValueBlock spec={spec} result={result} compare={props.compare} size="xl" />}
    <TargetBar value={result.value} target={null} bands={spec.bands} direction={spec.direction} unit="score" status={result.status} previous={result.previous[props.compare]} />
    {spec.formula.op === 'index' && result.value !== null && <SurpriseFacts spec={spec} result={result} props={props} />}
    <span className="g-tile-context">{result.coverage.unknownComponents.length ? `${result.coverage.unknownComponents.length} component${result.coverage.unknownComponents.length > 1 ? 's' : ''} unknown · weights renormalized` : `${result.coverage.records} records · ${spec.bands ? (spec.direction === 'lower' ? `${STATUS_LABEL.warn.toLowerCase()} above ${spec.bands.good}, ${STATUS_LABEL.bad.toLowerCase()} above ${spec.bands.warn}` : `${STATUS_LABEL.warn.toLowerCase()} below ${spec.bands.good}, ${STATUS_LABEL.bad.toLowerCase()} below ${spec.bands.warn}`) : 'no band set'}`}</span>
  </Tile>;
}

// ---------------------------------------------------------------------------------------------
// trend
// ---------------------------------------------------------------------------------------------
export function TrendChart({ spec, result, days, showTarget, height = 190 }: { spec: MetricSpec; result: MetricResult; days?: number | null; showTarget?: boolean | null; height?: number }) {
  const data = seriesFor(result, days).map(p => ({ date: p.date, value: p.value }));
  const trimmed = (days ? Math.min(days, result.series.length) : result.series.length) > data.length;
  const known = data.filter(p => p.value !== null) as { date: string; value: number }[];
  const refValue = showTarget !== false ? spec.target?.value ?? (spec.bands && spec.direction !== 'context' ? spec.bands.good : null) : null;
  const refLabel = spec.target ? `Target ${formatValue(spec.target.value, spec.unit)}` : refValue !== null ? `Good ${spec.direction === 'higher' ? '≥' : '≤'} ${formatValue(refValue, spec.unit)}` : '';
  const low = known.reduce<{ date: string; value: number } | null>((m, p) => !m || p.value < m.value ? p : m, null);
  const high = known.reduce<{ date: string; value: number } | null>((m, p) => !m || p.value > m.value ? p : m, null);
  const summary = known.length ? `${spec.title}, ${data.length} ${spec.cadence === 'weekly' ? 'weeks' : 'days'}: from ${formatValue(known[0].value, spec.unit)} on ${shortDay(known[0].date)} to ${formatValue(known.at(-1)!.value, spec.unit)} on ${shortDay(known.at(-1)!.date)}; lowest ${formatValue(low!.value, spec.unit)} on ${shortDay(low!.date)}, highest ${formatValue(high!.value, spec.unit)} on ${shortDay(high!.date)}.` : `${spec.title}: no values in this period.`;
  // The series is one neutral line (the product's chart colour); only today's point carries status colour.
  const line = '#749e26';
  const todayColour = result.status === 'bad' ? '#c76759' : result.status === 'warn' ? '#b38538' : line;
  const lastIndex = data.map(p => p.value).lastIndexOf(known.at(-1)?.value ?? NaN);
  const tick = (v: number) => formatValue(v, spec.unit).replace(/ days?$/, 'd').replace(/ weeks?$/, 'w');
  return <figure className="g-trend" aria-label={summary}>
    <div className="g-trend-plot" style={{ height }} role="img" aria-label={summary}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 10, right: 8, left: -8, bottom: 0 }}>
          <defs><linearGradient id={`g-fill-${spec.id}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={line} stopOpacity={0.1} /><stop offset="100%" stopColor={line} stopOpacity={0.01} /></linearGradient></defs>
          <CartesianGrid vertical={false} stroke="#edf0f1" />
          <XAxis dataKey="date" axisLine={false} tickLine={false} tick={{ fill: '#76828a', fontSize: 11 }} tickFormatter={shortDay} minTickGap={28} tickMargin={8} />
          <YAxis axisLine={false} tickLine={false} tick={{ fill: '#76828a', fontSize: 11 }} tickFormatter={tick} width={52} domain={spec.unit === 'score' || spec.unit === 'percent' ? [(min: number) => Math.max(0, Math.floor(Math.min(min, refValue ?? min) / 10) * 10 - 10), (max: number) => Math.min(spec.unit === 'score' ? 100 : Math.max(100, max), Math.ceil(Math.max(max, refValue ?? max) / 10) * 10 + 5)] : ['auto', 'auto']} allowDecimals={spec.unit !== 'count'} />
          <Tooltip cursor={{ stroke: '#9aa6ad', strokeWidth: 1 }} contentStyle={{ border: '1px solid #e5e8eb', borderRadius: 10, fontSize: 12, boxShadow: '0 6px 20px #18202c12' }} labelFormatter={l => weekday(String(l))} formatter={v => [v === null || v === undefined ? 'Unknown' : formatValue(Number(v), spec.unit), spec.title]} />
          {refValue !== null && <ReferenceLine y={refValue} stroke="#9aa6ad" strokeDasharray="4 4" />}
          <Area type="monotone" dataKey="value" stroke="none" fill={`url(#g-fill-${spec.id})`} connectNulls={false} isAnimationActive={false} />
          <Line type="monotone" dataKey="value" stroke={line} strokeWidth={2} dot={(d: { cx?: number; cy?: number; index?: number }) => d.index === lastIndex && d.cx !== undefined && d.cy !== undefined ? <circle key="today" cx={d.cx} cy={d.cy} r={4.5} fill={todayColour} stroke="#fff" strokeWidth={2} /> : <g key={`p${d.index}`} />} activeDot={{ r: 4, stroke: '#fff', strokeWidth: 2 }} connectNulls={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
    <div className="g-trend-legend" aria-hidden><span><i className="g-legend-line" />{spec.title}</span>{refValue !== null && <span><i className="g-legend-dash" />{refLabel}</span>}{result.status === 'bad' || result.status === 'warn' ? <span><i className={`g-legend-dot g-legend-${result.status}`} />Today: {STATUS_LABEL[result.status]}</span> : null}</div>
    {trimmed && data.length > 0 && <figcaption className="g-trend-note">History available from {shortDay(data[0].date)}; earlier mornings are unknown, not zero.</figcaption>}
    <table className="sr-only"><caption>{spec.title} by date</caption><tbody>{data.map(p => <tr key={p.date}><th scope="row">{longDay(p.date)}</th><td>{p.value === null ? 'Unknown' : formatValue(p.value, spec.unit)}</td></tr>)}</tbody></table>
  </figure>;
}

export function Trend(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  return <Card widget={props.widget}>
    <MetricHeader title={title} status={result.status} spec={spec} onOpen={() => props.onOpen(id)} />
    <div className="g-inline-value"><Value value={result.value} unit={spec.unit} size="md" />{result.value !== null && <DeltaBadge current={result.value} previous={result.previous[props.compare]} unit={spec.unit} direction={spec.direction} label={compareLabel(props.compare)} />}</div>
    {!result.coverage.historyAvailable ? <NoHistoryNote result={result} /> : result.series.filter(p => p.value !== null).length < 2 ? <p className="g-state"><History size={16} aria-hidden />Not enough history for a trend yet.</p> : <TrendChart spec={spec} result={result} days={props.widget.options.seriesDays} showTarget={props.widget.options.showTarget} />}
  </Card>;
}

// ---------------------------------------------------------------------------------------------
// barBreakdown, table
// ---------------------------------------------------------------------------------------------
function useOpenDetail(fallback: () => void) {
  const { onDetail } = usePrimitiveRuntime();
  return (detail: RecordRef['detail']) => detail && onDetail ? onDetail(detail) : fallback();
}
const DIMENSION_LABEL: Record<string, string> = { project: 'Project', employee: 'Person', department: 'Department', team: 'Team', source: 'Source', category: 'Category', severity: 'Severity', month: 'Month', week: 'Week', client: 'Client', stage: 'Stage' };

/** Breakdown rows for display. Person groups are alphabetical and never coloured or rated (principle 5). */
export function breakdownRows(spec: MetricSpec, result: MetricResult, limit: number) {
  const { person, rows } = orderBreakdown(spec, result.breakdown);
  return { person, rows: rows.slice(0, limit), label: person ? 'Person · alphabetical, not a ranking' : DIMENSION_LABEL[spec.breakdown?.dimension ?? ''] ?? 'Group' };
}
const rowStatus = (spec: MetricSpec, value: number | null, person: boolean): MetricStatus => person ? 'neutral' : value === null ? 'unknown' : statusFor(spec, value);

export function BarBreakdown(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  const open = useOpenDetail(() => props.onOpen(id, 'breakdown'));
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const { person, rows, label } = breakdownRows(spec, result, props.widget.options.limit ?? spec.breakdown?.limit ?? 10);
  const geometry = barGeometry([...rows.map(r => r.value), ...(spec.unit === 'percent' ? [100] : [])]);
  const flagged = rows.filter(r => { const s = rowStatus(spec, r.value, person); return s === 'bad' || s === 'warn'; });
  return <Card widget={props.widget}>
    <MetricHeader title={title} status={result.status} spec={spec} onOpen={() => props.onOpen(id)} eyebrow={spec.breakdown ? `By ${label.toLowerCase()}` : undefined} />
    {!rows.length ? <p className="g-empty">No breakdown for this morning.</p> : <ul className="g-bars" aria-label={`${title} by ${label.toLowerCase()}`}>
      {rows.map(r => {
        const s = rowStatus(spec, r.value, person);
        const flag = (s === 'bad' || s === 'warn') && flagged.length < rows.length;
        const g = geometry(r.value);
        const bar = <><span className="g-bar-label">{r.label}</span><span className="g-bar-track"><i className={`g-bar-fill g-bar-${toneOf(s)}${g.negative ? ' is-negative' : ''}`} style={{ left: `${g.left}%`, width: `${g.width}%` }} /></span><span className="g-bar-value">{formatValue(r.value, spec.unit)}{flag && <small className={`g-tone-${s}`}>{STATUS_LABEL[s]}</small>}</span></>;
        return <li key={r.key}>{r.detail ? <button type="button" className="g-bar" onClick={() => open(r.detail)}>{bar}</button> : <div className="g-bar">{bar}</div>}</li>;
      })}
    </ul>}
    {person && <p className="g-foot-note">Grouped by person for context only, in alphabetical order; this is not a rating of anyone.</p>}
    {!person && flagged.length > 0 && spec.bands && <p className="g-foot-note">{flagged.length === rows.length ? 'All' : flagged.length} of {rows.length} {spec.direction === 'higher' ? 'below' : 'above'} the good level ({spec.direction === 'higher' ? '≥' : '≤'} {formatValue(spec.bands.good, spec.unit)}){flagged.length === rows.length ? ', so all bars are marked' : ''}.</p>}
    {result.breakdown.length > rows.length && <button type="button" className="g-text-button" onClick={() => props.onOpen(id, 'breakdown')}>All {result.breakdown.length} groups <ChevronRight size={14} /></button>}
  </Card>;
}

export function BreakdownTable(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  const open = useOpenDetail(() => props.onOpen(id, 'breakdown'));
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const { person, rows, label } = breakdownRows(spec, result, props.widget.options.limit ?? spec.breakdown?.limit ?? 25);
  const additive = spec.unit === 'count' || spec.unit === 'usd' || spec.unit === 'hours';
  const total = additive ? result.breakdown.reduce((a, r) => a + (r.value ?? 0), 0) : 0; // share of all groups, not only the rows shown
  const hidden = result.breakdown.length - rows.length;
  const banded = !!spec.bands && !person;
  return <Card widget={props.widget}>
    <MetricHeader title={title} status={result.status} spec={spec} onOpen={() => props.onOpen(id)} />
    {!rows.length ? <p className="g-empty">No breakdown for this morning.</p> : <div className="g-table-wrap" tabIndex={0} role="region" aria-label={`${title} table`}><table className="g-table">
      <thead><tr><th scope="col">{label}</th><th scope="col" className="g-num">Value</th>{additive && <th scope="col" className="g-num">Share</th>}{banded && <th scope="col">Band</th>}</tr></thead>
      <tbody>{rows.map(r => { const s = rowStatus(spec, r.value, person); return <tr key={r.key}>
        <th scope="row">{r.detail ? <button type="button" className="g-link" onClick={() => open(r.detail)}>{r.label}</button> : r.label}</th>
        <td className="g-num">{formatValue(r.value, spec.unit)}</td>
        {additive && <td className="g-num g-muted">{total && r.value !== null ? `${Math.round(r.value / total * 100)}%` : '—'}</td>}
        {banded && <td><StatusPill status={s} /></td>}
      </tr>; })}</tbody>
      {additive && <tfoot><tr><th scope="row">{hidden > 0 ? `All ${result.breakdown.length} groups` : 'Total'}</th><td className="g-num">{formatValue(total, spec.unit)}</td><td className="g-num g-muted">100%</td>{banded && <td />}</tr></tfoot>}
    </table></div>}
    {person && <p className="g-foot-note">Grouped by person for context only, in alphabetical order; this is not a rating of anyone.</p>}
  </Card>;
}

// ---------------------------------------------------------------------------------------------
// indexBreakdown
// ---------------------------------------------------------------------------------------------
export function componentsOf(lineage: LineageNode): LineageNode[] {
  if (lineage.children.some(c => c.kind === 'component')) return lineage.children.filter(c => c.kind === 'component');
  for (const child of lineage.children) { const found = componentsOf(child); if (found.length) return found; }
  return [];
}
export function IndexBreakdown(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  const previous = useComparedResult(id, result ? comparedMorning(result, props.compare) : null);
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const components = componentsOf(result.lineage);
  const before = previous ? componentsOf(previous.lineage) : [];
  const total = components.reduce((a, c) => a + (c.weight ?? 0), 0) || 1;
  const knownWeight = components.filter(c => c.normalized != null).reduce((a, c) => a + (c.weight ?? 0), 0);
  return <Card widget={props.widget}>
    <MetricHeader title={title} status={result.status} spec={spec} onOpen={() => props.onOpen(id)} eyebrow="Index components" />
    <div className="g-inline-value"><Value value={result.value} unit={spec.unit} size="md" />{result.value !== null && <><DeltaBadge current={result.value} previous={result.previous[props.compare]} unit={spec.unit} direction={spec.direction} label={compareLabel(props.compare)} /><DeltaBadge current={result.value} previous={result.previous[props.compare === 'day' ? 'week' : 'day']} unit={spec.unit} direction={spec.direction} label={compareLabel(props.compare === 'day' ? 'week' : 'day')} quiet /></>}</div>
    {result.value === null && <UnknownState result={result} />}
    {!components.length ? <p className="g-empty">This metric has no index components.</p> : <div className="g-comps" role="list" aria-label={`${title} components`}>
      <div className="g-comp g-comp-head" aria-hidden><span>Component</span><span>Score, 0–100{previous ? ` · ghost = ${props.compare === 'day' ? 'yesterday' : 'last week'}` : ''}</span><span>Score</span><span>Raw</span></div>
      {components.map(c => <div role="listitem" key={c.id}><ComponentRow node={c} previous={before.find(b => b.id === c.id || b.label === c.label)} totalWeight={total} onOpen={() => props.onOpen(id, c.id)} /></div>)}
    </div>}
    <p className="g-foot-note">{previous ? `Dark tick: ${props.compare === 'day' ? 'yesterday' : 'last week'}. ` : ''}Coverage {Math.round(knownWeight / total * 100)}% of weight · {result.coverage.records} records{result.coverage.unknownComponents.length ? ` · unknown: ${result.coverage.unknownComponents.join(', ')}` : ''}. Measures how the area works, not any person.</p>
  </Card>;
}

// ---------------------------------------------------------------------------------------------
// exceptionList
// ---------------------------------------------------------------------------------------------
export function ExceptionList(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  const { onDetail } = usePrimitiveRuntime();
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const limit = props.widget.options.limit ?? 5;
  const items = result.highlight;
  const omit = constantFields(items);
  const openRecord = (r: RecordRef) => r.detail && onDetail ? onDetail(r.detail) : props.onOpen(id, 'highlight');
  return <Card widget={props.widget} className={items.length ? 'g-has-exceptions' : ''}>
    <MetricHeader title={props.widget.title ?? spec.highlight?.label ?? title} spec={spec} onOpen={() => props.onOpen(id)} eyebrow="Needs attention" aside={<span className={`g-count${items.length ? ' is-active' : ''}`}>{items.length}</span>} />
    {items.length > 1 && sharedPhrases(items).length > 0 && <p className="g-shared">All {items.length}: {sharedPhrases(items).join(' · ')}</p>}
    {!items.length ? <p className="g-calm"><CheckCircle2 size={17} aria-hidden />Nothing needs your attention here this morning.</p> : <div className="g-records">{items.slice(0, limit).map(r => <RecordRow key={r.id} record={r} onOpen={openRecord} tone={result.status === 'bad' ? 'bad' : 'warn'} omit={omit} />)}</div>}
    {items.length > limit && <button type="button" className="g-text-button" onClick={() => props.onOpen(id, 'highlight')}>All {items.length} records <ChevronRight size={14} /></button>}
  </Card>;
}

// ---------------------------------------------------------------------------------------------
// calendarHeatmap: daily values laid out Monday→Sunday by week, one sequential hue.
// ---------------------------------------------------------------------------------------------
const RAMP = ['#eef3e6', '#d6e6bd', '#b4d18a', '#8db55a', '#62873a'];
export function CalendarHeatmap(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  const [hover, setHover] = useState<{ date: string; value: number | null } | null>(null);
  const cells = useMemo(() => {
    if (!result) return [];
    const points = seriesFor(result, props.widget.options.seriesDays ?? 56);
    return points.map(p => { const d = new Date(`${p.date}T12:00:00Z`); return { ...p, dow: (d.getUTCDay() + 6) % 7, time: d.getTime() }; });
  }, [result, props.widget.options.seriesDays]);
  if (!spec || !result) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const known = cells.filter(c => c.value !== null).map(c => c.value!) as number[];
  const min = Math.min(...known), max = Math.max(...known);
  const step = (v: number) => max === min ? 2 : Math.min(RAMP.length - 1, Math.floor((v - min) / (max - min) * RAMP.length));
  const firstMonday = cells.length ? cells[0].time - cells[0].dow * 86_400_000 : 0;
  const weeks = cells.length ? Math.floor((cells.at(-1)!.time - firstMonday) / (7 * 86_400_000)) + 1 : 0;
  const size = 15, gap = 3, left = 26, top = 4;
  const width = left + weeks * (size + gap), height = top + 7 * (size + gap);
  const peak = cells.reduce<typeof cells[number] | null>((m, c) => c.value !== null && (!m || c.value > (m.value ?? -Infinity)) ? c : m, null);
  return <Card widget={props.widget}>
    <MetricHeader title={title} status={result.status} spec={spec} onOpen={() => props.onOpen(id)} eyebrow={result.coverage.historyAvailable ? `Daily · last ${cells.length} ${cells.length === 1 ? 'day' : 'days'}` : 'Daily'} />
    {!result.coverage.historyAvailable ? <NoHistoryNote result={result} /> : !known.length ? <p className="g-empty">No daily values yet.</p> : <>
      <div className="g-heat" role="img" aria-label={`${title}, daily for ${cells.length} days: from ${formatValue(min, spec.unit)} to ${formatValue(max, spec.unit)}; highest ${peak ? `${formatValue(peak.value, spec.unit)} on ${shortDay(peak.date)}` : 'unknown'}.`}>
        <svg viewBox={`0 0 ${width} ${height}`} style={{ maxWidth: width * 1.6 }} onMouseLeave={() => setHover(null)}>
          {['Mon', '', 'Wed', '', 'Fri', '', 'Sun'].map((d, i) => d && <text key={d} x={0} y={top + i * (size + gap) + size - 4} className="g-heat-day">{d}</text>)}
          {cells.map(c => {
            const week = Math.floor((c.time - firstMonday) / (7 * 86_400_000));
            const today = c.date === result.asOf;
            return <rect key={c.date} x={left + week * (size + gap)} y={top + c.dow * (size + gap)} width={size} height={size} rx={3} className={`g-heat-cell${c.value === null ? ' is-unknown' : ''}${today ? ' is-today' : ''}${hover?.date === c.date ? ' is-hover' : ''}`} fill={c.value === null ? '#fff' : RAMP[step(c.value)]} onMouseEnter={() => setHover(c)} />;
          })}
        </svg>
      </div>
      <div className="g-heat-foot">
        <span className="g-heat-legend" aria-hidden>{spec.direction === 'lower' ? 'Fewer' : 'Less'}{RAMP.map(c => <i key={c} style={{ background: c }} />)}{spec.direction === 'lower' ? 'More' : 'More'}</span>
        <span className="g-heat-caption" aria-live="polite">{hover ? `${weekday(hover.date)} · ${hover.value === null ? 'unknown' : formatValue(hover.value, spec.unit)}` : `${spec.direction === 'lower' ? 'Lower is better' : spec.direction === 'higher' ? 'Higher is better' : 'Context measure'} · today outlined`}</span>
      </div>
      <table className="sr-only"><caption>{title} by day</caption><tbody>{cells.map(c => <tr key={c.date}><th scope="row">{longDay(c.date)}</th><td>{c.value === null ? 'Unknown' : formatValue(c.value, spec.unit)}</td></tr>)}</tbody></table>
    </>}
  </Card>;
}

// ---------------------------------------------------------------------------------------------
// comparison: two to four related metrics on one card.
// ---------------------------------------------------------------------------------------------
export function Comparison(props: PrimitiveProps) {
  const rows = props.widget.metricIds.map((_, i) => resolve(props, i));
  const present = rows.filter(r => r.spec && r.result) as { id: string; spec: MetricSpec; result: MetricResult; title: string }[];
  const title = props.widget.title ?? present.map(r => r.spec.title).join(' vs ');
  if (present.length < 2) return <Missing widget={props.widget} title={title || 'Comparison'} reason="A comparison needs at least two metrics with results." />;
  const sameUnit = present.every(r => r.spec.unit === present[0].spec.unit);
  const max = Math.max(...present.map(r => Math.abs(r.result.value ?? 0)), 1);
  const [a, b] = present;
  const ratio = sameUnit && present.length === 2 && a.result.value && b.result.value !== null && (a.spec.unit === 'count' || a.spec.unit === 'usd') ? `${b.spec.title}: ${formatValue(b.result.value, b.spec.unit)} of ${formatValue(a.result.value, a.spec.unit)} (${Math.round(b.result.value! / a.result.value * 100)}%).` : null;
  return <Card widget={props.widget}>
    <MetricHeader title={title} eyebrow="Side by side" />
    <ul className="g-compare">{present.map(r => <li key={r.id}>
      <button type="button" className="g-compare-row" onClick={() => props.onOpen(r.id)}>
        <span className="g-compare-label"><strong>{r.spec.title}</strong><StatusPill status={r.result.status} showQuiet={false} /></span>
        {sameUnit && <span className="g-bar-track"><i className="g-bar-fill g-bar-neutral" style={{ width: `${Math.abs(r.result.value ?? 0) / max * 100}%` }} /></span>}
        <span className="g-compare-value"><Value value={r.result.value} unit={r.spec.unit} size="sm" /><DeltaBadge current={r.result.value} previous={r.result.previous[props.compare]} unit={r.spec.unit} direction={r.spec.direction} quiet /></span>
      </button>
    </li>)}</ul>
    {ratio && <p className="g-foot-note">{ratio}</p>}
  </Card>;
}

// ---------------------------------------------------------------------------------------------
// note: deterministic text only.
// ---------------------------------------------------------------------------------------------
export function Note(props: PrimitiveProps) {
  const { id, spec, result, title } = resolve(props);
  const { onDetail } = usePrimitiveRuntime();
  if (!spec) return <Missing widget={props.widget} title={title} reason="This metric is not available on this morning." />;
  const kind = props.widget.options.noteKind ?? 'definition';
  return <Card widget={props.widget} className="g-note">
    <MetricHeader title={title} eyebrow={kind === 'changes' ? 'What changed' : 'Definition'} onOpen={() => props.onOpen(id)} />
    {kind === 'definition' ? <div className="g-note-body"><p className="g-question">{spec.businessQuestion}</p><p>{spec.plainDefinition}</p></div>
      : result ? <ChangeList changes={result.changes} onOpen={r => r.detail && onDetail ? onDetail(r.detail) : props.onOpen(id)} /> : <p className="g-empty">No result for this morning.</p>}
  </Card>;
}

export const primitiveRegistry: Record<PrimitiveId, (props: PrimitiveProps) => ReactNode> = {
  kpi: KpiTile, kpiTarget: KpiTargetTile, trend: Trend, barBreakdown: BarBreakdown, indexBreakdown: IndexBreakdown, scoreGauge: ScoreGauge,
  exceptionList: ExceptionList, table: BreakdownTable, calendarHeatmap: CalendarHeatmap, comparison: Comparison, note: Note,
};
/** Renders a widget through the registry; an unknown primitive id becomes a neutral tile with drilldown. */
export function Widget(props: PrimitiveProps) {
  const Component = primitiveRegistry[props.widget.primitive];
  if (!Component) return <Missing widget={props.widget} title={props.widget.title ?? 'Widget'} reason={`Unknown primitive “${props.widget.primitive}”.`} onOpen={props.widget.metricIds[0] ? () => props.onOpen(props.widget.metricIds[0]) : undefined} />;
  return <Component {...props} />;
}
