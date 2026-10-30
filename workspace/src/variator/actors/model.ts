// Pure functions of the actors page: which artifact set to read for a view date, the list (filter, search,
// sections) and the small drawing helpers. No React here, so the unit tests run under tsx.
import type { ActorCard, CardGroup, CardSet } from './types';

export const KIND = 'actor-cards';

/** The actor-card sets of a `GET /api/variator/artifacts` answer; other sets are left out. */
export function cardSets(body: unknown): CardSet[] {
  const sets = (body as { sets?: unknown[] } | null)?.sets;
  if (!Array.isArray(sets)) return [];
  return sets.flatMap(raw => {
    const s = raw as { id?: unknown; manifest?: Record<string, unknown> };
    const m = s.manifest;
    if (typeof s.id !== 'string' || !m || m.kind !== KIND || typeof m.asOf !== 'string') return [];
    return [{ id: s.id, asOf: m.asOf, t: String(m.t ?? ''), builtAt: String(m.builtAt ?? '') }];
  });
}

/** The set for a view date: learned from data up to that date at the latest (nothing after the view date may
 * count); the newest build wins. Without a view date: the newest set. */
export function pickCardSet(sets: CardSet[], viewDate: string | null | undefined): CardSet | null {
  const ok = viewDate ? sets.filter(s => s.asOf <= viewDate) : sets;
  return [...ok].sort((a, b) => b.asOf.localeCompare(a.asOf) || b.builtAt.localeCompare(a.builtAt))[0] ?? null;
}

export type Filter = 'all' | Exclude<CardGroup, 'company'>;
export const FILTERS: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'people', label: 'People' },
  { id: 'clients', label: 'Clients' },
  { id: 'vendors', label: 'Vendors' },
  { id: 'cohorts', label: 'Cohorts' },
];

export function isFilter(v: string | null | undefined): v is Filter {
  return FILTERS.some(f => f.id === v);
}

const norm = (s: string) => s.toLowerCase().normalize('NFKD');

/** Cards of a filter whose name, title or section contains the query (any order of words). */
export function filterCards(cards: ActorCard[], filter: Filter, query: string): ActorCard[] {
  const words = norm(query).split(/\s+/).filter(Boolean);
  return cards.filter(c => {
    if (filter !== 'all' && c.group !== filter) return false;
    if (!words.length) return true;
    const hay = norm(`${c.name} ${c.title} ${c.section} ${c.subtitle ?? ''}`);
    return words.every(w => hay.includes(w));
  });
}

export interface ListSection { key: string; label: string; group: CardGroup; cards: ActorCard[] }

/** The list in sections: the company, people by department (in the order of the cards), key clients, vendors,
 * then the cohorts by their section (client cohorts, authorities, future hires). */
export function listSections(cards: ActorCard[]): ListSection[] {
  const order: CardGroup[] = ['company', 'people', 'clients', 'vendors', 'cohorts'];
  const out: ListSection[] = [];
  for (const g of order) {
    for (const c of cards.filter(x => x.group === g)) {
      const key = `${g}:${c.section}`;
      let s = out.find(x => x.key === key);
      if (!s) { s = { key, label: c.section, group: g, cards: [] }; out.push(s); }
      s.cards.push(c);
    }
  }
  return out;
}

/** An SVG path of a small line: x evenly spaced, y scaled to the height (min at the bottom). */
export function sparkPath(values: number[], width: number, height: number, pad = 2): string {
  if (values.length < 2) return '';
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const step = (width - 2 * pad) / (values.length - 1);
  return values.map((v, i) => {
    const x = pad + i * step, y = height - pad - ((v - lo) / span) * (height - 2 * pad);
    return `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(' ');
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
/** 2026-10-13 -> 13 Oct 2026; 2026-10 -> Oct 2026. */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const [y, m, d] = iso.split('-');
  const mon = MONTHS[Number(m) - 1] ?? m;
  return d ? `${Number(d)} ${mon} ${y}` : `${mon} ${y}`;
}

// ------------------------------------------------------------------------------------------------ people's words
// The card texts arrive formatted by Python for machines as much as for people («1,022,400 USD», «group ·
// clients:key», «-1.33 – 55»). The page shows amounts in dollars, short («$1.02M», «$255,600»), with the exact value
// in a tooltip; groups in words; ranges with a true minus sign.

/** A dollar amount for people: «$12.0M» from ten million, «$1.02M» from one million, else whole dollars. */
export function dollars(n: number): string {
  const abs = Math.abs(n), sign = n < 0 ? '−' : '';
  return `${sign}$${abs >= 1_000_000 ? `${(abs / 1_000_000).toFixed(abs >= 10_000_000 ? 1 : 2)}M` : Math.round(abs).toLocaleString('en-US')}`;
}
const exactDollars = (n: number) => `${n < 0 ? '−' : ''}$${Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 2 })}`;

/** A part of a text: words, or an amount with its exact value for the tooltip. */
export type TextPart = string | { money: string; exact: string };
const NUM = String.raw`\d[\d,]*(?:\.\d+)?`;
// «USD 12,001,200»; «1,022,400 USD»; «257,620-289,246 (median 268,189) USD»
const MONEY = new RegExp(String.raw`USD\s?(${NUM})|(${NUM})(?:\s?[-–]\s?(${NUM}))?(?:\s*\((median|mean) (${NUM})\))?\s?USD\b`, 'g');
const num = (s: string) => Number(s.replace(/,/g, ''));

/** A text with its USD amounts as dollars for people, each with its exact value. */
export function moneyParts(text: string): TextPart[] {
  const out: TextPart[] = [];
  let at = 0;
  for (const m of text.matchAll(MONEY)) {
    if (m.index! > at) out.push(text.slice(at, m.index));
    const [, before, a, b, word, c] = m;
    const show = (f: (n: number) => string) => before ? f(num(before)) : `${f(num(a))}${b ? `–${f(num(b))}` : ''}${c ? ` (${word} ${f(num(c))})` : ''}`;
    out.push({ money: show(dollars), exact: show(exactDollars) });
    at = m.index! + m[0].length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}
/** The text of `moneyParts` with the short amounts, for places without tooltips. */
export const moneyText = (text: string) => moneyParts(text).map(p => (typeof p === 'string' ? p : p.money)).join('');

/** Where a habit comes from, in words: «group · clients:key» -> «group · key clients», «role:backend» -> «backend role». */
export function sourceText(label: string): string {
  return label.replace(/\b([a-z]+):([a-z][\w ]*?)(?=$|[,;·)])/g, (_, what: string, which: string) => `${which.replace(/_/g, ' ').trim()} ${what}`);
}

/** The range of a habit for people: a true minus sign; NPS points as whole numbers («−1 to 55»). */
export function rangeText(h: { interval: string; unit: string; low: number | null; high: number | null }): string {
  if (!h.interval) return '';
  const minus = (s: string) => s.replace(/(^|[\s(])-(?=\d)/g, '$1−');
  if (/NPS/i.test(h.unit) && h.low != null && h.high != null) return `${minus(String(Math.round(h.low)))} to ${minus(String(Math.round(h.high)))}`;
  return h.low != null && h.low < 0 ? minus(h.interval).replace(' – ', ' to ') : minus(h.interval);
}

export const CHANGE_KIND: Record<string, string> = { decision: 'Decision', hypothesis: 'Hypothesis', shock: 'Shock' };
export const KIND_LABEL: Record<ActorCard['kind'], string> = {
  company: 'Company', person: 'Person', client: 'Key client', vendor: 'Vendor', regulator: 'Authority', cohort: 'Cohort',
};
