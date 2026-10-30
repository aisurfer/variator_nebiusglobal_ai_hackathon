// The intake agent of the cases page (src/variator/case/intake): deterministic, grounded in the company facts.
// Eight example questions: the intent, the filled slots, the questions asked, and no number that is neither in the
// user's words nor in the facts. The facts (config/company.facts.json, generated from the history spec) are checked
// against config/company.json and the case configs, so a change of the company cannot leave the agent saying old facts.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { analyse, buildFacts, monthDate, nextMessage } from '../src/variator/case/intake/engine.js';
import type { AgentMsg, IntakeConfig, IntakeMsg } from '../src/variator/case/intake/types.js';

const read = <T>(p: string) => JSON.parse(readFileSync(new URL(`../config/${p}`, import.meta.url), 'utf8')) as T;
const cfg = read<IntakeConfig>('cases/intake.json');
const company = read<{ name: string; headcount: number }>('company.json');
type Cfg = { forkDate: string; people: { id: string; name: string; title: string }[]; forks: { id: string; description: string }[]; variables: { name: string; dist: { value?: number } }[] };
const caseB = read<Cfg>('cases/company_best_year.json'), caseA = read<Cfg>('cases/delivery_best_way.json');
const facts = buildFacts(company.name, caseB.forkDate);

/** Runs a thread: the question, then each reply (a chip as [label, value] or free text), until the draft. */
function run(q: string, replies: (string | [string, string])[] = []) {
  const t: IntakeMsg[] = [{ role: 'user', text: q }];
  let m = nextMessage(t, cfg, facts); t.push(m);
  for (const r of replies) {
    if (m.kind === 'draft') break;
    t.push(typeof r === 'string' ? { role: 'user', text: r } : { role: 'user', text: r[0], value: r[1] });
    m = nextMessage(t, cfg, facts); t.push(m);
  }
  const agent = t.filter((x): x is AgentMsg => x.role === 'agent');
  return { t, agent, last: m, state: analyse(t, cfg, facts), asked: agent.filter(a => a.kind === 'question').map(a => a.slot) };
}

/** Numbers the agent may say: the user's, the counts and values of the facts, the years of the dates it names. */
function allowedNumbers(userTexts: string[]) {
  const ok = new Set<string>();
  for (const u of userTexts) for (const n of u.match(/\d+(?:\.\d+)?/g) ?? []) ok.add(n);
  const people = facts.people, depts = facts.departments;
  ok.add(String(people.length));
  for (const d of depts) ok.add(String(people.filter(p => p.department === d.id).length));
  const lead = people.filter(p => p.id === facts.ceo || p.manager === facts.ceo).length;
  ok.add(String(lead)); ok.add(String(people.length - lead));
  ok.add(String(facts.keyClients.length)); ok.add(String(facts.otherClients));
  for (const c of facts.keyClients) ok.add(String(c.acv / 1000));
  for (let m = 0; m <= 24; m++) ok.add(monthDate(facts.forkDate, m).split(' ')[1]);
  return ok;
}
function agentText(a: AgentMsg) {
  const d = a.draft;
  return [a.text, ...a.chips.map(c => c.label), ...(d ? [d.question, ...d.forks.map(f => f.label), ...d.hypotheses, ...d.criteria, ...d.shows, d.notice, ...d.next.flatMap(n => [n.title, n.text])] : [])].join(' \n ');
}
function assertGrounded(r: ReturnType<typeof run>) {
  const ok = allowedNumbers(r.t.filter(x => x.role === 'user').map(x => x.text));
  for (const a of r.agent) for (const n of agentText(a).match(/\d+(?:\.\d+)?/g) ?? []) assert.ok(ok.has(n), `the number ${n} is neither in the question nor in the facts: «${agentText(a).slice(0, 160)}…»`);
  for (const a of r.agent) assert.doesNotMatch(agentText(a), /\p{Extended_Pictographic}/u, 'no emoji');
}

test('intake facts match the company and the case configs', () => {
  const file = read<{ schema: string; company: string }>('company.facts.json');
  assert.equal(file.schema, 'variator-company-facts/1');
  assert.equal(file.company, company.name);
  assert.equal(facts.people.length, company.headcount);
  for (const p of [...caseB.people, ...caseA.people]) {
    const f = facts.people.find(x => x.id === p.id);
    assert.ok(f, `${p.id} of a case config is in the intake facts`);
    assert.equal(f.name, p.name); assert.equal(f.title, p.title);
  }
  const forkText = caseA.forks.map(f => f.description).join(' ');
  for (const v of facts.vendors.filter(x => x.kind === 'nearshore' || x.kind === 'offshore')) assert.ok(forkText.includes(v.name), `${v.name} is a vendor of case A`);
  assert.ok(facts.departments.every(d => facts.people.some(p => p.id === d.head)), 'every department head is a person');
  assert.ok(!('facts' in cfg), 'the intake config has no copy of the facts');
});

test('fire all employees in 3 months: scope, goal and compare are asked; the draft compares a vendor', () => {
  const r = run('What if I fire all employees in 3 months?', [['Everyone except the leadership (15)', 'non_leadership'], ['Cut costs', 'cost'], ['Replace delivery with a vendor', 'vendor']]);
  assert.equal(r.state.intent?.id, 'layoff');
  assert.deepEqual(r.asked, ['scope', 'goal', 'compare']);
  assert.equal(r.state.slots.when?.months, 3, '«in 3 months» is read, not asked');
  assert.match(r.agent[0].text, /20 people/); assert.match(r.agent[0].text, /all 20, including yourself, or everyone except the leadership/);
  assert.equal(r.last.kind, 'draft');
  const d = r.last.draft!;
  assert.equal(d.forks[0].label, 'Keep as is'); assert.ok(d.forks[0].baseline);
  assert.match(d.question, /everyone except the leadership \(15 people\) go in January 2027, to cut costs/);
  assert.ok(d.forks.some(f => /Vistula Code Works or Kaveri Tech Services/.test(f.label)));
  assert.deepEqual(d.links, ['company_best_year', 'delivery_best_way']);
  assertGrounded(r);
});

test('raise prices by 8% in January: size and month are read; scope and priority are asked', () => {
  const r = run('What if we raise prices by 8% in January?', [['All clients at renewal', 'all'], ['Keeping clients', 'churn']]);
  assert.equal(r.state.intent?.id, 'price');
  assert.equal(r.state.slots.size?.value, 'share:8');
  assert.equal(r.state.slots.when?.months, 3);
  assert.deepEqual(r.asked, ['scope', 'priority']);
  assert.equal(r.last.draft?.criteria[0], 'Client churn', 'the priority comes first');
  assertGrounded(r);
});

test('lose the biggest client: the client is named from the facts; its renewal dates the question', () => {
  const r = run('What if we lose our biggest client?', [['At the renewal', 'renewal'], ['Cut costs to protect cash', 'cost']]);
  assert.equal(r.state.intent?.id, 'lose_client');
  assert.equal(r.state.slots.scope?.value, 'client:1');
  assert.deepEqual(r.asked, ['when', 'compare']);
  assert.match(r.agent[0].text, /Meridian Freight Group renews in May 2027/);
  assert.match(r.last.draft!.question, /loses Meridian Freight Group in May 2027/);
  assertGrounded(r);
});

test('hire 3 engineers: the count and the role are read', () => {
  const r = run('What if we hire 3 engineers?', [['Next quarter', 'months:3'], ['Delivery on time', 'delivery']]);
  assert.equal(r.state.intent?.id, 'hire');
  assert.equal(r.state.slots.size?.value, 'count:3');
  assert.equal(r.state.slots.scope?.value, 'role:engineers');
  assert.deepEqual(r.asked, ['when', 'priority']);
  assert.match(r.last.draft!.forks[1].label, /Hire 3 engineers in January 2027/);
  assertGrounded(r);
});

test('outsource delivery: the vendors of the company are offered', () => {
  const r = run('What if we outsource delivery to a vendor?', [['Both', 'both'], ['Cut costs', 'cost']]);
  assert.equal(r.state.intent?.id, 'outsource');
  assert.equal(r.state.slots.scope?.value, 'delivery');
  assert.deepEqual(r.asked, ['compare', 'goal']);
  assert.match(r.agent[0].text, /Vistula Code Works .* Kaveri Tech Services/);
  assert.equal(r.last.draft!.forks.length, 3);
  assertGrounded(r);
});

test('a payroll tax: only the size is asked; free text answers it', () => {
  const r = run('What if the payroll tax goes up next year?', ['by 2 points']);
  assert.equal(r.state.intent?.id, 'tax');
  assert.equal(r.state.slots.scope?.value, 'payroll');
  assert.deepEqual(r.asked, ['size']);
  assert.match(r.last.draft!.question, /payroll tax rises by 2 percentage points in January 2027/);
  assertGrounded(r);
});

test('a GPU shortage: the cloud vendor is named', () => {
  const r = run('What if there is a GPU shortage?', [['Next month', 'months:1'], ['Reserve GPU capacity on a second cloud', 'reserve']]);
  assert.equal(r.state.intent?.id, 'gpu');
  assert.deepEqual(r.asked, ['when', 'compare']);
  assert.match(r.last.draft!.question, /GPU shortage at Nimbus Cloud in November 2026/);
  assertGrounded(r);
});

test('nonsense: one friendly question with the intent chips; a chip then starts the slots', () => {
  const r = run('blorp zzz qwerty');
  assert.equal(r.last.kind, 'unknown');
  assert.equal(r.state.intent, null);
  assert.ok(r.last.chips.length >= 6 && r.last.chips.every(c => c.value.startsWith('intent:')));
  const r2 = run('blorp zzz qwerty', [['Cut the team', 'intent:layoff'], 'engineering only', 'to save money', 'keep as is']);
  assert.equal(r2.state.intent?.id, 'layoff');
  assert.equal(r2.state.slots.scope?.value, 'dept:engineering');
  assert.equal(r2.state.slots.goal?.value, 'cost');
  assert.deepEqual(r2.last.draft!.forks.map(f => f.label), ['Keep as is', 'Let the engineering team (10 people) go']);
  assertGrounded(r); assertGrounded(r2);
});

test('at most three questions, then the draft', () => {
  const r = run('What if Natalie Brooks leaves?', ['soon', 'no idea', 'not sure', 'whatever']);
  assert.equal(r.state.intent?.id, 'leave');
  assert.equal(r.state.slots.scope?.value, 'person:LW-E18');
  assert.ok(r.asked.length <= cfg.maxQuestions);
  assert.equal(r.last.kind, 'draft');
  assertGrounded(r);
});
