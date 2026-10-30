// People's words for texts the knowledge base and the simulation write for machines: answers of «Ask», record titles,
// source texts and facts of dossiers. Amounts in dollars, dates a person reads, no machine ids, version suffixes,
// Python lists or snake_case. Pure functions, unit-tested in tests/kb-words.test.ts.
import { formatValue } from './api';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
// the id prefixes of knowledge-base entities (connectors and the simulation)
const ID = '(?:sim|org|person|mail|jira|gitlab|confluence|conf|xwiki|wiki|directory|fin|rev|ops|cust|ppl)';
const ISO_TIME = /\b(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?(?:\s*\(UTC[+-]\d\d(?::?\d\d)?\))?/g;
const ISO_DAY = /\b(\d{4})-(\d\d)-(\d\d)\b(?![T:\d])/g;
const MONEY_AFTER = /(-?\d[\d,]*(?:\.\d+)?)\s?USD\b/g;
const MONEY_BEFORE = /\bUSD\s?(-?\d[\d,]*(?:\.\d+)?)/g;
const PY_LIST = /\[(?:'[^'\]]*'|"[^"\]]*")(?:,\s*(?:'[^'\]]*'|"[^"\]]*"))*\]/g;
// «(LifeEvent, sim.SIG-0002; matched: …)», «(Decision, [sim.DEC-0004@1]; effective …)»
const ID_PARENS = new RegExp(`\\s*\\((?:[A-Za-z ]+,\\s*)?\\[?${ID}\\.[^()]*(?:\\([^()]*\\)[^()]*)*\\)`, 'g');
// a bare id, never one inside a citation «[sim.X@1]»
const BARE_ID = new RegExp(`(?<![\\[\\w#%-])${ID}\\.[A-Za-z0-9][\\w.-]*?(?:@\\d+)?(?=[\\s,;:)\\]]|\\.(?:\\s|$)|$)`, 'g');
// «caused by: process CTR-0001»: the cause type of the simulation is not what the record is
const CAUSE_TYPE = /\b(caused by|implements|based on|motivated by|relates to|triggered by)(:?)\s+(?:process|habit|rule|decision|life event|life_event|hypothesis)\s+(?=[A-Z0-9])/gi;

const day = (y: string, m: string, d: string) => `${Number(d)} ${MONTHS[Number(m) - 1] ?? m} ${y}`;
const money = (n: string) => formatValue(Number(n.replace(/,/g, '')), 'money');

/** A text for people: dollars, readable dates, no machine ids, version suffixes, Python lists or snake_case. */
export function peopleWords(text: string | null | undefined): string {
  if (!text) return '';
  return text
    .replace(MONEY_AFTER, (_, n: string) => money(n))
    .replace(MONEY_BEFORE, (_, n: string) => money(n))
    .replace(/\bUSD\b/g, 'US dollars')
    .replace(ISO_TIME, (_, y: string, m: string, d: string, hh: string, mm: string) => `${day(y, m, d)}, ${hh}:${mm}`)
    .replace(/\s*\(UTC[+-]\d\d(?::?\d\d)?\)/g, '')
    .replace(ISO_DAY, (_, y: string, m: string, d: string) => day(y, m, d))
    .replace(PY_LIST, '')
    .replace(ID_PARENS, '')
    .replace(CAUSE_TYPE, '$1$2 ')
    .replace(BARE_ID, '')
    .replace(/\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b/g, m => m.replace(/_/g, ' '))
    .replace(/\(\s*[;,]?\s*\)/g, '')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/ ([,.;:])/g, '$1');
}

/** Where a record comes from, for people: «sim» -> «Company records», «directory-company» -> «Directory». */
export function sourceLabel(source: string | null | undefined, simulation = false): string {
  if (!source) return '';
  if (source === 'sim') return simulation ? 'Simulation' : 'Company records';
  const name = source.replace(/-company$/, '').replace(/[-_]/g, ' ');
  const known: Record<string, string> = { gitlab: 'GitLab', xwiki: 'XWiki', jira: 'Jira', confluence: 'Confluence', mail: 'Mail', directory: 'Directory' };
  return known[name] ?? `${name[0]?.toUpperCase() ?? ''}${name.slice(1)}`;
}

/** The lines of an «Ask» answer: what a person reads, and what goes into the disclosure «How the question was
 * understood» (the subject match, the time and knowledge cut, the interpretation, the search fragments). */
export function splitAnswer(text: string): { main: string; technical: string } {
  const main: string[] = [], technical: string[] = [];
  let into = main;
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (/^(Time|Knowledge cut|Interpretation):/.test(t)) { technical.push(line); continue; }
    if (/^Fragments found:/.test(t)) { into = technical; into.push(line); continue; }
    if (/^(Subject|Chains from|Root causes|Found|Other objects may be meant|No declared causes)\b/.test(t)) into = main;
    into.push(line);
  }
  return { main: main.join('\n').trim(), technical: technical.join('\n').trim() };
}
