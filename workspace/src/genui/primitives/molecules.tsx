// Molecules: atoms combined into the recurring parts of a metric card.
import type { ReactNode } from 'react';
import { ArrowUpRight, ChevronRight, Minus, Plus } from 'lucide-react';
import type { LineageNode, MetricResult, MetricSpec, MetricStatus, RecordRef } from '../contracts';
import { DeltaBadge, InfoTip, MiniBar, StatusPill, Value } from './atoms';
import { compareLabel, formatDiff, formatNodeValue, formatValue, plainFormula, plainLabel, recordPhrases, shortDay, STATUS_LABEL, weightShare, type Unit } from './format';

export function MetricHeader({ title, status, onOpen, spec, eyebrow, aside }: { title: string; status?: MetricStatus; onOpen?: () => void; spec?: MetricSpec; eyebrow?: string; aside?: ReactNode }) {
  return <header className="g-head">
    <div className="g-head-copy">
      {eyebrow && <span className="g-eyebrow">{eyebrow}</span>}
      <h3 className="g-head-title">{onOpen ? <button type="button" className="g-head-open" onClick={onOpen}>{title}<ArrowUpRight size={14} aria-hidden /></button> : title}</h3>
    </div>
    <div className="g-head-aside">
      {aside}
      {status && <StatusPill status={status} showQuiet={false} />}
      {spec && <InfoTip label={title}><strong>{spec.businessQuestion}</strong>{spec.plainDefinition}</InfoTip>}
    </div>
  </header>;
}

/** Headline value with the selected comparison and the other one in smaller type. */
export function ValueBlock({ spec, result, compare, size = 'lg' }: { spec: MetricSpec; result: MetricResult; compare: 'day' | 'week'; size?: 'xl' | 'lg' | 'md' }) {
  const other = compare === 'day' ? 'week' : 'day';
  return <div className="g-valueblock">
    <Value value={result.value} unit={spec.unit} size={size} />
    {result.value !== null && (result.previous.day === null && result.previous.week === null
      ? <span className="g-delta g-tone-unknown g-delta-quiet">{result.coverage.historyAvailable ? 'No earlier value to compare' : 'Snapshot · no earlier values'}</span>
      : <div className="g-valueblock-deltas">
        <DeltaBadge current={result.value} previous={result.previous[compare]} unit={spec.unit} direction={spec.direction} label={compareLabel(compare)} />
        <DeltaBadge current={result.value} previous={result.previous[other]} unit={spec.unit} direction={spec.direction} label={compareLabel(other)} quiet />
      </div>)}
  </div>;
}

/** Few's bullet graph: qualitative bands in greys, the value as a bar, the target as a tick. */
export function TargetBar({ value, target, bands, direction, unit, status, previous }: { value: number | null; target: number | null; bands: MetricSpec['bands']; direction: MetricSpec['direction']; unit: Unit; status: MetricStatus; previous?: number | null }) {
  const candidates = [value, target, bands?.good, bands?.warn, previous].filter((v): v is number => typeof v === 'number');
  const max = unit === 'percent' || unit === 'score' ? Math.max(100, ...candidates) : Math.max(1, ...candidates) * 1.15;
  const pct = (v: number) => `${Math.max(0, Math.min(100, v / max * 100))}%`;
  const zones = bands ? (direction === 'lower'
    ? [{ from: 0, to: bands.good, k: 'good' }, { from: bands.good, to: bands.warn, k: 'warn' }, { from: bands.warn, to: max, k: 'bad' }]
    : [{ from: 0, to: bands.warn, k: 'bad' }, { from: bands.warn, to: bands.good, k: 'warn' }, { from: bands.good, to: max, k: 'good' }]) : [];
  const description = [value === null ? 'Value unknown' : `Value ${formatValue(value, unit)}`, target !== null ? `target ${formatValue(target, unit)}` : null, bands ? `${STATUS_LABEL.warn.toLowerCase()} band ${formatValue(bands.warn, unit)}–${formatValue(bands.good, unit)}` : null].filter(Boolean).join(', ');
  return <div className="g-bullet" role="img" aria-label={description}>
    <div className="g-bullet-track">
      {zones.map(z => <i key={z.k} className={`g-bullet-zone g-bullet-zone-${z.k}`} style={{ left: pct(z.from), width: `calc(${pct(z.to)} - ${pct(z.from)})` }} />)}
      {value !== null && <i className={`g-bullet-bar g-bullet-bar-${status}`} style={{ width: pct(value) }} />}
      {previous != null && <i className="g-bullet-previous" style={{ left: pct(previous) }} />}
      {target !== null && <i className="g-bullet-target" style={{ left: pct(target) }} />}
    </div>
    <div className="g-bullet-scale"><span>0</span>{target !== null && <span style={{ left: `min(${pct(target)}, calc(100% - 46px))` }}>Target {formatValue(target, unit)}</span>}</div>
  </div>;
}

/** Key facts of a record as plain phrases; `omit` drops fields every record of the list shares. */
export function recordFacts(ref: RecordRef, limit = 3, omit: Set<string> = new Set()): string[] {
  return recordPhrases(ref.fields, omit, limit, ref.dataset);
}

export function RecordRow({ record, onOpen, tone, omit }: { record: RecordRef; onOpen?: (r: RecordRef) => void; tone?: 'bad' | 'warn' | 'neutral'; omit?: Set<string> }) {
  const facts = recordFacts(record, 3, omit);
  const body = <><span className={`g-record-mark g-tone-${tone ?? 'neutral'}`} aria-hidden /><span className="g-record-copy"><strong>{plainLabel(record.label)}</strong>{facts.length > 0 && <small>{facts.join(' · ')}</small>}</span></>;
  return onOpen && record.detail ? <button type="button" className="g-record" onClick={() => onOpen(record)}>{body}<ChevronRight size={16} aria-hidden /></button> : <div className="g-record">{body}</div>;
}

export function ChangeList({ changes, onOpen, limit = 6 }: { changes: MetricResult['changes']; onOpen?: (r: RecordRef) => void; limit?: number }) {
  const rows = [...changes.added.map(r => ({ r, added: true })), ...changes.removed.map(r => ({ r, added: false }))];
  const addedTotal = changes.addedTotal ?? changes.added.length, removedTotal = changes.removedTotal ?? changes.removed.length;
  const hidden = addedTotal + removedTotal - Math.min(rows.length, limit);
  if (!addedTotal && !removedTotal) return <p className="g-empty">No records entered or left since {shortDay(changes.comparedTo)}.</p>;
  return <div className="g-changes">
    <p className="g-changes-summary">Since {shortDay(changes.comparedTo)}: <strong>{addedTotal} new</strong>, <strong>{removedTotal} left</strong></p>
    <ul>{rows.slice(0, limit).map(({ r, added }) => <li key={`${added}-${r.id}`}>
      <span className={`g-change-sign ${added ? 'is-added' : 'is-removed'}`} aria-label={added ? 'New' : 'Left'}>{added ? <Plus size={12} /> : <Minus size={12} />}</span>
      {onOpen && r.detail ? <button type="button" className="g-link" onClick={() => onOpen(r)}>{plainLabel(r.label)}</button> : <span>{plainLabel(r.label)}</span>}
    </li>)}</ul>
    {hidden > 0 && <p className="g-more">+{hidden} more in the full breakdown</p>}
  </div>;
}

/** A formula line; give it a breakdown step (`node`) and its numbers read as money, shares and dates
 * (format.ts plainFormula). */
export function FormulaLine({ text, node, label = 'Formula' }: { text?: string | null; node?: Parameters<typeof plainFormula>[0]; label?: string }) {
  const shown = node ? plainFormula(node) : text;
  if (!shown) return null;
  return <p className="g-formula"><span>{label}</span><code>{shown}</code></p>;
}

/** One weighted component of an index: share of weight, 0–100 position, raw value, movement. */
export function ComponentRow({ node, previous, totalWeight, onOpen }: { node: LineageNode; previous?: LineageNode | null; totalWeight: number; onOpen?: () => void }) {
  const normalized = node.normalized ?? null;
  const before = previous?.normalized ?? null;
  const share = node.weight ? weightShare(node.weight, totalWeight) : null;
  const tone = normalized === null ? 'muted' : normalized < 40 ? 'bad' : normalized < 60 ? 'warn' : 'neutral';
  const move = normalized !== null && before !== null ? normalized - before : null;
  const raw = node.value === null ? null : formatNodeValue(node.value, node.unit);
  const content = <>
    <span className="g-comp-label"><strong>{node.label}</strong><small>{share !== null ? `${share} of weight` : 'weight not set'}{node.sample !== undefined ? ` · ${node.sample} records` : ''}</small></span>
    <span className="g-comp-bar"><MiniBar value={normalized} previous={before} tone={tone} label={node.label} /></span>
    <span className="g-comp-score">{normalized === null ? <em>Unknown</em> : <strong>{Math.round(normalized)}</strong>}{move !== null && Math.round(move) !== 0 && <small className={`g-tone-${move > 0 ? 'good' : 'bad'}`}>{formatDiff(Math.round(move), 'count')}</small>}</span>
    <span className="g-comp-raw">{normalized === null ? (node.unknownReason ?? 'Not enough records') : raw ? `raw ${raw}` : ''}</span>
  </>;
  return onOpen ? <button type="button" className="g-comp" onClick={onOpen}>{content}<ChevronRight size={15} className="g-comp-chevron" aria-hidden /></button> : <div className="g-comp">{content}</div>;
}
