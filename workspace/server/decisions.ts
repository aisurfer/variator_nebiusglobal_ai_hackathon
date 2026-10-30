// Decisions of the case pages: per case, the draft and the committed decision (the chosen fork, warning signs,
// backup plans and fixes). A small JSON store in DECISIONS_DIR (a volume in deploy/mvp/compose.yaml), shared by every
// user of the company. Docs/TASKS.md: move it to the knowledge base as Decision records.
import express, { type Router } from 'express';
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const ID = /^[a-z0-9_-]{1,80}$/i;
const MAX_BYTES = 256 * 1024;

export function createDecisionsRouter(dir = resolve(process.env.DECISIONS_DIR ?? 'data/decisions')): Router {
  const router = express.Router();
  const file = (caseId: string) => join(dir, `${caseId}.json`);
  router.get('/:caseId', (req, res) => {
    if (!ID.test(req.params.caseId)) return void res.status(400).json({ error: 'Bad case id' });
    try { res.json(JSON.parse(readFileSync(file(req.params.caseId), 'utf8'))); } catch { res.json({ draft: null, committed: null }); }
  });
  router.put('/:caseId', express.json({ limit: MAX_BYTES }), (req, res) => {
    if (!ID.test(req.params.caseId)) return void res.status(400).json({ error: 'Bad case id' });
    const body = req.body as { draft?: unknown; committed?: unknown } | null;
    if (!body || typeof body !== 'object') return void res.status(400).json({ error: 'Expected { draft, committed }' });
    mkdirSync(dir, { recursive: true });
    // write a temporary file and rename it: a crash never leaves half a file
    const tmp = `${file(req.params.caseId)}.tmp`;
    writeFileSync(tmp, JSON.stringify({ draft: body.draft ?? null, committed: body.committed ?? null, savedAt: new Date().toISOString() }));
    renameSync(tmp, file(req.params.caseId));
    res.json({ ok: true });
  });
  return router;
}
