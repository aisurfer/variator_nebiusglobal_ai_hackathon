// Formula compiler and model compilation: a port of model-simulator.html ("formula parser" and "compile the model").
// The Python subset of variator.expr, compiled to closures (no eval). Booleans become 1/0.
import type { ExportedModel, ModelDerived, ModelOption, ModelRule, RuleAction, TimelineItem } from './types.js';

export type Env = Record<string, number>;
/** A slot environment: one Float64Array cell per name (CompiledModel.slots); NaN = not set. */
export type SlotEnv = Float64Array;
export interface CompiledFn {
  (env: Env | SlotEnv): number;
  names: Set<string>;
  src: string;
}

export function tokenize(s: string): string[] {
  const re = /\s*(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|[A-Za-z_][A-Za-z0-9_]*|\*\*|\/\/|<=|>=|==|!=|[-+*/%()<>,])/y;
  const out: string[] = [];
  re.lastIndex = 0;
  let m: RegExpExecArray | null;
  while (re.lastIndex < s.length) {
    m = re.exec(s);
    if (!m) {
      if (/^\s*$/.test(s.slice(re.lastIndex))) break;
      throw new Error('bad formula: ' + s);
    }
    out.push(m[1]);
  }
  return out;
}

const FUN: Record<string, (a: number[]) => number> = {
  min: a => Math.min(...a),
  max: a => Math.max(...a),
  minimum: a => Math.min(a[0], a[1]),
  maximum: a => Math.max(a[0], a[1]),
  abs: a => Math.abs(a[0]),
  exp: a => Math.exp(a[0]),
  log: a => Math.log(a[0]),
  sqrt: a => Math.sqrt(a[0]),
  clip: a => Math.min(Math.max(a[0], a[1]), a[2]),
  where: a => (a[0] ? a[1] : a[2]),
};

// A node reads a name map (Env) or a slot array (SlotEnv).
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Node = (e: any) => number | boolean;
const num = (v: number | boolean): number => (typeof v === 'boolean' ? (v ? 1 : 0) : v);
const CMP: Record<string, (x: number, y: number) => boolean> = {
  '<': (x, y) => x < y,
  '>': (x, y) => x > y,
  '<=': (x, y) => x <= y,
  '>=': (x, y) => x >= y,
  '==': (x, y) => x === y,
  '!=': (x, y) => x !== y,
};

/**
 * Compile a formula. Without `slot`, the function reads a name map (env[name]). With `slot` (name -> index), it reads
 * a SlotEnv (env[index]): the same closures and the same arithmetic order, only faster lookups.
 */
export function compile(src: string, slot?: (name: string) => number): CompiledFn {
  const t = tokenize(src);
  let i = 0;
  const names = new Set<string>();
  const peek = () => t[i];
  const next = () => t[i++];
  function ternary(): Node {
    const a = orx();
    if (peek() === 'if') {
      next();
      const c = orx();
      if (next() !== 'else') throw new Error('expected else');
      const b = ternary();
      return e => (c(e) ? a(e) : b(e));
    }
    return a;
  }
  function orx(): Node {
    let a = andx();
    while (peek() === 'or') {
      next();
      const l = a, r = andx();
      a = e => !!l(e) || !!r(e);
    }
    return a;
  }
  function andx(): Node {
    let a = cmp();
    while (peek() === 'and') {
      next();
      const l = a, r = cmp();
      a = e => !!l(e) && !!r(e);
    }
    return a;
  }
  function cmp(): Node {
    const first = sum();
    const ops: string[] = [], rhs: Node[] = [];
    while (['<', '>', '<=', '>=', '==', '!='].includes(peek())) {
      ops.push(next());
      rhs.push(sum());
    }
    if (!ops.length) return first;
    return e => {
      let x = num(first(e));
      for (let k = 0; k < ops.length; k++) {
        const y = num(rhs[k](e));
        if (!CMP[ops[k]](x, y)) return false;
        x = y;
      }
      return true;
    };
  }
  function sum(): Node {
    let a = term();
    while (peek() === '+' || peek() === '-') {
      const op = next(), l = a, r = term();
      a = op === '+' ? e => num(l(e)) + num(r(e)) : e => num(l(e)) - num(r(e));
    }
    return a;
  }
  function term(): Node {
    let a = unary();
    while (['*', '/', '//', '%'].includes(peek())) {
      const op = next(), l = a, r = unary();
      a = op === '*' ? e => num(l(e)) * num(r(e))
        : op === '/' ? e => num(l(e)) / num(r(e))
        : op === '//' ? e => Math.floor(num(l(e)) / num(r(e)))
        : e => { const x = num(l(e)), y = num(r(e)); return x - y * Math.floor(x / y); };
    }
    return a;
  }
  function unary(): Node {
    if (peek() === '-') {
      next();
      const a = unary();
      return e => -num(a(e));
    }
    if (peek() === '+') {
      next();
      return unary();
    }
    return power();
  }
  function power(): Node {
    const a = atom();
    if (peek() === '**') {
      next();
      const b = unary();
      return e => Math.pow(num(a(e)), num(b(e)));
    }
    return a;
  }
  function atom(): Node {
    const tok = next();
    if (tok === '(') {
      const a = ternary();
      if (next() !== ')') throw new Error('expected )');
      return a;
    }
    if (tok !== undefined && /^[\d.]/.test(tok)) {
      const v = parseFloat(tok);
      return () => v;
    }
    if (tok !== undefined && /^[A-Za-z_]/.test(tok)) {
      if (peek() === '(') {
        next();
        const args: Node[] = [];
        if (peek() !== ')') {
          args.push(ternary());
          while (peek() === ',') {
            next();
            args.push(ternary());
          }
        }
        next();
        const f = FUN[tok];
        if (!f) throw new Error('unknown function ' + tok);
        return e => f(args.map(x => num(x(e))));
      }
      if (tok === 'True') return () => 1;
      if (tok === 'False') return () => 0;
      names.add(tok);
      if (slot) {
        const k = slot(tok);
        return (e: SlotEnv) => e[k];
      }
      return (e: Env) => e[tok];
    }
    throw new Error('unexpected token ' + tok + ' in ' + src);
  }
  const fn = ternary();
  if (i < t.length) throw new Error('unexpected ' + t[i] + ' in ' + src);
  const out = ((e: Env | SlotEnv) => num(fn(e))) as CompiledFn;
  out.names = names;
  out.src = src;
  return out;
}

export interface CompiledAction extends RuleAction {
  setFns: [string, CompiledFn][];
  /** [slot, formula] of each "set". */
  setSlots: [number, CompiledFn][];
}
export interface CompiledRule extends ModelRule { conds: CompiledFn[]; acts: (CompiledAction | null)[] }
export interface CompiledOption extends ModelOption { fns: CompiledFn[] }
export interface CompiledTimelineItem extends TimelineItem { cf: CompiledFn | null; sf: CompiledFn | null }

export interface CompiledModel {
  dmap: Record<string, ModelDerived & { fn: CompiledFn }>;
  /** Derived names in dependency order. */
  order: string[];
  /** [slot, formula] of each derived value, in dependency order. */
  orderSlots: [number, CompiledFn][];
  /** Name -> slot of the SlotEnv: variables, derived values, criteria, then any other name a formula reads. */
  slots: Record<string, number>;
  /** A SlotEnv with every cell NaN (a name not set reads as NaN, as an unset key on the page reads as undefined). */
  blank: SlotEnv;
  opts: CompiledOption[];
  rules: CompiledRule[];
  /** Name -> derived values and criteria that use it directly. */
  users: Record<string, string[]>;
  tl: CompiledTimelineItem[];
}

/**
 * Compile a model: derived values in dependency order, option criteria, rules and timeline formulas, all over one
 * slot layout (SlotEnv). Same closures and arithmetic as the page; only the name lookups are array reads.
 */
export function compileModel(M: ExportedModel): CompiledModel {
  const slots: Record<string, number> = {};
  let size = 0;
  const slot = (name: string) => (name in slots ? slots[name] : (slots[name] = size++));
  M.variables.forEach(v => slot(v.name));
  M.derived.forEach(d => slot(d.name));
  M.criteria.forEach(c => slot(c.name));
  const cmp = (src: string) => compile(src, slot);
  const dmap: CompiledModel['dmap'] = {};
  const fns: Record<string, CompiledFn> = {};
  for (const d of M.derived) dmap[d.name] = { ...d, fn: undefined as unknown as CompiledFn };
  const order: string[] = [], state: Record<string, number> = {};
  const visit = (n: string) => {
    if (state[n] === 2) return;
    if (state[n] === 1) throw new Error('derived cycle at ' + n);
    state[n] = 1;
    fns[n] = fns[n] || cmp(dmap[n].formula);
    dmap[n].fn = fns[n];
    for (const dep of fns[n].names) if (dmap[dep]) visit(dep);
    state[n] = 2;
    order.push(n);
  };
  M.derived.forEach(d => visit(d.name));
  const crit = M.criteria;
  const opts: CompiledOption[] = M.options.map(o => ({ ...o, fns: crit.map((c, k) => cmp(k === 0 ? o.formula : o.metrics[c.name])) }));
  const action = (a: RuleAction | null): CompiledAction | null => {
    if (!a) return null;
    const setFns = Object.entries(a.set || {}).map(([k, v]) => [k, cmp(String(v))] as [string, CompiledFn]);
    return { ...a, setFns, setSlots: setFns.map(([k, f]) => [slot(k), f] as [number, CompiledFn]) };
  };
  const rules: CompiledRule[] = M.rules.map(r => ({
    ...r,
    conds: r.branches.map(b => cmp(b.if)),
    acts: [...r.branches.map(b => b.then), r.else].map(action),
  }));
  const users: Record<string, string[]> = {};
  M.derived.forEach(d => dmap[d.name].fn.names.forEach(n => (users[n] = users[n] || []).push(d.name)));
  opts[0].fns.forEach((f, k) => f.names.forEach(n => (users[n] = users[n] || []).push('criterion ' + crit[k].name)));
  const tl: CompiledTimelineItem[] = M.timeline
    ? M.timeline.items.map(it => ({ ...it, cf: it.condition ? cmp(it.condition) : null, sf: it.show ? cmp(it.show) : null }))
    : [];
  const blank = new Float64Array(size).fill(NaN);
  return { dmap, order, orderSlots: order.map(n => [slots[n], fns[n]] as [number, CompiledFn]), slots, blank, opts, rules, users, tl };
}

/** A copy of env with every derived value, in dependency order. */
export function withDerived(C: CompiledModel, env: SlotEnv): SlotEnv {
  const e = env.slice();
  const os = C.orderSlots;
  for (let i = 0; i < os.length; i++) e[os[i][0]] = os[i][1](e);
  return e;
}
