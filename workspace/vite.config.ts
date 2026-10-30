import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// The app lives under /workspace/ on its site (src/appUrl.ts): pages, assets and API calls. The root of the domain
// holds the landing page (deploy/mvp/Caddyfile). Keep BASE_PATH in server/app.ts equal to this base.
// Dev server: the UI on VITE_PORT (5173) at /workspace/, /workspace/api proxied to the workspace server on API_PORT
// (3002, the PORT of npm run dev:server); that server accepts paths with and without the base.
// API_TARGET (for example https://variator.trijet.tech) sends /workspace/api to a deployed site instead: UI work
// with hot reload and no local stack.
const apiPort = process.env.API_PORT ?? '3002';
const apiTarget = process.env.API_TARGET ?? `http://127.0.0.1:${apiPort}`;

// WP29 (NFR7, the first view in under 3 s): the pages other than the brief are loaded when they open (src/pages.tsx),
// and the heavy libraries that only some of them use get chunks of their own, so the first view loads only what it
// shows and a browser keeps the libraries across releases. A package listed here must not be imported by the entry
// (main.tsx, App, the brief): the entry would then load its whole chunk.
const VENDOR: [chunk: string, packages: RegExp][] = [
  ['react', /^(react|react-dom|scheduler|react-router|react-router-dom|cookie|set-cookie-parser)$/],
  ['charts', /^(recharts|victory-vendor|d3-.+|internmap|decimal\.js-light|es-toolkit|@reduxjs\/toolkit|redux|redux-thunk|react-redux|reselect|immer|eventemitter3|tiny-invariant|use-sync-external-store|react-is|clsx)$/],
  ['markdown', /^(react-markdown|unified|bail|trough|extend|is-plain-obj|devlop|vfile|vfile-message|remark-.+|micromark|micromark-.+|mdast-.+|hast-.+|hastscript|unist-.+|property-information|space-separated-tokens|comma-separated-tokens|decode-named-character-reference|character-entities.*|estree-util-.+|html-url-attributes|inline-style-parser|style-to-js|style-to-object|trim-lines|ccount|@ungap\/structured-clone|zwitch|longest-streak|markdown-table)$/],
  ['dialog', /^(@radix-ui\/.+|react-remove-scroll|react-remove-scroll-bar|react-style-singleton|use-callback-ref|use-sidecar|get-nonce|aria-hidden|tslib)$/],
];
function vendorChunk(id: string): string | undefined {
  const at = id.lastIndexOf('/node_modules/');
  if (at < 0) return undefined;
  const parts = id.slice(at + 14).split('/');
  const name = parts[0].startsWith('@') ? `${parts[0]}/${parts[1]}` : parts[0];
  return VENDOR.find(([, re]) => re.test(name))?.[0];
}

export default defineConfig({
  base: '/workspace/',
  plugins: [react()],
  build: {
    // one stylesheet, in the order of the imports, as before the split: the cascade of the pages stays the same
    cssCodeSplit: false,
    rollupOptions: { output: { manualChunks: vendorChunk } },
  },
  server: { port: Number(process.env.VITE_PORT ?? 5173), strictPort: true, proxy: { '/workspace/api': { target: apiTarget, changeOrigin: true } } },
});
