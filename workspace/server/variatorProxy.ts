// Pass-through proxy to the VARIATOR API (src/variator/web, Starlette). The canonical API path is /api/variator/*:
// /api/variator/<path>?<query> is forwarded with the full path unchanged to ${VARIATOR_API_URL}/api/variator/<path>?<query>,
// with the same method, headers and body; the answer is streamed back as is.
// The timeout covers the wait for the response headers; a streamed body (progress of a live run) is not cut.
import express, { Router, type Request, type Response } from 'express';
import { Readable } from 'node:stream';
import { stripBasePath } from './basePath.js';

export const DEFAULT_VARIATOR_API_URL = 'http://127.0.0.1:8730';
// Connection-level headers are not forwarded; the body is re-sent, so its length and encoding are set again.
const HOP_BY_HOP = new Set(['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length', 'accept-encoding']);
const DROP_FROM_RESPONSE = new Set([...HOP_BY_HOP, 'content-encoding']);

export interface VariatorProxyOptions { baseUrl?: string; timeoutMs?: number }

export function createVariatorProxy(options: VariatorProxyOptions = {}) {
  const baseUrl = (options.baseUrl ?? process.env.VARIATOR_API_URL ?? DEFAULT_VARIATOR_API_URL).replace(/\/+$/, '');
  const timeoutMs = options.timeoutMs ?? Number(process.env.VARIATOR_TIMEOUT_MS ?? 30_000);
  const router = Router();
  router.use(express.raw({ type: () => true, limit: process.env.VARIATOR_BODY_LIMIT ?? '10mb' }));
  router.use(async (req: Request, res: Response) => {
    // the full path without the app's base (the VARIATOR API serves /api/variator/*)
    const target = `${baseUrl}${stripBasePath(req.originalUrl) ?? req.originalUrl}`;
    const headers = new Headers();
    for (const [k, v] of Object.entries(req.headers)) {
      if (v === undefined || HOP_BY_HOP.has(k)) continue;
      headers.set(k, Array.isArray(v) ? v.join(', ') : v);
    }
    if (req.ip) headers.set('x-forwarded-for', req.ip);
    const hasBody = !['GET', 'HEAD'].includes(req.method) && Buffer.isBuffer(req.body) && req.body.length > 0;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    // a client that goes away cancels the upstream request
    res.on('close', () => { if (!res.writableEnded) ctl.abort(); });
    let upstream: globalThis.Response;
    try {
      upstream = await fetch(target, { method: req.method, headers, body: hasBody ? new Uint8Array(req.body as Buffer) : undefined, signal: ctl.signal, redirect: 'manual' });
    } catch (error) {
      clearTimeout(timer);
      if (res.headersSent || res.destroyed) return;
      const timedOut = (error as Error)?.name === 'AbortError';
      const cause = (error as { cause?: { code?: string; message?: string } })?.cause;
      const reason = cause?.code ?? cause?.message ?? (error as Error)?.message ?? String(error);
      res.status(timedOut ? 504 : 502).json({
        error: timedOut ? `The VARIATOR API did not answer within ${timeoutMs >= 1000 ? `${Math.round(timeoutMs / 1000)} s` : `${timeoutMs} ms`}` : `The VARIATOR API is unreachable (${reason})`,
        service: 'variator-api', url: baseUrl,
      });
      return;
    }
    clearTimeout(timer);
    res.status(upstream.status);
    upstream.headers.forEach((value, key) => { if (!DROP_FROM_RESPONSE.has(key) && key !== 'set-cookie') res.setHeader(key, value); });
    const cookies = upstream.headers.getSetCookie();
    if (cookies.length) res.setHeader('set-cookie', cookies);
    if (!upstream.body || req.method === 'HEAD') { res.end(); return; }
    Readable.fromWeb(upstream.body as import('node:stream/web').ReadableStream).on('error', () => res.destroy()).pipe(res);
  });
  return router;
}
