// Labels and number formats of the page (model-simulator.html "rendering"), shared by the engine texts and the UI.
import type { ExportedModel } from './types.js';

/** Name for people: page.labels, else the description, else the id without "_" (the page's lbl). */
export function lbl(M: ExportedModel, id: string, desc?: string | null): string {
  return (M.page.labels || {})[id] || desc || id.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
}
export const optionLabel = (M: ExportedModel, j: number): string => lbl(M, M.options[j].name);
export const criterionLabel = (M: ExportedModel, k: number): string => lbl(M, M.criteria[k].name);
export function variableLabel(M: ExportedModel, name: string): string {
  const v = M.variables.find(z => z.name === name);
  return lbl(M, name, v && v.desc);
}

export const fmt = (x: number): string => {
  if (!Number.isFinite(x)) return String(x);
  const a = Math.abs(x);
  return a >= 1e6 ? (x / 1e6).toFixed(2) + 'M' : a >= 1e4 ? (x / 1e3).toFixed(0) + 'k' : a >= 100 ? x.toFixed(0) : a >= 10 ? x.toFixed(1) : a >= 1 ? x.toFixed(2) : x.toFixed(4);
};
export const pct = (x: number): string => (100 * x).toFixed(0) + '%';
/** A value with its unit: shares as percent (two decimals near 100%), other units after the number. */
export const vfmt = (v: number, unit: string): string => (unit === 'share' ? (100 * v).toFixed(Math.abs(v) > 0.95 ? 2 : 0) + '%' : `${fmt(v)} ${unit}`);
export const fmt1 = (v: number): string => String(+(+v).toFixed(1));
/** Odds in words. */
export const odds = (p: number): string => (p >= 0.095 ? `${Math.round(p * 10)} in 10` : p >= 0.015 ? `1 in ${Math.round(1 / p)}` : 'below 1 in 50');
export const ppTxt = (d: number): string => `${d > 0 ? '+' : '−'}${Math.abs(Math.round(100 * d))} pp`;

const EMOJI = /^(\p{Extended_Pictographic}|\p{Emoji_Presentation})(️|‍(\p{Extended_Pictographic}|\p{Emoji_Presentation})️?)*\s*/u;
/** "💰 You may need..." -> {emoji: "💰", text: "You may need..."}. */
export function splitEmoji(text: string): { emoji: string; text: string } {
  const m = EMOJI.exec(text);
  return m ? { emoji: m[0].trim(), text: text.slice(m[0].length) } : { emoji: '', text };
}

// ---------------------------------------------------------------- for people (presentation only)
// The engine keeps the page's texts ("1.92M USD", "8 of 10 futures", "weight of cash x0.5") so it stays equal to
// model-simulator.html bit for bit. These helpers turn values and engine texts into the case page's language.

const ALL_EMOJI = /(\p{Extended_Pictographic}|\p{Emoji_Presentation})(\u{FE0F}|\u{200D}|\u{20E3})*/gu;

/** Text without any emoji and without double spaces. */
export const noEmoji = (text: string): string => text.replace(ALL_EMOJI, '').replace(/\u{FE0F}|\u{200D}/gu, '').replace(/\s{2,}/g, ' ').trim();

/** Money for people: 1920000 -> "$1.92M", 250000 -> "$250k", -1200000 -> "−$1.20M", 900 -> "$900". */
export function money(x: number): string {
  if (!Number.isFinite(x)) return String(x);
  const a = Math.abs(x), sign = x < 0 ? '−' : '';
  const body = a >= 1e6 ? `${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `${(a / 1e3).toFixed(0)}k` : a.toFixed(0);
  return `${sign}$${body}`;
}

/** "4,096 scenarios" (one: "1 scenario"). */
export const scenarios = (n: number): string => `${Math.round(n).toLocaleString('en-US')} scenario${Math.round(n) === 1 ? '' : 's'}`;

/** A value with its unit for people: money with $, shares in %, months in words; other units after the number. */
export function peopleValue(v: number, unit: string): string {
  if (!Number.isFinite(v)) return String(v);
  if (unit === 'USD') return money(v);
  if (unit === 'USD/year') return `${money(v)} a year`;
  if (unit === 'USD/month') return `${money(v)} a month`;
  if (unit === 'share') return `${(100 * v).toFixed(Math.abs(v) >= 0.95 && Math.abs(v) < 1 ? 2 : 1)}%`;
  if (unit === 'months') return v >= 59.95 ? '60+ months' : `${v.toFixed(1)} months`;
  if (unit === 'month') return `month ${fmt1(v)}`;
  if (unit === '0/1') return v >= 0.5 ? 'yes' : 'no';
  return unit ? `${fmt(v)} ${unit}` : fmt(v);
}

const MONEY_TEXT = /(−|-)?(\d+(?:\.\d+)?)([kM]?) USD(\/year|\/month)?(?![\w/])/g;

/** An engine text for people: "1.92M USD" -> "$1.92M" ("USD/year" -> "a year"), "futures" -> "scenarios", no emoji. */
export function peopleText(text: string): string {
  const out = text.replace(MONEY_TEXT, (_m, sign: string | undefined, n: string, u: string, per: string | undefined) => {
    const amount = u ? `${sign ? '−' : ''}$${n}${u}` : money((sign ? -1 : 1) * Number(n)); // keep the engine's precision
    return `${amount}${per === '/year' ? ' a year' : per === '/month' ? ' a month' : ''}`;
  })
    .replace(/\bfutures\b/g, 'scenarios').replace(/\bfuture\b/g, 'scenario').replace(/\bFutures\b/g, 'Scenarios');
  return noEmoji(out);
}

/** First letter small unless the word is an acronym ("NPS"). */
const lowFirst = (s: string): string => (/^[A-Z][A-Z]/.test(s) ? s : s.charAt(0).toLowerCase() + s.slice(1));

/** How a risk aversion is judged, in words: 0 the average result; 0.25 the average with a margin; 1 the worst 5%. */
export function cautionWords(riskAversion: number): string {
  if (riskAversion <= 0.1) return 'the average result';
  if (riskAversion < 0.5) return 'the average with a margin';
  return 'the worst 5% of the scenarios';
}

/**
 * A "changes if" line of the best path for people, from its machine form (BreakChange.change):
 * "weight of cash x0.5" -> "cash at year end matters half as much"; "weight of nps x2" -> "NPS matters twice as much";
 * "risk aversion 1" -> "you judge on the worst 5% of the scenarios". Lower case first, to follow "Look again if".
 */
export function breakText(M: ExportedModel, change: string | { change: string }): string {
  const c = typeof change === 'string' ? change : change.change;
  const w = /^weight of (\w+) x([\d.]+)$/.exec(c);
  if (w) {
    const k = M.criteria.findIndex(x => x.name === w[1]), f = Number(w[2]);
    const name = lowFirst(k >= 0 ? criterionLabel(M, k) : w[1]);
    return `${name} ${f === 2 ? 'matters twice as much' : f === 0.5 ? 'matters half as much' : `counts ×${f}`}`;
  }
  const r = /^risk aversion ([\d.]+)$/.exec(c);
  if (r) return `you judge on ${cautionWords(Number(r[1]))}`;
  return peopleText(c);
}
