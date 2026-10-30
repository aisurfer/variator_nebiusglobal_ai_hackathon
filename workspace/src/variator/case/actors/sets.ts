// The artifact sets of a case on the VARIATOR API (read by ./load.ts): the month set (manifest `schema:
// variator.future/1`, naming the case; `future-<case>` in the stack) and WP19's day set (`schema: variator.day/1`), and
// the URLs of their files. No imports beyond the app's base: the first view of a case (src/firstView.ts) asks for the
// month file with these before the code of the case page has arrived.
import { apiUrl } from '../../../appUrl';

const API = apiUrl('/api/variator');
/** The list of artifact sets. */
export const ARTIFACTS_URL = `${API}/artifacts`;

export interface SetEntry { id: string; manifest?: Record<string, unknown>; error?: string }
export interface CaseSets { month: string | null; day: string | null; monthFiles: Set<string> | null; dayFiles: Set<string> | null }

const caseOf = (m: Record<string, unknown>) =>
  (m.case as string | undefined) ?? (m.provenance as { case?: string } | undefined)?.case ?? (typeof m.id === 'string' && m.id.startsWith('future:') ? m.id.slice(7) : undefined);

/** The files a set's manifest lists (`files` as {path: sha}, [{path}] or [path]); null when it lists none. */
export function listedFiles(manifest: Record<string, unknown> | undefined): Set<string> | null {
  const f = manifest?.files;
  if (Array.isArray(f)) return new Set(f.map(x => typeof x === 'string' ? x : (x as { path?: string })?.path).filter((x): x is string => !!x));
  if (f && typeof f === 'object') return new Set(Object.keys(f));
  return null;
}

/** The month set and the day set of a case (ids and listed files) in a list of artifact sets. */
export function caseSets(list: { sets?: SetEntry[] } | null, caseId: string): CaseSets {
  const sets = (list?.sets ?? []).filter(s => s.manifest && !s.error);
  const pick = (schema: string) => sets
    .filter(s => s.manifest!.schema === schema && caseOf(s.manifest!) === caseId)
    // the stack's name first (future-<case>, day-<case>), then any other set of the case
    .sort((a, b) => Number(/^(future|day)-/.test(b.id)) - Number(/^(future|day)-/.test(a.id)))[0] ?? null;
  const m = pick('variator.future/1'), d = pick('variator.day/1');
  return { month: m?.id ?? null, day: d?.id ?? null, monthFiles: listedFiles(m?.manifest), dayFiles: listedFiles(d?.manifest) };
}

/** The URL of a file of a set. */
export const setFileUrl = (set: string, path: string) => `${API}/artifacts/${encodeURIComponent(set)}/${path.split('/').map(encodeURIComponent).join('/')}`;

/** The day files of a case to try, in order: the day set's `day_examples.json`, then the month set's
 *  `day/day_examples.json`; each only when its set's manifest lists it (no 404 for a case without one). */
export function dayFileUrls(sets: CaseSets): string[] {
  const out: string[] = [];
  if (sets.day && sets.dayFiles?.has('day_examples.json')) out.push(setFileUrl(sets.day, 'day_examples.json'));
  if (sets.month && sets.monthFiles?.has('day/day_examples.json')) out.push(setFileUrl(sets.month, 'day/day_examples.json'));
  return out;
}
