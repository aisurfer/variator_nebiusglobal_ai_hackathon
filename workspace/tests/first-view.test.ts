// The first view (WP29, NFR7): the server names the first API requests of a page in index.html, the browser code starts
// them and the ones that need their answers before the page's code has arrived, and a page takes each answer once (the
// list of artifact sets: every reader in the first seconds). index.html is never kept by a browser; the hashed assets
// are kept for good, and a file of an older release is a 404.
import test, { after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { firstViewRequests, withPreloads } from '../server/preload.js';
import { createApp } from '../server/app.js';
import { earlyFetch, prefetch, resetEarly } from '../src/early.js';
import { startFirstView } from '../src/firstView.js';
import { ORG } from './kbFixture.js';

const servers: Server[] = [];
after(() => { for (const s of servers) { s.closeAllConnections(); s.close(); } });
const listen = (server: Server) => new Promise<string>(resolve => { servers.push(server); server.listen(0, '127.0.0.1', () => resolve(`http://127.0.0.1:${(server.address() as AddressInfo).port}`)); });

// a fake network: answers by URL, counts the requests
const realFetch = globalThis.fetch;
let asked: string[] = [];
let answers: Record<string, unknown> = {};
beforeEach(() => {
  resetEarly();
  asked = [];
  answers = {};
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input);
    asked.push(url);
    if (url === 'fail') throw new TypeError('network down');
    return url in answers ? Response.json(answers[url]) : Response.json({ error: 'no such file' }, { status: 404 });
  }) as typeof fetch;
});
after(() => { globalThis.fetch = realFetch; });
const settle = () => new Promise(r => setTimeout(r, 20));

test('the server names the first requests of each page; a simulated future waits for the app', () => {
  const company = { id: 'company' };
  const shell = ['/workspace/api/kb/organizations', '/workspace/api/kb/company/dates', '/workspace/api/kb/company/status'];
  assert.deepEqual(firstViewRequests('/brief?tour=reviewer', company), [...shell, '/workspace/api/variator/artifacts']);
  assert.deepEqual(firstViewRequests('/', company), [...shell, '/workspace/api/variator/artifacts']);
  assert.deepEqual(firstViewRequests('/cases/company_best_year', company), [...shell, '/workspace/api/variator/artifacts']);
  assert.deepEqual(firstViewRequests('/actors?actor=CL-001&tour=reviewer&step=2', company), [...shell, '/workspace/api/variator/artifacts']);
  assert.deepEqual(firstViewRequests('/ask', company), shell);
  assert.deepEqual(firstViewRequests('/cases', company), shell); // the list of cases reads no artifact set
  assert.deepEqual(firstViewRequests('/brief?asOf=2026-10-19', company), [shell[0], shell[1], '/workspace/api/kb/company/status?asOf=2026-10-19', '/workspace/api/variator/artifacts']);
  // the branch of a knowledge-base page is checked by the app first; the case pages ignore it
  assert.deepEqual(firstViewRequests('/brief?branch=company-b-x', company), ['/workspace/api/kb/organizations']);
  assert.deepEqual(firstViewRequests('/cases/x?branch=company-b-x', company), [...shell, '/workspace/api/variator/artifacts']);
  assert.deepEqual(firstViewRequests('/brief', null), ['/workspace/api/kb/organizations', '/workspace/api/variator/artifacts']);
});

test('the preloads go into the head before the code; a page without a head stays as it is', () => {
  const html = '<!doctype html><html><head><title>x</title><script type="module" src="/workspace/assets/index.js"></script></head><body></body></html>';
  const out = withPreloads(html, '/ask?q=a"b', { id: 'company' });
  assert.match(out, /<title>x<\/title><link rel="preload" as="fetch" crossorigin href="\/workspace\/api\/kb\/organizations">.*<script type="module"/);
  assert.equal(out.match(/rel="preload"/g)?.length, 3);
  assert.equal(withPreloads('<div id="root"></div>', '/brief', { id: 'company' }), '<div id="root"></div>');
});

test('index.html is asked again on every visit and names its preloads; hashed assets are kept for good', async () => {
  const dist = mkdtempSync(join(tmpdir(), 'ws-dist-'));
  writeFileSync(join(dist, 'index.html'), '<!doctype html><html><head><title>t</title><script type="module" src="/workspace/assets/index-abc.js"></script></head><body><div id="root"></div></body></html>');
  mkdirSync(join(dist, 'assets'));
  writeFileSync(join(dist, 'assets', 'index-abc.js'), 'export {}');
  const app = await listen(createServer(createApp({ distDir: dist, kb: { organizations: [ORG] }, variator: { baseUrl: 'http://127.0.0.1:9' } })));
  const page = await realFetch(`${app}/workspace/cases/company_best_year?tour=1`);
  assert.equal(page.status, 200);
  assert.equal(page.headers.get('cache-control'), 'no-cache');
  const html = await page.text();
  for (const href of [`/workspace/api/kb/${ORG.id}/dates`, '/workspace/api/variator/artifacts']) assert.ok(html.includes(`href="${href}"`), href);
  const asset = await realFetch(`${app}/workspace/assets/index-abc.js`);
  assert.equal(asset.headers.get('cache-control'), 'public, max-age=31536000, immutable');
  // a file of an older release: 404, not the page
  assert.equal((await realFetch(`${app}/workspace/assets/CasesRoute-old.js`)).status, 404);
});

test('a page takes an early answer once; the next read goes to the network', async () => {
  answers['/a'] = { n: 1 };
  prefetch('/a');
  const first = await earlyFetch('/a');
  assert.deepEqual(await first.json(), { n: 1 });
  assert.deepEqual(asked, ['/a']);
  await (await earlyFetch('/a')).json();
  assert.deepEqual(asked, ['/a', '/a']);
  // not a plain GET: never an early answer
  prefetch('/a');
  await earlyFetch('/a', { method: 'POST', body: '{}' });
  assert.equal(asked.length, 4);
});

test('a shared answer serves every reader; a peek leaves the answer to the page', async () => {
  answers['/sets'] = { sets: [1, 2] };
  const peeked = await prefetch('/sets', { shared: true, peek: true });
  assert.deepEqual(peeked, { sets: [1, 2] });
  const [a, b] = await Promise.all([earlyFetch('/sets'), earlyFetch('/sets', { headers: { Accept: 'application/json' } })]);
  assert.deepEqual([await a.json(), await b.json()], [{ sets: [1, 2] }, { sets: [1, 2] }]);
  assert.deepEqual(asked, ['/sets']);
  // an error answer: no JSON for the peek, the same answer for the page
  assert.equal(await prefetch('/missing', { peek: true }), null);
  assert.equal((await earlyFetch('/missing')).status, 404);
  // a shared error answer is not handed on: the reader asks again
  asked = [];
  prefetch('/down', { shared: true });
  assert.equal((await earlyFetch('/down')).status, 404);
  assert.deepEqual(asked, ['/down', '/down']);
});

test('a failed early request is asked again by the page; the reader\'s signal still stops its wait', async () => {
  prefetch('fail');
  await assert.rejects(earlyFetch('fail'), /network down/); // the network is still down for the page's own request
  assert.deepEqual(asked, ['fail', 'fail']);
  answers['/slow'] = {};
  prefetch('/slow');
  const ctl = new AbortController();
  ctl.abort();
  await assert.rejects(earlyFetch('/slow', { signal: ctl.signal }), (e: Error) => e.name === 'AbortError');
});

test('the first view of the brief asks for the company, its dates, the sets and the brief file of the view date', async () => {
  answers['/api/kb/organizations'] = [{ id: 'company' }];
  answers['/api/kb/company/dates'] = { default: '2026-10-26' };
  answers['/api/kb/company/status'] = {};
  answers['/api/variator/artifacts'] = { sets: [
    { id: 'history-old', manifest: { schema: 2, kind: 'history-metrics', asOf: '2026-10-19', brief: { role: 'ceo', file: 'briefs/ceo.json' } } },
    { id: 'history-metrics-ceo', manifest: { schema: 2, kind: 'history-metrics', asOf: '2026-10-26', brief: { role: 'ceo', file: 'briefs/ceo.json' } } },
  ] };
  startFirstView('/brief', '?tour=reviewer');
  await settle();
  assert.deepEqual(asked.sort(), ['/api/kb/company/dates', '/api/kb/company/status', '/api/kb/organizations', '/api/variator/artifacts', '/api/variator/artifacts/history-metrics-ceo/briefs/ceo.json']);
  // the pages take these answers: no second request
  for (const url of ['/api/kb/organizations', '/api/kb/company/dates', '/api/variator/artifacts']) await (await earlyFetch(url)).json();
  assert.equal(asked.length, 5);
});

test('the first view of a case asks for its files and, from the sets, its month file and day file', async () => {
  answers['/api/kb/organizations'] = [{ id: 'company' }];
  answers['/api/variator/artifacts'] = { sets: [
    { id: 'future-company_best_year', manifest: { schema: 'variator.future/1', case: 'company_best_year', files: { 'month.json': 'x' } } },
    { id: 'day-company_best_year', manifest: { schema: 'variator.day/1', case: 'company_best_year', files: ['day_examples.json'] } },
    { id: 'future-delivery_best_way', manifest: { schema: 'variator.future/1', case: 'delivery_best_way' } },
  ] };
  const cases: string[] = [];
  startFirstView('/cases/company_best_year', '', { readCase: async id => { cases.push(id); } });
  await settle();
  assert.deepEqual(cases, ['company_best_year']);
  assert.ok(asked.includes('/api/variator/artifacts/future-company_best_year/month.json'), asked.join());
  assert.ok(asked.includes('/api/variator/artifacts/day-company_best_year/day_examples.json'), asked.join());
  assert.ok(!asked.some(u => u.includes('delivery_best_way')));
});
