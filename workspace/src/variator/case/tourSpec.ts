// What a case tour (config/cases/<case>.tour.json) may refer to on the case page: the anchors (`data-tour` ids), the
// actions of «Show me» with their arguments, and the template variables of the texts. Pure data and checks, so the unit
// test (tests/tour.test.ts) validates every tour file against its case config without a browser; tour.tsx binds them.

/** The case config fields the checks read (config/cases/<case>.json). */
export interface TourCaseConfig {
  id: string;
  forks: { id: string }[];
  hypotheses: { id: string }[];
  priorityPresets: { id: string }[];
  criteria: { name: string; unit: string }[];
  sections: { id: string }[];
  help: Record<string, unknown>;
  helpKeys?: Record<string, string>;
  timeline?: { items: { name: string; kind: string; month: string | number | null }[] };
  page?: { chain?: string[] };
}

/** The page slot (DOM id and anchor) of a config section, as CasePage renders it. */
export const slotOf = (id: string) => id === 'month-time' ? 'overtime' : id.replace(/^month-/, '');

/** Anchors inside the sections and the header (the section slots are anchors too: `slotOf(section.id)`). */
export const TOUR_ANCHORS: Record<string, string> = {
  'tour-button': 'The «Take the tour» button of the header',
  'origin-button': 'The «Your request» button of the header',
  'origin-modal': 'The origin modal «Where these forks come from»',
  'fork-cards': 'The fork cards (rank and P(best) per fork)',
  'answer-main': 'The decision, the close call or «changes if», and the expected result per criterion',
  'answer-verdict': 'The verdict of the actor simulation at the top of «Answer» (while its ranking is current)',
  'answer-decision': 'The decision card and the close call, equal choices or «changes if» next to it',
  'ranking': 'The ranking table of «Compare»',
  'priorities': 'The priorities panel of «Compare» (presets, weights, caution)',
  'risk-calendar': 'The risk calendar of «Over time»',
  'plan-timeline': 'The plan timeline of «Plan» with P(met)',
  'why-chain': 'The chain of causes of «Why»',
  'week-tree': 'The tree of weekly paths of «Week»',
  'day-examples': 'The three example days of «Day»',
  'decision': 'The «Your decision» button of the sticky steps (warning signs, backup plans, fixes, the commit)',
};

export const sectionAnchors = (cfg: TourCaseConfig) => cfg.sections.map(s => slotOf(s.id));

const items = (cfg: TourCaseConfig, kinds: string[]) => (cfg.timeline?.items ?? []).filter(i => kinds.includes(i.kind));

/** «Show me» actions: id → the check of its arguments against the case config (a list of problems, empty when fine). */
export const TOUR_ACTIONS: Record<string, { what: string; check: (cfg: TourCaseConfig, args: Record<string, unknown>) => string[] }> = {
  'origin.open': { what: 'Opens «Where these forks come from»', check: () => [] },
  'hypotheses.on': {
    what: 'Turns world hypotheses on (args.ids)',
    check: (cfg, a) => !Array.isArray(a.ids) || !a.ids.length ? ['args.ids: a list of hypothesis ids'] : (a.ids as unknown[]).filter(id => !cfg.hypotheses.some(h => h.id === id)).map(id => `unknown hypothesis ${String(id)}`),
  },
  'timeline.move': {
    what: 'Moves a lever or an event on the timeline by args.by months from its default; the plan shows its fork',
    check: (cfg, a) => [
      ...(items(cfg, ['plan', 'event']).some(i => i.name === a.item && typeof i.month === 'string') ? [] : [`args.item: no movable timeline item ${String(a.item)}`]),
      ...(typeof a.by === 'number' && a.by !== 0 ? [] : ['args.by: a number of months, not 0']),
    ],
  },
  'priority.apply': {
    what: 'Applies a priority preset (args.preset); the page reruns on the actors by itself after it (args.rerunActors: kept for older tours, no effect)',
    check: (cfg, a) => [
      ...(cfg.priorityPresets.some(p => p.id === a.preset) ? [] : [`unknown priority preset ${String(a.preset)}`]),
      ...(a.rerunActors === undefined || typeof a.rerunActors === 'boolean' ? [] : ['args.rerunActors: true or false']),
    ],
  },
  'forks.keep': {
    what: 'Keeps the suggested forks for the week level and the leader for the day level',
    check: cfg => ['week-review', 'day-review'].every(id => cfg.sections.some(s => s.id === id)) ? [] : ['the case has no week and day review'],
  },
};

const STATS = ['p5', 'p50', 'p95', 'mean'];
/** Template variables: a name or a pattern, and the check of the ids it names. */
export const TOUR_VARS: { re: RegExp; what: string; check?: (cfg: TourCaseConfig, m: RegExpMatchArray) => string | null }[] = [
  { re: /^(author|requestedOn|frozenOn)$/, what: 'The request: its author, its date, the date of the review' },
  { re: /^drafted\.(forks|hypotheses|criteria)$/, what: 'How many forks, hypotheses, criteria were drafted from the request' },
  { re: /^forkList$/, what: 'The forks of the case, by name' },
  { re: /^(leader|runnerUp)$/, what: 'The leading fork and the runner-up' },
  { re: /^(pBest|feasible)$/, what: 'P(best) and P(feasible) of the leader' },
  { re: /^pBestOf$/, what: 'Which forks P(best) is computed over: «of 5 forks» (all the forks of the ranking)' },
  { re: /^closeCall$/, what: 'After the decision: «, best in N% of the scenarios (of 5 forks)», or «, but it is a close call: <rival> comes out on top in N% of the scenarios (of 5 forks)»' },
  { re: /^answer$/, what: 'The answer, short: of the actor verdict while it is current, else of the formula model; honest about ties (the levers that matter) and close calls' },
  { re: /^levers$/, what: 'The answer with every lever against the decision (for «Know more»)' },
  { re: /^(from|to)$/, what: 'The months of the step\'s timeline move: from its default, to its target' },
  { re: /^examples$/, what: 'How many example days the day level shows for the leader' },
  { re: /^hyp\.(\w+)$/, what: 'A hypothesis by name', check: (cfg, m) => cfg.hypotheses.some(h => h.id === m[1]) ? null : `unknown hypothesis ${m[1]}` },
  { re: /^fork\.(\w+)$/, what: 'A fork by name', check: (cfg, m) => cfg.forks.some(f => f.id === m[1]) ? null : `unknown fork ${m[1]}` },
  { re: /^preset\.(\w+)$/, what: 'A priority preset by name', check: (cfg, m) => cfg.priorityPresets.some(p => p.id === m[1]) ? null : `unknown preset ${m[1]}` },
  { re: /^item\.(\w+)$/, what: 'A lever or an event by name', check: (cfg, m) => items(cfg, ['plan', 'event']).some(i => i.name === m[1]) ? null : `unknown timeline item ${m[1]}` },
  { re: /^milestone\.(\w+)$/, what: 'A milestone or a goal by name', check: (cfg, m) => items(cfg, ['milestone', 'goal']).some(i => i.name === m[1]) ? null : `unknown milestone ${m[1]}` },
  { re: /^crit\.(\w+)$/, what: 'A criterion by name (lower case)', check: (cfg, m) => cfg.criteria.some(c => c.name === m[1]) ? null : `unknown criterion ${m[1]}` },
  { re: /^stat\.(\w+)\.(\w+)$/, what: 'A statistic of a criterion for the leader, formatted', check: (cfg, m) => !cfg.criteria.some(c => c.name === m[1]) ? `unknown criterion ${m[1]}` : STATS.includes(m[2]) ? null : `unknown statistic ${m[2]}` },
  { re: /^month\.(\w+)\.(\w+)$/, what: 'A statistic in months of a criterion for the leader, as a calendar month', check: (cfg, m) => { const c = cfg.criteria.find(x => x.name === m[1]); return !c ? `unknown criterion ${m[1]}` : c.unit !== 'months' ? `criterion ${m[1]} is not in months` : STATS.includes(m[2]) ? null : `unknown statistic ${m[2]}`; } },
  { re: /^pmet\.(\w+)\.(\w+)$/, what: 'P(met) of a milestone or goal for the leader, the fork the Plan shows (plan) or a fork by id', check: (cfg, m) => !items(cfg, ['milestone', 'goal']).some(i => i.name === m[1]) ? `unknown milestone ${m[1]}` : ['leader', 'plan'].includes(m[2]) || cfg.forks.some(f => f.id === m[2]) ? null : `unknown fork ${m[2]}` },
  { re: /^risk\.([1-3])\.(head|month|odds)$/, what: 'A risk to watch of the answer (R1–R3): its consequence, month or odds' },
  { re: /^cal\.top\.(label|month|share)$/, what: 'The risk calendar row of the leader most likely to fire: its name, median month, share' },
  { re: /^chain\.(first|last)$/, what: 'The first and the last link of the chain of causes', check: cfg => (cfg.page?.chain?.length ?? 0) >= 2 ? null : 'the case has no chain' },
];

/** The problems of a template variable name for a case (null when it is known). */
export function checkVar(cfg: TourCaseConfig, name: string): string | null {
  for (const v of TOUR_VARS) { const m = name.match(v.re); if (m) return v.check ? v.check(cfg, m) : null; }
  return `unknown variable {${name}}`;
}

/** Help keys of the page: the config's dictionary plus the page's short names (helpKeys). */
export const helpKeysOf = (cfg: TourCaseConfig) => new Set([...Object.keys(cfg.help ?? {}), ...Object.keys(cfg.helpKeys ?? {})]);
