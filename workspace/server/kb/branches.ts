// Simulated futures of the cases as knowledge-base branches (WP19). Each example future of a case fork is loaded as
// its own tenant, `<company tenant>-<case letter>-<fork>-p<10|50|90>` (variator.orgsim.company.forward.pipeline
// tenant_of). The knowledge base has no list of its tenants: the candidates come from the case files
// (config/cases/index.json and each case config: its letter, forks and fork date), and the knowledge base says which
// of them are loaded (its watermarks; an unknown tenant is a 404). A branch is listed once every projection has caught
// up with its journal, so a link to it never opens a half-loaded future.
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { KbBranch, KbOrganization } from '../../src/kb/types.js';
import type { KbClient } from './client.js';

export const DEFAULT_CASES_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../config/cases');
/** The three example futures of a fork, by their tenant suffix (WP19's naming), their kind and their order by cash at
 * the end among the three. The suffix is not a percentile: the three examples can sit close together among the
 * 1,000 scenarios of the fork (their true place is `percentileAtWindowEnd` of the day set). */
export const BRANCH_KINDS: [number, KbBranch['kind'], KbBranch['place']][] = [[10, 'pessimistic', 'lowest'], [50, 'median', 'middle'], [90, 'optimistic', 'highest']];
const PROJECTIONS = ['graph', 'search', 'descriptors'];
const TTL_MS = 30_000;

interface CaseIndex { cases?: { id?: string; letter?: string; config?: string }[] }
interface CaseConfig { id?: string; letter?: string; forkDate?: string; forks?: { id: string; label?: string }[] }

/** Every branch tenant the case files allow for the company, with its label («Reduce tech debt · middle example»). */
export function branchCandidates(o: KbOrganization, casesDir = process.env.CASES_DIR ?? DEFAULT_CASES_DIR): KbBranch[] {
  const read = <T,>(file: string): T | null => { try { return JSON.parse(readFileSync(join(casesDir, file), 'utf8')) as T; } catch { return null; } };
  const out: KbBranch[] = [];
  for (const entry of read<CaseIndex>('index.json')?.cases ?? []) {
    const cfg = entry.config ? read<CaseConfig>(entry.config) : null;
    const letter = (cfg?.letter ?? entry.letter ?? '').toLowerCase();
    if (!cfg || !letter || !cfg.forkDate) continue;
    for (const f of cfg.forks ?? []) {
      for (const [suffix, kind, place] of BRANCH_KINDS) {
        out.push({ tenant: `${o.tenant}-${letter}-${f.id}-p${suffix}`, caseId: cfg.id ?? entry.id ?? '', fork: f.id, kind, place,
          label: `${f.label ?? f.id} · ${place} example`, from: cfg.forkDate });
      }
    }
  }
  return out;
}

/** Loaded and fully processed: a journal, and graph, search and descriptors caught up with it. */
export function branchReady(w: Record<string, unknown>): boolean {
  const journal = w.journal;
  return typeof journal === 'number' && journal > 0 && PROJECTIONS.every(k => typeof w[k] === 'number' && (w[k] as number) >= journal);
}

/** The loaded branches of a company, probed in the knowledge base and kept for 30 s. */
export class BranchFinder {
  private cache = new Map<string, { at: number; list: Promise<KbBranch[]> }>();
  constructor(readonly client: KbClient, readonly principal: string, readonly casesDir?: string, readonly ttlMs = TTL_MS) {}

  /** The listed branches; after the 30 s the known list is served at once while a new probe runs in the background
   * (a branch appears one probe later, and the probe never delays a page). */
  list(o: KbOrganization): Promise<KbBranch[]> {
    const hit = this.cache.get(o.tenant);
    if (hit && (Date.now() - hit.at < this.ttlMs || this.probing.has(o.tenant))) return hit.list;
    const probe = this.probe(o);
    if (!hit) {
      this.cache.set(o.tenant, { at: Date.now(), list: probe });
      return probe;
    }
    this.probing.add(o.tenant);
    probe.then(l => { this.cache.set(o.tenant, { at: Date.now(), list: Promise.resolve(l) }); }, () => undefined)
      .finally(() => this.probing.delete(o.tenant));
    return hit.list;
  }
  private probing = new Set<string>();
  private probe(o: KbOrganization): Promise<KbBranch[]> {
    const candidates = branchCandidates(o, this.casesDir);
    return Promise.all(candidates.map(b => this.client.watermarks(b.tenant, this.principal).then(branchReady, () => false)))
      .then(ready => candidates.filter((_, i) => ready[i]));
  }
}
