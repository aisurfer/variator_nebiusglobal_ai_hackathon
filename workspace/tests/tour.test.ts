// The guided tours of the cases (config/cases/<case>.tour.json): every step names an anchor the case page renders
// (`data-tour`), an action the page supports with arguments that exist in the case config, help keys of the page and
// template variables the page can fill; no number is written by hand; links go to pages that exist. A later change of
// a section or a config cannot break a tour silently.
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { checkVar, helpKeysOf, sectionAnchors, TOUR_ACTIONS, TOUR_ANCHORS, type TourCaseConfig } from '../src/variator/case/tourSpec.js';
import { literalText, placeholders } from '../src/variator/tour/template.js';
import type { TourDef, TourStepDef, TourTarget } from '../src/variator/tour/types.js';

const dir = new URL('../config/cases/', import.meta.url);
const read = <T>(name: string) => JSON.parse(readFileSync(new URL(name, dir), 'utf8')) as T;
const index = read<{ cases: { id: string; status: string; config: string | null }[] }>('index.json');
const tours = readdirSync(dir).filter(f => f.endsWith('.tour.json'));
const tourDir = new URL('../config/tours/', import.meta.url);

// every `data-tour="<id>"` the case page source renders (the section slots render `data-tour={s.id}`)
const srcDir = new URL('../src/variator/case/', import.meta.url);
const sources = (function walk(d: URL): string[] {
  return readdirSync(d, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(new URL(`${e.name}/`, d)) : e.name.endsWith('.tsx') ? [readFileSync(new URL(e.name, d), 'utf8')] : []);
})(srcDir).join('\n');
// data-tour="x", tour="x" (a prop that becomes data-tour), or data-tour={cond ? 'x' : undefined} (every quoted name)
const rendered = new Set([
  ...[...sources.matchAll(/data-tour="([\w-]+)"|tour="([\w-]+)"/g)].map(m => m[1] ?? m[2]),
  ...[...sources.matchAll(/data-tour=\{([^}]*)\}/g)].flatMap(m => [...m[1].matchAll(/'([\w-]+)'/g)].map(q => q[1])),
]);

test('both ready cases have a tour', () => {
  for (const c of index.cases.filter(x => x.status === 'ready')) assert.ok(existsSync(new URL(`${c.id}.tour.json`, dir)), `${c.id}.tour.json exists`);
  assert.ok(tours.length >= 2);
});

test('every static anchor of the spec is rendered by the case page', () => {
  for (const id of Object.keys(TOUR_ANCHORS)) assert.ok(rendered.has(id), `data-tour="${id}" is in src/variator/case`);
  assert.match(sources, /data-tour=\{s\.id\}/, 'CasePage renders the section slots as anchors');
});

for (const file of tours) {
  test(`tour ${file}`, () => {
    const t = read<TourDef>(file);
    const entry = index.cases.find(c => c.id === t.caseId);
    assert.ok(entry?.config, `case ${t.caseId} is in index.json`);
    assert.equal(file, `${t.caseId}.tour.json`);
    const cfg = read<TourCaseConfig>(entry!.config!);
    assert.equal(t.schema, 'variator-tour/1');
    assert.ok(t.minutes > 0 && t.minutes <= 10, 'a short tour');
    assert.equal(t.steps.length, 6, 'six steps: the five of the analysis and the decision');
    assert.equal(new Set(t.steps.map(s => s.id)).size, t.steps.length, 'step ids are unique');

    const anchors = new Set([...Object.keys(TOUR_ANCHORS), ...sectionAnchors(cfg)]);
    const target = (x: TourTarget, where: string) => {
      assert.ok(anchors.has(x.anchor), `${where}: anchor ${x.anchor} is a section or a known anchor`);
      for (const f of typeof x.focus === 'string' ? [x.focus] : x.focus ?? []) assert.ok(TOUR_ANCHORS[f], `${where}: focus ${f} is a known anchor`);
    };
    const help = helpKeysOf(cfg);
    const texts = (s: TourStepDef) => [s.title, s.body, s.try, s.action?.label, s.see?.label, s.more?.label, s.more?.text, ...(s.links ?? []).map(l => l.label)].filter((x): x is string => !!x);

    for (const s of t.steps) {
      const where = `${t.caseId}/${s.id}`;
      target(s, where);
      if (s.see) target(s.see, `${where} see`);
      if (s.action) {
        const spec = TOUR_ACTIONS[s.action.id];
        assert.ok(spec, `${where}: action ${s.action.id} is supported`);
        assert.deepEqual(spec.check(cfg, s.action.args ?? {}), [], `${where}: arguments of ${s.action.id}`);
        if (s.action.then) target(s.action.then, `${where} then`);
      }
      for (const k of s.help ?? []) assert.ok(help.has(k), `${where}: help key ${k}`);
      for (const text of texts(s)) {
        for (const v of placeholders(text)) assert.equal(checkVar(cfg, v), null, `${where}: {${v}}`);
        assert.doesNotMatch(literalText(text), /\d/, `${where}: no number written by hand in «${text}»`);
        assert.doesNotMatch(text, /\p{Extended_Pictographic}/u, `${where}: no emoji`);
        assert.doesNotMatch(text, /[Ѐ-ӿ]/, `${where}: English only`);
      }
      const sentences = s.body.split(/(?<=[.!?])\s+/).filter(Boolean).length;
      assert.ok(sentences >= 1 && sentences <= 3, `${where}: one to three sentences (${sentences})`);
      for (const l of s.links ?? []) {
        const m = /^\/cases\/([\w-]+)$/.exec(l.to);
        if (m) {
          assert.ok(index.cases.some(c => c.id === m[1] && c.status === 'ready'), `${where}: link to a ready case ${m[1]}`);
          if (l.tour) assert.ok(existsSync(new URL(`${m[1]}.tour.json`, dir)), `${where}: ${m[1]} has a tour`);
        } else assert.ok(['/ask', '/brief', '/sources', '/cases'].includes(l.to), `${where}: link ${l.to} is a page`);
      }
    }
    assert.equal(t.steps[0].anchor, 'origin-button', 'step 1 starts at «Your request»');
    // the tour ends in action: «Your decision», and from there the reviewer goes on to the other case with its tour
    assert.equal(t.steps[t.steps.length - 1].anchor, 'decision', 'the last step is «Your decision»');
    assert.ok(t.steps[t.steps.length - 1].links?.some(l => l.to.startsWith('/cases/') && l.tour), 'the last step links to the other case with its tour');
  });
}

// «Start from the company» (config/tours/reviewer.tour.json): pages and anchors that exist, at most three steps, no
// number written by hand, and it leads on into a case that has its own tour.
test('the reviewer tour across pages', () => {
  const t = JSON.parse(readFileSync(new URL('reviewer.tour.json', tourDir), 'utf8')) as { id: string; steps: (TourStepDef & { page: string })[]; next?: { to: string; label: string } };
  assert.equal(t.id, 'reviewer');
  assert.ok(t.steps.length >= 1 && t.steps.length <= 3, 'at most three steps');
  const pages: Record<string, URL> = { '/brief': new URL('../src/kb/KbBrief.tsx', import.meta.url), '/actors': new URL('../src/variator/actors/ActorsPage.tsx', import.meta.url) };
  for (const s of t.steps) {
    const path = s.page.split('?')[0], src = pages[path];
    assert.ok(src, `${s.id}: page ${path} is one of ${Object.keys(pages).join(', ')}`);
    const code = readFileSync(src, 'utf8');
    for (const a of [s.anchor, ...(typeof s.focus === 'string' ? [s.focus] : s.focus ?? [])]) assert.match(code, new RegExp(`data-tour=("${a}"|\\{[^}]*'${a}')`), `${s.id}: data-tour="${a}" on ${path}`);
    assert.match(code, /<ReviewerTour \/>/, `${path} mounts the reviewer tour`);
    for (const text of [s.title, s.body, s.try].filter((x): x is string => !!x)) {
      assert.doesNotMatch(text, /\d/, `${s.id}: no number written by hand`);
      assert.deepEqual(placeholders(text), [], `${s.id}: static text`);
    }
  }
  const m = /^\/cases\/([\w-]+)$/.exec(t.next?.to ?? '');
  assert.ok(m && existsSync(new URL(`${m[1]}.tour.json`, dir)), 'it leads on into a case tour');
});
