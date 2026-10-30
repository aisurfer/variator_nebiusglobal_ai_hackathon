// What the company configures for the workspace: currency of its amounts, the engineering department, the North Star.
// Defaults keep an unconfigured company working. Texts and word lists are English only.
import type { KbNorthStar, KbNorthStarMeasure, KbOrganization } from '../../src/kb/types.js';

export const currencyOf = (o: KbOrganization): string => (o.currency ?? 'USD').toUpperCase();
export const engineeringOf = (o: KbOrganization): string => o.engineeringDepartment ?? 'Engineering';
const SYMBOL: Record<string, string> = { USD: '$', EUR: '€', GBP: '£' };
export const currencySymbol = (o: KbOrganization): string => SYMBOL[currencyOf(o)] ?? currencyOf(o);

/** An amount for headlines: «$1.44M» / «$12,500», or «12,500 CHF» for a currency without a symbol. */
export function formatMoney(value: number | null | undefined, o: KbOrganization): string {
  const v = value ?? 0, cur = currencyOf(o);
  const sym = SYMBOL[cur], body = v >= 1_000_000 ? `${(v / 1_000_000).toFixed(2)}M` : Math.round(v).toLocaleString('en-US');
  return sym ? `${sym}${body}` : `${body} ${cur}`;
}

// ------------------------------------------------------------------------------------------------ word lists
export const ESCALATION = /\burgent|\basap\b|complain|\bclaim|\bdemand|penalt|terminat|dissatisf|unacceptable|apolog|escalat|\bdisput|\bbreach|\blegal action/i;
export const SENIOR = /\bceo\b|\bcfo\b|\bcoo\b|\bcto\b|\bfounder|\bco-founder|\bowner\b|\bpresident|\bchief\b[\w\s]*\bofficer|\bdirector\b|\bgeneral manager|\bvp\b|\bvice president/i;
export const PRICE_TOPIC = /discount|\bpric(?:e|es|ing)\b|\btariff|\brebate|\brate card/i;
/** Reply and forward prefixes of a subject: Re:, Fwd:, FW:, AW:, WG:. */
export const REPLY_PREFIX = /^\s*((re|fwd?|aw|wg)\s*:\s*)+/i;

// ------------------------------------------------------------------------------------------------ north star
export interface NorthStarConfig {
  title: string; metricTitle: string; reportLabel: string; property: string; period: string | null; unit: string; field: string;
  target: number | null; by: string | null; secondary: Required<KbNorthStarMeasure>[];
  question: string; definition: string; howCalculated: string[]; headline: string;
}
const lowerFirst = (s: string) => (s && !/^[A-Z]{2}/.test(s) ? s[0].toLowerCase() + s.slice(1) : s);
/** «MRR, $k» -> «mrrK», «Paying customers» -> «payingCustomers»: a stable field name for a report value. */
export function fieldName(label: string): string {
  const words = label.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  return words.map((w, i) => (i ? w[0].toUpperCase() + w.slice(1) : w)).join('') || 'value';
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
/** «Dec 31, 2026», or «Dec 31» without the year. */
export function dateText(iso: string, year = true): string {
  const [y, m, d] = iso.split('-');
  return `${MONTHS[Number(m) - 1]} ${Number(d)}${year ? `, ${y}` : ''}`;
}
const LONG_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const longDate = (iso: string) => { const [y, m, d] = iso.split('-'); return `${Number(d)} ${LONG_MONTHS[Number(m) - 1]} ${y}`; };

const nsMemo = new WeakMap<KbOrganization, NorthStarConfig | null>();
/** The configured North Star with every text resolved, or null when the company has none. */
export function northStarOf(o: KbOrganization): NorthStarConfig | null {
  if (nsMemo.has(o)) return nsMemo.get(o)!;
  const ns: KbNorthStar | null | undefined = o.northStar;
  let out: NorthStarConfig | null = null;
  if (ns && ns.property) {
    const unit = ns.unit ?? lowerFirst(ns.property);
    const title = ns.title || ns.property;
    const goal = ns.target !== null && ns.target !== undefined && ns.by ? `${ns.target} by ${dateText(ns.by)}` : null;
    const secondary = (ns.secondary ?? []).map(x => (typeof x === 'string' ? { property: x, label: x } : x))
      .map(x => ({ property: x.property, label: x.label || x.property, field: x.field ?? fieldName(x.label || x.property), summary: x.summary ?? true }));
    out = {
      title, metricTitle: `North Star · ${lowerFirst(title)}`, reportLabel: ns.reportLabel ?? '', property: ns.property, period: ns.period ?? null, unit,
      field: ns.field ?? 'northStar', target: ns.target ?? null, by: ns.by ?? null, secondary,
      question: ns.question ?? `Is the number of ${lowerFirst(title)} growing every week?`,
      definition: ns.definition ?? `${title}, as published in the weekly report (property «${ns.property}»).${ns.target != null && ns.by ? ` The company goal is ${ns.target} ${unit} by ${longDate(ns.by)}.` : ''}`,
      howCalculated: ns.howCalculated ?? [
        `The latest weekly report published by the date${ns.reportLabel ? ` (pages labelled «${ns.reportLabel}»)` : ''}.`,
        `Its «${ns.property}» value.`,
        ...(goal ? [`On track when the average weekly gain over the last four reports is at least the gain still needed each week to reach ${goal}; watch at half of it.`] : []),
      ],
      headline: ns.headline ?? `{value} ${lowerFirst(title)}`,
    };
  }
  nsMemo.set(o, out);
  return out;
}

// ------------------------------------------------------------------------------------------------ loading
/** The company card a brief carries: identity and roles, not the workspace configuration. */
export function publicOrg(o: KbOrganization): KbOrganization {
  const { id, tenant, name, sector, headcount, timezone, ownDomain, roles } = o;
  return { id, tenant, name, sector, headcount, timezone, ownDomain, roles };
}
/** The company file holds one company; a list (of the company and, later, its scenario branches) loads as well. */
export function normalizeOrganizations(raw: unknown): KbOrganization[] {
  const list = Array.isArray(raw) ? raw : raw && typeof raw === 'object' && Array.isArray((raw as { organizations?: unknown }).organizations)
    ? (raw as { organizations: unknown[] }).organizations : raw && typeof raw === 'object' ? [raw] : [];
  return list.filter((x): x is KbOrganization => !!x && typeof x === 'object' && typeof (x as KbOrganization).id === 'string' && Array.isArray((x as KbOrganization).roles))
    .map(x => ({ ...x, tenant: x.tenant ?? x.id }));
}
