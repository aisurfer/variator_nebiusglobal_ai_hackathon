// The origin of each ready case (config/cases/<id>.origin.json, hand-written): every drafted fork, hypothesis and
// criterion exists in the case config, the dates follow the config's fork date, the request is the author's own
// words of a sensible length, without emoji. A later fix of a model cannot break the origin silently.
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const dir = new URL('../config/cases/', import.meta.url);
const read = <T>(name: string) => JSON.parse(readFileSync(new URL(name, dir), 'utf8')) as T;
const index = read<{ cases: { id: string; status: string; config: string | null }[] }>('index.json');

interface Origin { caseId: string; request: string; qa: { q: string; a: string }[]; author: string; requestedOn: string; frozenOn: string; drafted: { forks: string[]; hypotheses: string[]; criteria: string[] }; reviewEdits: string[] }
interface Config { id: string; forkDate: string; forks: { id: string }[]; hypotheses: { id: string }[]; criteria: { name: string }[] }

for (const c of index.cases.filter(x => x.status === 'ready' && x.config)) {
  test(`origin of ${c.id}`, () => {
    const file = new URL(`${c.id}.origin.json`, dir);
    assert.ok(existsSync(file), `${c.id}.origin.json exists`);
    const o = read<Origin>(`${c.id}.origin.json`), cfg = read<Config>(c.config!);
    assert.equal(o.caseId, c.id);
    for (const f of o.drafted.forks) assert.ok(cfg.forks.some(x => x.id === f), `fork ${f} is in ${c.id}`);
    for (const h of o.drafted.hypotheses) assert.ok(cfg.hypotheses.some(x => x.id === h), `hypothesis ${h} is in ${c.id}`);
    for (const k of o.drafted.criteria) assert.ok(cfg.criteria.some(x => x.name === k), `criterion ${k} is in ${c.id}`);
    assert.deepEqual([...o.drafted.forks].sort(), cfg.forks.map(x => x.id).sort(), 'every fork of the case was drafted');
    for (const d of [o.requestedOn, o.frozenOn]) assert.ok(d === 'forkDate' || d === cfg.forkDate, 'the dates follow the fork date of the config');
    assert.equal(o.author, 'LW-E01');
    const words = o.request.trim().split(/\s+/).length;
    assert.ok(words >= 80 && words <= 130, `request of ${words} words`);
    assert.ok(o.qa.length >= 1 && o.qa.length <= 3, '1-3 clarifying questions');
    assert.ok(o.reviewEdits.length >= 1 && o.reviewEdits.length <= 3, '1-3 review edits');
    assert.doesNotMatch(JSON.stringify(o), /\p{Extended_Pictographic}/u, 'no emoji');
  });
}
