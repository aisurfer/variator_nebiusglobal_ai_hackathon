// The view of one case: the page's CaseConfig and Preview built from the case config (config/cases/<id>.json), its
// preview file and its line of the index (engine/types.ts is the contract). Everything the page shows about a case
// comes from these files; this module only reshapes them: ids stay in the logic, people see labels.
import { loadCase, noEmoji, type Affect, type CaseConfig as EngineConfig, type CaseItem, type Dist as EngineDist, type LoadedCase, type PreviewFuture, type PreviewMetric } from '../../engine';
import type { CaseBundle } from './cases';
import type { CaseConfig, CaseOrigin, Caution, ChainNode, DayExample, Dist, Edits, HelpEntry, Intervention, InterventionValue, Preview, PreviewMetricDef, RecomputeClass, Unit, WeekNode } from './types';

// ---------------------------------------------------------------- text helpers
const firstSentence = (s: string) => { const m = /^(.+?[.!?])(\s|$)/.exec(s); return m ? m[1] : s; };
export const lowFirst = (s: string) => /^[A-Z][A-Z]/.test(s) ? s : s.charAt(0).toLowerCase() + s.slice(1);
const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/[_-]+/g, ' ');

/** The engine and the page meet here: everything a case needs at run time. */
export interface CaseRuntime {
  id: string;
  config: EngineConfig;
  preview: PreviewFuture | null;
  lc: LoadedCase;
  view: CaseConfig;
  previewView: Preview;
  /** Month variables of movable markers (an edit of one is a timeline move). */
  monthVars: Set<string>;
  label: (id: string) => string;
}

/** The origin file as the page shows it: dates resolved against the fork date, the author named. */
function originView(b: CaseBundle, people: CaseConfig['people']): CaseOrigin | null {
  const o = b.origin;
  if (!o) return null;
  const date = (d: string) => d === 'forkDate' ? b.config.forkDate : d;
  const p = people.find(x => x.id === o.author);
  return {
    request: o.request, qa: o.qa ?? [], requestedOn: date(o.requestedOn), frozenOn: date(o.frozenOn), drafted: o.drafted, reviewEdits: o.reviewEdits,
    author: { id: o.author, name: p?.name ?? o.authorName ?? o.author, title: p?.title ?? o.authorTitle ?? '', entityId: p?.entityId ?? `person.${o.author.toLowerCase()}` },
  };
}

/** Page slot of a config section (DOM id, progress bar). */
const slotOf = (id: string) => id === 'month-time' ? 'overtime' : id.replace(/^month-/, '');
/** Names of a lever's modes for people: the control, and the plan line («8 people, from a vendor»). */
export const MODE_WORDS: Record<string, { label: string; phrase: string }> = {
  hire: { label: 'Hire', phrase: 'hired' }, outsource: { label: 'Vendor', phrase: 'from a vendor' }, vendor: { label: 'Vendor', phrase: 'from a vendor' }, inhouse: { label: 'In-house', phrase: 'in-house' },
};
const toDist = (d: EngineDist): Dist => d.kind === 'fixed' ? { kind: 'fixed', value: d.value ?? 0 } : { kind: 'pert', low: d.low ?? 0, mode: d.mode ?? 0, high: d.high ?? 0 };

export function buildRuntime(b: CaseBundle): CaseRuntime {
  const C = b.config, P = b.preview, E = b.entry;
  const lc = loadCase(C);
  const variable = (name: string) => C.variables.find(v => v.name === name);
  const label = (id: string) => noEmoji(C.labels?.[id] ?? variable(id)?.desc ?? capital(id)).replace(/^(Lever|Milestone|Goal|Placed event): /, '');
  const help = (key: string) => C.help[key];
  const helpEntry = (key: string): HelpEntry | undefined => {
    const h = help(key);
    if (!h) return undefined;
    const more = h.more ? noEmoji(h.more) : '';
    return { title: noEmoji(h.title), short: noEmoji(firstSentence(h.body)), full: [noEmoji(h.body), more && !h.body.includes(more) ? more : ''].filter(Boolean).join(' '), metric: h.metric };
  };
  const items = C.timeline?.items ?? [], levers = C.timeline?.levers ?? [];
  const monthVars = new Set(items.map(i => i.month).filter((m): m is string => typeof m === 'string'));
  const people = C.people ?? [];
  const personId = (p: string | null | undefined) => (p && people.find(x => x.id === p)?.id) || undefined;
  const affectLabels = (xs: Affect[] | undefined) => (xs ?? []).map(a => a.label);

  // ------------------------------------------------ the values an edit path stands for
  const bare = (path: string) => path.replace(/\|(value|low|mode|high)$/, '');
  const pathDist = (path: string): EngineDist | undefined => {
    const [k, a, c] = path.split('|');
    if (k === 'v') return variable(a)?.dist;
    if (k === 'o') return C.options.find(o => o.name === a)?.overrides[c];
    if (k === 'rm') return C.risks.find(r => r.name === a)?.magnitude;
    return undefined;
  };
  const pathVar = (path: string) => { const p = path.split('|'); return p[0] === 'o' ? p[2] : p[0] === 'rm' ? C.risks.find(r => r.name === p[1])?.variable ?? p[1] : p[1]; };
  /** One editable value: a PERT range, a choice (a lever's modes) or a number between min and max. */
  const valueDef = (raw: string, opts: { label?: string; min: number; max: number; step: number; unit?: string } | { label?: string; options: { value: number; label: string }[] } | { label?: string }): InterventionValue | null => {
    const path = bare(raw.includes('|') ? raw : `v|${raw}`), d = pathDist(path);
    if (!d) return null;
    const unit = ('unit' in opts && opts.unit ? opts.unit : variable(pathVar(path))?.unit ?? 'count') as Unit, lbl = opts.label ?? label(pathVar(path));
    if (d.kind !== 'fixed') return { kind: 'range', path, label: lbl, unit };
    if ('options' in opts) return { kind: 'choice', path, label: lbl, unit, options: opts.options };
    if ('min' in opts) return { kind: 'number', path, label: lbl, unit, min: opts.min, max: Math.max(opts.max, d.value ?? 0), step: opts.step };
    return null;
  };

  // ------------------------------------------------ interventions: plan items (levers) and events on the timeline (config `items`)
  const interventions: Intervention[] = items.filter(i => (i.kind === 'plan' || i.kind === 'event') && C.items?.[i.name]).map(it => {
    const ci: CaseItem = C.items[it.name], lever = ci.kind === 'lever', monthVar = String(it.month);
    const values: InterventionValue[] = [];
    const add = (v: InterventionValue | null) => { if (v && !values.some(x => x.path === v.path)) values.push(v); };
    // the amount or size the config names, then a lever's mode (hire or vendor), then the lead time or size range
    if (ci.value) add(valueDef(ci.value.path, { label: ci.value.label, min: ci.value.min, max: ci.value.max, step: ci.value.step, unit: ci.value.unit }));
    for (const lv of levers.filter(l => l.mode && Object.keys(l.modes ?? {}).length && ci.value && l.variable === ci.value.variable)) {
      const options = Object.entries(lv.modes).map(([name, val]) => ({ value: val, label: MODE_WORDS[name]?.label ?? capital(name) }));
      add(valueDef(`v|${lv.mode}`, { label: capital(options.map(o => o.label.toLowerCase()).join(' or ')), options }));
    }
    let lead: string | undefined;
    if (ci.range) { const v = valueDef(ci.range, {}); add(v); if (v?.kind === 'range') lead = v.path; }
    return {
      id: it.name, lane: ci.lane, kind: ci.kind, label: noEmoji(ci.label), description: noEmoji(ci.description),
      monthPath: `v|${monthVar}`, placePath: lever ? undefined : `ev|${it.name}`, forks: it.options ?? undefined, values, lead,
      source: ci.source, affects: affectLabels(ci.affects), entityId: ci.entityId ?? undefined, person: personId(ci.person), owner: personId(ci.owner), shock: ci.shock ?? undefined,
    };
  });

  // ------------------------------------------------ defaults: the only source of «as it was»
  const defaults: Edits = {};
  for (const v of C.variables) defaults[`v|${v.name}`] = toDist(v.dist);
  for (const o of C.options) for (const [k, d] of Object.entries(o.overrides)) defaults[`o|${o.name}|${k}`] = toDist(d);
  for (const r of C.risks) { defaults[`r|${r.name}|p`] = r.p; defaults[`rm|${r.name}`] = toDist(r.magnitude); }
  for (const i of interventions) if (i.placePath) defaults[i.placePath] = false;
  for (const f of C.forks) defaults[`fork|${f.id}`] = true;
  for (const h of C.hypotheses) defaults[`hyp|${h.id}`] = false;
  for (const c of C.criteria) defaults[`w|${c.name}`] = C.defaults.weights[c.name] ?? c.weight;
  for (const it of items) if (it.condition) defaults[`goal|${it.name}|off`] = false;
  const balanced = C.priorityPresets.find(p => C.criteria.every(c => p.weights[c.name] === (C.defaults.weights[c.name] ?? c.weight)));
  const cautionOf = (a: number): Caution => C.caution.reduce((best, c) => Math.abs(c.value - a) < Math.abs((C.caution.find(x => x.id === best)?.value ?? 9) - a) ? c.id as Caution : best, 'margin' as Caution);
  Object.assign(defaults, { caution: cautionOf(C.defaults.riskAversion), priority: balanced?.id ?? 'custom', week: [], day: [], dayExample: 'median', 'sim|n': C.defaults.n, 'sim|seed': C.defaults.seed });

  // ------------------------------------------------ help: the config's dictionary, plus the page's short names (helpKeys)
  const helpDict: Record<string, HelpEntry> = {};
  for (const k of Object.keys(C.help)) helpDict[k] = helpEntry(k)!;
  for (const [page, key] of Object.entries(C.helpKeys ?? {})) { const e = helpEntry(key); if (e) helpDict[page] = e; }

  // ------------------------------------------------ inputs: Model settings by block; «As before» lists the config's stays
  const placeVars = new Set(items.flatMap(i => Object.keys(i.place ?? {})));
  const stays = new Set(C.stays ?? []);
  const inputs = C.variables.filter(v => v.block !== 'lever' && !placeVars.has(v.name)).map(v => ({ path: `v|${v.name}`, label: label(v.name), unit: v.unit as Unit, block: v.block, source: v.source, untouched: stays.has(v.name) }));

  // ------------------------------------------------ the causal chain of «Why»
  const chainNames = C.page.chain ?? [];
  const derived = new Map(C.derived.map(d => [d.name, d]));
  // a chain card names its value as the «affects» chips do (short); its full description is the definition
  const short = new Map([...Object.values(C.items ?? {}), ...Object.values(C.shocks ?? {})].flatMap(x => x.affects ?? []).map(a => [a.id, a.label]));
  const chain: ChainNode[] = chainNames.map(id => {
    const d = derived.get(id), h = helpEntry(`derived.${id}`);
    return { id, label: short.get(id) ?? label(id).replace(/ \(.*\)$/, ''), unit: (d?.unit ?? '') as Unit, definition: h ? `${h.short.replace(/ A link of the cause-and-effect chain\.?/, '')} ${noEmoji(help(`derived.${id}`)?.more ?? '')}`.trim() : d?.desc ?? id, dependsOn: d ? chainNames.filter(n => n !== id && new RegExp(`\\b${n}\\b`).test(d.formula)) : [], higher: C.chainHigher?.[id] ?? null };
  });

  const view: CaseConfig = {
    id: C.id, letter: C.letter ?? E.letter ?? '?', title: C.title, question: noEmoji(E.question ?? C.question), lead: noEmoji(C.lead),
    forkDate: C.forkDate, horizonMonths: C.horizonMonths, company: C.company, owner: personId(C.owner),
    criteria: C.criteria.map(c => { const h = helpEntry(`criterion.${c.name}`); return { id: c.name, label: label(c.name), unit: c.unit as Unit, higher: c.higher, description: h?.short ?? c.desc, help: h?.full ?? c.desc }; }),
    forks: C.forks.map(f => ({ id: f.id, label: noEmoji(f.label), line: noEmoji(f.description), recompute: (f.recompute ?? 'baseline') as RecomputeClass, baseline: f.baseline || undefined, color: f.color, icon: f.icon })),
    hypotheses: C.hypotheses.map(h => ({ id: h.id, label: noEmoji(h.label), line: noEmoji(h.description), changes: h.effects.map(noEmoji), icon: h.icon })),
    priorityPresets: C.priorityPresets.map(p => ({ id: p.id, label: noEmoji(p.label), line: noEmoji(p.description), weights: p.weights })),
    caution: C.caution.filter(c => ['average', 'margin', 'worst'].includes(c.id)).map(c => ({ id: c.id as Caution, label: c.label, description: c.description, value: c.value })),
    interventions,
    milestones: items.filter(i => i.kind === 'milestone' || i.kind === 'goal').map(i => ({ id: i.name, label: noEmoji(i.label), kind: i.kind as 'milestone' | 'goal', monthPath: typeof i.month === 'string' ? `v|${i.month}` : null, fixedMonth: typeof i.month === 'number' ? i.month : undefined, forks: i.options ?? undefined, dependsOn: i.depends, condition: i.condition ?? '' })),
    levers: levers.map(l => ({ path: `v|${l.variable}`, label: noEmoji(l.label), low: l.low, high: l.high, step: l.step, forks: l.options ?? [] })),
    inputs, blocks: C.blocks ?? [],
    shocks: C.risks.map(r => {
      const m = C.shocks?.[r.name];
      return { id: r.name, label: noEmoji(m?.label ?? r.desc), probabilityPath: `r|${r.name}|p`, magnitudePath: `rm|${r.name}`, unit: (variable(r.variable)?.unit ?? 'share') as Unit,
        lane: m?.lane, person: personId(m?.person), owner: personId(m?.owner), entityId: m?.entityId ?? undefined, affects: affectLabels(m?.affects), event: m?.event ?? undefined };
    }),
    chain, chainPath: (C.chainPath ?? []).map(col => col.filter(id => chainNames.includes(id))).filter(col => col.length),
    rules: C.rules.map(r => ({ id: r.id, label: label(r.id), kind: r.kind, text: helpEntry(`rule.${r.id}`)?.full ?? r.desc })),
    steps: C.steps.map(s => ({ id: s.id, label: s.title, number: s.index, help: s.help })),
    sections: C.sections.map(s => ({ id: slotOf(s.id), step: s.step, label: s.title, purpose: noEmoji(s.summary), help: s.help })),
    help: helpDict, defaults, people, goals: C.goals ?? {}, driverCriterion: C.criteria[0].name, riskRows: C.riskRows ?? {},
    origin: originView(b, people),
  };
  return { id: C.id, config: C, preview: P, lc, view, previewView: buildPreview(view, P), monthVars, label };
}

// ---------------------------------------------------------------- the preview of the future sections
const EVENT_TITLE: Record<string, string> = { incident: 'Incident', payment: 'Payment', shock: 'Shock', decision: 'Decision', people: 'People', client: 'Client', vendor: 'Vendor', milestone: 'Milestone' };
const pct = (p: number) => `${Math.round(p * 100)}%`;
function fmtMetric(v: number, unit: string) {
  if (!Number.isFinite(v)) return '—';
  if (unit === 'USD' || unit.startsWith('USD')) { const a = Math.abs(v); return `${v < 0 ? '−' : ''}$${a >= 1e6 ? (a / 1e6).toFixed(2) + 'M' : Math.round(a / 1e3) + 'k'}`; }
  if (unit === 'share') return pct(v);
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}
/** A count (incidents, defects) is drawn as bars; levels and amounts as lines. */
const metricDef = (m: PreviewMetric): PreviewMetricDef => ({ id: m.id, label: noEmoji(m.label), unit: m.unit as Unit, higher: m.higher ?? null, bars: m.unit === 'count' });

function buildPreview(cfg: CaseConfig, P: PreviewFuture | null): Preview {
  const empty: Preview = { preview: true, note: 'No preview data for this case yet.', covers: [], horizon: cfg.horizonMonths, metricLabels: {}, monthMetrics: [], weekMetrics: [], dayMetrics: [], paths: 0, fans: {}, calendar: {}, tree: {}, weekly: {}, days: {} };
  if (!P) return empty;
  const nameOf = (id?: string) => id ? cfg.people.find(p => p.id === id)?.name : undefined;
  const fans: Preview['fans'] = {}, calendar: Preview['calendar'] = {}, tree: Preview['tree'] = {}, weekly: Preview['weekly'] = {}, days: Preview['days'] = {};
  for (const [f, o] of Object.entries(P.options)) {
    fans[f] = o.fans;
    calendar[f] = o.riskCalendar.map(r => ({ id: r.id, label: noEmoji(r.label), consequence: noEmoji(r.consequence ?? ''), byMonth: r.pByMonth, medianMonth: r.medianFirstMonth, total: r.pEver, ref: r.ref ?? undefined, kind: r.kind }));
  }
  const weekMetrics = P.weekMetrics.map(metricDef);
  let paths = 0;
  for (const [f, t] of Object.entries(P.weekTrees)) {
    paths = Math.max(paths, t.maxNodes || 0);
    const lead = weekMetrics[0], up = lead?.higher !== false;
    const nodes: WeekNode[] = [];
    t.levels.forEach((level, w) => {
      const band = lead && t.bands[lead.id]?.find(b => b.week === w);
      level.forEach(n => {
        const vals = n.values, v = lead ? vals[lead.id] : NaN;
        const hi = band && v > band.p50 + (band.p90 - band.p50) * 0.5, lo = band && v < band.p50 - (band.p50 - band.p10) * 0.5;
        const tone = !band ? 'neutral' : (up ? hi : lo) ? 'good' : (up ? lo : hi) ? 'bad' : 'neutral';
        nodes.push({
          id: n.id, week: w, label: w === 0 ? 'Fork date' : `Week ${w}`, share: n.share, parent: n.parent >= 0 && w > 0 ? t.levels[w - 1][n.parent]?.id ?? null : null, tone,
          summary: noEmoji(n.summary), events: [],
          numbers: [{ label: 'Share of paths', value: pct(n.share) }, ...weekMetrics.map(m => ({ label: m.label, value: fmtMetric(vals[m.id], m.unit) }))],
          end: lead ? fmtMetric(v, lead.unit) : '',
        });
      });
    });
    tree[f] = nodes;
    weekly[f] = weekMetrics.flatMap(m => (t.bands[m.id] ?? []).filter(b => b.week > 0).map(b => ({ week: b.week, metric: m.id, p10: b.p10, p50: b.p50, p90: b.p90 })));
  }
  const t0 = new Date(`${P.anchor}T12:00:00Z`).getTime(), dayOf = (iso: string) => Math.round((new Date(`${iso}T12:00:00Z`).getTime() - t0) / 864e5);
  const dayMetrics = P.dayMetrics.map(metricDef);
  const crit0 = cfg.criteria[0];
  // an «incident» event is what the day's count metric counts: an incident in case B, a defect in case A
  const counted = dayMetrics.find(m => m.unit === 'count')?.label.replace(/s$/, '');
  const title = (type: string) => type === 'incident' && counted ? counted : EVENT_TITLE[type] ?? capital(type);
  for (const [f, exs] of Object.entries(P.dayExamples)) {
    days[f] = exs.map((e): DayExample => ({
      id: e.kind, label: e.label, outcome: noEmoji(e.summary.replace(/^[^:]*:\s*/, '')),
      headline: { value: e.yearEnd[crit0.id] ?? NaN, unit: crit0.unit, label: lowFirst(crit0.label) },
      daily: e.days.map(d => ({ ...d.values, day: dayOf(d.date) })),
      events: e.events.map(ev => ({ day: dayOf(ev.date), title: title(ev.type), text: noEmoji(ev.text), tone: ev.type === 'incident' || ev.type === 'shock' ? 'bad' as const : ev.type === 'payment' || ev.type === 'decision' || ev.type === 'milestone' ? 'good' as const : 'event' as const, entityId: ev.entityId, person: nameOf(ev.person) })),
    }));
  }
  return {
    preview: true, note: P.note, covers: Object.keys(P.weekTrees).filter(f => P.dayExamples[f]), horizon: P.horizonMonths ?? cfg.horizonMonths,
    metricLabels: Object.fromEntries(P.metrics.map(m => [m.id, m.label])), monthMetrics: P.metrics.map(metricDef), weekMetrics, dayMetrics, paths, fans, calendar, tree, weekly, days,
  };
}
