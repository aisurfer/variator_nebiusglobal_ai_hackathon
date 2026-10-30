// The case files of the page: config/cases/index.json (the list of cases), config/cases/<id>.json (a case config) and
// its preview file. Vite bundles every JSON file of the folder as its own chunk, so a case loads only when it opens and
// a new case is a new file plus a line in the index (no code change). The contract is engine/types.ts.
import type { CaseConfig as EngineConfig, CaseIndexEntry, CasesIndex, PreviewFuture } from '../../engine';
import type { IntakeConfig } from '../intake/types';
import type { TourDef } from '../../tour/types';

const FILES = import.meta.glob<unknown>('../../../../config/cases/*.json', { import: 'default' });
const DIR = '../../../../config/cases/';

/** config/cases/<id>.origin.json (hand-written, the exporter leaves it alone): the request the forks were drafted from. */
export interface CaseOriginFile {
  schema: 'variator-case-origin/1';
  caseId: string;
  /** The situation in the words of the author (the CEO), first person. */
  request: string;
  /** The clarifying questions of the intake and the author's answers. */
  qa: { slot: string; q: string; a: string }[];
  /** Person id (HR id) of the author; `authorName` and `authorTitle` name a person the case config does not list. */
  author: string; authorName?: string; authorTitle?: string;
  /** "forkDate" (the case's fork date T) or an ISO date. */
  requestedOn: string; frozenOn: string;
  /** What the system drafted from the request: fork, hypothesis and criterion ids of the case config. */
  drafted: { forks: string[]; hypotheses: string[]; criteria: string[] };
  /** What the author changed in the review, one line each. */
  reviewEdits: string[];
}

export interface CaseBundle { entry: CaseIndexEntry; config: EngineConfig; preview: PreviewFuture | null; origin: CaseOriginFile | null }

async function readJson<T>(name: string): Promise<T | null> {
  const load = FILES[`${DIR}${name}`];
  return load ? (await load()) as T : null;
}

/** The cases in index order. */
export async function readIndex(): Promise<CaseIndexEntry[]> {
  const index = await readJson<CasesIndex>('index.json');
  if (!index || !Array.isArray(index.cases)) throw new Error('The list of cases (config/cases/index.json) is missing.');
  return index.cases;
}

/** The origin of a case (null when the case has none). */
export const readOrigin = (id: string) => readJson<CaseOriginFile>(`${id}.origin.json`);
/** The guided tour of a case (config/cases/<id>.tour.json; null when the case has none). */
export const readTour = (id: string) => readJson<TourDef>(`${id}.tour.json`);
/** The intake chat of the cases page. */
export async function readIntake(): Promise<IntakeConfig> {
  const f = await readJson<IntakeConfig>('intake.json');
  if (!f) throw new Error('The intake of new cases (config/cases/intake.json) is missing.');
  return f;
}

const bundles = new Map<string, Promise<CaseBundle>>();

/** The first view of a case (src/firstView.ts): its files under their usual names (<id>.json, .preview, .origin,
 *  .tour) start loading with the index, not after it; readCase then takes them as the index names them. */
export function preloadCase(id: string): Promise<CaseBundle> {
  for (const name of [`${id}.json`, `${id}.preview.json`, `${id}.origin.json`, `${id}.tour.json`]) FILES[`${DIR}${name}`]?.().catch(() => undefined);
  return readCase(id);
}

/** The config and the preview of a case of the index (cached). */
export function readCase(id: string): Promise<CaseBundle> {
  let p = bundles.get(id);
  if (!p) {
    p = (async () => {
      const entry = (await readIndex()).find(e => e.id === id);
      if (!entry) throw new Error(`There is no case «${id}» in config/cases/index.json.`);
      if (!entry.config) throw new Error(`The case «${entry.title}» is still in preparation.`);
      // the three files of the case at once (WP29: one round trip instead of three)
      const [config, preview, origin] = await Promise.all([
        readJson<EngineConfig>(entry.config), entry.preview ? readJson<PreviewFuture>(entry.preview) : Promise.resolve(null), readOrigin(id),
      ]);
      if (!config) throw new Error(`The config of the case «${entry.title}» (config/cases/${entry.config}) is missing.`);
      return { entry, config, preview, origin };
    })();
    p.catch(() => bundles.delete(id));
    bundles.set(id, p);
  }
  return p;
}
