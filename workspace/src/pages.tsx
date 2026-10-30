// The pages of the workspace. A new screen is one entry here: its id is the first path segment (/brief, /ask…), its
// label and icon go to the sidebar group it belongs to, and `render` receives the shell context (company, the user's
// role, view date, navigation helpers). Pages that need more of the URL read it themselves (useLocation).
// The brief is in the entry chunk (the first view of most visits); the other pages load when they open (src/routes.ts)
// and show `fallback` meanwhile: the same box as their own first loading state, so nothing moves when they arrive.
import { lazy, type ReactNode } from 'react';
import { BookOpen, Cable, Gauge, LoaderCircle, MessagesSquare, UsersRound, Waypoints, type LucideIcon } from 'lucide-react';
import type { KbDates, KbOrganization, KbRole, KbSourceStatus } from './kb/types';
import { KbBriefPage } from './kb/KbBrief';
import { CaseCrumbs, CaseTools } from './variator/case/chrome';
import { PAGE_CODE } from './routes';

const KbAskPage = lazy(() => PAGE_CODE.ask().then(m => ({ default: m.KbAskPage })));
const KbSourcesPage = lazy(() => PAGE_CODE.sources().then(m => ({ default: m.KbSourcesPage })));
const DocsPage = lazy(() => PAGE_CODE.docs().then(m => ({ default: m.DocsPage })));
const ActorsPage = lazy(() => PAGE_CODE.actors().then(m => ({ default: m.ActorsPage })));
const CasesRoute = lazy(() => PAGE_CODE.cases().then(m => ({ default: m.CasesRoute })));

export type Patch = Record<string, string | null>;
export interface PageContext {
  org: KbOrganization;
  /** The user: the one role of the MVP, the CEO. */
  role: KbRole;
  /** The view date (URL parameter asOf); undefined: the last day of data. */
  asOf?: string;
  dates: KbDates | null;
  /** The dates of the knowledge base are still loading (dates is null until then). */
  datesLoading: boolean;
  status: { data: KbSourceStatus | null; error: string; loading: boolean; reload: () => void };
  /** The href of `path` (the app's base before it) with the given URL parameters changed (null removes one); other
   *  parameters are kept. For an <a href>; `go` takes the in-app path. */
  link: (path: string, patch?: Patch) => string;
  go: (path: string, patch?: Patch) => void;
  path: string;
  /** Opens the dossier of a knowledge-base entity, or the sheet of a metric, over the current page. */
  openEntity: (id: string) => void;
  openMetric: (id: string) => void;
}
export interface PageDef {
  id: string; label: string; Icon: LucideIcon; render: (c: PageContext) => ReactNode;
  /** Optional top-bar path after the company (default: the page label). */
  crumbs?: (c: PageContext) => ReactNode;
  /** Optional top-bar tools; they replace the view date of the knowledge base. */
  tools?: (c: PageContext) => ReactNode;
  /** What the page shows while its code loads (default: a quiet spinner). */
  fallback?: (c: PageContext) => ReactNode;
}

/** The waiting state of a page whose code is loading: the box of the knowledge-base pages' own loading state. */
export const PageLoading = () => <div className="workspace-state" role="status" aria-label="Loading the page"><LoaderCircle className="spin" size={28} /></div>;

const brief: PageDef = {
  id: 'brief', label: 'Morning brief', Icon: Gauge,
  render: c => <KbBriefPage org={c.org} role={c.role} asOf={c.asOf} defaultAsOf={c.dates?.default} datesLoading={c.datesLoading} onAsOf={d => c.go('/brief', { asOf: d === c.dates?.default ? null : d })} onMetric={id => c.go('/brief', { metric: id, entity: null })} onOpen={c.openEntity} />,
};
const ask: PageDef = {
  id: 'ask', label: 'Ask the knowledge base', Icon: MessagesSquare,
  render: c => <KbAskPage org={c.org} role={c.role} asOf={c.asOf} defaultAsOf={c.dates?.default} onOpen={c.openEntity} />,
};
// S6: the actor cards (people, key clients, vendors, cohorts) and the register of the risks that already fired.
const actors: PageDef = {
  id: 'actors', label: 'Actors and risks', Icon: UsersRound,
  render: c => <ActorsPage ctx={c} />,
  // ActorsPage's own first state («Loading the actors…»)
  fallback: () => <div className="ac-state card" role="status"><h1>Loading the actors…</h1></div>,
};
const sources: PageDef = {
  id: 'sources', label: 'Data sources', Icon: Cable,
  render: c => <KbSourcesPage org={c.org} status={c.status} />,
};

// The end-user guide: what the workspace gives a manager and how to use it.
const docs: PageDef = {
  id: 'docs', label: 'Documentation', Icon: BookOpen,
  render: () => <DocsPage />,
};

// Decisions: the cases list (/cases) and one case page (/cases/:id) with its forks, month, week and day levels.
const cases: PageDef = {
  id: 'cases', label: 'Cases', Icon: Waypoints,
  render: c => <CasesRoute ctx={c} />,
  crumbs: c => <CaseCrumbs onCases={() => c.go('/cases', { metric: null, entity: null })} />,
  tools: () => <CaseTools />,
  // CasesRoute's own first states: a case («Loading the case…») or the list of cases
  fallback: c => c.path.split('/')[2]
    ? <div className="cs-page-state"><LoaderCircle className="spin" size={22} /><h1>Loading the case…</h1></div>
    : <div className="cs-cases"><div className="cs-page-state is-inline"><LoaderCircle className="spin" size={20} /><p>Loading…</p></div></div>,
};

/** Sidebar groups, in order. VARIATOR screens (cases, forks, month, week, day) get their own group. */
export const NAV: { heading: string; pages: PageDef[] }[] = [
  { heading: 'KNOWLEDGE BASE', pages: [brief, actors, ask] },
  { heading: 'DECISIONS', pages: [cases] },
];
/** Pages reached from the bottom of the sidebar, not from a group. */
export const BOTTOM_PAGES: PageDef[] = [sources, docs];
/** The page of `/` and of unknown paths. */
export const DEFAULT_PAGE = brief;
export const PAGES: PageDef[] = [...NAV.flatMap(g => g.pages), ...BOTTOM_PAGES];
