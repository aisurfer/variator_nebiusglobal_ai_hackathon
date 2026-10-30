// WP24: the day file of a case (WP19, schema variator.day/1) and the names of the people, read once per case from the
// VARIATOR API and shared by the Day section through a small store (useSyncExternalStore). The day file is the day
// set's `day_examples.json`, else the month set's `day/day_examples.json`, each asked for only when its set's manifest
// lists it (no 404 for a case without one); the names come from the learned actors (`actors/actors.json`: id, name,
// knowledge-base id). The viewer's chart metrics are kept in localStorage.
import { useEffect, useSyncExternalStore } from 'react';
import type { DayFile } from '../actors/types';
import type { People } from './model';
import { apiUrl } from '../../../appUrl';
import { earlyFetch } from '../../../early';

const API = apiUrl('/api/variator');
const file = (set: string, path: string) => `${API}/artifacts/${encodeURIComponent(set)}/${path.split('/').map(encodeURIComponent).join('/')}`;

// the first view of a case starts the list and the day file early (src/firstView.ts)
async function getJson<T>(url: string): Promise<T | null> {
  const r = await earlyFetch(url, { headers: { Accept: 'application/json' } });
  if (!r.ok) return null;
  return await r.json().catch(() => null) as T | null;
}
const isDayFile = (d: unknown): d is DayFile => !!d && typeof d === 'object' && !!(d as DayFile).dayExamples && typeof (d as DayFile).dayExamples === 'object';

export interface DayRecord { status: 'loading' | 'ready' | 'error'; file: DayFile | null; people: People }
const EMPTY: DayRecord = { status: 'loading', file: null, people: {} };
const records = new Map<string, DayRecord>();
const listeners = new Set<() => void>();
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };
const set = (id: string, patch: Partial<DayRecord>) => { records.set(id, { ...(records.get(id) ?? EMPTY), ...patch }); listeners.forEach(l => l()); };

interface SetEntry { id: string; manifest?: Record<string, unknown>; error?: string }
/** The case a set's manifest names (`case`, `provenance.case`, or an id «future:<case>»). */
const caseOf = (m: Record<string, unknown>) =>
  (m.case as string | undefined) ?? (m.provenance as { case?: string } | undefined)?.case ?? (typeof m.id === 'string' && m.id.includes(':') ? m.id.split(':').slice(1).join(':') : undefined);
/** Whether a manifest lists a file: `files` as a list of {path} (or of paths) or as a map keyed by path. */
export function listsFile(manifest: Record<string, unknown> | undefined, path: string): boolean {
  const f = manifest?.files;
  if (Array.isArray(f)) return f.some(x => (typeof x === 'string' ? x : (x as { path?: string })?.path) === path);
  return !!f && typeof f === 'object' && path in (f as object);
}
/** The sets of a case with this schema, the stack's own name first (`day-<case>`, `future-<case>`). */
function setsOf(sets: SetEntry[], caseId: string, schema: string): SetEntry[] {
  return sets.filter(s => s.manifest && !s.error && s.manifest.schema === schema && caseOf(s.manifest) === caseId)
    .sort((a, b) => Number(/^(future|day)-/.test(b.id)) - Number(/^(future|day)-/.test(a.id)));
}

async function loadDayFile(caseId: string, sets: SetEntry[]): Promise<DayFile | null> {
  const tries: [string, string][] = [
    ...setsOf(sets, caseId, 'variator.day/1').filter(s => listsFile(s.manifest, 'day_examples.json')).map(s => [s.id, 'day_examples.json'] as [string, string]),
    ...setsOf(sets, caseId, 'variator.future/1').filter(s => listsFile(s.manifest, 'day/day_examples.json')).map(s => [s.id, 'day/day_examples.json'] as [string, string]),
  ];
  for (const [set, path] of tries) { const d = await getJson<unknown>(file(set, path)); if (isDayFile(d)) return d; }
  return null;
}
async function loadPeople(sets: SetEntry[]): Promise<People> {
  const set = sets.find(s => s.id === 'actors' && listsFile(s.manifest, 'actors.json')) ?? sets.find(s => s.manifest?.schema === 'variator.actors/1' && listsFile(s.manifest, 'actors.json'));
  if (!set) return {};
  const doc = await getJson<{ actors?: { id: string; kind?: string; name?: string; kb_ids?: string[] }[] }>(file(set.id, 'actors.json')).catch(() => null);
  const out: People = {};
  for (const a of doc?.actors ?? []) if (a.kind === 'person' && a.name) out[a.id] = { name: a.name, entity: a.kb_ids?.[0] };
  return out;
}

function ensure(caseId: string) {
  if (records.has(caseId)) return;
  records.set(caseId, EMPTY);
  // one listing of the artifact sets: a file is asked for only when its set lists it
  const list = getJson<{ sets: SetEntry[] }>(`${API}/artifacts`).then(l => l?.sets ?? []);
  list.then(sets => loadDayFile(caseId, sets)).then(f => set(caseId, { status: 'ready', file: f }), () => set(caseId, { status: 'error', file: null }));
  list.then(loadPeople).then(people => set(caseId, { people }), () => undefined);
}

/** The day file and the people of a case (loaded once per page load). */
export function useDayData(caseId: string): DayRecord {
  useEffect(() => ensure(caseId), [caseId]);
  return useSyncExternalStore(subscribe, () => records.get(caseId) ?? EMPTY, () => EMPTY);
}

/** Test hook: forget every loaded case. */
export function resetDayStore() { records.clear(); listeners.forEach(l => l()); }

// ------------------------------------------------------------------------------------------------ the viewer's metrics
const KEY = (caseId: string) => `variator:day-metrics:${caseId}`;
export function savedMetrics(caseId: string): string[] | null {
  try {
    const v = localStorage.getItem(KEY(caseId));
    const a: unknown = v ? JSON.parse(v) : null;
    return Array.isArray(a) ? a.filter((x): x is string => typeof x === 'string') : null;
  } catch { return null; }
}
export function saveMetrics(caseId: string, ids: string[] | null): void {
  try { if (ids) localStorage.setItem(KEY(caseId), JSON.stringify(ids)); else localStorage.removeItem(KEY(caseId)); } catch { /* storage off: the choice lasts this visit */ }
}
