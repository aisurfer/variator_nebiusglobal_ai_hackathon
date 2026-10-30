// The workspace server: the knowledge-base API of the company (/api/kb/*), the pass-through proxy to the VARIATOR API
// (/api/variator/*), health, and the built frontend (dist/) with a single-page fallback.
import express, { type ErrorRequestHandler } from 'express';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { stripBasePath } from './basePath.js';
import { withPreloads } from './preload.js';
import { createKbRouter, loadKbOrganizations } from './kb/router.js';
import { createVariatorProxy, type VariatorProxyOptions } from './variatorProxy.js';
import { createDecisionsRouter } from './decisions.js';

export interface AppOptions { distDir?: string; kb?: Parameters<typeof createKbRouter>[0]; variator?: VariatorProxyOptions }

export function createApp({ distDir = resolve('dist'), kb, variator }: AppOptions = {}) {
  const app = express();
  app.disable('x-powered-by');
  // One set of routes with or without the app's base (server/basePath.ts). No redirect here: behind caddy the two
  // look the same, so a redirect would loop.
  app.use((req, _res, next) => { const rest = stripBasePath(req.url); if (rest !== null) req.url = rest; next(); });
  const organizations = kb?.organizations ?? loadKbOrganizations();
  const company = organizations[0] ?? null;
  app.get('/api/health', (_req, res) => {
    res.json({ status: 'ok', company: company ? { id: company.id, name: company.name, tenant: company.tenant } : null });
  });
  app.use('/api/kb', createKbRouter({ ...kb, organizations }));
  app.use('/api/variator', createVariatorProxy(variator));
  app.use('/api/decisions', createDecisionsRouter());
  app.use('/api', (_req, res) => { res.status(404).json({ error: 'API route not found' }); });
  if (existsSync(join(distDir, 'index.html'))) {
    // WP29: the built files have content hashes in their names (assets/): a browser keeps them for good; index.html is
    // asked again on every visit (a new release shows at once) and names the first view's API requests (preload.ts)
    app.use(express.static(distDir, {
      index: false,
      setHeaders: (res, file) => { if (/[\\/]assets[\\/]/.test(file)) res.setHeader('Cache-Control', 'public, max-age=31536000, immutable'); },
    }));
    // a file of an older release is gone: 404, never the page in its place (the app reloads to the new release)
    app.use('/assets', (_req, res) => { res.status(404).type('text').send('Not found'); });
    const page = indexPage(join(resolve(distDir), 'index.html'));
    app.get('/{*path}', (req, res) => {
      res.setHeader('Cache-Control', 'no-cache');
      res.type('html').send(withPreloads(page(), req.url, company));
    });
  }
  app.use((_req, res) => { res.status(404).json({ error: 'Route not found. Build the frontend with npm run build.' }); });
  const errorHandler: ErrorRequestHandler = (error, _req, res, _next) => {
    console.error('[api]', error instanceof Error ? error.message : error);
    if (res.headersSent) return;
    res.status(500).json({ error: 'The workspace server failed to answer this request.' });
  };
  app.use(errorHandler);
  return app;
}

/** The text of index.html, read again when the file changes (a new build under a running server). */
function indexPage(file: string): () => string {
  let mtime = -1, text = '';
  return () => {
    const m = statSync(file).mtimeMs;
    if (m !== mtime) { text = readFileSync(file, 'utf8'); mtime = m; }
    return text;
  };
}
