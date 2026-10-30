// The pass-through proxy /api/variator/* -> ${VARIATOR_API_URL}/api/variator/*: method, full path, query, headers and body
// reach the VARIATOR API unchanged; its status, headers and body come back; a down or silent API gives a clear JSON error.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer, type IncomingMessage, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { createApp, type AppOptions } from '../server/app.js';
import { ORG } from './kbFixture.js';

const servers: Server[] = [];
after(() => { for (const s of servers) { s.closeAllConnections(); s.close(); } });
const urlOf = (server: Server) => `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
const listen = (server: Server) => new Promise<string>(resolve => { servers.push(server); server.listen(0, '127.0.0.1', () => resolve(urlOf(server))); });
const workspace = (variator: AppOptions['variator']) => new Promise<string>(resolve => { const s = createApp({ kb: { organizations: [ORG] }, variator }).listen(0, '127.0.0.1', () => resolve(urlOf(s))); servers.push(s); });
const body = (req: IncomingMessage) => new Promise<string>(resolve => { let s = ''; req.on('data', c => { s += c; }); req.on('end', () => resolve(s)); });

test('requests pass through to the VARIATOR API and answers come back unchanged', async () => {
  const seen: { method?: string; url?: string; type?: string; body: string }[] = [];
  const base = await listen(createServer(async (req, res) => {
    seen.push({ method: req.method, url: req.url, type: req.headers['content-type'], body: await body(req) });
    if (req.url?.startsWith('/api/variator/missing')) { res.writeHead(404, { 'content-type': 'application/json' }); res.end('{"detail":"Not Found"}'); return; }
    res.writeHead(201, { 'content-type': 'application/json', 'x-run-id': 'r-1' });
    res.end(JSON.stringify({ ok: true, path: req.url }));
  }));
  const app = await workspace({ baseUrl: `${base}/` });
  const get = await fetch(`${app}/api/variator/cases/b?level=month&x=1`);
  assert.equal(get.status, 201);
  assert.equal(get.headers.get('x-run-id'), 'r-1');
  assert.deepEqual(await get.json(), { ok: true, path: '/api/variator/cases/b?level=month&x=1' });
  const post = await fetch(`${app}/api/variator/runs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ case: 'B', seed: 7 }) });
  assert.equal(post.status, 201);
  await post.json();
  assert.deepEqual(seen.at(-1), { method: 'POST', url: '/api/variator/runs', type: 'application/json', body: '{"case":"B","seed":7}' });
  const missing = await fetch(`${app}/api/variator/missing`);
  assert.equal(missing.status, 404);
  assert.deepEqual(await missing.json(), { detail: 'Not Found' });
});

test('a VARIATOR API that is down gives 502, one that does not answer in time gives 504, both as JSON', async () => {
  const closed = createServer();
  const deadUrl = await new Promise<string>(resolve => closed.listen(0, '127.0.0.1', () => resolve(urlOf(closed))));
  await new Promise(resolve => closed.close(resolve));
  const silentUrl = await listen(createServer(() => { /* never answers */ }));
  const down = await workspace({ baseUrl: deadUrl });
  const slow = await workspace({ baseUrl: silentUrl, timeoutMs: 300 });
  const a = await fetch(`${down}/api/variator/health`);
  assert.equal(a.status, 502);
  const ab = await a.json();
  assert.match(ab.error, /^The VARIATOR API is unreachable/);
  assert.equal(ab.service, 'variator-api');
  const b = await fetch(`${slow}/api/variator/health`);
  assert.equal(b.status, 504);
  assert.equal((await b.json()).error, 'The VARIATOR API did not answer within 300 ms');
});

test('the workspace health does not depend on the VARIATOR API or the knowledge base', async () => {
  const app = await workspace({ baseUrl: 'http://127.0.0.1:9' });
  const res = await fetch(`${app}/api/health`);
  assert.equal(res.status, 200);
  assert.equal((await res.json()).company.name, 'Larchwood Systems');
});
