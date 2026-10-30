// Knowledge-base integration API (/api/kb/*): the CEO brief, metric sheets, dossiers, questions and source status of
// the company, read live from the knowledge base (kb/, FastAPI /api/v1). Numbers are computed here by formula.
// The MVP has one user role, the CEO: the `role` parameter is optional and defaults to it.
import express, { Router, type Request, type Response } from 'express';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { KbAskAnswer, KbAskPlan, KbBranch, KbBrief, KbDates, KbDossier, KbDossierGroup, KbDossierLink, KbDossierRelated, KbMetricResult, KbOrganization, KbRole, KbSourceStatus, KbWhy, KbWhyNode } from '../../src/kb/types.js';
import { BranchFinder } from './branches.js';
import { KbClient, KbError } from './client.js';
import { BRIEF, briefMetricsFor, endOfDay, evaluate, localDate, metricById, weeks } from './metrics.js';
import { formatMoney, normalizeOrganizations, northStarOf, publicOrg, dateText } from './org.js';
import { SnapshotCache, type Snapshot } from './snapshot.js';

const DEFAULT_COMPANY = resolve(dirname(fileURLToPath(import.meta.url)), '../../config/company.json');
const SERVICE_PRINCIPAL = process.env.KB_SERVICE_PRINCIPAL ?? 'user:admin';
const BRIEFS_MAX = 64;
const STATUS_TTL_MS = 60_000;
const STATUS_DOWN_TTL_MS = 10_000;
const isoDate = /^\d{4}-\d{2}-\d{2}$/;
const PEOPLE: Record<string, string> = { author: 'Author', addressed_to: 'To', cc_to: 'Copy', participant: 'Participant', assignee: 'Assignee', reporter: 'Reporter', owner: 'Owner' };
const FACTS: Record<string, string[]> = {
  Email: ['sent_at', 'from', 'to', 'cc'], Task: ['key', 'status', 'priority', 'assignee', 'reporter', 'created'], Bug: ['key', 'status', 'priority', 'assignee', 'reporter', 'created'],
  Epic: ['key', 'status', 'assignee', 'created'], Story: ['key', 'status', 'assignee', 'created'], MergeRequest: ['reference', 'state', 'author', 'created', 'merged_at'],
  Meeting: ['held_at', 'space', 'note_taker'], Decision: ['statement', 'state', 'decided_at'], ActionItem: ['assignee', 'state', 'due'],
  Invoice: ['number', 'doc_date', 'amount', 'counterparty'], Act: ['number', 'doc_date', 'amount', 'counterparty'], Contract: ['number', 'doc_date', 'amount', 'counterparty'],
  Amendment: ['number', 'doc_date', 'counterparty'], CommercialProposal: ['number', 'doc_date', 'amount', 'counterparty'], Claim: ['number', 'doc_date', 'amount', 'counterparty'],
  Person: ['role', 'department', 'email'], Organization: ['kind', 'domain', 'description'],
  Incident: ['severity', 'status', 'component', 'started_at', 'client_name', 'owner_name'], Deal: ['status', 'amount', 'client_name', 'expected_close', 'owner_name'],
};
class HttpError extends Error { constructor(public status: number, message: string) { super(message); } }

/** The company (config/company.json, or the file named by COMPANY_FILE): one company object, or a list. */
export function loadKbOrganizations(path = process.env.COMPANY_FILE ?? DEFAULT_COMPANY): KbOrganization[] {
  try { return normalizeOrganizations(JSON.parse(readFileSync(path, 'utf8'))); } catch { return []; }
}

export function createKbRouter(options: { client?: KbClient; organizations?: KbOrganization[]; casesDir?: string; branches?: BranchFinder } = {}) {
  const client = options.client ?? new KbClient();
  const orgs = options.organizations ?? loadKbOrganizations();
  const cache = new SnapshotCache(client);
  const finder = options.branches ?? new BranchFinder(client, SERVICE_PRINCIPAL, options.casesDir);
  const router = Router();
  router.use(express.json({ limit: '256kb' }));

  // the briefs already built (tenant, date, role, journal position), the most recent last
  const briefs = new Map<string, KbBrief>();
  // the state of the sources per tenant and date
  const statuses = new Map<string, { at: number; value: KbSourceStatus | null; pending: Promise<KbSourceStatus> | null }>();

  /** The company of a request, or one of its simulated futures with `?branch=<tenant>` (every route): a branch must be
   * listed (loaded and processed), and its requests read its own tenant. One object per branch keeps the caches. */
  const branchOrgs = new Map<string, KbOrganization>();
  const org = async (req: Request): Promise<KbOrganization> => {
    const o = orgs.find(x => x.id === req.params.org);
    if (!o) throw new HttpError(404, 'Company not found');
    const asked = req.query.branch;
    if (asked === undefined || asked === '') return o;
    const list = typeof asked === 'string' ? await finder.list(o) : [];
    const b = list.find(x => x.tenant === asked);
    if (!b) throw new HttpError(400, `Unknown branch; loaded: ${list.map(x => x.tenant).join(', ') || 'none'}`);
    let out = branchOrgs.get(b.tenant);
    if (!out) {
      const { dataEnd: _end, branches: _list, ...rest } = o;
      out = { ...rest, tenant: b.tenant, branch: b };
      branchOrgs.set(b.tenant, out);
    }
    return out;
  };
  /** The role of a request. `role` is optional: the MVP has one role (the CEO), the first of the company file. An old
   * request with `role=ceo` still works; an unknown value is refused. */
  const role = (o: KbOrganization, value: unknown): KbRole => {
    const r = value === undefined || value === '' ? o.roles[0] : o.roles.find(x => x.id === value);
    if (!r) throw new HttpError(400, `Unknown role; allowed: ${o.roles.map(x => x.id).join(', ')}`);
    return r;
  };
  const asOfParam = (s: Snapshot, o: KbOrganization, value: unknown): string => {
    const d = datesOf(s, o);
    if (value === undefined || value === '') return d.default;
    if (typeof value !== 'string' || !isoDate.test(value) || value < d.start || value > d.max) throw new HttpError(400, `asOf must be a date from ${d.start} to ${d.max}`);
    return value;
  };
  /** The view date as an instant: the end of that local day (what was known by then). */
  const viewInstant = (asOf: string, o: KbOrganization) => new Date(endOfDay(asOf, o.timezone)).toISOString();
  /** The knowledge cut of the view date: the end of that day, but never past the last recorded version — later the
   * state of the base does not change, and the base refuses a cut after its data. `now` of a question stays the end of
   * the chosen day, so «last week» on a day after the data honestly finds nothing. */
  const knownAtOf = (asOf: string, o: KbOrganization, s: Snapshot | null) => {
    const k = endOfDay(asOf, o.timezone);
    return new Date(s && s.recordedEnd > 0 ? Math.min(k, s.recordedEnd) : k).toISOString();
  };
  const route = (fn: (req: Request, res: Response) => Promise<unknown>) => async (req: Request, res: Response) => {
    try { res.json(await fn(req, res)); } catch (error) {
      const status = error instanceof HttpError ? error.status : error instanceof KbError ? (error.status === 404 ? 404 : 502) : 500;
      if (status >= 500) console.error('[kb]', error instanceof Error ? error.message : error);
      if (status === 502) statuses.clear(); // the base failed: the next status is read again, not served from the cache
      res.status(status).json({ error: error instanceof Error ? error.message : 'Knowledge base request failed' });
    }
  };

  // the company with its loaded simulated futures; a knowledge base that is slow to answer delays the list by 3 s at most
  router.get('/organizations', route(async () => Promise.all(orgs.map(async o => {
    const branches = await Promise.race([finder.list(o), new Promise<KbBranch[]>(r => setTimeout(() => r([]), 3000).unref())]);
    return { ...o, branches };
  }))));

  router.get('/:org/dates', route(async req => {
    const o = await org(req);
    return datesOf(await cache.current(o), o);
  }));

  // the state of the sources: three knowledge-base calls, kept 60 s per tenant and date (10 s when unreachable); an
  // older answer is served at once while a new one is read in the background
  router.get('/:org/status', route(async req => {
    const o = await org(req);
    const key = `${o.tenant}|${typeof req.query.asOf === 'string' ? req.query.asOf : ''}`;
    let entry = statuses.get(key);
    if (!entry) {
      entry = { at: 0, value: null, pending: null };
      statuses.set(key, entry);
    }
    const e = entry;
    const ttl = e.value?.reachable === false ? STATUS_DOWN_TTL_MS : STATUS_TTL_MS;
    if (e.value && Date.now() - e.at < ttl) return e.value;
    if (!e.pending) {
      e.pending = readStatus(o, req.query.asOf)
        .then(v => { e.value = v; e.at = Date.now(); return v; })
        .finally(() => { e.pending = null; });
      e.pending.catch(() => { if (!e.value) statuses.delete(key); });
    }
    return e.value ?? e.pending; // an older answer at once while the new one is read
  }));

  async function readStatus(o: KbOrganization, asOfValue: unknown): Promise<KbSourceStatus> {
    const out: KbSourceStatus = { tenant: o.tenant, reachable: false, watermark: null, projections: {}, entitiesByKind: {}, sources: [], causal: {}, asOf: null, causalAsOf: null };
    try {
      // record counts and link statuses on the view date, counted by the knowledge base as known then
      const snap = await cache.current(o).catch(() => null);
      const asOf = snap ? asOfParam(snap, o, asOfValue) : null;
      const knownAt = snap && asOf ? knownAtOf(asOf, o, snap) : undefined;
      const [w, st, src] = await Promise.all([client.watermarks(o.tenant, SERVICE_PRINCIPAL), client.stats(o.tenant, SERVICE_PRINCIPAL, knownAt), client.sources(o.tenant, SERVICE_PRINCIPAL)]);
      out.reachable = true;
      out.watermark = typeof w.journal === 'number' ? w.journal : null;
      out.projections = Object.fromEntries(Object.entries(w).filter(([, v]) => typeof v === 'number')) as Record<string, number>;
      out.entitiesByKind = (st.entities_by_kind ?? {}) as Record<string, number>;
      out.causal = (st.causal_links_by_status ?? {}) as Record<string, number>;
      out.asOf = asOf;
      out.causalAsOf = asOf;
      out.sources = (((src as { items?: unknown[] })?.items ?? []) as Record<string, string>[]).map(x => ({ id: x.source_id, provider: x.provider, name: x.name }));
    } catch (error) {
      if (error instanceof HttpError) throw error; // a bad asOf is the caller's error, not an unreachable base
      out.error = error instanceof Error ? error.message : String(error);
    }
    return out;
  }

  router.get('/:org/brief', route(async req => {
    const o = await org(req), r = role(o, req.query.role);
    const s = await cache.current(o);
    const asOf = asOfParam(s, o, req.query.asOf);
    // a brief of a tenant, a date and a role never changes while the journal stays where it is
    const key = `${o.tenant}|${asOf}|${r.id}|${s.watermark}|${s.builtAt}`;
    const hit = briefs.get(key);
    if (hit) { briefs.delete(key); briefs.set(key, hit); return hit; }
    const visible = await cache.visibleFor(o, r.principal, s.watermark);
    // numbers as known at the end of the date (the as-of rule, README.md); range and weeks span the whole data (navigation)
    const metrics = briefMetricsFor(r.id, o).map(m => evaluate(m, s, asOf, o.timezone, visible, 8, r.metadata, r.principal));
    const ids = new Set(metrics.map(m => m.id));
    const brief: KbBrief = {
      // a simulated future starts at its fork date: no week before it is offered
      organization: publicOrg(o), role: r, asOf, range: { start: datesOf(s, o).start, end: localDate(s.end, o.timezone) },
      weeks: weeks(s, localDate(s.end, o.timezone), o.timezone, 16).filter(w => w >= datesOf(s, o).start), headline: headline(o, metrics), metrics,
      top: BRIEF.top.filter(id => ids.has(id)), areas: BRIEF.areas.map(a => ({ ...a, metrics: a.metrics.filter(id => ids.has(id)) })),
      builtAt: s.builtAt, watermark: s.watermark, ...(o.branch ? { branch: o.branch } : {}),
    };
    briefs.set(key, brief);
    while (briefs.size > BRIEFS_MAX) briefs.delete(briefs.keys().next().value!);
    return brief;
  }));

  // Any built-in metric opens (there are no access rights); the CEO brief shows all of them.
  router.get('/:org/metrics/:id', route(async req => {
    const o = await org(req), r = role(o, req.query.role);
    const m = metricById(String(req.params.id), o);
    if (!m) throw new HttpError(404, 'Metric not found');
    const s = await cache.current(o);
    const asOf = asOfParam(s, o, req.query.asOf);
    const visible = await cache.visibleFor(o, r.principal, s.watermark);
    return evaluate(m, s, asOf, o.timezone, visible, 200, r.metadata, r.principal);
  }));

  router.get('/:org/entity/:id', route(async req => {
    const o = await org(req), r = role(o, req.query.role);
    const id = String(req.params.id);
    const s = await cache.current(o).catch(() => null);
    const asOf = s && req.query.asOf ? asOfParam(s, o, req.query.asOf) : null;
    const k = asOf ? knownAtOf(asOf, o, s) : undefined;
    const [entity, history, why, effects, allVersions] = await Promise.all([
      client.entity(o.tenant, r.principal, id), client.history(o.tenant, r.principal, id, k),
      client.why(o.tenant, r.principal, id, k).catch(() => null), client.effects(o.tenant, r.principal, id, k).catch(() => null),
      k ? client.history(o.tenant, r.principal, id).catch(() => null) : Promise.resolve(null),
    ]);
    if (k) {
      // the dossier on the view date: the version recorded by then and the relations open then
      const at = await client.entityAt(o.tenant, r.principal, id, k).catch((e: unknown) => { if (e instanceof KbError && e.status === 404) return null; throw e; });
      if (!at || (history.versions ?? []).length === 0) throw new HttpError(404, `Not known yet on ${asOf}: the first version was recorded later`);
      const st = await client.statements(o.tenant, r.principal, id, k).catch(() => ({ statements: [] }));
      const current = (entity.head ?? {}) as Record<string, unknown>;
      const then = at.version as Record<string, unknown>;
      // the text of the version known then (the current one when it did not change since)
      const body = then.version_id === current.version_id ? current.body
        : (await client.version(o.tenant, r.principal, String(then.version_id)).catch(() => null))?.body ?? '';
      entity.head = { ...then, body };
      entity.statements = st.statements;
    }
    const out = dossier(entity, history, why, effects, s, k);
    out.since = await sinceOf(out, ((entity.head ?? {}) as { fields?: Record<string, unknown> }).fields ?? {}, cid => client.entity(o.tenant, r.principal, cid));
    out.asOf = asOf;
    out.versionsLater = allVersions ? Math.max(0, (allVersions.versions ?? []).length - (history.versions ?? []).length) : 0;
    return out;
  }));

  router.post('/:org/ask', route(async req => {
    const o = await org(req), r = role(o, req.body?.role);
    const question = typeof req.body?.question === 'string' ? req.body.question.trim().slice(0, 2000) : '';
    if (!question) throw new HttpError(400, 'question is required');
    const started = Date.now();
    const snap0 = await cache.current(o).catch(() => null);
    // one view date for every page: the question is asked «now» = the end of that day, from what was known then
    const asOf = snap0 ? asOfParam(snap0, o, req.body?.asOf) : localDate(Date.now(), o.timezone);
    const at = viewInstant(asOf, o), knownAt = knownAtOf(asOf, o, snap0);
    const res = await client.ask(o.tenant, r.principal, question, req.body?.mode === 'deterministic' ? 'deterministic' : 'auto', { now: at, knownAt });
    const cites = (res.citations ?? []) as Record<string, unknown>[];
    const evidence = (res.evidence ?? []) as Record<string, unknown>[];
    const titles = new Map(evidence.filter(e => e.version_id).map(e => [String(e.version_id), e]));
    const snap = await cache.current(o).catch(() => null);
    const answer: KbAskAnswer = {
      answer: String(res.answer ?? ''), mode: String(res.answer_mode ?? ''), intent: res.intent ? String(res.intent) : null,
      citations: cites.map(c => {
        const vid = String(c.version_id ?? c.id ?? c), id = vid.split('@')[0];
        const e = (titles.get(vid) ?? c) as Record<string, unknown>, item = snap?.items.get(id);
        return { versionId: vid, id, title: String(e.title ?? item?.title ?? vid), kind: String(e.kind ?? item?.kind ?? '') };
      }).filter(c => c.id),
      unknowns: ((res.unknowns ?? []) as unknown[]).map(String), latencyMs: Date.now() - started,
      asOf, now: at, knownAt, plan: planOf(res.plan), ...listOf(res), why: whyOf(res.results),
    };
    return answer;
  }));

  router.get('/:org/search', route(async req => {
    const o = await org(req), r = role(o, req.query.role);
    const q = String(req.query.q ?? '').trim().slice(0, 300);
    if (!q) return { query: q, hits: [] };
    const res = await client.call<{ hits: Record<string, unknown>[] }>('/search', { tenant: o.tenant, principal: r.principal, query: { q, limit: 20 } });
    return { query: q, hits: res.hits.map(h => ({ id: h.entity_id, kind: h.kind, title: h.title, snippet: h.snippet, versionId: h.version_id })) };
  }));
  return router;
}

/** The view dates of the company: data from the first to the last day (or the configured `dataEnd`); any date
 * up to today may be chosen, so a question about «the last week» can be asked for the real calendar too.
 * A simulated future runs after T: from its fork date to the last simulated day, never past it. */
function datesOf(s: Snapshot, o: KbOrganization): KbDates {
  if (o.branch) {
    const last = localDate(s.end, o.timezone);
    const first = localDate(s.start, o.timezone);
    const start = o.branch.from > first && o.branch.from <= last ? o.branch.from : first;
    return { start, end: last, max: last, default: last, timezone: o.timezone };
  }
  const start = localDate(s.start, o.timezone);
  const end = o.dataEnd && isoDate.test(o.dataEnd) ? o.dataEnd : localDate(s.end, o.timezone);
  const today = localDate(Date.now(), o.timezone);
  return { start, end, max: today > end ? today : end, default: end, timezone: o.timezone };
}

/** The plan of /ask for people: what was looked for, the entities it was matched to, relations and the window. */
function planOf(raw: unknown): KbAskPlan | null {
  if (!raw || typeof raw !== 'object') return null;
  const p = raw as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const nodes = (Array.isArray(p.nodes) ? p.nodes : []) as Record<string, any>[]; // eslint-disable-line @typescript-eslint/no-explicit-any
  const name = (id: string) => {
    if (id === 'x') return (p.kinds ?? []).join(', ') || 'result';
    const n = nodes.find(x => x.id === id);
    if (!n) return id;
    if (n.group) return n.group === 'internal' ? 'staff only' : 'external people';
    return n.entities?.[0]?.label ?? n.text ?? n.key ?? (n.kinds ?? []).join(', ');
  };
  const t = (p.time ?? {}) as Record<string, string | null>;
  return {
    planner: String(p.planner ?? ''), operation: String(p.operation ?? ''), kinds: (p.kinds ?? []).map(String), understood: String(p.understood ?? ''), text: String(p.text ?? ''),
    entities: nodes.flatMap(n => (n.entities ?? []).slice(0, 1).map((e: Record<string, string>) => ({ text: String(n.text ?? n.key ?? ''), id: e.id, label: e.label, kind: e.kind }))),
    relations: ((p.edges ?? []) as Record<string, any>[]).map(e => ({ from: name(e.src), predicate: String(e.predicate), to: name(e.dst), negate: Boolean(e.negate) })), // eslint-disable-line @typescript-eslint/no-explicit-any
    time: { start: t.start ?? null, end: t.end ?? null, event: String(t.event ?? 'occurred'), knownAt: t.known_at ?? null, interpretation: t.interpretation ?? null },
    notes: (p.notes ?? []).map(String),
  };
}
/** The «why» result of the knowledge base for people: the subject, up to three declared chains (effect first, each
 * step with the relation that leads to it) and the root causes, by entity id, title, kind label, date and actor. */
export function whyOf(results: unknown): KbWhy | null {
  const w = (results as { why?: Record<string, any> } | null)?.why; // eslint-disable-line @typescript-eslint/no-explicit-any
  if (!w || !w.target) return null;
  const nodes = (w.nodes ?? {}) as Record<string, Record<string, any>>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const node = (vid: string, via: string | null = null): KbWhyNode => {
    const n = nodes[vid] ?? (w.target.version_id === vid ? w.target : {});
    const fields = (n.key_fields ?? {}) as Record<string, unknown>;
    return { id: String(n.entity_id ?? vid.split('@')[0]), title: String(n.title ?? vid.split('@')[0]), kind: String(n.kind_label ?? n.kind ?? '').toLowerCase(),
      date: (n.effective_from ?? null) as string | null, actor: (n.actor ?? null) as string | null, via,
      statement: typeof fields.statement === 'string' ? fields.statement : null };
  };
  // the specific causes first (a decision, a life event, an incident), the standing processes and policies of the
  // company after them («Opening balances», «Net 30 payment terms» are true but explain little); then longer chains
  const chains = ((w.chains ?? []) as Record<string, any>[]) // eslint-disable-line @typescript-eslint/no-explicit-any
    .filter(c => Array.isArray(c.versions) && c.versions.length > 1)
    .map(c => (c.versions as string[]).map((v, i) => node(v, i ? String(c.steps?.[i - 1]?.predicate_label ?? c.steps?.[i - 1]?.predicate ?? '') || null : null)))
    .map((c, i) => ({ c, i, standing: STANDING.has(c[c.length - 1].kind) }))
    .sort((a, b) => Number(a.standing) - Number(b.standing) || b.c.length - a.c.length || a.i - b.i)
    .slice(0, 3).map(x => x.c);
  const roots = ((w.roots ?? []) as string[]).map(v => node(v));
  const specific = roots.filter(r => !STANDING.has(r.kind));
  return { subject: node(String(w.target.version_id ?? '')), chains, roots: specific.length ? specific : roots };
}
/** Since when a client or vendor works with the company: the earliest start of its contracts (start date, else the
 * signing date), else its earliest related record. The record of the organisation itself is only the day the directory
 * was loaded («Created 26 Oct 2025» for a client since April 2025). Null for other objects. */
export async function sinceOf(d: Pick<KbDossier, 'kind' | 'related'>, fields: Record<string, unknown>, entity: (id: string) => Promise<Record<string, unknown>>): Promise<KbDossier['since']> {
  const role = d.kind === 'Organization' ? String(fields.kind ?? '') : '';
  const label = role === 'client' ? 'Client since' : role === 'vendor' ? 'Vendor since' : null;
  if (!label) return null;
  const day = (v: unknown) => (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v) ? v.slice(0, 10) : null);
  const contracts = d.related.filter(g => g.kind === 'Contract').flatMap(g => g.items).slice(0, 4);
  const starts = await Promise.all(contracts.map(c => entity(c.id).then(e => {
    const f = ((e.head ?? {}) as { fields?: Record<string, unknown> }).fields ?? {};
    return day(f.start_date) ?? day(f.signed_at) ?? day(f.doc_date);
  }, () => null)));
  const dates = starts.filter((x): x is string => !!x);
  if (!dates.length) dates.push(...d.related.flatMap(g => g.items).map(i => day(i.date)).filter((x): x is string => !!x));
  return dates.length ? { label, date: dates.sort()[0] } : null;
}
/** Kinds of the standing rules and processes of a company: causes of everything, so the last to show. */
const STANDING = new Set(['process', 'policy', 'rule', 'habit']);

function listOf(res: Record<string, unknown>): { total: number | null; shown: number | null } {
  const l = ((res.results ?? {}) as Record<string, unknown>).list as { total?: number; shown?: number } | undefined;
  return { total: l?.total ?? null, shown: l?.shown ?? null };
}

/** The CEO headline: the North Star, then one sentence per area of the brief (money, clients, delivery). A clause is
 * left out when its metric is not in the brief. */
function headline(o: KbOrganization, ms: KbMetricResult[]): string[] {
  // an unknown number (no records of its kind here, such as mail in a simulated future) leaves its clause out
  const v = (id: string) => ms.find(m => m.id === id && m.value !== null);
  const n = (x: number | null | undefined, one: string, many: string) => `${x ?? 0} ${x === 1 ? one : many}`;
  const money = (x: number | null | undefined) => formatMoney(x, o);
  const line = (label: string, parts: (string | false | null | undefined)[]) => {
    const kept = parts.filter((p): p is string => !!p);
    return kept.length ? `${label}: ${kept.join('; ')}.` : null;
  };
  const star = northStarOf(o);
  const out: (string | null)[] = [];
  // a simulated future says so first: its numbers are never real history
  if (o.branch) out.push(`Simulated future: ${o.branch.label}, simulated from ${dateText(o.branch.from)}. Not real history.`);
  const ns = v('company.northStar');
  if (star && ns?.value != null) out.push(`North Star: ${star.headline.replace('{value}', String(ns.value))}${ns.target != null && star.by ? ` (goal ${ns.target} by ${dateText(star.by, false)})` : ''}${ns.previous != null ? `, ${ns.value - ns.previous >= 0 ? '+' : '−'}${Math.abs(ns.value - ns.previous)} vs the week before` : ''}.`);
  const inv = v('money.invoiced'), pay = v('money.payables'), prop = v('deals.proposals');
  out.push(line('Money', [
    inv && `invoiced ${money(inv.value)} to clients${pay ? ` and received ${money(pay.value)} of supplier bills` : ''} in 30 days`,
    prop && `open proposals worth ${money(prop.value)}`,
  ]));
  const awaiting = v('clients.awaiting'), esc = v('clients.escalations'), silent = v('deals.silent');
  const oldest = awaiting?.items[0]?.facts['working days'];
  out.push(line('Clients', [
    awaiting && `${n(awaiting.value, 'client letter waits', 'client letters wait')} for a reply${oldest ? ` (oldest ${oldest} working days)` : ''}`,
    esc && `${n(esc.value, 'escalation', 'escalations')} in 14 days`,
    silent && `${n(silent.value, 'prospect has', 'prospects have')} gone quiet for over a week`,
  ]));
  const team = v('team.activity'), done = v('eng.throughput'), bugs = v('eng.bugs'), stale = v('decisions.staleWork');
  out.push(line('Delivery', [
    team?.value != null && `team activity index ${team.value} of 100${team.baseline != null ? ` against a baseline of ${team.baseline}` : ''}, ${n(team.itemsTotal, 'person needs', 'people need')} attention`,
    done && `${n(done.value, 'ticket', 'tickets')} done this week`,
    bugs && `${n(bugs.value, 'high-priority bug', 'high-priority bugs')} open`,
    // a simulated future has no grounds that changed after the fact: its records were written once, by the simulation
    stale && !o.branch && `${n(stale.value, 'item rests', 'items rest')} on grounds that have since changed`,
  ]));
  return out.filter((x): x is string => !!x);
}

/** A declared cause of a simulated history quotes only «<cause type>: <id>» («process: CTR-0102», «habit:
 * confluence.page-2000014»). Such a quote names an object, not words of a document: when it is the linked object, the
 * quote goes (its title is shown); else the quote is the title of the object it names, or its bare id. */
// the cause types of variator.orgsim.events.CAUSE_TYPES
const CAUSE_QUOTE = /^(decision|hypothesis|life_event|habit|rule|process):\s*([\w.:-]+)$/;
export function linkQuote(raw: unknown, otherId: string, s: Snapshot | null): string | null {
  if (typeof raw !== 'string' || !raw.trim()) return null;
  const m = CAUSE_QUOTE.exec(raw.trim());
  if (!m) return raw;
  const ids = [m[2], `sim.${m[2]}`];
  if (ids.includes(otherId)) return null;
  return ids.map(id => s?.items.get(id)?.title).find(Boolean) ?? m[2];
}

// The related objects of a dossier, grouped by relation and kind («Assigned: tasks», «On call for: incidents»).
const VERB: Record<string, string> = {
  assignee: 'Assigned', author: 'Authored', owner: 'Owns', reporter: 'Reported', participant: 'Took part in', addressed_to: 'Mail received',
  cc_to: 'In copy', employee: 'Employee records', mentions: 'Mentioned in', works_at: 'People', counterparty: 'Records', client: 'Records', vendor: 'Records',
};
const ORDER = ['assignee', 'author', 'owner', 'reporter', 'participant', 'addressed_to', 'cc_to', 'works_at', 'counterparty', 'client', 'vendor', 'employee', 'mentions'];
const PLURAL: Record<string, string> = {
  Task: 'tasks', Bug: 'bugs', Story: 'stories', Epic: 'epics', ActionItem: 'action items', MergeRequest: 'merge requests', Meeting: 'meetings', Email: 'letters',
  Incident: 'incidents', Invoice: 'invoices', SupportTicket: 'support tickets', BankEntry: 'bank entries', Payment: 'payments', Contract: 'contracts', Amendment: 'amendments',
  NpsResponse: 'NPS answers', Person: 'people', Deal: 'deals', Document: 'documents', Report: 'reports', Decision: 'decisions', LifeEvent: 'events', Deploy: 'deploys',
  Requisition: 'requisitions', Organization: 'organisations', EmployeeEvent: 'employee events', Process: 'processes', CommercialProposal: 'proposals',
};
const GROUP_ITEMS = 50;
const plural = (kind: string) => PLURAL[kind] ?? `${kind.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()}s`;
// a relation and kind that read as one label
const WHOLE: Record<string, string> = {
  'participant|Meeting': 'Meetings', 'author|Email': 'Mail sent', 'addressed_to|Email': 'Mail received', 'cc_to|Email': 'Mail in copy', 'works_at|Person': 'People',
};
function labelOf(predicate: string, kind: string): string {
  const whole = WHOLE[`${predicate}|${kind}`];
  if (whole) return whole;
  // the records of a client or vendor read as their kind: «Support tickets», «Invoices»
  if (['counterparty', 'client', 'vendor'].includes(predicate)) return `${plural(kind)[0].toUpperCase()}${plural(kind).slice(1)}`;
  const words = predicate.replace(/_/g, ' ');
  const verb = predicate === 'assignee' && kind === 'Incident' ? 'On call for' : VERB[predicate] ?? `${words[0]?.toUpperCase() ?? ''}${words.slice(1)}`;
  return `${verb}: ${plural(kind)}`;
}

/** Statements about an entity (it is their object), as its related objects: one group per relation and kind, each
 * object once (its newest statement), newest first, at most 50 per group with the full count; the entity's own
 * statements to other objects (a client of an incident, a deploy it relates to) form the group «Linked to». */
export function relatedGroups(statements: Record<string, unknown>[], self: string, s: Snapshot | null, knownAt?: string): KbDossierGroup[] {
  const groups = new Map<string, { predicate: string; kind: string; label: string; items: Map<string, KbDossierRelated> }>();
  const add = (key: string, predicate: string, kind: string, label: string, item: KbDossierRelated) => {
    const g = groups.get(key) ?? groups.set(key, { predicate, kind, label, items: new Map() }).get(key)!;
    const old = g.items.get(item.id);
    if (!old || String(item.date ?? '') > String(old.date ?? '')) g.items.set(item.id, item);
  };
  const statusOf = (id: string): string | null => {
    const item = s?.items.get(id);
    if (!item) return null;
    const v = s!.at(item, knownAt ? Date.parse(knownAt) : Number.MAX_SAFE_INTEGER);
    return s!.field(v, 'status') ?? s!.field(v, 'state');
  };
  for (const x of statements) {
    const predicate = String(x.predicate_id ?? '');
    const date = (x.valid_from ?? null) as string | null;
    if (x.object_id === self && x.subject_id && x.subject_id !== self) {
      const kind = String(x.subject_kind ?? '');
      const id = String(x.subject_id);
      add(`${predicate}|${kind}`, predicate, kind, labelOf(predicate, kind),
        { id, title: String(x.subject_title ?? id), kind, date, status: statusOf(id), via: predicate.replace(/_/g, ' ') });
    } else if (x.subject_id === self && x.object_id && x.object_id !== self && x.object_kind !== 'Person') {
      const id = String(x.object_id), kind = String(x.object_kind ?? '');
      add('out', 'out', kind, 'Linked to', { id, title: String(x.object_title ?? id), kind, date, status: statusOf(id), via: predicate.replace(/_/g, ' ') });
    }
  }
  const rank = (p: string) => { const i = ORDER.indexOf(p); return p === 'out' ? ORDER.length + 1 : i < 0 ? ORDER.length : i; };
  return [...groups.entries()]
    .map(([key, g]) => {
      const items = [...g.items.values()].sort((a, b) => String(b.date ?? '').localeCompare(String(a.date ?? '')));
      return { key, label: g.label, kind: g.kind, total: items.length, items: items.slice(0, GROUP_ITEMS), rank: rank(g.predicate) };
    })
    .sort((a, b) => a.rank - b.rank || b.total - a.total)
    .map(({ rank: _r, ...g }) => g);
}

function dossier(entity: Record<string, unknown>, history: { versions: Record<string, unknown>[] }, why: Record<string, unknown> | null, effects: Record<string, unknown> | null, s: Snapshot | null, knownAt?: string): KbDossier {
  const head = (entity.head ?? {}) as Record<string, unknown>;
  const fields = (head.fields ?? {}) as Record<string, unknown>;
  const kind = String(entity.kind ?? '');
  const show = (v: unknown) => Array.isArray(v) ? v.join(', ') : String(v);
  const facts = (FACTS[kind] ?? []).filter(k => fields[k] !== undefined && fields[k] !== null && fields[k] !== '' && !(Array.isArray(fields[k]) && !(fields[k] as unknown[]).length))
    .map(k => ({ label: k.replace(/_/g, ' '), value: k === 'counterparty' ? s?.orgs.get(String(fields[k]))?.name ?? show(fields[k]) : show(fields[k]) }));
  // statements of this entity (it is the subject): its people; statements about it (it is the object): the objects that
  // name it — a person's tasks, incidents and meetings, a client's invoices — never the entity itself again
  const statements = ((entity.statements ?? []) as Record<string, unknown>[]).filter(x => !x.retracted_at);
  const self = String(entity.entity_id);
  const seenPeople = new Set<string>();
  const people = statements.filter(x => x.subject_id === self && PEOPLE[String(x.predicate_id)] && x.object_title)
    .map(x => ({ role: PEOPLE[String(x.predicate_id)], name: String(x.object_title) }))
    .filter(p => { const k = `${p.role}|${p.name}`; if (seenPeople.has(k)) return false; seenPeople.add(k); return true; });
  const related = relatedGroups(statements, self, s, knownAt);
  const name = (actor: unknown): string | null => actor ? s?.persons.get(String(actor))?.name ?? String(actor) : null;
  const links = (res: Record<string, unknown> | null, side: 'cause' | 'effect'): KbDossierLink[] => {
    if (!res) return [];
    const nodes = (res.nodes ?? {}) as Record<string, Record<string, unknown>>;
    const seen = new Set<string>();
    return ((res.links ?? []) as Record<string, unknown>[]).filter(l => (side === 'cause' ? l.effect_entity_id === entity.entity_id : l.cause_entity_id === entity.entity_id)).flatMap(l => {
      const vid = String(side === 'cause' ? l.cause_version : l.effect_version);
      const n = nodes[vid] ?? {};
      const otherId = String(side === 'cause' ? l.cause_entity_id : l.effect_entity_id);
      if (seen.has(otherId)) return [];
      seen.add(otherId);
      const ev = (l.evidence ?? {}) as Record<string, unknown>;
      return [{ linkId: String(l.link_id), predicate: String(l.predicate), label: String(l.predicate_label ?? l.predicate), inferred: Boolean(l.inferred), status: String(l.status ?? ''), quote: linkQuote(ev.quote, otherId, s),
        other: { id: otherId, title: String(n.title ?? otherId), kind: String(n.kind ?? ''), date: (n.effective_from ?? null) as string | null } }];
    });
  };
  const versions = history.versions ?? [];
  return {
    id: String(entity.entity_id), kind, title: String(entity.title ?? ''), url: (entity.url ?? null) as string | null, source: (entity.source_id ?? null) as string | null,
    created: (versions[0]?.effective_from ?? null) as string | null, updated: (head.effective_from ?? null) as string | null, facts, people,
    excerpt: String(head.body ?? '').slice(0, 2400),
    history: versions.map(v => ({ seq: Number(v.seq), at: String(v.effective_from), actor: name(v.actor_id), changed: (Array.isArray(v.changed_fields) ? v.changed_fields : []).map(String).filter(f => f !== 'body') })).reverse(),
    why: links(why, 'cause'), effects: links(effects, 'effect'), related,
    chains: ((why?.chains ?? []) as Record<string, unknown>[]).slice(0, 4).map(c => String(c.text ?? '')).filter(Boolean),
    asOf: null, versionsLater: 0, since: null,
  };
}
