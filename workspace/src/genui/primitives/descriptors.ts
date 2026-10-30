// Primitive catalog as data: what each primitive shows, what it requires and which sizes it takes. A catalog lists it
// for whoever chooses widgets; the renderer (organisms.tsx) maps each id to a component.
import type { PrimitiveDescriptor } from '../contracts';

export const PRIMITIVE_DESCRIPTORS: PrimitiveDescriptor[] = [
  { id: 'kpi', level: 'molecule', label: 'KPI tile', purpose: 'One headline number with status, change since the comparison date and a small sparkline. Default for most metrics in the top "Today" section.', requires: [], sizes: ['sm', 'md'] },
  { id: 'kpiTarget', level: 'molecule', label: 'Progress to target', purpose: 'A value against an explicit target (bullet bar). Use only when the spec has a target.', requires: ['target'], sizes: ['sm', 'md', 'lg'] },
  { id: 'trend', level: 'organism', label: 'Trend', purpose: 'How the metric moved day by day or week by week; optional target line. Use when the change over time is the point.', requires: ['series'], sizes: ['md', 'lg', 'xl'] },
  { id: 'barBreakdown', level: 'organism', label: 'Breakdown bars', purpose: 'Compare the metric across projects, teams, departments or other dimensions. Requires a breakdown in the spec.', requires: ['breakdown'], sizes: ['md', 'lg'] },
  { id: 'indexBreakdown', level: 'organism', label: 'Index components', purpose: 'Show which weighted components drive a composite score and how each moved. Only for metrics whose formula is an index.', requires: ['components'], sizes: ['md', 'lg', 'xl'] },
  { id: 'scoreGauge', level: 'molecule', label: 'Score gauge', purpose: 'A 0–100 score with good/attention/risk bands. For composite indexes and scores only.', requires: ['components'], sizes: ['sm', 'md'] },
  { id: 'exceptionList', level: 'organism', label: 'Needs attention', purpose: 'The concrete records the executive should look at today (overdue directives, surprises, blocked promises). Requires a highlight in the spec.', requires: ['highlight'], sizes: ['md', 'lg', 'xl'] },
  { id: 'table', level: 'organism', label: 'Breakdown table', purpose: 'Exact values per dimension with links; for executives who want the numbers rather than bars.', requires: ['breakdown'], sizes: ['lg', 'xl'] },
  { id: 'calendarHeatmap', level: 'organism', label: 'Daily heatmap', purpose: 'Daily values laid out by week to reveal rhythm and bad days. Daily-cadence metrics with history only.', requires: ['series'], sizes: ['lg', 'xl'] },
  { id: 'comparison', level: 'organism', label: 'Side-by-side', purpose: 'Two to four related metrics compared on one card (e.g. promised vs delivered). Requires several metric ids.', requires: ['multipleMetrics'], sizes: ['md', 'lg', 'xl'] },
  { id: 'note', level: 'molecule', label: 'Note', purpose: 'Short deterministic text: the plain definition of a metric or the list of what changed since the comparison date. Never free-form claims.', requires: [], sizes: ['sm', 'md', 'lg', 'xl'] },
];
