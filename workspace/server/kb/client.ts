// HTTP client of the knowledge-base backend (kb/, FastAPI /api/v1). Every call names the tenant and the principal
// whose ACL applies (X-KB-Principal); the backend never returns what that principal may not see.

export class KbError extends Error {
  constructor(public status: number, message: string, public path = '') { super(message); }
}

export interface KbVersion {
  version_id: string; entity_id: string; seq: number; op: 'create' | 'update' | 'delete'; kind: string; title: string;
  fields: Record<string, unknown>; changed_fields?: string[]; effective_from: string; recorded_at: string;
  superseded_at: string | null; is_current: boolean; actor_id: string | null;
}
/** A declared link that was STALE or BROKEN, with the intervals of the knowledge axis in which it was (/causal/stale/history). */
export interface KbStaleHistoryItem {
  link_id: string; effect_entity_id: string; cause_entity_id: string; predicate: string; predicate_label: string;
  segments: { from: string; to: string | null; status: string; changed_at: string | null; effect_kind: string; effect_title: string; cause_kind: string | null; cause_title: string | null }[];
}
type Query = Record<string, string | number | boolean | string[] | undefined | null>;

export class KbClient {
  constructor(readonly base = process.env.KB_URL ?? 'http://127.0.0.1:8720', readonly timeoutMs = Number(process.env.KB_TIMEOUT_MS ?? 60_000)) {}

  async call<T>(path: string, opts: { tenant: string; principal: string; query?: Query; body?: unknown; timeoutMs?: number }): Promise<T> {
    const url = new URL(`/api/v1${path}`, this.base);
    url.searchParams.set('tenant', opts.tenant);
    for (const [k, v] of Object.entries(opts.query ?? {})) {
      if (v === undefined || v === null) continue;
      if (Array.isArray(v)) v.forEach(x => url.searchParams.append(k, x)); else url.searchParams.set(k, String(v));
    }
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), opts.timeoutMs ?? this.timeoutMs);
    try {
      const res = await fetch(url, {
        method: opts.body === undefined ? 'GET' : 'POST', signal: ctl.signal,
        headers: { 'X-KB-Principal': opts.principal, 'X-KB-Tenant': opts.tenant, ...(opts.body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      });
      const text = await res.text();
      if (!res.ok) throw new KbError(res.status, text.slice(0, 300) || res.statusText, path);
      return (text ? JSON.parse(text) : null) as T;
    } catch (error) {
      if (error instanceof KbError) throw error;
      const aborted = (error as Error)?.name === 'AbortError';
      throw new KbError(aborted ? 504 : 502, aborted ? 'Knowledge base did not answer in time' : `Knowledge base is unreachable: ${(error as Error)?.message ?? error}`, path);
    } finally { clearTimeout(timer); }
  }

  /** Every version of the given kinds, oldest first (the endpoint caps a page at 2000 events). */
  async timeline(tenant: string, principal: string, kinds: string[]): Promise<KbVersion[]> {
    const out: KbVersion[] = [];
    const seen = new Set<string>();
    let start: string | undefined;
    for (;;) {
      const page = await this.call<{ events: KbVersion[] }>('/timeline', { tenant, principal, query: { kinds, limit: 2000, start } });
      const fresh = page.events.filter(e => !seen.has(e.version_id));
      fresh.forEach(e => { seen.add(e.version_id); out.push(e); });
      if (page.events.length < 2000 || !fresh.length) return out;
      start = page.events[page.events.length - 1].effective_from;
    }
  }

  /** Ids of every entity the principal may see (ACL already applied by the backend). */
  async visibleIds(tenant: string, principal: string): Promise<Set<string>> {
    const ids = new Set<string>();
    for (let offset = 0; ; offset += 500) {
      const page = await this.call<{ items: { entity_id: string }[] }>('/entities', { tenant, principal, query: { limit: 500, offset } });
      page.items.forEach(i => ids.add(i.entity_id));
      if (page.items.length < 500) return ids;
    }
  }

  watermarks(tenant: string, principal: string) { return this.call<Record<string, unknown>>('/admin/watermarks', { tenant, principal, timeoutMs: 10_000 }); }
  /** Counts of the journal as known at `knownAt` (the end of the data when omitted). */
  stats(tenant: string, principal: string, knownAt?: string) { return this.call<Record<string, unknown>>('/admin/stats', { tenant, principal, query: { known_at: knownAt }, timeoutMs: 15_000 }); }
  sources(tenant: string, principal: string) { return this.call<unknown>('/sources', { tenant, principal }); }
  stale(tenant: string, principal: string) { return this.call<{ items?: unknown[] } | unknown[]>('/causal/stale', { tenant, principal, query: { limit: 200 } }); }
  /** Stale links with their intervals: what /causal/stale answers on any date (a backend without it: null). */
  async staleHistory(tenant: string, principal: string): Promise<KbStaleHistoryItem[] | null> {
    try { return (await this.call<{ items: KbStaleHistoryItem[] }>('/causal/stale/history', { tenant, principal })).items; }
    catch (error) { if (error instanceof KbError && (error.status === 404 || error.status === 405)) return null; throw error; }
  }
  entity(tenant: string, principal: string, id: string) { return this.call<Record<string, unknown>>(`/entities/${encodeURIComponent(id)}`, { tenant, principal }); }
  /** The version of the entity as recorded at `knownAt` (404 when it was not known yet). */
  entityAt(tenant: string, principal: string, id: string, knownAt: string) { return this.call<{ version: Record<string, unknown> }>(`/entities/${encodeURIComponent(id)}/as-of`, { tenant, principal, query: { known_at: knownAt } }); }
  version(tenant: string, principal: string, versionId: string) { return this.call<Record<string, unknown>>(`/versions/${encodeURIComponent(versionId)}`, { tenant, principal }); }
  statements(tenant: string, principal: string, id: string, knownAt?: string) { return this.call<{ statements: Record<string, unknown>[] }>(`/entities/${encodeURIComponent(id)}/statements`, { tenant, principal, query: { known_at: knownAt } }); }
  /** Versions as known at `knownAt` (all when omitted). */
  history(tenant: string, principal: string, id: string, knownAt?: string) { return this.call<{ versions: Record<string, unknown>[] }>(`/entities/${encodeURIComponent(id)}/history`, { tenant, principal, query: { known_at: knownAt } }); }
  why(tenant: string, principal: string, id: string, knownAt?: string) { return this.call<Record<string, unknown>>(`/causal/why/${encodeURIComponent(id)}`, { tenant, principal, query: { depth: 4, include_inferred: true, known_at: knownAt } }); }
  effects(tenant: string, principal: string, id: string, knownAt?: string) { return this.call<Record<string, unknown>>(`/causal/effects/${encodeURIComponent(id)}`, { tenant, principal, query: { depth: 2, include_inferred: true, known_at: knownAt } }); }
  /** /ask with the reference time of the question (`now`) and the knowledge cut (`known_at`), both ISO instants. */
  ask(tenant: string, principal: string, question: string, mode = 'auto', at: { now?: string; knownAt?: string } = {}) {
    return this.call<Record<string, unknown>>('/ask', { tenant, principal, body: { tenant, question, mode, now: at.now, known_at: at.knownAt }, timeoutMs: 180_000 });
  }
}
