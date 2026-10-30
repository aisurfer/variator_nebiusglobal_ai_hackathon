// The code of the pages (WP29, NFR7: the first view in under 3 s). The brief is in the entry chunk; every other page,
// the metric and dossier sheets and the libraries only they use (charts, markdown, dialogs: vite.config.ts) come in
// chunks of their own, loaded when they open. `preloadPage` starts a download before that: a link under the pointer,
// the next step of the reviewer tour, the first view of a case. An import is shared: the page that opens finds its
// code loaded or loading.
export const PAGE_CODE = {
  ask: () => import('./kb/KbAsk'),
  sources: () => import('./kb/KbSources'),
  docs: () => import('./kb/DocsPage'),
  actors: () => import('./variator/actors/ActorsPage'),
  cases: () => import('./variator/case/CasesRoute'),
};
/** The metric and dossier sheets over the knowledge-base pages (recharts, react-markdown, the dialog). */
export const SHEETS_CODE = () => import('./kb/KbDossier');

/** The page id of an in-app path: its first segment ('/actors?actor=CL-001' -> 'actors', '/' -> ''). */
export const pageIdOf = (path: string) => path.split(/[?#]/)[0].split('/')[1] ?? '';

/** Loads the sheets once the browser is idle (the brief calls it when its numbers are on screen: a number opens its
 *  sheet at once, and the download does not compete with the first view). Returns the cancel. */
export function preloadSheetsWhenIdle(): () => void {
  const load = () => { SHEETS_CODE().catch(() => undefined); };
  if (typeof window.requestIdleCallback === 'function') { const h = window.requestIdleCallback(load, { timeout: 4000 }); return () => window.cancelIdleCallback(h); }
  const t = window.setTimeout(load, 1500);
  return () => window.clearTimeout(t);
}

/** Starts loading the code of the page of an in-app path; a page in the entry (the brief) has nothing to load. */
export function preloadPage(path: string): void {
  const load = PAGE_CODE[pageIdOf(path) as keyof typeof PAGE_CODE];
  load?.().catch(() => undefined); // a failure shows when the page opens (App: PageBoundary)
}
