// The case and the user layer: defaults -> active hypotheses (definition order) -> user edits -> timeline moves.
// Layering is a port of the page's applyOp / setPath / layered (mirrors variator.domain.apply_scenarios).
// State helpers are immutable: each returns a new CaseState.
import { snapMonth } from './timeline.js';
import {
  CASE_SCHEMA,
  type CaseConfig, type CaseState, type Dist, type EditValue, type EffectiveModel, type ExportedModel, type Path, type Plan,
  type Prefs, type PresetTrail, type ScenarioOp, type SectionId, type StatePart,
} from './types.js';

export interface LoadedCase {
  id: string;
  config: CaseConfig;
  /** The template model of the config (the defaults applied). */
  base: ExportedModel;
}

const MODEL_KEYS = ['question', 'variables', 'derived', 'options', 'criteria', 'risks', 'rules', 'assumptions', 'page', 'scenarios', 'timeline'] as const;
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x));

/** Check a config and prepare it. Throws an Error that names the first problem. */
export function loadCase(config: CaseConfig): LoadedCase {
  if (!config || typeof config !== 'object') throw new Error('case config: not an object');
  if (config.schema !== CASE_SCHEMA) throw new Error(`case config: schema ${String(config.schema)}, expected ${CASE_SCHEMA}`);
  for (const k of ['variables', 'derived', 'options', 'criteria', 'risks', 'rules', 'sections', 'forks', 'hypotheses', 'priorityPresets'] as const) {
    if (!Array.isArray(config[k])) throw new Error(`case config ${config.id}: ${k} is not a list`);
  }
  if (!config.options.length || !config.criteria.length) throw new Error(`case config ${config.id}: no options or criteria`);
  if (!config.defaults || typeof config.defaults.n !== 'number' || typeof config.defaults.seed !== 'number') {
    throw new Error(`case config ${config.id}: defaults need n and seed`);
  }
  const base = {} as ExportedModel;
  for (const k of MODEL_KEYS) (base as unknown as Record<string, unknown>)[k] = clone(config[k] ?? null);
  base.scenarios = base.scenarios || [];
  base.assumptions = base.assumptions || [];
  for (const c of base.criteria) if (typeof config.defaults.weights?.[c.name] === 'number') c.weight = config.defaults.weights[c.name];
  return { id: config.id, config, base };
}

// ---------------------------------------------------------------- layering

const LOC = ['value', 'mean', 'low', 'mode', 'high'] as const;

function note(trail: PresetTrail | null, path: string, who: string | null, b: number, a: number) {
  if (trail && who) (trail[path] = trail[path] || []).push([who, b, a]);
}

function distOp(d: Dist, op: ScenarioOp, path: string, who: string | null, trail: PresetTrail | null) {
  if (d.kind === 'bernoulli') {
    const b = d.p as number;
    d.p = Math.min(Math.max(op.scale != null ? b * op.scale : b + (op.shift as number), 0), 1);
    note(trail, path + '|p', who, b, d.p);
    return;
  }
  for (const k of LOC) {
    if (d[k] != null) {
      const b = d[k] as number;
      d[k] = op.scale != null ? b * op.scale : b + (op.shift as number);
      note(trail, `${path}|${k}`, who, b, d[k] as number);
    }
  }
  if (op.scale != null && d.std != null) {
    const b = d.std;
    d.std *= op.scale;
    note(trail, path + '|std', who, b, d.std);
  }
}

/** Apply one scenario op to a model (in place). */
export function applyOp(m: ExportedModel, op: ScenarioOp, who: string | null = null, trail: PresetTrail | null = null): void {
  const kind = op.kind || 'variable';
  if (kind === 'weight') {
    const c = m.criteria.find(x => x.name === op.target);
    if (!c) throw new Error(`no criterion ${op.target}`);
    const b = c.weight;
    c.weight *= op.scale as number;
    note(trail, 'w|' + c.name, who, b, c.weight);
    return;
  }
  if (kind === 'risk') {
    const r = m.risks.find(x => x.name === op.target);
    if (!r) throw new Error(`no risk ${op.target}`);
    if (op.probability != null) {
      const b = r.p;
      r.p = 1 - (1 - r.p) * (1 - op.probability);
      note(trail, `r|${r.name}|p`, who, b, r.p);
    } else distOp(r.magnitude, op, 'rm|' + r.name, who, trail);
    return;
  }
  const v = m.variables.find(x => x.name === op.target);
  if (!v) throw new Error(`no variable ${op.target}`);
  if (!op.options) distOp(v.dist, op, 'v|' + v.name, who, trail);
  m.options.forEach(o => {
    const path = `o|${o.name}|${v.name}`;
    if (!op.options) {
      if (o.overrides[v.name]) distOp(o.overrides[v.name], op, path, who, trail);
    } else if (op.options.includes(o.name)) {
      o.overrides[v.name] = o.overrides[v.name] || clone(v.dist);
      distOp(o.overrides[v.name], op, path, who, trail);
    }
  });
}

/** Set one input path (v|, o|, r|, rm|) on a model, in place. Other paths (w|, tloff|, ...) are ignored. */
export function setPath(m: ExportedModel, path: Path, val: number): void {
  const p = path.split('|');
  const variable = (name: string) => {
    const v = m.variables.find(x => x.name === name);
    if (!v) throw new Error(`no variable ${name}`);
    return v;
  };
  if (p[0] === 'v') (variable(p[1]).dist as unknown as Record<string, number>)[p[2]] = val;
  else if (p[0] === 'o') {
    const o = m.options.find(x => x.name === p[1]);
    if (!o) throw new Error(`no option ${p[1]}`);
    (o.overrides[p[2]] = o.overrides[p[2]] || clone(variable(p[2]).dist));
    (o.overrides[p[2]] as unknown as Record<string, number>)[p[3]] = val;
  } else if (p[0] === 'r') {
    const r = m.risks.find(x => x.name === p[1]);
    if (!r) throw new Error(`no risk ${p[1]}`);
    r.p = val;
  } else if (p[0] === 'rm') {
    const r = m.risks.find(x => x.name === p[1]);
    if (!r) throw new Error(`no risk ${p[1]}`);
    (r.magnitude as unknown as Record<string, number>)[p[2]] = val;
  }
}

/** True when an edit path names an input of the model (saved edits of another model version are dropped). */
export function pathExists(m: ExportedModel, path: Path): boolean {
  const p = path.split('|');
  if (p[0] === 'v') return p.length === 3 && m.variables.some(x => x.name === p[1]);
  if (p[0] === 'o') return p.length === 4 && m.options.some(x => x.name === p[1]) && m.variables.some(x => x.name === p[2]);
  if (p[0] === 'r' || p[0] === 'rm') return p.length === 3 && m.risks.some(x => x.name === p[1]);
  return false;
}

/**
 * The model to simulate: the template, then the active hypotheses in definition order, then the user's edits (they
 * win), then the placed events and the moved markers. `trail` says which hypothesis changed which path.
 */
export function effectiveModel(c: LoadedCase, state: CaseState): EffectiveModel {
  const m = clone(c.base), trail: PresetTrail = {};
  const active = new Set(state.activePresets);
  m.scenarios.forEach(s => { if (active.has(s.name)) s.ops.forEach(op => applyOp(m, op, `${s.emoji} ${s.label}`.trim(), trail)); });
  for (const [p, v] of Object.entries(state.edits)) if (!p.startsWith('w|') && typeof v === 'number') setPath(m, p, v);
  // the user's backup plans: effect rules that start a lever only in the scenarios where their condition holds
  for (const b of state.backups ?? []) {
    if (!m.variables.some(v => v.name === b.variable)) continue;
    m.rules.push({ id: `backup_${b.id}`, pack: 'user', kind: 'effect', desc: b.desc, branches: [{ if: b.if, then: { set: { [b.variable]: `${b.variable} + ${b.change}` }, fail: null, flag: null, criterion: null } }], else: null, options: null });
  }
  const items = m.timeline ? m.timeline.items : [];
  for (const name of state.timeline.events) {
    const ev = items.find(it => it.name === name && it.kind === 'event');
    if (ev) for (const [k, v] of Object.entries(ev.place || {})) setPath(m, `v|${k}|value`, v);
  }
  for (const [name, month] of Object.entries(state.timeline.moves)) {
    const v = m.variables.find(x => x.name === name);
    if (v) v.dist = { kind: 'fixed', value: month };
  }
  return { model: m, trail };
}

/** Weights and risk aversion in force: the user's values, else the model weights after the hypotheses. */
export function effectivePrefs(c: LoadedCase, state: CaseState, model?: ExportedModel): Prefs {
  const m = model ?? effectiveModel(c, state).model;
  const weights: Record<string, number> = {};
  for (const cr of m.criteria) weights[cr.name] = state.prefs.weights[cr.name] ?? cr.weight;
  return { weights, riskAversion: state.prefs.riskAversion ?? c.config.defaults.riskAversion };
}

// ---------------------------------------------------------------- the user layer

export function defaultState(c: LoadedCase): CaseState {
  const d = c.config.defaults;
  return {
    version: 1,
    caseId: c.id,
    activePresets: [...(d.activePresets || [])],
    edits: { ...(d.edits || {}) },
    prefs: { weights: {}, riskAversion: null, priority: null },
    timeline: { moves: {}, events: [], off: [] },
    timelineOption: 'best',
    selectedForks: [],
    n: d.n,
    seed: d.seed,
  };
}

/** Every path the user changed (see the Path doc in types.ts). */
export function modifiedPaths(state: CaseState, c?: LoadedCase): Path[] {
  const out: Path[] = Object.keys(state.edits);
  for (const k of Object.keys(state.prefs.weights)) out.push('w|' + k);
  if (state.prefs.riskAversion != null) out.push('w|__risk');
  for (const k of Object.keys(state.timeline.moves)) out.push(`v|${k}|value`);
  for (const k of state.timeline.events) out.push('ev|' + k);
  for (const k of state.timeline.off) out.push('tloff|' + k);
  for (const k of state.activePresets) out.push('preset|' + k);
  if (state.timelineOption !== 'best') out.push('tlopt');
  if (state.selectedForks.length) out.push('forks|selected');
  if (c && state.n !== c.config.defaults.n) out.push('run|n');
  if (c && state.seed !== c.config.defaults.seed) out.push('run|seed');
  return out;
}

const hit = (prefix: string) => (p: string) => p === prefix || p.startsWith(prefix + '|');

/** True when the user changed the path or anything under it ("v|cash0" covers "v|cash0|value"). */
export function isModified(state: CaseState, path: Path, c?: LoadedCase): boolean {
  return modifiedPaths(state, c).some(hit(path));
}

/** Hypothesis steps [label, before, after] for the path and the paths under it ("was -> now" for the preset mark). */
export function fromPreset(c: LoadedCase, state: CaseState, path: Path): [string, number, number][] {
  const { trail } = effectiveModel(c, state);
  return Object.entries(trail).filter(([p]) => hit(path)(p)).flatMap(([, steps]) => steps);
}

/** Remove the user's change of the path and of the paths under it. */
export function resetPath(c: LoadedCase, state: CaseState, path: Path): CaseState {
  const h = hit(path), d = c.config.defaults;
  const weights = Object.fromEntries(Object.entries(state.prefs.weights).filter(([k]) => !h('w|' + k)));
  const changedWeights = Object.keys(weights).length !== Object.keys(state.prefs.weights).length;
  return {
    ...state,
    edits: Object.fromEntries(Object.entries(state.edits).filter(([k]) => !h(k))),
    prefs: {
      weights,
      riskAversion: h('w|__risk') ? null : state.prefs.riskAversion,
      priority: changedWeights ? null : state.prefs.priority,
    },
    timeline: {
      moves: Object.fromEntries(Object.entries(state.timeline.moves).filter(([k]) => !h(`v|${k}|value`))),
      events: state.timeline.events.filter(k => !h('ev|' + k)),
      off: state.timeline.off.filter(k => !h('tloff|' + k)),
    },
    activePresets: state.activePresets.filter(k => !h('preset|' + k)),
    timelineOption: h('tlopt') ? 'best' : state.timelineOption,
    selectedForks: h('forks|selected') ? [] : state.selectedForks,
    n: h('run|n') ? d.n : state.n,
    seed: h('run|seed') ? d.seed : state.seed,
  };
}

/** Clear the state parts of one section (CaseSection.resets). */
export function resetParts(c: LoadedCase, state: CaseState, parts: StatePart[]): CaseState {
  const s = { ...state }, fresh = defaultState(c);
  for (const part of parts) {
    if (part === 'activePresets') s.activePresets = fresh.activePresets;
    else if (part === 'edits') s.edits = fresh.edits;
    else if (part === 'prefs') s.prefs = fresh.prefs;
    else if (part === 'timeline') s.timeline = fresh.timeline;
    else if (part === 'timelineOption') s.timelineOption = fresh.timelineOption;
    else if (part === 'selectedForks') s.selectedForks = fresh.selectedForks;
    else if (part === 'n') s.n = fresh.n;
    else if (part === 'seed') s.seed = fresh.seed;
  }
  return s;
}

export function resetSection(c: LoadedCase, state: CaseState, section: SectionId): CaseState {
  const sec = c.config.sections.find(x => x.id === section);
  if (!sec) throw new Error(`no section ${section}`);
  return resetParts(c, state, sec.resets);
}

/** Back to the start: the defaults of the config. */
export function resetAll(c: LoadedCase): CaseState {
  return defaultState(c);
}

// ---------------------------------------------------------------- edits

export function setEdit(state: CaseState, path: Path, value: EditValue): CaseState {
  return { ...state, edits: { ...state.edits, [path]: value } };
}

/** Turn a hypothesis on or off. */
export function togglePreset(state: CaseState, name: string, on?: boolean): CaseState {
  const has = state.activePresets.includes(name), want = on ?? !has;
  if (want === has) return state;
  return { ...state, activePresets: want ? [...state.activePresets, name] : state.activePresets.filter(x => x !== name) };
}

export function setWeight(state: CaseState, criterion: string, weight: number): CaseState {
  return { ...state, prefs: { ...state.prefs, weights: { ...state.prefs.weights, [criterion]: weight }, priority: null } };
}

export function setRiskAversion(state: CaseState, value: number | null): CaseState {
  return { ...state, prefs: { ...state.prefs, riskAversion: value } };
}

/** Apply a priority preset: all its weights. */
export function applyPriority(c: LoadedCase, state: CaseState, id: string): CaseState {
  const p = c.config.priorityPresets.find(x => x.id === id);
  if (!p) throw new Error(`no priority preset ${id}`);
  return { ...state, prefs: { ...state.prefs, weights: { ...p.weights }, priority: id } };
}

/** Move a timeline marker (month variable) to a month, rounded to half a month inside the horizon. */
export function moveMarker(c: LoadedCase, state: CaseState, monthVar: string, month: number): CaseState {
  const horizon = c.base.timeline ? c.base.timeline.horizon : 12;
  return { ...state, timeline: { ...state.timeline, moves: { ...state.timeline.moves, [monthVar]: snapMonth(month, horizon) } } };
}

/** Place or remove a certain event. */
export function toggleEvent(state: CaseState, item: string, on?: boolean): CaseState {
  const has = state.timeline.events.includes(item), want = on ?? !has;
  if (want === has) return state;
  return { ...state, timeline: { ...state.timeline, events: want ? [...state.timeline.events, item] : state.timeline.events.filter(x => x !== item) } };
}

/** Delete (off = true) or restore a milestone or goal. */
export function setItemOff(state: CaseState, item: string, off: boolean): CaseState {
  const has = state.timeline.off.includes(item);
  if (off === has) return state;
  return { ...state, timeline: { ...state.timeline, off: off ? [...state.timeline.off, item] : state.timeline.off.filter(x => x !== item) } };
}

/** Apply several month moves at once (suggested dates). */
export function applyMoves(c: LoadedCase, state: CaseState, moves: Record<string, number>): CaseState {
  return Object.entries(moves).reduce((s, [k, v]) => moveMarker(c, s, k, v), state);
}

/** Apply a plan: each lever (and its mode) as a value edit, as the page's "Apply the plan". */
export function applyPlan(c: LoadedCase, state: CaseState, plan: Plan): CaseState {
  let s = state;
  for (const st of plan.steps) {
    s = moveMarker(c, s, st.variable, st.value);
    const lever = c.base.timeline?.levers.find(l => l.variable === st.variable);
    if (lever?.mode && st.modeValue != null) s = setEdit(s, `v|${lever.mode}|value`, st.modeValue);
  }
  return s;
}

// ---------------------------------------------------------------- storage

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

export const stateKey = (caseId: string): string => `variator:case:${caseId}:state:v1`;

function storageOf(storage?: StorageLike | null): StorageLike | null {
  if (storage !== undefined) return storage;
  try {
    return (globalThis as { localStorage?: StorageLike }).localStorage ?? null;
  } catch {
    return null; // access itself can throw (blocked site data)
  }
}

/** Save the user layer; false when the storage is missing or refuses. */
export function saveState(state: CaseState, storage?: StorageLike | null): boolean {
  try {
    const st = storageOf(storage);
    if (!st) return false;
    st.setItem(stateKey(state.caseId), JSON.stringify(state));
    return true;
  } catch {
    return false;
  }
}

/** Remove the saved user layer; false when the storage is missing or refuses. */
export function clearState(caseId: string, storage?: StorageLike | null): boolean {
  try {
    const st = storageOf(storage);
    if (!st || !st.removeItem) return false;
    st.removeItem(stateKey(caseId));
    return true;
  } catch {
    return false;
  }
}

const isNum = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x);
const isRecord = (x: unknown): x is Record<string, unknown> => !!x && typeof x === 'object' && !Array.isArray(x);
const numRecord = (x: unknown): Record<string, number> =>
  isRecord(x) ? Object.fromEntries(Object.entries(x).filter(([, v]) => isNum(v))) as Record<string, number> : {};
const strList = (x: unknown, known?: Set<string>): string[] =>
  Array.isArray(x) ? x.filter((v): v is string => typeof v === 'string' && (!known || known.has(v))) : [];

/** The saved user layer merged on the defaults; the defaults when nothing is saved, the data is broken or storage throws. */
export function loadState(c: LoadedCase, storage?: StorageLike | null): CaseState {
  const fresh = defaultState(c);
  let raw: string | null = null;
  try {
    const st = storageOf(storage);
    raw = st ? st.getItem(stateKey(c.id)) : null;
  } catch {
    return fresh;
  }
  if (!raw) return fresh;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return fresh;
  }
  if (!isRecord(data) || data.version !== 1 || data.caseId !== c.id) return fresh;
  const presets = new Set(c.base.scenarios.map(s => s.name));
  const items = new Set((c.base.timeline?.items || []).map(it => it.name));
  const forks = new Set(c.base.options.map(o => o.name));
  const prefs = isRecord(data.prefs) ? data.prefs : {};
  const tl = isRecord(data.timeline) ? data.timeline : {};
  const edits = isRecord(data.edits)
    ? Object.fromEntries(Object.entries(data.edits).filter(([p, v]) => (isNum(v) || typeof v === 'boolean') && pathExists(c.base, p))) as Record<string, EditValue>
    : {};
  const option = typeof data.timelineOption === 'string' && (data.timelineOption === 'best' || forks.has(data.timelineOption)) ? data.timelineOption : 'best';
  return {
    ...fresh,
    activePresets: strList(data.activePresets, presets),
    edits,
    prefs: {
      weights: numRecord(prefs.weights),
      riskAversion: isNum(prefs.riskAversion) ? prefs.riskAversion : null,
      priority: typeof prefs.priority === 'string' ? prefs.priority : null,
    },
    timeline: { moves: numRecord(tl.moves), events: strList(tl.events, items), off: strList(tl.off, items) },
    timelineOption: option,
    selectedForks: strList(data.selectedForks, forks),
    n: isNum(data.n) && data.n >= 1 ? Math.round(data.n) : fresh.n,
    seed: isNum(data.seed) ? data.seed : fresh.seed,
  };
}
