// Browser client of /api/kb/*. Errors carry the server message; there is no mock fallback: a missing knowledge
// base must show as missing, never as sample data.
import { useEffect, useState } from 'react';
import type { KbAskAnswer, KbBrief, KbDates, KbDossier, KbMetricResult, KbOrganization, KbSourceStatus, KbUnit } from './types';
import { apiUrl } from '../appUrl';
import { earlyFetch } from '../early';
import { gbDate } from '../genui/primitives/format';

// The simulated future the workspace reads (URL parameter branch; the shell sets it): every call carries it, so the
// pages read the branch tenant without knowing about it.
let branch: { tenant: string; label: string } | null = null;
export function setKbBranch(b: { tenant: string; label: string } | null): void { branch = b ? { tenant: b.tenant, label: b.label } : null; }
export const kbBranch = () => branch;
const withBranch = (path: string) => branch ? `${path}${path.includes('?') ? '&' : '?'}branch=${encodeURIComponent(branch.tenant)}` : path;

/** The URL of a knowledge-base API path, with the branch the workspace reads. */
export const kbUrl = (path: string) => apiUrl(`/api/kb${withBranch(path)}`);
async function call<T>(path: string, init?: RequestInit): Promise<T> {
  // the first view starts the dates and the state of the sources before this code runs (src/firstView.ts)
  const res = await earlyFetch(kbUrl(path), init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.error || `Knowledge base request failed (${res.status})`);
  return body as T;
}
const q = (p: Record<string, string | undefined>) => { const s = new URLSearchParams(); Object.entries(p).forEach(([k, v]) => { if (v) s.set(k, v); }); return s.size ? `?${s}` : ''; };

/** The paths of the shell's reads on every page (src/firstView.ts and server/preload.ts start them early). */
export const datesPath = (org: string) => `/${org}/dates`;
export const statusPath = (org: string, asOf?: string) => `/${org}/status${q({ asOf })}`;

// The MVP has one user role (the CEO): requests carry no role, the server uses the only one.
export const kbApi = {
  brief: (org: string, asOf?: string) => call<KbBrief>(`/${org}/brief${q({ asOf })}`),
  metric: (org: string, id: string, asOf?: string) => call<KbMetricResult>(`/${org}/metrics/${encodeURIComponent(id)}${q({ asOf })}`),
  entity: (org: string, id: string, asOf?: string) => call<KbDossier>(`/${org}/entity/${encodeURIComponent(id)}${q({ asOf })}`),
  status: (org: string, asOf?: string) => call<KbSourceStatus>(statusPath(org, asOf)),
  dates: (org: string) => call<KbDates>(datesPath(org)),
  ask: (org: string, question: string, asOf?: string) => call<KbAskAnswer>(`/${org}/ask`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question, asOf }) }),
  search: (org: string, text: string) => call<{ hits: { id: string; kind: string; title: string; snippet: string }[] }>(`/${org}/search${q({ q: text })}`),
};

export function useKb<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: string; loading: boolean; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: string; loading: boolean }>({ data: null, error: '', loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setState(s => ({ ...s, loading: true, error: '' }));
    load().then(data => { if (live) setState({ data, error: '', loading: false }); }, (e: Error) => { if (live) setState({ data: null, error: e.message, loading: false }); });
    return () => { live = false; };
  }, [...deps, tick]); // eslint-disable-line react-hooks/exhaustive-deps
  return { ...state, reload: () => setTick(t => t + 1) };
}

// Amounts and dates follow the company on screen: its currency (money metrics, document amounts) and time zone.
let current = { currency: 'USD', timezone: 'UTC' };
export function setKbFormat(o: Pick<KbOrganization, 'currency' | 'timezone'>): void {
  current = { currency: (o.currency ?? 'USD').toUpperCase(), timezone: o.timezone || 'UTC' };
}
export const isMoney = (unit: KbUnit) => unit === 'money';
const SYMBOL: Record<string, string> = { USD: '$', EUR: '€', GBP: '£' };
function money(value: number, currency: string): string {
  const abs = Math.abs(value), sign = value < 0 ? '−' : '';
  const body = abs >= 1_000_000 ? `${(abs / 1_000_000).toFixed(abs >= 10_000_000 ? 1 : 2)}M` : Math.round(abs).toLocaleString('en-US');
  return SYMBOL[currency] ? `${sign}${SYMBOL[currency]}${body}` : `${sign}${body} ${currency}`;
}
/** A value as people read it: in the unit's form, with a true minus sign (the headline uses the same numbers). */
export function formatValue(value: number | null | undefined, unit: KbUnit): string {
  return plainValue(value, unit).replace(/^-/, '−');
}
function plainValue(value: number | null | undefined, unit: KbUnit): string {
  if (value === null || value === undefined) return '—';
  if (unit === 'money') return money(value, current.currency);
  if (unit === 'hours') return `${value.toLocaleString('en-US', { maximumFractionDigits: 1 })} h`;
  if (unit === 'days') return `${value.toLocaleString('en-US', { maximumFractionDigits: 1 })} d`;
  if (unit === 'months') return `${value.toLocaleString('en-US', { maximumFractionDigits: 1 })} mo`;
  if (unit === 'ms') return `${value.toLocaleString('en-US', { maximumFractionDigits: 0 })} ms`;
  // 99.991% is not 100%: a share close to 100 keeps its decimals
  if (unit === 'percent') return `${value.toLocaleString('en-US', { maximumFractionDigits: value >= 99 && value < 100 ? 3 : 1 })}%`;
  if (unit === 'score') return `${Math.round(value)} / 100`;
  if (unit === 'index') return Math.round(value).toLocaleString('en-US'); // its own scale (NPS: −100 to 100)
  return value.toLocaleString('en-US');
}
export function formatFact(key: string, value: string | number | null): string {
  if (value === null || value === '') return '—';
  if (typeof value === 'number' && /amount/.test(key)) return formatValue(value, 'money');
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(value)) return formatDate(value, true);
  return String(value);
}
export function formatDate(iso: string | null | undefined, time = false): string {
  if (!iso) return '—';
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return gbDate(d, { timeZone: current.timezone, day: 'numeric', month: 'short', ...(time && iso.length > 10 ? { hour: '2-digit', minute: '2-digit' } : {}) }, true);
}
/** A day with its year, in the company's time zone: «27 Oct 2026». */
export function formatDay(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00Z` : iso);
  return Number.isNaN(d.getTime()) ? '' : gbDate(d, { timeZone: current.timezone, day: 'numeric', month: 'short', year: 'numeric' });
}
export const KIND_LABEL: Record<string, string> = {
  Email: 'Mail', Task: 'Jira task', Bug: 'Jira bug', Epic: 'Jira epic', Story: 'Jira story', MergeRequest: 'Merge request', Meeting: 'Meeting notes',
  Decision: 'Decision', ActionItem: 'Action item', Invoice: 'Invoice', Act: 'Act', Contract: 'Contract', Amendment: 'Amendment',
  CommercialProposal: 'Proposal', Deal: 'Deal', Claim: 'Claim', SupportTicket: 'Support ticket', Incident: 'Incident', Deploy: 'Deploy',
  BankEntry: 'Bank entry', Payment: 'Payment', NpsResponse: 'NPS answer', LifeEvent: 'Event', Payroll: 'Payroll', Requisition: 'Requisition',
  EmployeeEvent: 'Employee event', CloudBill: 'Cloud bill', TaxPayment: 'Tax payment', Process: 'Process', Policy: 'Policy', Letter: 'Letter', Document: 'Document', Report: 'Report', Person: 'Person', Organization: 'Organization', KBArticle: 'Wiki',
};
