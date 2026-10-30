// Types of the intake chat of the cases page: config/cases/intake.json and the thread. The agent is a deterministic
// slot filler in this version; `nextMessage(thread, config, facts)` is the one entry point, so a question generator on
// a language model (the user's key) can take its place later with the same thread in and the same message out.

export type SlotId = 'scope' | 'when' | 'size' | 'goal' | 'compare' | 'priority';
export interface Chip { label: string; value: string }

export interface UserMsg { role: 'user'; text: string; /** The value of a quick-reply chip. */ value?: string }
export interface AgentMsg {
  role: 'agent'; kind: 'question' | 'unknown' | 'draft'; text: string; chips: Chip[];
  /** The slot a question asks for. */
  slot?: SlotId;
  intent?: string;
  draft?: DraftCase;
}
export type IntakeMsg = UserMsg | AgentMsg;

export interface DraftCase {
  question: string;
  /** «Keep as is» first (the baseline). */
  forks: { label: string; baseline?: boolean }[];
  hypotheses: string[];
  criteria: string[];
  /** What such a case would show, without numbers. */
  shows: string[];
  next: { title: string; text: string }[];
  notice: string;
  /** Case ids of the ready cases to explore. */
  links: string[];
}

// ---------------------------------------------------------------- config/cases/intake.json
export interface Extractor { re: string; months?: number | string; weeks?: string; monthName?: string; share?: string; count?: string; value?: string }
export interface ScopeRule { re?: string; entity?: 'person' | 'client' | 'vendor'; value?: string; ambiguous?: boolean }
export interface QuestionDef { text: string; chips: Chip[] }
export interface IntentDef {
  id: string; area: 'people' | 'clients' | 'vendors' | 'factors' | 'rules'; label: string;
  patterns: string[];
  scope: ScopeRule[];
  scopeText?: Record<string, string>;
  /** Slots in the order they are asked (only missing or ambiguous ones, at most `maxQuestions`). */
  ask: SlotId[];
  questions: Partial<Record<SlotId, QuestionDef>>;
  draft: {
    question: string; forks: string[];
    compareForks?: Record<string, string>; defaultCompare?: string;
    /** Compare value → indices of `forks` to keep (outsourcing: one vendor or both). */
    compareFilter?: Record<string, number[]>;
    hypotheses: string[]; criteria: string[]; shows: string[];
  };
}
/** config/company.facts.json (generated from the history spec by scripts/mvp/make_company_facts.py). */
export interface IntakeFactsFile {
  schema: 'variator-company-facts/1';
  source: string;
  /** The fork date T: the people are the staff at T. */
  asOf: string;
  /** The company name of the history spec. */
  company: string;
  people: { id: string; name: string; title: string; department: string; manager: string | null }[];
  departments: { id: string; name: string; head: string }[];
  ceo: string;
  /** `acv`: at the start of the history; `arrAtT`: the ARR at the fork date T. */
  keyClients: { id: string; name: string; acv: number; arrAtT: number; renewalInMonths: number | null }[];
  otherClients: number;
  vendors: { id: string; name: string; kind: string }[];
}
export interface IntakeConfig {
  schema: 'variator-intake/1';
  title: string; subtitle: string; placeholder: string; examples: string[];
  maxQuestions: number;
  slots: Record<'when' | 'size' | 'goal' | 'compare' | 'priority', Extractor[]>;
  criteria: Record<string, string>;
  goalText: Record<string, string>;
  intents: IntentDef[];
  unknown: { text: string; again: string; chips: string[] };
  draftIntro: string;
  final: { baseline: string; next: { title: string; text: string }[]; notice: string; links: string[] };
}

/** The facts the agent may say: the company of the history plus what the case configs hold (fork date, ARR, cash). */
export interface IntakeFacts extends IntakeFactsFile {
  company: string;
  /** ISO date T: «in 3 months» is a month after it. */
  forkDate: string;
}
