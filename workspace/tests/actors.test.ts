// S6 «Actors and risks»: which actor-card set the page reads for a view date, the list (filter, search, sections),
// the sparkline path, and the shape of a real card set when one is on disk (ACTORS_CARDS, default the WP20 output).
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { cardSets, dollars, filterCards, isFilter, listSections, moneyParts, moneyText, pickCardSet, rangeText, shortDate, sourceText, sparkPath } from '../src/variator/actors/model.js';
import type { ActorCard, CardsFile, RisksFile } from '../src/variator/actors/types.js';

const card = (id: string, group: ActorCard['group'], section: string, name: string, title = ''): ActorCard => ({
  id, kind: group === 'people' ? 'person' : group === 'clients' ? 'client' : group === 'vendors' ? 'vendor' : group === 'company' ? 'company' : 'cohort',
  group, section, name, title, dossier: null, kbId: `sim.CARD-${id}`, profile: { source: 'writer', sentences: [] },
  habits: [], changes: [], risks: [], events: [],
});
const CARDS = [
  card('company', 'company', 'Company', 'Larchwood Systems'),
  card('LW-E06', 'people', 'Engineering', 'Daniel Kowalski', 'Lead Backend Engineer'),
  card('LW-E02', 'people', 'Finance and HR', 'Morgan Reyes', 'Head of Finance'),
  card('LW-E07', 'people', 'Engineering', 'Aisha Rahman', 'Senior Backend Engineer'),
  card('CL-001', 'clients', 'Key clients', 'Meridian Freight Group', 'Key client · logistics'),
  card('VN-03', 'vendors', 'Vendors', 'Nimbus Cloud', 'Cloud provider'),
  card('future:qa', 'cohorts', 'Future hires', 'Future QA engineer'),
  card('clients:standard', 'cohorts', 'Client cohorts', 'Standard clients'),
];

test('the page reads the newest actor-card set learned up to the view date, never a later one', () => {
  const body = { sets: [
    { id: 'actors', manifest: { as_of: '2026-10-27' } },
    { id: 'history-metrics', manifest: { kind: 'history-metrics', asOf: '2026-10-26' } },
    { id: 'cards-a', manifest: { kind: 'actor-cards', asOf: '2026-10-26', t: '2026-10-27', builtAt: '2026-10-27T20:00:00Z' } },
    { id: 'cards-b', manifest: { kind: 'actor-cards', asOf: '2026-10-26', t: '2026-10-27', builtAt: '2026-10-27T21:00:00Z' } },
    { id: 'cards-old', manifest: { kind: 'actor-cards', asOf: '2026-09-30', builtAt: '2026-10-27T22:00:00Z' } },
    { id: 'broken', error: 'manifest.json is not valid JSON' },
  ] };
  const sets = cardSets(body);
  assert.deepEqual(sets.map(s => s.id), ['cards-a', 'cards-b', 'cards-old']);
  assert.equal(pickCardSet(sets, '2026-10-26')?.id, 'cards-b');
  assert.equal(pickCardSet(sets, '2026-10-01')?.id, 'cards-old');
  assert.equal(pickCardSet(sets, '2026-09-01'), null);
  assert.equal(pickCardSet(sets, undefined)?.id, 'cards-b');
  assert.deepEqual(cardSets(null), []);
});

test('the list filters by group, searches names and roles, and keeps people in their departments', () => {
  assert.deepEqual(filterCards(CARDS, 'people', '').map(c => c.id), ['LW-E06', 'LW-E02', 'LW-E07']);
  assert.deepEqual(filterCards(CARDS, 'all', 'backend').map(c => c.id), ['LW-E06', 'LW-E07']);
  assert.deepEqual(filterCards(CARDS, 'all', 'kowalski lead').map(c => c.id), ['LW-E06']);
  assert.deepEqual(filterCards(CARDS, 'clients', 'nimbus'), []);
  const sections = listSections(CARDS);
  assert.deepEqual(sections.map(s => `${s.label}:${s.cards.length}`),
    ['Company:1', 'Engineering:2', 'Finance and HR:1', 'Key clients:1', 'Vendors:1', 'Future hires:1', 'Client cohorts:1']);
  assert.ok(isFilter('cohorts') && !isFilter('company') && !isFilter(null));
});

test('a sparkline spans the box with the lowest point at the bottom', () => {
  assert.equal(sparkPath([1], 84, 22), '');
  const d = sparkPath([1, 3, 2], 84, 22);
  assert.equal(d, 'M2.0 20.0 L42.0 2.0 L82.0 11.0');
  assert.equal(sparkPath([5, 5], 84, 22), 'M2.0 20.0 L82.0 20.0');
  assert.equal(shortDate('2026-10-13'), '13 Oct 2026');
  assert.equal(shortDate('2026-10'), 'Oct 2026');
  assert.equal(shortDate(null), '—');
});

const dir = process.env.ACTORS_CARDS ?? '/tmp/variator-1000/mvp-artifacts/actors/cards';
test('a real card set: every card has a cited profile and formatted habits; people carry no personal risk', { skip: !existsSync(`${dir}/cards.json`) && 'no card set on disk' }, () => {
  const cards = JSON.parse(readFileSync(`${dir}/cards.json`, 'utf8')) as CardsFile;
  const risks = JSON.parse(readFileSync(`${dir}/risks.json`, 'utf8')) as RisksFile;
  assert.equal(cards.cards.length, 46);
  assert.equal(risks.risks.length, 19);
  for (const c of cards.cards) {
    assert.ok(c.profile.sentences.length >= 2, `${c.id}: profile`);
    for (const s of c.profile.sentences) assert.ok(s.cites.length >= 1, `${c.id}: a sentence without a citation`);
    for (const h of c.habits) assert.ok(h.value && h.label && h.sourceKind, `${c.id}: habit ${h.key}`);
    assert.ok(c.habits.filter(h => h.primary).length <= 6, `${c.id}: at most 6 key habits`);
    if (c.kind === 'person') {
      assert.ok(!c.habits.some(h => h.key === 'resignation_hazard'), `${c.id}: personal risk`);
      assert.deepEqual(c.risks, []);
    }
  }
  const meridian = cards.cards.find(c => c.id === 'CL-001')!;
  assert.equal(meridian.dossier, 'org.CL-001');
  assert.ok(meridian.changes.some(ch => ch.kind === 'shock'));
  assert.ok(risks.risks.some(r => r.shocks.length > 0));
});

test('people\'s words on the cards: dollars with the exact value, groups in words, ranges with a minus sign (m1)', () => {
  assert.deepEqual([dollars(12_001_200), dollars(1_022_400), dollars(255_600), dollars(2_000_000), dollars(-64_740.92)], ['$12.0M', '$1.02M', '$255,600', '$2.00M', '−$64,741']);
  assert.equal(moneyText('ARR now stands at USD 12,001,200 and cash at USD 2,000,000.'), 'ARR now stands at $12.0M and cash at $2.00M.');
  assert.deepEqual(moneyParts('Its ARR now stands at 1,022,400 USD (status active).'), ['Its ARR now stands at ', { money: '$1.02M', exact: '$1,022,400' }, ' (status active).']);
  assert.equal(moneyText('It bills 257,620-289,246 (median 268,189) USD per month across 13 bills.'), 'It bills $257,620–$289,246 (median $268,189) per month across 13 bills.');
  assert.equal(moneyText('the bills changed by -4.8% per year, 9,000 USD'), 'the bills changed by -4.8% per year, $9,000');
  assert.equal(moneyText('no amounts here'), 'no amounts here');
  assert.deepEqual([sourceText('group · clients:key'), sourceText('group · clients:standard'), sourceText('group · role:analyst designer'), sourceText('own data')],
    ['group · key clients', 'group · standard clients', 'group · analyst designer role', 'own data']);
  assert.equal(rangeText({ interval: '-1.33 – 55', unit: 'NPS points', low: -1.33, high: 54.53 }), '−1 to 55');
  assert.equal(rangeText({ interval: '12.7 – 66', unit: 'NPS points', low: 12.7, high: 66.2 }), '13 to 66');
  assert.equal(rangeText({ interval: '0.18 – 1.88', unit: '1/year', low: 0.18, high: 1.88 }), '0.18 – 1.88');
  assert.equal(rangeText({ interval: '', unit: '', low: null, high: null }), '');
});
