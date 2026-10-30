// The artifact set of the actor cards (Python `variator.actors.cards.artifacts`, manifest kind `actor-cards`):
// cards.json (every card with its cited profile, habit rows and the case items that change it) and risks.json
// (the register of the risks that already fired). Every number arrives formatted; the page only lays it out.

export interface Cite { id: string; label: string; title: string }
export interface Sentence { text: string; facts: string[]; cites: Cite[] }
export interface Series { label: string; points: [string, number][] }
export type SourceKind = 'data' | 'group' | 'web' | 'llm' | 'assumption';

export interface HabitRow {
  key: string;
  label: string;
  help: string;
  value: string;
  interval: string;
  suffix: string;
  point: number | null;
  low: number | null;
  high: number | null;
  n: number;
  unit: string;
  source: string;
  sourceKind: SourceKind;
  sourceLabel: string;
  method: string;
  primary: boolean;
  series?: Series;
}

export interface CaseChange {
  case: string;
  caseId: string;
  caseTitle: string;
  kind: 'decision' | 'hypothesis' | 'shock';
  id: string;
  label: string;
  text: string;
}

export interface LifeEvent { text: string; date: string | null; cites: Cite[] }
export interface ExternalFact {
  id: string;
  statement: string;
  source: string;
  evidence: string;
  title: string;
  purpose: string;
  confidence: number;
  period?: string | null;
}

export type CardGroup = 'company' | 'people' | 'clients' | 'vendors' | 'cohorts';
export interface ActorCard {
  id: string;
  kind: 'company' | 'person' | 'client' | 'vendor' | 'regulator' | 'cohort';
  group: CardGroup;
  section: string;
  name: string;
  title: string;
  subtitle?: string;
  dossier: string | null;
  contract?: string | null;
  kbId: string;
  profile: { source: 'writer' | 'facts'; sentences: Sentence[] };
  habits: HabitRow[];
  changes: CaseChange[];
  risks: string[];
  events: LifeEvent[];
  external?: ExternalFact[];
  members?: { count: number | null; kind: string };
}

export interface CardsFile {
  schema: string;
  asOf: string;
  t: string;
  company: { name: string; tenant?: string; timezone?: string };
  groups: { id: CardGroup; label: string }[];
  cards: ActorCard[];
  research?: { path: string; facts: number } | null;
}

export interface SizeRow { label: string; value: string; interval: string; suffix: string }
export interface Risk {
  id: string;
  code: string;
  title: string;
  kind: string;
  polarity: 'negative' | 'positive';
  count: number;
  perMonth: { value: string; interval: string; point: number; low: number; high: number; source: string };
  first: string | null;
  last: string | null;
  effect: string;
  note: string;
  examples: Cite[];
  evidenceCount: number;
  actors: number;
  cards: string[];
  size: SizeRow[];
  shocks: CaseChange[];
  external?: ExternalFact[];
}
export interface RisksFile { schema: string; asOf: string; t: string; risks: Risk[]; research?: { path: string; facts: number } | null }

/** An artifact set of the VARIATOR API with actor cards. */
export interface CardSet { id: string; asOf: string; t: string; builtAt: string }
