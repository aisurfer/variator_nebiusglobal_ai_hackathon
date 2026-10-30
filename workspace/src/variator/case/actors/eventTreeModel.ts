// The week EVENT TREE of a fork (WP18c): a node is the set of scenarios that had the same material events so far; a
// split is the event that set a group apart (with its share); rare events are grouped as «Other events»; each node
// carries weekly bands (p10 / p50 / p90) of cash, load, incidents and ARR, so the scenarios inside a node still vary.
// Paths are scenarios through the tree: the pessimistic, median and optimistic examples (their seeds keep the KB day
// branches valid) and any other listed scenario. Pure: the normalisation and the layout are unit-tested.

export type Band = [number, number, number, number]; // week, p10, p50, p90

export interface TreeSplit {
  week: number; type: string; text: string;
  entityId?: string; actor?: string;
  /** The event happens in different weeks across the scenarios: `week` is its median, these its 10% and 90% weeks. */
  weekP10?: number; weekP90?: number;
  /** «No such event»: the event it is the rest of. */
  of?: string;
  /** «Other events»: the rare events grouped, with their scenario counts. */
  items?: { text: string; n: number }[];
  /** Grouped rare events («Other events»). */
  other?: boolean;
  /** The scenarios of the parent without the event («No such event»). */
  rest?: boolean;
  tone?: 'good' | 'bad' | 'neutral';
}
export interface TreeNodeRaw { id: string; parent: string | null; from: number; to: number; share: number; n?: number; split: TreeSplit | null; bands: Record<string, Band[]>; paths?: string[] }
export interface PathEvent { week: number; type: string; text: string; entityId?: string; actor?: string; material?: boolean }
export interface TreePathRaw {
  id: string; kind?: 'pessimistic' | 'median' | 'optimistic'; scenario?: number; seed?: number; leaf: string; nodes: string[];
  events: PathEvent[]; weeks: Record<string, number>[];
  /** A pinned path: the leaf of a KB day branch (WP19), replayed with its seed; not counted in the shares. */
  fixed?: boolean; treeLeaf?: string;
}
export interface EventTreeRaw { version?: number; unit?: 'week' | 'month'; option: string; label?: string; n?: number; weeks: number; metrics?: string[]; outcome?: string; minShare?: number; nodes: TreeNodeRaw[]; paths?: TreePathRaw[] }

/** True for the event-tree shape (not the older week tree of levels). */
export const isEventTree = (x: unknown): x is EventTreeRaw =>
  !!x && typeof x === 'object' && Array.isArray((x as EventTreeRaw).nodes) && (x as EventTreeRaw).nodes.every(n => typeof n.id === 'string' && 'from' in n && 'to' in n);

// ------------------------------------------------------------------------------------------------ the view
export interface TreeNode extends TreeNodeRaw {
  children: string[];
  depth: number;
  /** Share of the parent's scenarios (1 for the root). */
  ofParent: number;
  tone: 'good' | 'bad' | 'neutral';
  /** «Meridian Freight Group gives notice · w31 · 18%», «No such event · 82%», «All scenarios». */
  label: string;
  /** Paths through the node: the examples and the paths the engine lists for it. */
  paths: string[];
  /** The median outcome (cash, or money in case A) at the horizon: of the node itself when it reaches the horizon,
   *  else of its branches weighted by their share. */
  outcomeEnd: number | null;
}
/** `unit`: a step of the tree is a week (case B) or a month (case A); `tick` is its short mark (w, m). */
export interface EventTreeView { option: string; unit: 'week' | 'month'; tick: string; n: number; weeks: number; metrics: string[]; outcome: string; nodes: TreeNode[]; byId: Map<string, TreeNode>; paths: TreePathRaw[]; root: TreeNode }

const BAD = /notice|leave|resign|quit|incident|outage|shortage|breach|churn|loss|lost|late|overrun|price war|competitor|cut|default|delay/i;
const GOOD = /win|won|signs|signed|deal|renew|expan|hire|joins|upsell|paid|ready|live/i;
const pct = (p: number) => `${p > 0 && p < 0.01 ? '<1' : Math.round(p * 100)}%`;

/** The tone of a split: the engine's, else from its words (a notice or an incident is bad, a won deal good). */
export function splitTone(s: TreeSplit | null): TreeNode['tone'] {
  if (!s || s.rest || s.other) return s?.tone ?? 'neutral';
  if (s.tone) return s.tone;
  return BAD.test(s.text) ? 'bad' : GOOD.test(s.text) ? 'good' : 'neutral';
}

/** A verb in the event's words: «Rhys Llewellyn hands in notice» reads «Without: …», «GPU shortage at …» reads «No …». */
const VERB = /\b(hands|gives|resigns|leaves|wins|won|lost|signs|adds|rotates|cuts|raises|moves|lowers|reduces|falls|passes|joins|is|are|goes|comes|asks|changes|starts|stops|misses)\b/i;
const lowFirst = (t: string) => /^[A-Z][A-Z]/.test(t) || /^[A-Z][a-z]+ [A-Z]/.test(t) ? t : t.charAt(0).toLowerCase() + t.slice(1);

/** What a split is, in words: the event; «No GPU shortage …» or «Without: <the event>» for the rest; «Other events». */
export function splitWhat(s: TreeSplit | null): string {
  if (!s) return 'All scenarios';
  if (s.other) return 'Other events';
  if (!s.rest) return s.text.replace(/\.$/, '');
  if (!s.of) return 'No such event';
  const of = s.of.replace(/\.$/, '');
  return VERB.test(of) || /^(A|An|The) /.test(of) && VERB.test(of) ? `Without: ${of}` : `No ${lowFirst(of.replace(/^(A|An|The) /, ''))}`;
}

/** A short, readable name of an event for a narrow ribbon («GPU shortage», «Overload», «Rhys leaves», «Quillon
 *  leaves», «Big deal won», «SEV1 outage»), derived from its type and words; the full text stays in the tooltip and the
 *  sheet. */
export function shortEvent(text: string, type = ''): string {
  // the subject of an event: a name's first word («Quillon», «Rhys»), never an article («A new client» → «New client»)
  const t = text.replace(/\.$/, '').trim(), first = (x: string) => {
    const w = x.trim().split(/\s+/);
    if (/^(a|an|the)$/i.test(w[0]) && w.length > 1) { const rest = w.slice(1, 3).join(' '); return rest.charAt(0).toUpperCase() + rest.slice(1); }
    return w[0];
  };
  const rules: [RegExp, (m: RegExpExecArray) => string][] = [
    [/GPU shortage/i, () => 'GPU shortage'],
    [/team over capacity/i, () => 'Overload'],
    [/competitor cuts prices/i, () => 'Price cut'],
    [/currency move/i, () => 'Currency move'],
    [/downturn/i, () => 'Downturn'],
    [/^(.+?) hands in notice/i, m => `${first(m[1])} leaves`],
    [/^(.+?) resigns/i, m => `${first(m[1])} leaves`],
    [/^(.+?) gives notice/i, m => `${first(m[1])} leaves`],
    [/big new deal won/i, () => 'Big deal won'],
    [/^deal won/i, () => 'Deal won'],
    [/^deal lost/i, () => 'Deal lost'],
    [/scope creep/i, () => 'Scope creep'],
    [/engineers on board|hires? (are )?on board/i, () => 'Hires on board'],
    [/SEV1/i, () => 'SEV1 outage'],
    [/^(.+?) rotates the team/i, m => `${first(m[1])} rotation`],
    [/over (the )?budget/i, () => 'Over budget'],
    [/go-live target passes/i, () => 'Go-live passes'],
    [/^other events$/i, () => 'Other events'],
  ];
  for (const [re, f] of rules) { const m = re.exec(t); if (m) return f(m); }
  // else the words before a bracket, a colon or a place, at most three
  const head = t.split(/ \(|: | at | in | for /)[0].split(/\s+/).slice(0, 3).join(' ');
  return head || (type ? type.charAt(0).toUpperCase() + type.slice(1) : t);
}

/** The short name of a split: the event's, «No GPU shortage» / «Rhys stays» for the rest, «Other events». */
export function splitShort(s: TreeSplit | null): string {
  if (!s) return 'All scenarios';
  if (s.other) return 'Other events';
  if (!s.rest) return shortEvent(s.text, s.type);
  if (!s.of) return 'No such event';
  const ev = shortEvent(s.of, s.type);
  return / leaves$/.test(ev) ? ev.replace(/ leaves$/, ' stays') : ev === 'Other events' ? 'No other event' : `No ${/^[A-Z]{2}/.test(ev) || /^[A-Z][a-z]+ [A-Z]/.test(ev) ? ev : ev.charAt(0).toLowerCase() + ev.slice(1)}`;
}

/** The week of a split with its spread: «w23 (w5–w46)». */
export function splitWeek(s: TreeSplit, tick = 'w'): string {
  return `${tick}${s.week}${s.weekP10 != null && s.weekP90 != null && s.weekP90 > s.weekP10 ? ` (${tick}${s.weekP10}–${tick}${s.weekP90})` : ''}`;
}

/** The words of a split for people, with its week (or month) and its spread, and its share. */
export function splitLabel(s: TreeSplit | null, share: number, withWeek = true, tick = 'w'): string {
  if (!s) return 'All scenarios';
  return `${splitWhat(s)}${withWeek && !s.rest ? ` · ${splitWeek(s, tick)}` : ''} · ${pct(share)}`;
}

/** A label that fits `room` characters: the words are cut, the share stays («GPU shortage at the clo… · 24%»). */
export function fitLabel(n: TreeNode, room: number, tick = 'w'): string {
  const share = ` · ${pct(n.share)}`, what = splitWhat(n.split), wk = n.split && !n.split.rest ? ` · ${tick}${n.split.week}` : '';
  if (n.label.length <= room) return n.label;
  if ((what + wk + share).length <= room) return what + wk + share;
  const left = room - share.length;
  return left >= 6 ? `${what.slice(0, left - 1)}…${share}` : pct(n.share);
}

/** The median outcome at the horizon of a raw node: its own when it reaches the horizon, else its branches' (by share). */
function outcomeEnds(nodes: TreeNodeRaw[], kids: Map<string, TreeNodeRaw[]>, outcome: string, horizon: number): Map<string, number | null> {
  const out = new Map<string, number | null>();
  const end = (n: TreeNodeRaw): number | null => {
    if (out.has(n.id)) return out.get(n.id)!;
    const ch = kids.get(n.id) ?? [], b = n.bands?.[outcome], last = b?.[b.length - 1];
    let v: number | null;
    if (!ch.length || (last && last[0] >= horizon - 0.5)) v = last?.[2] ?? null;
    else {
      const vals = ch.map(k => [end(k), k.share] as const).filter((x): x is readonly [number, number] => x[0] != null), w = vals.reduce((a, [, sh]) => a + sh, 0);
      v = w > 0 ? vals.reduce((a, [x, sh]) => a + x * sh, 0) / w : null;
    }
    out.set(n.id, v);
    return v;
  };
  for (const n of nodes) end(n);
  return out;
}

export function eventTreeView(raw: EventTreeRaw): EventTreeView | null {
  const nodes = raw.nodes.filter(n => n && typeof n.id === 'string');
  const root0 = nodes.find(n => !n.parent);
  if (!root0) return null;
  const kids = new Map<string, TreeNodeRaw[]>();
  for (const n of nodes) if (n.parent) kids.set(n.parent, [...(kids.get(n.parent) ?? []), n]);
  const paths = raw.paths ?? [];
  const through = new Map<string, string[]>();
  for (const p of paths) for (const id of p.nodes) through.set(id, [...(through.get(id) ?? []), p.id]);
  const unit = raw.unit === 'month' ? 'month' : 'week', tick = unit === 'month' ? 'm' : 'w';
  const outcome = raw.outcome ?? raw.metrics?.[0] ?? Object.keys(root0.bands ?? {})[0] ?? 'cash';
  const ends = outcomeEnds(nodes, kids, outcome, raw.weeks);
  // siblings top to bottom by their outcome at the end: a higher ribbon ends with more cash (money in case A)
  const siblingOrder = (a: TreeNodeRaw, b: TreeNodeRaw) => (ends.get(b.id) ?? -Infinity) - (ends.get(a.id) ?? -Infinity) || b.share - a.share || a.id.localeCompare(b.id);
  const out: TreeNode[] = [], byId = new Map<string, TreeNode>();
  const walk = (n: TreeNodeRaw, depth: number, parentShare: number) => {
    const children = (kids.get(n.id) ?? []).sort(siblingOrder);
    const v: TreeNode = { ...n, children: children.map(c => c.id), depth, ofParent: parentShare > 0 ? n.share / parentShare : 1, tone: splitTone(n.split), label: splitLabel(n.split, n.share, true, tick), paths: [...new Set([...(through.get(n.id) ?? []), ...(n.paths ?? [])])], outcomeEnd: null };
    out.push(v); byId.set(v.id, v);
    for (const c of children) walk(c, depth + 1, n.share);
  };
  walk(root0, 0, root0.share || 1);
  const metrics = raw.metrics?.length ? raw.metrics : Object.keys(root0.bands ?? {});
  // the outcome at the horizon of each node, and the tone: better or worse than all scenarios together by more than
  // 1% (an event branch can end better: more work, a deal won); the engine's tone, when it gives one, wins
  for (const n of out) n.outcomeEnd = ends.get(n.id) ?? null;
  const all = out[0].outcomeEnd;
  for (const n of out) {
    if (n.split?.tone) n.tone = n.split.tone;
    else if (!n.split || n.split.rest || n.split.other) n.tone = 'neutral';
    else if (all != null && n.outcomeEnd != null) { const rel = (n.outcomeEnd - all) / Math.max(1, Math.abs(all)); n.tone = rel > 0.01 ? 'good' : rel < -0.01 ? 'bad' : 'neutral'; }
  }
  return { option: raw.option, unit, tick, n: raw.n ?? 0, weeks: raw.weeks, metrics, outcome, nodes: out, byId, paths, root: byId.get(root0.id)! };
}

// ------------------------------------------------------------------------------------------------ the layout
export interface NodeBox { id: string; x0: number; x1: number; y0: number; y1: number }
export interface TreeLayout { boxes: Map<string, NodeBox>; width: number; height: number; x: (week: number) => number; left: number; right: number; top: number; bottom: number }

/** An icicle over time: a node is a ribbon from its first week to its last; the children of a node share its height by
 *  their share, each at least `minH` tall (the larger ones give way). Nothing overlaps; every path is visible. */
export function layoutEventTree(t: EventTreeView, opts: { width: number; height: number; left?: number; right?: number; top?: number; bottom?: number; minH?: number }): TreeLayout {
  const { width, height } = opts, left = opts.left ?? 8, right = opts.right ?? 8, top = opts.top ?? 8, bottom = opts.bottom ?? 22, minH = opts.minH ?? 4;
  const x = (w: number) => left + (Math.max(0, Math.min(t.weeks, w)) / Math.max(1, t.weeks)) * (width - left - right);
  const boxes = new Map<string, NodeBox>();
  const place = (id: string, y0: number, y1: number) => {
    const n = t.byId.get(id)!;
    boxes.set(id, { id, x0: x(n.from), x1: x(n.to), y0, y1 });
    const kids = n.children.map(c => t.byId.get(c)!);
    if (!kids.length) return;
    const H = y1 - y0, tot = kids.reduce((a, k) => a + Math.max(k.share, 1e-9), 0);
    // heights by share, then lift the small ones to minH and take it from the large ones in proportion
    let h = kids.map(k => Math.max(k.share, 1e-9) / tot * H);
    const floor = Math.min(minH, H / kids.length);
    for (let it = 0; it < 4; it++) {
      const small = h.map(v => v < floor), need = h.reduce((a, v, i) => a + (small[i] ? floor - v : 0), 0);
      if (need <= 1e-9) break;
      const big = h.reduce((a, v, i) => a + (small[i] ? 0 : v - floor), 0);
      h = h.map((v, i) => small[i] ? floor : big > 0 ? v - need * (v - floor) / big : v);
    }
    let y = y0;
    kids.forEach((k, i) => { place(k.id, y, y + h[i]); y += h[i]; });
  };
  place(t.root.id, top, height - bottom);
  return { boxes, width, height, x, left, right, top, bottom };
}

/** The scale of a metric over the whole tree (p10 to p90 of every node), for the bands inside the ribbons. */
export function metricRange(t: EventTreeView, metric: string): [number, number] {
  let lo = Infinity, hi = -Infinity;
  for (const n of t.nodes) for (const [, p10, , p90] of n.bands[metric] ?? []) { lo = Math.min(lo, p10); hi = Math.max(hi, p90); }
  return Number.isFinite(lo) ? (hi > lo ? [lo, hi] : [lo - 1, hi + 1]) : [0, 1];
}

/** The polyline of a path: along each node's ribbon at its middle, with a step at each split week. `labelRoom`: the
 *  height kept free for a split's label at the top of a ribbon tall enough for it and the lines (the paths then run in
 *  the middle of the rest, so they do not cross the label). */
export function pathPoints(t: EventTreeView, L: TreeLayout, p: TreePathRaw, opts: { labelRoom?: number } = {}): [number, number][] {
  const pts: [number, number][] = [], room = opts.labelRoom ?? 0;
  for (const id of p.nodes) {
    const b = L.boxes.get(id);
    if (!b) continue;
    const h = b.y1 - b.y0, labelled = room > 0 && !!t.byId.get(id)?.split && h >= 2 * room + 4;
    const ym = labelled ? b.y0 + room + (h - room) / 2 : (b.y0 + b.y1) / 2;
    if (pts.length) pts.push([b.x0, pts[pts.length - 1][1]]);
    pts.push([b.x0, ym], [b.x1, ym]);
  }
  return pts;
}

/** The boxes a drawn polyline covers, one per segment, `pad` px around its centre line (the stroke and a little air):
 *  the labels keep off them. */
export function lineBoxes(pts: [number, number][], pad = 2): Box[] {
  const out: Box[] = [];
  for (let i = 1; i < pts.length; i++) {
    const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
    if (ax === bx && ay === by) continue;
    out.push([Math.min(ax, bx) - pad, Math.min(ay, by) - pad, Math.max(ax, bx) + pad, Math.max(ay, by) + pad]);
  }
  return out;
}

/** The material events of a path, week by week (the events that make its branch, and the others it lists). */
export function pathWeeks(p: TreePathRaw): { week: number; events: PathEvent[] }[] {
  const by = new Map<number, PathEvent[]>();
  for (const e of p.events) if (e.material !== false) by.set(e.week, [...(by.get(e.week) ?? []), e]);
  return [...by.entries()].sort((a, b) => a[0] - b[0]).map(([week, events]) => ({ week, events }));
}

// ------------------------------------------------------------------------------------------------ the marked paths
const KINDS = ['pessimistic', 'median', 'optimistic'] as const;
const endValue = (p: TreePathRaw, outcome: string) => { const w = p.weeks[p.weeks.length - 1]; return w ? w[outcome] : undefined; };

/** The tree's own pessimistic, median and optimistic paths, by the outcome at the END OF THE HORIZON: the engine's
 *  P/M/O of the tree when it lists them (not the pinned day examples), else the listed paths at the 10%, 50% and 90%
 *  points of their outcome at the end. */
export function examplePaths(t: EventTreeView): TreePathRaw[] {
  const own = t.paths.filter(p => p.kind && !p.fixed);
  if (KINDS.every(k => own.some(p => p.kind === k))) return KINDS.map(k => own.find(p => p.kind === k)!);
  const listed = t.paths.filter(p => !p.fixed && Number.isFinite(endValue(p, t.outcome))).sort((a, b) => endValue(a, t.outcome)! - endValue(b, t.outcome)!);
  if (listed.length < 3) return [];
  const at = (q: number) => listed[Math.min(listed.length - 1, Math.max(0, Math.round(q * (listed.length - 1))))];
  return KINDS.map((k, i) => ({ ...at([0.1, 0.5, 0.9][i]), kind: k }));
}

/** The pinned day examples (WP19's knowledge-base branches): replays outside the shares, ranked on the Day level. */
export const dayPaths = (t: EventTreeView): TreePathRaw[] => t.paths.filter(p => p.fixed && p.kind);

// ------------------------------------------------------------------------------------------------ the labels
/** Width of a label in px at the tree's 10.5px font (a little generous: the browser passes its own measure). */
export const textWidth = (t: string) => t.length * 6.3 + 2;
export type Box = [number, number, number, number]; // x0, y0, x1, y1
export const overlaps = (a: Box, b: Box) => !(a[2] <= b[0] || a[0] >= b[2] || a[3] <= b[1] || a[1] >= b[3]);
/** A text row of a ribbon: 13 px; the box of a text is 12 px tall from the row's top, its baseline 10 px below it. */
export const LABEL_ROW = 13;
export const baselineOf = (b: Box) => b[1] + 10;
/** a before b, number by number. */
const before = (a: number[], b: number[]) => { for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i]; return false; };
export interface PlacedLabel {
  id: string; left: string | null; end: string | null;
  /** The boxes of the two texts: the label starts at its box's x0, the value at the end stops at its box's x1. */
  leftBox: Box | null; endBox: Box | null;
  /** The text sits on a path line (no row of its ribbon is free of one): the line passes under its plate. */
  leftOnLine: boolean; endOnLine: boolean;
}

/** The labels of the ribbons, each inside its own ribbon and never over another text (`taken`: boxes already used,
 *  such as the end marks of the paths), and off the path lines (`lines`, see lineBoxes) where a row of the ribbon allows.
 *  A split reads, the first that fits: its words with the week and the share («GPU shortage at the cloud provider · w23
 *  · 24%»), its short name with them («GPU shortage · w23 · 24%»), the short name and the share, the week and the share,
 *  the share; else nothing (the tooltip and the sheet name it). Words are never cut. The outcome at the end (`ends`: its
 *  texts, the full one first, e.g. «$2.90M», «$2.9M») shows at the right of a final ribbon, in the label's row when
 *  there is room beside it, else in a row of its own under it. Larger ribbons are placed first. In each ribbon the
 *  choice is, in order: a label; the value at the end too; the fuller label; fewer texts on a path line; the value in the
 *  label's row; the fuller value; the rows nearest the top. `measure` and `measureEnd`: the width of a label and of a
 *  value in px; `maxRows`: the rows tried from the top of a ribbon. */
export function placeLabels(t: EventTreeView, L: TreeLayout, ends: Map<string, string | string[]>, opts: { minH?: number; measure?: (s: string) => number; measureEnd?: (s: string) => number; taken?: Box[]; lines?: Box[]; maxRows?: number } = {}): PlacedLabel[] {
  const minH = opts.minH ?? 13, mw = opts.measure ?? textWidth, me = opts.measureEnd ?? ((x: string) => textWidth(x) + 2), lines = opts.lines ?? [], maxRows = opts.maxRows ?? 4;
  const out: PlacedLabel[] = [], boxes: Box[] = [...(opts.taken ?? [])];
  const free = (b: Box) => boxes.every(o => !overlaps(b, o));
  const onLine = (b: Box) => lines.some(o => overlaps(b, o));
  type Opt = { text: string; i: number; box: Box; row: number; line: boolean };
  for (const n of [...t.nodes].sort((a, b) => b.share - a.share)) {
    const box = L.boxes.get(n.id);
    if (!box || !n.split) continue;
    const w = box.x1 - box.x0 - 8, h = box.y1 - box.y0;
    if (h < minH || w < 18) { out.push({ id: n.id, left: null, end: null, leftBox: null, endBox: null, leftOnLine: false, endOnLine: false }); continue; }
    const rows = Array.from({ length: Math.max(1, Math.min(maxRows, Math.floor((h - 1) / LABEL_ROW))) }, (_, k) => box.y0 + 1 + k * LABEL_ROW);
    const what = splitWhat(n.split), short = splitShort(n.split), wk = n.split.rest || n.split.other ? '' : ` · ${t.tick}${n.split.week}`, share = ` · ${pct(n.share)}`;
    const variants = [...new Set([`${what}${wk}${share}`, `${short}${wk}${share}`, `${short}${share}`, wk ? `${t.tick}${n.split.week}${share}` : '', pct(n.share)].filter(Boolean))];
    const endRaw = ends.get(n.id), endTexts = endRaw == null ? [] : Array.isArray(endRaw) ? endRaw : [endRaw];
    const lefts: Opt[] = [], rights: Opt[] = [];
    variants.forEach((v, i) => { if (mw(v) <= w) rows.forEach((y, row) => { const b: Box = [box.x0 + 4, y, box.x0 + 4 + mw(v), y + LABEL_ROW - 1]; if (free(b)) lefts.push({ text: v, i, box: b, row, line: onLine(b) }); }); });
    endTexts.forEach((e, i) => { if (me(e) <= w) rows.forEach((y, row) => { const b: Box = [box.x1 - 4 - me(e), y, box.x1 - 4, y + LABEL_ROW - 1]; if (free(b)) rights.push({ text: e, i, box: b, row, line: onLine(b) }); }); });
    let best: { l: Opt | null; e: Opt | null; key: number[] } | null = null;
    for (const l of [null, ...lefts]) for (const e of [null, ...rights]) {
      // the value beside the label keeps 8 px of air
      if (l && e && overlaps([l.box[0], l.box[1], l.box[2] + 8, l.box[3]], e.box)) continue;
      const key = [l ? 0 : 1, e ? 0 : 1, l ? l.i : 0, Number(!!l?.line) + Number(!!e?.line), l && e && l.row !== e.row ? 1 : 0, e ? e.i : 0, (l?.row ?? 0) + (e?.row ?? 0)];
      if (!best || before(key, best.key)) best = { l, e, key };
    }
    const l = best?.l ?? null, e = best?.e ?? null;
    if (l) boxes.push(l.box);
    if (e) boxes.push(e.box);
    out.push({ id: n.id, left: l?.text ?? null, end: e?.text ?? null, leftBox: l?.box ?? null, endBox: e?.box ?? null, leftOnLine: !!l?.line, endOnLine: !!e?.line });
  }
  return out;
}
