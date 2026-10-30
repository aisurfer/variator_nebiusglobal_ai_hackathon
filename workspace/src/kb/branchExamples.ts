// Example questions of a simulated future (a knowledge-base branch). WP19 writes a few validated «why» questions per
// branch as examples.json into the day artifact set of the case (schema variator.day/1, manifest `tenants` and
// `files`), read here through the VARIATOR API (/api/variator/artifacts). A branch without examples shows no chips: the
// company's history questions would ask about the wrong world.
import { apiUrl } from '../appUrl';

const API = apiUrl('/api/variator/artifacts');
const MAX = 4;

interface SetEntry { id?: string; manifest?: { schema?: string; tenants?: string[]; files?: { path?: string }[] } }

/** The example files a set may hold for a branch, most specific first. */
export function exampleFiles(set: SetEntry, tenant: string): string[] {
  const m = set.manifest;
  if (!m || !String(m.schema ?? '').startsWith('variator.day/') || !(m.tenants ?? []).includes(tenant)) return [];
  const paths = (m.files ?? []).map(f => String(f.path ?? '')).filter(p => /(^|\/)examples(\.[\w-]+)?\.json$/.test(p));
  const own = paths.filter(p => p.includes(tenant));
  return [...own, ...paths.filter(p => !own.includes(p))];
}

/** The questions of a branch from an examples file, whatever its form: a list of questions or of {question}, an object
 * with `questions` or `examples` (WP19's delivery file: {tenant, checkedAt, examples}), or a map from tenant to either
 * (WP19's day set file: {<tenant>: [{question, …}]}, directly or under `branches`). */
export function examplesOf(doc: unknown, tenant: string): string[] {
  const list = (x: unknown): string[] | null => {
    if (Array.isArray(x)) return x.map(q => (typeof q === 'string' ? q : typeof (q as { question?: unknown })?.question === 'string' ? (q as { question: string }).question : '')).filter(Boolean);
    const inner = x && typeof x === 'object' ? (x as { questions?: unknown; examples?: unknown }).questions ?? (x as { examples?: unknown }).examples : undefined;
    return Array.isArray(inner) ? list(inner) : null;
  };
  if (!doc || typeof doc !== 'object') return [];
  const byTenant = (doc as Record<string, unknown>)[tenant] ?? ((doc as { branches?: Record<string, unknown> }).branches ?? {})[tenant];
  const own = byTenant !== undefined ? list(byTenant) : (doc as { tenant?: unknown }).tenant === undefined || (doc as { tenant?: unknown }).tenant === tenant ? list(doc) : null;
  return [...new Set((own ?? []).map(q => q.trim()).filter(q => q.length > 3))].slice(0, MAX);
}

/** The example questions of a branch, or none. */
export async function loadBranchExamples(tenant: string, get: (url: string) => Promise<unknown> = getJson): Promise<string[]> {
  try {
    const sets = ((await get(API)) as { sets?: SetEntry[] })?.sets ?? [];
    for (const set of sets) {
      for (const file of exampleFiles(set, tenant)) {
        const found = examplesOf(await get(`${API}/${encodeURIComponent(set.id ?? '')}/${file.split('/').map(encodeURIComponent).join('/')}`).catch(() => null), tenant);
        if (found.length) return found;
      }
    }
  } catch { /* no artifacts: no chips */ }
  return [];
}

/** The true place of a branch among the scenarios of its fork at the end of the window: `percentile` (0–100), among
 * how many `scenarios`, on which `date`. */
export interface BranchPosition { percentile: number; scenarios: number | null; date: string | null }

/** The position of a branch from WP19's day file (`dayExamples`: fork -> examples with `tenant`,
 * `percentileAtWindowEnd` and `windowEndFan` {n, date}); null while the file does not give it. */
export function positionOf(doc: unknown, tenant: string): BranchPosition | null {
  const forks = (doc as { dayExamples?: Record<string, unknown> } | null)?.dayExamples;
  if (!forks || typeof forks !== 'object') return null;
  for (const list of Object.values(forks)) {
    for (const e of Array.isArray(list) ? list as Record<string, unknown>[] : []) {
      if (e?.tenant !== tenant) continue;
      const raw = e.percentileAtWindowEnd as number | { percentile?: number; n?: number } | null | undefined;
      const p = typeof raw === 'number' ? raw : raw && typeof raw === 'object' ? raw.percentile : undefined;
      if (typeof p !== 'number' || !Number.isFinite(p)) return null;
      const fan = (e.windowEndFan ?? {}) as { n?: unknown; date?: unknown };
      const n = typeof fan.n === 'number' ? fan.n : typeof raw === 'object' && typeof raw?.n === 'number' ? raw.n : null;
      const date = typeof fan.date === 'string' ? fan.date : typeof e.horizonEnd === 'string' ? e.horizonEnd : null;
      return { percentile: Math.max(0, Math.min(100, p)), scenarios: n && n > 0 ? Math.round(n) : null, date };
    }
  }
  return null;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const ordinal = (n: number) => { const r = n % 100; return `${n}${r >= 11 && r <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`; };
const PLACE = { lowest: 'Lowest', middle: 'Middle', highest: 'Highest' } as const;

/** Where a branch stands, honestly: its true percentile when the day set gives it («≈ 52nd percentile of cash among
 * 1,000 scenarios on 26 Apr 2027»), else only its order among the three examples («Middle of the three example
 * days»); never the `-p50` of its tenant name. */
export function placeText(place: keyof typeof PLACE, pos: BranchPosition | null): string {
  if (!pos) return `${PLACE[place]} of the three example days`;
  const m = pos.date?.match(/^(\d{4})-(\d\d)-(\d\d)/);
  const on = m ? ` on ${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[1]}` : '';
  return `≈ ${ordinal(Math.round(pos.percentile))} percentile of cash${pos.scenarios ? ` among ${pos.scenarios.toLocaleString('en-US')} scenarios` : ''}${on}`;
}

const positions = new Map<string, Promise<BranchPosition | null>>();
/** The position of a branch from the day set that lists its tenant, once per tenant and page load; null without it. */
export function loadBranchPosition(tenant: string, get: (url: string) => Promise<unknown> = getJson): Promise<BranchPosition | null> {
  const hit = positions.get(tenant);
  if (hit && get === getJson) return hit;
  const found = (async () => {
    try {
      const sets = ((await get(API)) as { sets?: SetEntry[] })?.sets ?? [];
      for (const set of sets) {
        const m = set.manifest;
        if (!String(m?.schema ?? '').startsWith('variator.day/') || !(m?.tenants ?? []).includes(tenant)) continue;
        for (const file of (m?.files ?? []).map(f => String(f.path ?? '')).filter(p => /(^|\/)day_examples\.json$/.test(p))) {
          const p = positionOf(await get(`${API}/${encodeURIComponent(set.id ?? '')}/${file.split('/').map(encodeURIComponent).join('/')}`).catch(() => null), tenant);
          if (p) return p;
        }
      }
    } catch { /* no artifacts: the order of the three examples only */ }
    return null;
  })();
  if (get === getJson) positions.set(tenant, found);
  return found;
}

async function getJson(url: string): Promise<unknown> {
  const res = await fetch(url, { signal: AbortSignal.timeout(6000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
