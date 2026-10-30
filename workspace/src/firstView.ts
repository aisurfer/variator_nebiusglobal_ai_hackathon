// The first view of a visit (WP29, NFR7: the first view in under 3 s), started before the first render (main.tsx).
// Every page needs the company, its dates and the state of the sources (the shell); then, by page:
//   the brief: the list of artifact sets and the brief file of the catalogue set of the view date;
//   a case: the code of the case page, the case's files, the list of artifact sets, the month file and the day file;
//   the actors: the code of the page, the list of artifact sets and the card files of the view date.
// The server has already asked for the requests it can name (server/preload.ts); asking again here takes those answers
// (src/early.ts), and the pages read every answer through `earlyFetch`. A guess that turns out wrong costs one unused
// request: the page asks for what it needs itself, as before.
import { apiUrl } from './appUrl';
import { prefetch } from './early';
import { datesPath, kbUrl, statusPath } from './kb/api';
import { API as ARTIFACT_SETS, briefFileUrl, catalogueSets, pickSet } from './kb/catalogue';
import { PAGE_CODE, pageIdOf } from './routes';
import { cardFileUrl } from './variator/actors/api';
import { cardSets, pickCardSet } from './variator/actors/model';
import { caseSets, dayFileUrls, setFileUrl } from './variator/case/actors/sets';

/** The pages that read the simulated future of the URL parameter `branch` (App.tsx BRANCH_PAGES). */
const BRANCH_PAGES = new Set(['brief', 'ask', 'sources']);
const OTHER_PAGES = new Set(['ask', 'sources', 'actors', 'cases']);
const quiet = (p: Promise<unknown>) => { p.catch(() => undefined); };

/** Starts the requests of the first view of the in-app path `path` with the query `search`. `readCase` loads the files
 *  of a case (src/variator/case/model/cases.ts; main.tsx passes it: that module needs Vite). */
export function startFirstView(path: string, search: string, { readCase }: { readCase?: (id: string) => Promise<unknown> } = {}): void {
  const params = new URLSearchParams(search);
  const segment = pageIdOf(path);
  const page = OTHER_PAGES.has(segment) ? segment : 'brief'; // the brief is the page of `/` and of unknown paths
  const asOf = params.get('asOf') ?? undefined;
  // a simulated future is checked against the company's branches first: its reads wait for the shell
  const future = BRANCH_PAGES.has(page) && !!params.get('branch');

  const orgs = prefetch(apiUrl('/api/kb/organizations'), { peek: true }) as Promise<{ id?: string }[] | null>;
  const dates = future ? Promise.resolve(null) : orgs.then(list => {
    const org = list?.[0]?.id;
    if (!org) return null;
    prefetch(kbUrl(statusPath(org, asOf)));
    return prefetch(kbUrl(datesPath(org)), { peek: true }) as Promise<{ default?: string } | null>;
  });
  const viewDate = asOf ? Promise.resolve(asOf) : dates.then(d => d?.default ?? null);

  if (page === 'brief' && !future) {
    const list = prefetch(ARTIFACT_SETS, { shared: true, peek: true });
    quiet(Promise.all([list, viewDate]).then(([body, date]) => {
      const set = pickSet(catalogueSets(body), date);
      if (set) prefetch(briefFileUrl(set));
    }));
  } else if (page === 'actors') {
    quiet(PAGE_CODE.actors());
    const list = prefetch(ARTIFACT_SETS, { shared: true, peek: true });
    quiet(Promise.all([list, viewDate]).then(([body, date]) => {
      const set = pickCardSet(cardSets(body), date);
      if (set) for (const file of ['cards.json', 'risks.json']) prefetch(cardFileUrl(set.id, file));
    }));
  } else if (page === 'cases') {
    quiet(PAGE_CODE.cases());
    const id = path.split(/[?#]/)[0].split('/')[2];
    if (!id) return;
    const caseId = decodeURIComponent(id);
    if (readCase) quiet(readCase(caseId));
    // the month file is the largest answer of the page: it waits for the code and the case's files (low priority)
    quiet(prefetch(ARTIFACT_SETS, { shared: true, peek: true }).then(body => {
      const sets = caseSets(body as Parameters<typeof caseSets>[0], caseId);
      if (sets.month) prefetch(setFileUrl(sets.month, 'month.json'), { priority: 'low' });
      const day = dayFileUrls(sets)[0];
      if (day) prefetch(day, { shared: true, priority: 'low' }); // the actors' loader and the Day section both read it
    }));
  }
}
