// Formatting of the case page: money, shares, months, odds; one place so every section reads the same.
import { money as engineMoney } from '../../engine/format';
import type { CaseConfig, Dist, Unit } from '../model/types';

/** Categorical slots of the forks (validated: lightness, CVD and contrast; slot 0 is the neutral baseline). */
export const FORK_COLORS = ['#4a5761', '#3a77c9', '#1d9488', '#8a5fc0', '#b86a2c'];
export const forkColor = (cfg: CaseConfig, id: string) => FORK_COLORS[(cfg.forks.find(f => f.id === id)?.color ?? 0) % FORK_COLORS.length];
export const forkLabel = (cfg: CaseConfig, id: string) => cfg.forks.find(f => f.id === id)?.label ?? id;
export const critOf = (cfg: CaseConfig, id: string) => cfg.criteria.find(c => c.id === id)!;

/** Money for people (engine/format.ts): $1.92M, $250k, −$1.20M; a dash for no value. */
export const money = (v: number): string => Number.isFinite(v) ? engineMoney(v) : '—';
/** Money where room is short: millions with one decimal ($2.9M, −$1.2M); smaller amounts as money() reads them. */
export const moneyShort = (v: number): string => !Number.isFinite(v) ? '—' : Math.abs(v) >= 1e6 ? `${v < 0 ? '−' : ''}$${(Math.abs(v) / 1e6).toFixed(1)}M` : money(v);
export function fmt(v: number | null | undefined, unit: Unit | string, opts: { compact?: boolean } = {}): string {
  if (v == null || !Number.isFinite(v)) return '—';
  switch (unit) {
    case 'USD': case 'USD/year': case 'USD/month': return money(v) + (unit === 'USD/year' && !opts.compact ? ' a year' : unit === 'USD/month' && !opts.compact ? ' a month' : '');
    case 'share': return `${(v * 100).toFixed(Math.abs(v) >= 0.95 && Math.abs(v) < 1 ? 2 : Math.abs(v) < 0.1 ? 1 : Math.abs(v) >= 0.2 ? 0 : 1)}%`;
    case 'months': return v >= 59.95 ? '60+ mo' : `${v.toFixed(v < 10 ? 1 : 0)} mo`;
    case 'month': return `month ${+v.toFixed(1)}`;
    case 'points': return v.toFixed(1);
    case 'ms': return `${Math.round(v)} ms`;
    case 'hours': return `${v.toFixed(1)} h`;
    case 'days': return `${Math.round(v)} days`;
    case 'people': return `${Math.round(v)}`;
    case 'FTE': return opts.compact ? `${+v.toFixed(1)}` : `${+v.toFixed(v < 10 ? 1 : 0)} FTE`;
    case 'FTE-months': return opts.compact ? `${v.toFixed(1)}` : `${v.toFixed(v < 100 ? 1 : 0)} FTE-mo`;
    case 'story points': return opts.compact ? `${Math.round(v)}` : `${Math.round(v)} pts`;
    case 'points/month': return `${Math.round(v)} pts/mo`;
    case 'count': return v.toFixed(v < 10 ? 1 : 0);
    case 'ratio': case 'index': case 'x': case 'score': return v.toFixed(2);
    default: return v.toFixed(2);
  }
}
/** A difference in the unit of the value ("+$120k", "−1.2 pp", "+0.4 mo"). */
export function fmtDiff(d: number, unit: Unit | string): string {
  if (!Number.isFinite(d)) return '—';
  const s = d > 0 ? '+' : d < 0 ? '−' : '±', a = Math.abs(d);
  switch (unit) {
    case 'USD': case 'USD/year': case 'USD/month': return `${s}${money(a)}`;
    case 'share': return `${s}${(a * 100).toFixed(a * 100 < 1 ? 2 : 1)} pp`;
    case 'months': return `${s}${a.toFixed(1)} mo`;
    case 'points': return `${s}${a.toFixed(1)}`;
    case 'ms': return `${s}${Math.round(a)} ms`;
    case 'FTE-months': return `${s}${a.toFixed(1)} FTE-mo`;
    case 'FTE': return `${s}${+a.toFixed(1)} FTE`;
    case 'story points': return `${s}${Math.round(a)} pts`;
    case 'points/month': return `${s}${Math.round(a)} pts/mo`;
    default: return `${s}${a.toFixed(2)}`;
  }
}
/** good / bad / neutral by the direction of the value (never by its sign). */
export const toneOf = (d: number, higher: boolean | null, eps = 1e-9): 'good' | 'bad' | 'neutral' => higher == null || Math.abs(d) <= eps ? 'neutral' : (d > 0) === higher ? 'good' : 'bad';
export { odds, pct, scenarios } from '../../engine/format';
export const distMode = (d: Dist) => d.kind === 'fixed' ? d.value : d.mode;
export function fmtDist(d: unknown, unit: Unit | string): string {
  if (typeof d === 'number') return fmt(d, unit);
  if (typeof d === 'boolean') return d ? 'on' : 'off';
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.length ? `${d.length} selected` : 'none';
  if (d && typeof d === 'object' && 'kind' in d) {
    const x = d as Dist;
    return x.kind === 'fixed' ? fmt(x.value, unit) : `${fmt(x.low, unit, { compact: true })} – ${fmt(x.mode, unit, { compact: true })} – ${fmt(x.high, unit, { compact: true })}`;
  }
  return '—';
}
export const fmtMonth = (v: number) => `${+v.toFixed(1)}`;
/** Which forks a P(best) is computed over, in a few words: «of 5 forks». A fork is best in a scenario among all the
 *  forks compared (the ranking's), whatever a section shows; `n` is their number, from the ranking itself. */
export const pBestScope = (n: number) => `of ${n} fork${n === 1 ? '' : 's'}`;
// Dates read day first, the month in three letters, as everywhere in the app: «26 Oct», «26 Oct 2026», «Mon, 26 Apr
// 2027», «30 Oct 2026, 14:05». Built from the parts, not toLocaleString: the browser's locale (04/26/2027) and its
// version («Sept») never change them.
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const ymd = (iso: string) => { const [y, m, d] = iso.slice(0, 10).split('-').map(Number); return { y, m, d }; };
/** «26 Oct»: a date (YYYY-MM-DD, or the date of a timestamp as written). */
export const dayMonth = (iso: string) => { const { m, d } = ymd(iso); return MON[m - 1] && d ? `${d} ${MON[m - 1]}` : '—'; };
/** «26 Oct 2026». */
export const longDate = (iso: string) => { const { y } = ymd(iso), dm = dayMonth(iso); return dm === '—' ? dm : `${dm} ${y}`; };
/** «Mon, 26 Apr 2027». */
export const weekdayDate = (iso: string) => { const t = new Date(`${iso.slice(0, 10)}T12:00:00Z`); return Number.isNaN(t.getTime()) ? '—' : `${WEEKDAY[t.getUTCDay()]}, ${longDate(iso)}`; };
/** «30 Oct 2026, 14:05»: a moment (an ISO timestamp) on the viewer's clock (`timeZone` for a fixed one), 24 hours. */
export function dateTime(iso: string, timeZone?: string): string {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '—';
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-GB', { timeZone, year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(t).map(x => [x.type, x.value]));
  return `${Number(p.day)} ${MON[Number(p.month) - 1]} ${p.year}, ${p.hour}:${p.minute}`;
}

/** Calendar month of month m after the fork date T: «Jan ’27»; a half month reads «mid-Jan ’27». */
export function calMonth(cfg: CaseConfig, m: number, opts: { year?: 'always' | 'jan' | 'never' } = {}): string {
  const t = new Date(`${cfg.forkDate}T12:00:00Z`), whole = Math.floor(m + 1e-9), half = m - whole > 0.25;
  const d = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + whole, 1)), mon = MON[d.getUTCMonth()];
  const yr = `’${String(d.getUTCFullYear()).slice(2)}`, year = opts.year ?? 'always';
  const y = year === 'always' || (year === 'jan' && d.getUTCMonth() === 0) ? ` ${yr}` : '';
  return `${half ? 'mid-' : ''}${mon}${y}`;
}
/** PERT density (for the small curve of a range). */
export function pertPdf(a: number, m: number, c: number, x: number): number {
  if (!(c > a) || x < a || x > c) return 0;
  const al = 1 + 4 * (m - a) / (c - a), be = 1 + 4 * (c - m) / (c - a), t = (x - a) / (c - a);
  return Math.pow(t, al - 1) * Math.pow(1 - t, be - 1);
}
/** Values in the units people type: shares as percent, money in thousands. */
export const displayScale = (unit: string) => unit === 'share' ? 100 : unit === 'USD' || unit.startsWith('USD/') ? 0.001 : 1;
export const displaySuffix = (unit: string) => unit === 'share' ? '%' : unit === 'USD' ? '$k' : unit === 'USD/year' ? '$k/yr' : unit === 'USD/month' ? '$k/mo' : unit === 'months' ? 'mo' : unit === 'month' ? 'month' : unit === '0/1' ? '' : unit;
/** A value as people read it: «5 people», «15%», «$300k a year», «2.5 months». */
export function fmtValue(v: number, unit: string) {
  if (unit === 'people' || unit === 'FTE') return `${Math.round(v)} ${Math.round(v) === 1 ? 'person' : 'people'}`;
  if (unit === 'months') return `${+v.toFixed(1)} months`;
  if (unit === 'story points' || unit === 'points') return `${Math.round(v)} points`;
  if (unit === 'count') return `${+v.toFixed(Number.isInteger(v) ? 0 : 1)}`;
  return fmt(v, unit);
}
/** The fork's name is fixed text («Hire 5 engineers»): the edited sizes of its own levers, to show next to it. */
export function forkNote(cfg: CaseConfig, fork: string, value: (path: string) => unknown, edited: (path: string) => boolean): string | null {
  const vals = cfg.interventions.filter(i => i.forks?.includes(fork)).flatMap(i => i.values).filter(v => v.kind !== 'range' && edited(v.path));
  if (!vals.length) return null;
  const num = (d: unknown) => typeof d === 'number' ? d : (d as { value?: number; mode?: number } | null)?.value ?? (d as { mode?: number } | null)?.mode ?? 0;
  return `edited: ${vals.map(v => v.kind === 'choice' ? v.options.find(o => o.value === num(value(v.path)))?.label ?? '' : fmtValue(num(value(v.path)), v.unit)).join(', ')}`;
}
/** The question in one short line: the part before the list of options («Which lever gives X the best year: keep …?»
 *  reads «Which lever gives X the best year?»); a question without a colon stays as it is. */
export function shortQuestion(q: string): string {
  const i = q.indexOf(':');
  if (i < 12) return q.trim();
  return `${q.slice(0, i).trim().replace(/[?.!]$/, '')}?`;
}
