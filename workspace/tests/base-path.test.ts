// The app under /workspace/ (WP27): the browser helpers put the base before in-app paths and take it off browser paths;
// the workspace server answers the same routes with and without the base (caddy takes it off, a direct visit keeps it)
// and the VARIATOR proxy forwards the path without it.
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { joinBase, stripBase } from '../src/appUrl.js';
import { stripBasePath } from '../server/basePath.js';
import { createApp } from '../server/app.js';
import { ORG } from './kbFixture.js';

const servers: Server[] = [];
after(() => { for (const s of servers) { s.closeAllConnections(); s.close(); } });
const urlOf = (server: Server) => `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
const listen = (server: Server) => new Promise<string>(resolve => { servers.push(server); server.listen(0, '127.0.0.1', () => resolve(urlOf(server))); });

test('an in-app path gets the base; a relative href or a full URL stays', () => {
  assert.equal(joinBase('/workspace', '/brief?asOf=2026-10-19'), '/workspace/brief?asOf=2026-10-19');
  assert.equal(joinBase('/workspace', '/api/kb/organizations'), '/workspace/api/kb/organizations');
  assert.equal(joinBase('/workspace', '?entity=org.CL-001'), '?entity=org.CL-001');
  assert.equal(joinBase('/workspace', '#day'), '#day');
  assert.equal(joinBase('/workspace', 'https://example.com/x'), 'https://example.com/x');
  assert.equal(joinBase('/workspace', '//cdn.example.com/x'), '//cdn.example.com/x');
  assert.equal(joinBase('', '/brief'), '/brief');
});

test('a browser path loses the base; a path outside it is null', () => {
  assert.equal(stripBase('/workspace', '/workspace/cases/company_best_year'), '/cases/company_best_year');
  assert.equal(stripBase('/workspace', '/workspace/'), '/');
  assert.equal(stripBase('/workspace', '/workspace'), '/');
  assert.equal(stripBase('/workspace', '/brief'), null);
  assert.equal(stripBase('/workspace', '/workspaces'), null);
  assert.equal(stripBase('', '/brief'), '/brief');
});

test('the server takes the base off a request URL', () => {
  assert.equal(stripBasePath('/workspace/api/health'), '/api/health');
  assert.equal(stripBasePath('/workspace/'), '/');
  assert.equal(stripBasePath('/workspace'), '/');
  assert.equal(stripBasePath('/workspace?tour=reviewer'), '/?tour=reviewer');
  assert.equal(stripBasePath('/workspaces/x'), null);
  assert.equal(stripBasePath('/api/health'), null);
});

test('the server answers the same routes with and without the base; the proxy drops the base', async () => {
  const seen: string[] = [];
  const api = await listen(createServer((req, res) => { seen.push(req.url ?? ''); res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"ok":true}'); }));
  const dist = mkdtempSync(join(tmpdir(), 'ws-dist-'));
  writeFileSync(join(dist, 'index.html'), '<!doctype html><div id="root"></div>');
  const app = await listen(createServer(createApp({ distDir: dist, kb: { organizations: [ORG] }, variator: { baseUrl: api } })));
  for (const path of ['/api/health', '/workspace/api/health']) {
    const res = await fetch(`${app}${path}`);
    assert.equal(res.status, 200, path);
    assert.equal((await res.json()).status, 'ok', path);
  }
  for (const path of ['/workspace/api/variator/artifacts?x=1', '/api/variator/artifacts?x=1']) assert.equal((await fetch(`${app}${path}`)).status, 200, path);
  assert.deepEqual(seen, ['/api/variator/artifacts?x=1', '/api/variator/artifacts?x=1']);
  for (const path of ['/workspace/', '/workspace/brief', '/workspace/cases/company_best_year?tour=1', '/brief']) {
    const res = await fetch(`${app}${path}`, { redirect: 'manual' });
    assert.equal(res.status, 200, path);
    assert.match(await res.text(), /<div id="root">/, path);
  }
  assert.equal((await fetch(`${app}/workspace/api/no-such-route`)).status, 404);
});
