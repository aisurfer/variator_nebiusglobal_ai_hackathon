// A read model of one KB tenant for the brief metrics: every version of the business kinds, the directories and
// the stale causal links, normalised once and cached until the KB journal moves (its watermark changes).
// Built with the service principal (organisation-wide aggregates); what a viewer may open is checked for the user's
// role against the ids that viewer's principal can see (see visibleFor).
//
// As of a date (the as-of rule, README.md): `view(t)` is the read model a knowledge base loaded only up to t would give —
// versions recorded by t, links stale at t. Metrics evaluate a date on its view, so nothing
// recorded after the date can change a number of that date.
import type { KbOrganization } from '../../src/kb/types.js';
import { KbClient, type KbStaleHistoryItem, type KbVersion } from './client.js';

export const KINDS = ['Email', 'Task', 'Bug', 'Epic', 'Story', 'MergeRequest', 'Decision', 'Meeting', 'ActionItem', 'Invoice', 'Act',
  'Contract', 'Amendment', 'CommercialProposal', 'Claim', 'Letter', 'Report', 'Document', 'Organization', 'Person', 'Deal'];
export const TICKET_KINDS = new Set(['Task', 'Bug', 'Epic', 'Story']);
const SERVICE_PRINCIPAL = process.env.KB_SERVICE_PRINCIPAL ?? 'user:admin';
const RECHECK_MS = 30_000;

export interface Item { id: string; kind: string; title: string; versions: KbVersion[]; created: string }
export interface Org { id: string; name: string; kind: string; domain: string }
export interface Person { id: string; name: string; email: string; role: string; department: string | null; orgId: string | null; external: boolean }
export interface Mail { id: string; at: number; from: string; fromOrg: Org | null; fromPerson: Person | null; to: string[]; subject: string; messageId: string; inReplyTo: string | null; threadId: string; internal: boolean }
export interface Stale { effectId: string; effectTitle: string; effectKind: string; causeId: string; causeTitle: string; causeKind: string; changedAt: string; predicate: string }

export interface SnapshotInputs { versions: KbVersion[]; stale: unknown; staleHistory: KbStaleHistoryItem[] | null }

export class Snapshot {
  items = new Map<string, Item>();
  byKind = new Map<string, Item[]>();
  orgs = new Map<string, Org>();
  domains = new Map<string, Org>();
  persons = new Map<string, Person>();
  personByName = new Map<string, Person>();
  mails: Mail[] = [];
  threads = new Map<string, Mail[]>();
  stale: Stale[] = [];
  start = 0;
  end = 0;
  /** The last moment a version was recorded (the end of the data on the knowledge axis): a date of view sent to the
   * knowledge base never goes past it, the base refuses a moment after its data (the as-of rule, README.md). */
  recordedEnd = 0;
  inputs: SnapshotInputs | null = null;
  private views = new Map<number, Snapshot>();
  constructor(readonly org: KbOrganization, readonly watermark: number | null, readonly builtAt = new Date().toISOString()) {}

  kind(k: string): Item[] { return this.byKind.get(k) ?? []; }
  /** The version effective at instant t (ms), null before the item existed or after its deletion. */
  at(item: Item, t: number): KbVersion | null {
    let v: KbVersion | null = null;
    for (const x of item.versions) { if (Date.parse(x.effective_from) <= t) v = x; else break; }
    return v && v.op !== 'delete' ? v : null;
  }
  field<T = string>(v: KbVersion | null, name: string): T | null {
    const x = v?.fields?.[name];
    return x === undefined || x === null || x === 'None' ? null : x as T;
  }
  orgOfAddress(address: string): Org | null { return this.domains.get(address.split('@').pop()?.toLowerCase() ?? '') ?? null; }
  /** Parent e-mail of an attachment entity («mail.msg-000333.att-1» → «mail.msg-000333»). */
  parentMail(id: string): Mail | null { const m = /^(.*)\.att-\d+$/.exec(id); return m ? this.mails.find(x => x.id === m[1]) ?? null : null; }

  /** The read model as known at instant t (ms): what a knowledge base loaded only up to t would give. A snapshot
   *  whose every input is known by t is its own view. Views are memoised per instant. */
  view(t: number): Snapshot {
    const inp = this.inputs;
    if (!inp) return this;
    const hit = this.views.get(t);
    if (hit) return hit;
    const versions = inp.versions.filter(v => Date.parse(v.recorded_at) <= t);
    const out = versions.length === inp.versions.length && !inp.staleHistory
      ? this : fill(new Snapshot(this.org, this.watermark, this.builtAt), { ...inp, versions }, t);
    if (this.views.size > 64) this.views.delete(this.views.keys().next().value!);
    this.views.set(t, out);
    return out;
  }
}

export async function buildSnapshot(client: KbClient, org: KbOrganization, watermark: number | null): Promise<Snapshot> {
  const p = SERVICE_PRINCIPAL;
  const [versions, stale, staleHistory] = await Promise.all([
    client.timeline(org.tenant, p, KINDS),
    client.stale(org.tenant, p).catch(() => ({ items: [] })),
    client.staleHistory(org.tenant, p).catch(() => null),
  ]);
  return fill(new Snapshot(org, watermark), { versions, stale, staleHistory }, null);
}

/** Normalise the inputs into the snapshot; `t` (a view) picks the stale links of that instant from the history. */
function fill(s: Snapshot, inp: SnapshotInputs, t: number | null): Snapshot {
  const { versions, stale, staleHistory } = inp;
  const org = s.org;
  if (t === null) s.inputs = inp;
  for (const v of versions) {
    let item = s.items.get(v.entity_id);
    if (!item) { item = { id: v.entity_id, kind: v.kind, title: v.title, versions: [], created: v.effective_from }; s.items.set(v.entity_id, item); }
    item.versions.push(v);
  }
  for (const item of s.items.values()) {
    item.versions.sort((a, b) => a.seq - b.seq);
    const head = item.versions[item.versions.length - 1];
    item.title = head.title || item.title;
    item.kind = head.kind;
    item.created = item.versions[0].effective_from;
    (s.byKind.get(item.kind) ?? s.byKind.set(item.kind, []).get(item.kind)!).push(item);
  }
  for (const o of s.kind('Organization')) {
    const f = o.versions[o.versions.length - 1].fields;
    const org: Org = { id: String(f.org_id ?? o.id), name: o.title, kind: String(f.kind ?? 'other'), domain: String(f.domain ?? '').toLowerCase() };
    s.orgs.set(org.id, org);
    if (org.domain) s.domains.set(org.domain, org);
  }
  for (const pi of s.kind('Person')) {
    const f = pi.versions[pi.versions.length - 1].fields;
    const person: Person = { id: pi.id, name: pi.title, email: String(f.email ?? '').toLowerCase(), role: String(f.role ?? ''), department: f.department ? String(f.department) : null, orgId: f.org ? String(f.org) : null, external: Boolean(f.external) };
    s.persons.set(pi.id, person);
    s.personByName.set(person.name, person);
  }
  const byEmail = new Map([...s.persons.values()].map(x => [x.email, x]));
  for (const m of s.kind('Email')) {
    const f = m.versions[0].fields;
    const from = String(f.from ?? '').toLowerCase();
    const mail: Mail = {
      id: m.id, at: Date.parse(String(f.sent_at ?? m.created)), from, fromOrg: s.orgOfAddress(from), fromPerson: byEmail.get(from) ?? null,
      to: listOf(f.to), subject: String(f.subject ?? m.title), messageId: String(f.message_id ?? m.id),
      inReplyTo: f.in_reply_to && f.in_reply_to !== 'None' ? String(f.in_reply_to) : null, threadId: String(f.thread_id ?? m.id),
      internal: from.endsWith(`@${org.ownDomain}`),
    };
    s.mails.push(mail);
  }
  s.mails.sort((a, b) => a.at - b.at);
  for (const m of s.mails) (s.threads.get(m.threadId) ?? s.threads.set(m.threadId, []).get(m.threadId)!).push(m);
  if (staleHistory) {
    // the links stale at the instant (the end of the history without one), as /causal/stale would list them then
    const at = t ?? Infinity;
    s.stale = staleHistory.flatMap(x => x.segments.filter(g => Date.parse(g.from) <= at && (g.to === null || Date.parse(g.to) > at)).map(g => ({
      effectId: x.effect_entity_id, effectTitle: g.effect_title, effectKind: g.effect_kind, causeId: x.cause_entity_id, causeTitle: g.cause_title ?? '',
      causeKind: g.cause_kind ?? '', changedAt: g.changed_at ?? g.from, predicate: x.predicate_label || x.predicate })));
  } else {
    const staleItems = (Array.isArray(stale) ? stale : (stale as { items?: unknown[] }).items ?? []) as Record<string, string>[];
    s.stale = staleItems.map(x => ({ effectId: x.effect_entity_id, effectTitle: x.effect_title, effectKind: x.effect_kind, causeId: x.cause_entity_id, causeTitle: x.cause_title, causeKind: x.cause_kind, changedAt: x.changed_at, predicate: x.predicate_label || x.predicate }));
  }
  const times = [...s.items.values()].map(i => Date.parse(i.created)).filter(Number.isFinite);
  s.start = Math.min(...times);
  s.end = Math.max(...[...s.items.values()].flatMap(i => i.versions.map(v => Date.parse(v.effective_from))).filter(Number.isFinite));
  s.recordedEnd = Math.max(0, ...[...s.items.values()].flatMap(i => i.versions.map(v => Date.parse(v.recorded_at))).filter(Number.isFinite));
  return s;
}

function listOf(v: unknown): string[] {
  if (Array.isArray(v)) return v.map(x => String(x).toLowerCase());
  if (typeof v === 'string' && v.startsWith('[')) { try { return (JSON.parse(v.replace(/'/g, '"')) as string[]).map(x => x.toLowerCase()); } catch { return []; } }
  return v ? [String(v).toLowerCase()] : [];
}

/** Snapshots and per-principal visibility, refreshed when the KB journal watermark moves. */
export class SnapshotCache {
  private snaps = new Map<string, { snap: Snapshot; checked: number }>();
  private building = new Map<string, Promise<Snapshot>>();
  private visible = new Map<string, { watermark: number | null; ids: Promise<Set<string>> }>();
  constructor(readonly client: KbClient) {}

  // keyed by tenant: the company and each of its simulated futures (branches) have their own read model
  async get(org: KbOrganization): Promise<Snapshot> {
    const cached = this.snaps.get(org.tenant);
    if (cached && Date.now() - cached.checked < RECHECK_MS) return cached.snap;
    const watermark = await this.watermark(org);
    if (cached && cached.snap.watermark === watermark) { cached.checked = Date.now(); return cached.snap; }
    const key = `${org.tenant}@${watermark}`;
    if (!this.building.has(key)) {
      this.building.set(key, buildSnapshot(this.client, org, watermark)
        .then(snap => { this.snaps.set(org.tenant, { snap, checked: Date.now() }); return snap; })
        .finally(() => this.building.delete(key)));
    }
    return this.building.get(key)!;
  }

  /** The tenants with a read model in the cache. */
  tenants(): string[] { return [...this.snaps.keys()]; }

  /** The read model without waiting for the knowledge base when one is cached: a due watermark check runs in the
   * background, and a new journal position builds a new read model for the next request. The history of a tenant does
   * not change, so this costs at most one request on an old read model, never a wrong as-of answer. */
  private refreshing = new Set<string>();
  async current(org: KbOrganization): Promise<Snapshot> {
    const cached = this.snaps.get(org.tenant);
    if (!cached) return this.get(org);
    if (Date.now() - cached.checked >= RECHECK_MS && !this.refreshing.has(org.tenant)) {
      this.refreshing.add(org.tenant);
      this.get(org).catch(() => undefined).finally(() => this.refreshing.delete(org.tenant));
    }
    return cached.snap;
  }

  async visibleFor(org: KbOrganization, principal: string, watermark: number | null): Promise<Set<string>> {
    const key = `${org.tenant}|${principal}`;
    const hit = this.visible.get(key);
    if (hit && hit.watermark === watermark) return hit.ids;
    const ids = this.client.visibleIds(org.tenant, principal);
    this.visible.set(key, { watermark, ids });
    ids.catch(() => this.visible.delete(key));
    return ids;
  }

  private async watermark(org: KbOrganization): Promise<number | null> {
    const w = await this.client.watermarks(org.tenant, SERVICE_PRINCIPAL).catch(() => null);
    return w && typeof w.journal === 'number' ? w.journal : null;
  }
}
