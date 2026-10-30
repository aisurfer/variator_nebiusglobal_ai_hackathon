// Starts the workspace server. Configuration comes from the environment: HOST, PORT, KB_URL, VARIATOR_API_URL,
// COMPANY_FILE (see README.md). HOST is the address to listen on: 0.0.0.0 by default (the container), 127.0.0.1 for
// a dev server on this machine only. Nothing is stored locally: the knowledge base and the VARIATOR API hold all data.
import { createApp } from './app.js';
import { loadKbOrganizations } from './kb/router.js';

const port = Number(process.env.PORT ?? 3002);
if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('PORT must be an integer from 0 to 65535');
const host = (process.env.HOST ?? '').trim() || '0.0.0.0';
if (/\s|\/|^\[|\]$/.test(host)) throw new Error('HOST must be a host name or an IP address, for example 127.0.0.1 or ::1');
const organizations = loadKbOrganizations();
if (!organizations.length) console.error('No company is configured: config/company.json (or COMPANY_FILE) is missing or invalid.');
const server = createApp({ kb: { organizations } }).listen(port, host, (error?: Error) => {
  if (error) { console.error(`Unable to start the workspace on ${host}:${port}: ${error.message}`); process.exitCode = 1; return; }
  const address = server.address(), bound = address && typeof address === 'object' ? address.port : port;
  console.log(`Workspace listening on http://${host.includes(':') ? `[${host}]` : host}:${bound} · company ${organizations[0]?.name ?? '(none)'} · knowledge base ${process.env.KB_URL ?? 'http://127.0.0.1:8720'} · VARIATOR API ${process.env.VARIATOR_API_URL ?? 'http://127.0.0.1:8730'}`);
  if (process.env.KB_PREWARM !== '0') void prewarm(`http://${host === '0.0.0.0' || host === '::' ? '127.0.0.1' : host.includes(':') ? `[${host}]` : host}:${bound}`);
});

/** In the background after the start (KB_PREWARM=0: off): the branch list, the view dates and the brief of the company
 * at its default date, so the first visitor gets them from the cache. A failure only logs. */
async function prewarm(origin: string) {
  const started = Date.now();
  for (const o of organizations) {
    for (const path of ['/api/kb/organizations', `/api/kb/${o.id}/dates`, `/api/kb/${o.id}/brief`, `/api/kb/${o.id}/status`]) {
      try {
        const res = await fetch(origin + path, { signal: AbortSignal.timeout(300_000) });
        if (!res.ok) throw new Error(`${res.status}`);
        await res.arrayBuffer();
      } catch (error) { console.error(`[prewarm] ${path}: ${error instanceof Error ? error.message : error}`); return; }
    }
  }
  console.log(`[prewarm] brief of ${organizations.map(o => o.id).join(', ')} cached in ${((Date.now() - started) / 1000).toFixed(1)} s`);
}
for (const signal of ['SIGTERM', 'SIGINT'] as const) process.on(signal, () => server.close(() => process.exit(0)));
