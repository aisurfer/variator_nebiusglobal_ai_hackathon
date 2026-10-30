// The actor engine's files through the VARIATOR API (variator-api, read-only artifact sets; workspace/server proxies
// /api/variator/*): the month set of a case is the set whose manifest says `schema: variator.future/1` and names the
// case (`future-<case>` in the stack), WP19's day set says `schema: variator.day/1`. A day file may also sit inside the
// month set (`day/day_examples.json`). The rerun posts the page state to WP18's month route.
// The first view of a case starts the list, the month file and the day file early (src/firstView.ts, ./sets.ts).
import { isActorMonth } from './adapt';
import type { ActorMonth, DayFile, MonthRequest } from './types';
import { apiUrl } from '../../../appUrl';
import { earlyFetch } from '../../../early';
import { ARTIFACTS_URL, caseSets, dayFileUrls, setFileUrl, type CaseSets, type SetEntry } from './sets';

export { listedFiles, type CaseSets } from './sets';

const API = apiUrl('/api/variator');

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T | null> {
  const r = await earlyFetch(url, { signal, headers: { Accept: 'application/json' } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return await r.json() as T;
}

/** The sets of a case: the month set and the day set (ids and listed files), from the list of artifact sets. */
export async function findSets(caseId: string, signal?: AbortSignal): Promise<CaseSets> {
  return caseSets(await getJson<{ sets: SetEntry[] }>(ARTIFACTS_URL, signal), caseId);
}
/** A file is asked for only when its set's manifest lists it (no 404 in the console for a set without it). */
const has = (files: Set<string> | null, path: string) => !!files?.has(path);

const file = setFileUrl;

export interface LoadedActors { month: ActorMonth; day: DayFile | null; set: string }

/** The month file of a case and its day file; null when the API has no actor set for the case. */
export async function loadActors(caseId: string, signal?: AbortSignal): Promise<LoadedActors | null> {
  const sets = await findSets(caseId, signal);
  if (!sets.month) return null;
  const month = await getJson<unknown>(file(sets.month, 'month.json'), signal);
  if (!isActorMonth(month)) return null;
  // a month file without trees (an older export): the week tree file of the set
  if ((!month.weekTrees || !Object.keys(month.weekTrees).length) && has(sets.monthFiles, 'week-tree.json')) {
    const tree = await getJson<{ weekTrees?: ActorMonth['weekTrees']; dayExamples?: ActorMonth['dayExamples'] }>(file(sets.month, 'week-tree.json'), signal).catch(() => null);
    if (tree?.weekTrees) { month.weekTrees = tree.weekTrees; month.dayExamples = month.dayExamples && Object.keys(month.dayExamples).length ? month.dayExamples : tree.dayExamples; }
  }
  const dayDoc = async (): Promise<DayFile | null> => {
    const ok = (d: DayFile | null) => d && d.dayExamples && Object.keys(d.dayExamples).length ? d : null;
    for (const url of dayFileUrls(sets)) { const d = ok(await getJson<DayFile>(url, signal).catch(() => null)); if (d) return d; }
    return null;
  };
  return { month, day: await dayDoc(), set: sets.month };
}

/** The month route of WP18: POST, the page state in, a month file out. */
export const monthRoute = (caseId: string) => `${API}/cases/${encodeURIComponent(caseId)}/month`;

/** Whether the API has the month route (a GET answers 405 when only POST exists, 404 when the route is missing). */
export async function probeRerun(caseId: string, signal?: AbortSignal): Promise<boolean> {
  try {
    const r = await fetch(monthRoute(caseId), { method: 'GET', signal, headers: { Accept: 'application/json' } });
    return r.status !== 404 && r.status !== 502 && r.status !== 504;
  } catch { return false; }
}

/** A rerun on actors: the month file for the page state, and the time it took (ms, measured in the browser). */
export async function rerunMonth(caseId: string, body: MonthRequest, signal?: AbortSignal): Promise<{ month: ActorMonth; ms: number }> {
  const t0 = performance.now();
  const r = await fetch(monthRoute(caseId), { method: 'POST', signal, headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) });
  const text = await r.text();
  let doc: unknown = null;
  try { doc = JSON.parse(text); } catch { /* not JSON */ }
  if (!r.ok) {
    const e = (doc as { error?: string | { message?: string }; detail?: string } | null);
    throw new Error((typeof e?.error === 'string' ? e.error : e?.error?.message) ?? e?.detail ?? `HTTP ${r.status}`);
  }
  const month = (doc as { month?: unknown } | null)?.month ?? doc;
  if (!isActorMonth(month)) throw new Error('The answer is not a month file of the actor engine');
  return { month, ms: performance.now() - t0 };
}
