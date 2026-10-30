// The intake agent of the cases page, deterministic in this version: it maps a question to an intent (people,
// clients, vendors, factors, rules), fills the slots it can read from the words (scope, when, size, goal, what to
// compare, what matters most), asks at most `maxQuestions` questions for what is missing or ambiguous, and then shows
// the draft of a case. Every number it says comes from the user's words or from the company facts. Pure functions:
// `nextMessage(thread, config, facts)` is the entry point (no DOM, no storage), also run by the unit tests in Node.
import { COMPANY_FACTS } from './facts';
import type { AgentMsg, Chip, DraftCase, IntakeConfig, IntakeFacts, IntakeFactsFile, IntakeMsg, IntentDef, SlotId } from './types';

export interface SlotValue { value: string; raw?: string; ambiguous?: boolean; months?: number }
export interface IntakeState {
  intent: IntentDef | null;
  slots: Partial<Record<SlotId, SlotValue>>;
  asked: SlotId[];
  questions: number;
  unknowns: number;
}

const MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'];
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const rx = (src: string) => new RegExp(src, 'i');
const PRIORITY_CUE = /\b(?:matters?|important|priorit|care (?:most )?about|focus on|first and foremost)\b/i;
const join = (xs: string[]) => xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`;
const money = (v: number) => v >= 1e6 ? `$${+(v / 1e6).toFixed(2)}M` : `$${Math.round(v / 1e3)}k`;

/** «January 2027»: the month m months after the fork date. */
export function monthDate(forkDate: string, m: number): string {
  const t = new Date(`${forkDate}T12:00:00Z`), d = new Date(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + Math.round(m), 1));
  return `${cap(MONTHS[d.getUTCMonth()])} ${d.getUTCFullYear()}`;
}
/** Months from the fork date to the next given month (a month name in the future). */
function monthsUntil(forkDate: string, name: string): number {
  const t = new Date(`${forkDate}T12:00:00Z`), target = MONTHS.indexOf(name.toLowerCase());
  const diff = (target - t.getUTCMonth() + 12) % 12;
  return diff === 0 ? 12 : diff;
}

// ---------------------------------------------------------------- facts the agent may say
const leadershipOf = (f: IntakeFacts) => f.people.filter(p => p.id === f.ceo || p.manager === f.ceo);
const deptCount = (f: IntakeFacts, id: string) => f.people.filter(p => p.department === id).length;
const clientsByValue = (f: IntakeFacts) => [...f.keyClients].sort((a, b) => b.acv - a.acv);
const vendorOf = (f: IntakeFacts, kind: string) => f.vendors.find(v => v.kind === kind);

/** A placeholder of a template: a fact, a slot, or a named entity («{dept:engineering}», «{client:1}»). */
function factVar(key: string, f: IntakeFacts): string | undefined {
  const [k, a] = key.split(':');
  const lead = leadershipOf(f), dept = (id: string) => f.departments.find(d => d.id === id);
  switch (k) {
    case 'company': return f.company;
    case 'headcount': return String(f.people.length);
    case 'departmentsLine': return join([...f.departments].map(d => ({ d, n: deptCount(f, d.id) })).filter(x => x.n > 0).sort((x, y) => y.n - x.n).map(x => `${x.n} in ${x.d.name}`));
    case 'leadershipCount': return String(lead.length);
    case 'leadershipLine': return join(lead.map(p => p.id === f.ceo ? 'you' : `${p.name} (${dept(p.department)?.name ?? p.department})`));
    case 'nonLeadershipCount': return String(f.people.length - lead.length);
    case 'dept': return String(deptCount(f, a));
    case 'head': return f.people.find(p => p.id === dept(a)?.head)?.name;
    case 'person': return f.people.find(p => p.id === a)?.name;
    case 'client': case 'keyClient': return clientsByValue(f)[Number(a) - 1]?.name;
    case 'keyClientCount': return String(f.keyClients.length);
    case 'otherClients': return String(f.otherClients);
    case 'keyClientsLine': return join(clientsByValue(f).map((c, i) => `${c.name} (${money(c.acv)}${i === 0 ? ' a year' : ''})`));
    case 'keyClientMinAcv': return money(Math.min(...f.keyClients.map(c => c.acv)));
    case 'keyClientMaxAcv': return money(Math.max(...f.keyClients.map(c => c.acv)));
    case 'vendor': return vendorOf(f, a)?.name;
    case 'vendorsLine': return join(f.vendors.slice(0, 5).map(v => `${v.name} (${v.kind})`));
    default: return undefined;
  }
}

function fill(template: string, f: IntakeFacts, extra: Record<string, string> = {}): string {
  return template.replace(/\{([\w:-]+)\}/g, (m, key: string) => extra[key] ?? factVar(key, f) ?? m);
}

// ---------------------------------------------------------------- reading the words
export function detectIntent(text: string, cfg: IntakeConfig): IntentDef | null {
  return cfg.intents.find(i => i.patterns.some(p => rx(p).test(text))) ?? null;
}

/** A named person, key client or vendor in the words. */
function entityIn(text: string, kind: 'person' | 'client' | 'vendor', f: IntakeFacts): string | null {
  const t = text.toLowerCase();
  if (kind === 'person') { const p = f.people.find(x => t.includes(x.name.toLowerCase()) || new RegExp(`\\b${x.name.split(' ')[0].toLowerCase()}\\b`).test(t)); return p ? `person:${p.id}` : null; }
  if (kind === 'client') { const list = clientsByValue(f), i = list.findIndex(c => t.includes(c.name.toLowerCase()) || new RegExp(`\\b${c.name.split(' ')[0].toLowerCase()}\\b`).test(t)); return i >= 0 ? `client:${i + 1}` : null; }
  const v = f.vendors.find(x => t.includes(x.name.toLowerCase()) || new RegExp(`\\b${x.name.split(' ')[0].toLowerCase()}\\b`).test(t));
  return v ? `vendor:${v.id}` : null;
}

/** The value of one slot in the words, or null. Goal, compare and priority count only with a value the intent knows. */
export function readSlot(slot: SlotId, text: string, intent: IntentDef, cfg: IntakeConfig, f: IntakeFacts): SlotValue | null {
  if (slot === 'scope') {
    for (const r of intent.scope) {
      if (r.entity) { const v = entityIn(text, r.entity, f); if (v) return { value: v }; continue; }
      if (r.re && rx(r.re).test(text)) return { value: r.value!, ambiguous: r.ambiguous };
    }
    return null;
  }
  for (const e of cfg.slots[slot] ?? []) {
    const m = rx(e.re).exec(text);
    if (!m) continue;
    const g = (x: string | number | undefined) => typeof x === 'string' ? x.replace(/\$(\d)/g, (_s, n: string) => m[Number(n)] ?? '') : x;
    if (slot === 'when') {
      const months = e.monthName ? monthsUntil(f.forkDate, String(g(e.monthName))) : e.weeks ? Math.round(Number(g(e.weeks)) / 4.35 * 2) / 2 : Number(g(e.months));
      return { value: `months:${months}`, months };
    }
    if (slot === 'size') return e.share ? { value: `${/point/i.test(m[0]) ? 'pp' : 'share'}:${g(e.share)}` } : { value: `count:${g(e.count)}` };
    const v = String(e.value), known = [...(intent.questions[slot]?.chips ?? []).map(c => c.value), ...Object.keys(intent.draft.compareForks ?? {})];
    if (slot === 'priority' ? cfg.criteria[v] : known.includes(v)) return { value: v };
  }
  return null;
}

/** A chip's value as a slot value («months:3», «renewal», «client:2»). */
function chipSlot(slot: SlotId, value: string, f: IntakeFacts, st: IntakeState): SlotValue {
  if (slot === 'when') {
    if (value === 'renewal') { const c = clientOfScope(st, f); const m = c?.renewalInMonths; return m != null ? { value: `months:${m}`, months: m } : { value: 'renewal' }; }
    const m = /^months:(\d+(?:\.\d+)?)$/.exec(value);
    if (m) return { value, months: Number(m[1]) };
  }
  return { value };
}
const clientOfScope = (st: IntakeState, f: IntakeFacts) => { const m = /^client:(\d+)$/.exec(st.slots.scope?.value ?? ''); return m ? clientsByValue(f)[Number(m[1]) - 1] : undefined; };

/** What the thread says so far: the intent, the filled slots, the questions asked. */
export function analyse(thread: IntakeMsg[], cfg: IntakeConfig, f: IntakeFacts): IntakeState {
  const st: IntakeState = { intent: null, slots: {}, asked: [], questions: 0, unknowns: 0 };
  const userTexts: string[] = [];
  const absorb = (text: string) => {
    if (!st.intent) return;
    for (const s of ['scope', 'when', 'size', 'goal', 'compare', 'priority'] as SlotId[]) {
      if (st.slots[s] && !st.slots[s]!.ambiguous) continue;
      // «what matters most» only from a sentence that says so (not from any word about clients or the team)
      if (s === 'priority' && !PRIORITY_CUE.test(text)) continue;
      const v = readSlot(s, text, st.intent, cfg, f);
      if (v && !(st.slots[s]?.ambiguous && v.ambiguous)) st.slots[s] = v;
    }
  };
  let last: IntakeMsg | null = null;
  for (const msg of thread) {
    if (msg.role === 'agent') {
      if (msg.kind === 'question' && msg.slot) { st.asked.push(msg.slot); st.questions++; }
      if (msg.kind === 'unknown') st.unknowns++;
      last = msg;
      continue;
    }
    userTexts.push(msg.text);
    if (!st.intent) {
      const chosen = msg.value?.startsWith('intent:') ? cfg.intents.find(i => i.id === msg.value!.slice(7)) : undefined;
      st.intent = chosen ?? detectIntent(msg.text, cfg);
      if (st.intent) userTexts.forEach(absorb);
    } else if (last?.role === 'agent' && last.kind === 'question' && last.slot) {
      const s = last.slot;
      const v = msg.value ? chipSlot(s, msg.value, f, st) : readSlot(s, msg.text, st.intent, cfg, f) ?? { value: 'raw', raw: msg.text.trim() };
      st.slots[s] = { ...v, ambiguous: false };
      absorb(msg.text);
    } else absorb(msg.text);
    last = msg;
  }
  return st;
}

// ---------------------------------------------------------------- writing the answer
/** The slot values as words for the templates. */
function slotVars(st: IntakeState, cfg: IntakeConfig, f: IntakeFacts): Record<string, string> {
  const it = st.intent!, s = st.slots, out: Record<string, string> = {};
  const sc = s.scope;
  if (sc) {
    const ent = /^(person|client|vendor):(.+)$/.exec(sc.value);
    const name = ent ? ent[1] === 'person' ? f.people.find(p => p.id === ent[2])?.name : ent[1] === 'client' ? clientsByValue(f)[Number(ent[2]) - 1]?.name : f.vendors.find(v => v.id === ent[2])?.name : undefined;
    out.scope = sc.raw ? `«${sc.raw}»` : name ?? fill(it.scopeText?.[sc.value] ?? sc.value.replace(/^\w+:/, ''), f);
    out.scopeName = name ?? out.scope;
  }
  const c = clientOfScope(st, f);
  out.scopeRenewal = c?.renewalInMonths != null ? `${c.name} renews in ${monthDate(f.forkDate, c.renewalInMonths)}.` : '';
  const w = s.when;
  out.when = !w ? '' : w.raw ? ` («${w.raw}»)` : w.months === 0 ? ' right away' : w.months != null ? ` in ${monthDate(f.forkDate, w.months)}` : '';
  const z = s.size;
  out.size = !z ? 'a size you choose' : z.raw ? `«${z.raw}»` : z.value.startsWith('share:') ? `${z.value.slice(6)}%` : z.value.startsWith('pp:') ? `${z.value.slice(3)} percentage points` : z.value.slice(6);
  const g = s.goal;
  out.goal = !g ? '' : g.raw ? `, to «${g.raw}»` : cfg.goalText[g.value] ?? '';
  return out;
}

function question(st: IntakeState, slot: SlotId, cfg: IntakeConfig, f: IntakeFacts): AgentMsg {
  const it = st.intent!, q = it.questions[slot] ?? { text: `Tell me more about the ${slot}.`, chips: [] };
  const vars = slotVars(st, cfg, f);
  let text = fill(q.text, f, vars);
  if (slot === 'scope' && it.id === 'leave') text = text.replace('{keyPeopleLine}', join(q.chips.map(ch => { const p = f.people.find(x => `person:${x.id}` === ch.value); return p ? `${p.name} (${p.title})` : ''; }).filter(Boolean)));
  const chips: Chip[] = q.chips.map(ch => ({ label: fill(ch.label, f, vars), value: ch.value }));
  return { role: 'agent', kind: 'question', slot, intent: it.id, text: text.replace(/\s+\./g, '.').replace(/\s{2,}/g, ' ').trim(), chips };
}

function draft(st: IntakeState, cfg: IntakeConfig, f: IntakeFacts): AgentMsg {
  const it = st.intent!, d = it.draft, vars = slotVars(st, cfg, f), cmp = st.slots.compare?.value;
  let forks = d.forks.map(x => fill(x, f, vars));
  if (d.compareFilter) forks = forks.filter((_x, i) => (d.compareFilter![cmp ?? 'both'] ?? d.compareFilter!.both ?? [i]).includes(i));
  const extra = cmp && cmp !== 'keep' ? d.compareForks?.[cmp] : !cmp && d.defaultCompare ? d.compareForks?.[d.defaultCompare] : undefined;
  if (st.slots.compare?.raw) forks.push(cap(st.slots.compare.raw));
  else if (extra) forks.push(fill(extra, f, vars));
  const pr = st.slots.priority?.value;
  const crit = [...new Set([...(pr && cfg.criteria[pr] ? [pr] : []), ...d.criteria])].map(k => cfg.criteria[k]).filter(Boolean);
  const dc: DraftCase = {
    question: fill(d.question, f, vars).replace(/\s{2,}/g, ' '),
    forks: [{ label: cfg.final.baseline, baseline: true }, ...forks.map(label => ({ label: cap(label) }))],
    hypotheses: d.hypotheses.map(x => fill(x, f, vars)), criteria: crit, shows: d.shows.map(x => fill(x, f, vars)),
    next: cfg.final.next, notice: cfg.final.notice, links: cfg.final.links,
  };
  return { role: 'agent', kind: 'draft', intent: it.id, text: cfg.draftIntro, chips: [], draft: dc };
}

/** The agent's next message for a thread that ends with a message of the user. */
export function nextMessage(thread: IntakeMsg[], cfg: IntakeConfig, f: IntakeFacts): AgentMsg {
  const st = analyse(thread, cfg, f);
  if (!st.intent) {
    const chips = cfg.unknown.chips.map(id => cfg.intents.find(i => i.id === id)).filter((i): i is IntentDef => !!i).map(i => ({ label: i.label, value: `intent:${i.id}` }));
    return { role: 'agent', kind: 'unknown', text: st.unknowns ? cfg.unknown.again : cfg.unknown.text, chips };
  }
  const missing = st.intent.ask.filter(s => (!st.slots[s] || st.slots[s]!.ambiguous) && !st.asked.includes(s) && st.intent!.questions[s]);
  if (missing.length && st.questions < cfg.maxQuestions) return question(st, missing[0], cfg, f);
  return draft(st, cfg, f);
}

/** The facts of the intake: the company facts file (config/company.facts.json) plus the company name and the fork date. */
export function buildFacts(company: string, forkDate: string, facts: IntakeFactsFile = COMPANY_FACTS): IntakeFacts {
  return { ...facts, company, forkDate };
}
