// The place of the app on its site. Vite's `base` (vite.config.ts) puts the whole app under /workspace/: its pages,
// its assets and its API calls; the root of the domain holds the landing page. The router takes the base off the
// address (main.tsx), so in-app paths ('/brief', '/cases/<id>') stay as they are in code, configs and artifact sets.
// These helpers put the base before a URL that the browser reads itself: an href, a fetch. Outside Vite (node tests)
// the base is empty.
const env = (import.meta as { env?: { BASE_URL?: string } }).env;

/** The base path without the last slash: '/workspace' in a build or the dev server, '' in node tests. */
export const BASE = (env?.BASE_URL ?? '/').replace(/\/+$/, '');

/** `to` under `base`: an in-app path ('/brief?asOf=…') gets the base; a relative href ('?entity=…', '#day') or a full
 *  URL stays as it is. */
export const joinBase = (base: string, to: string) => (to.startsWith('/') && !to.startsWith('//') ? `${base}${to}` : to);

/** The in-app path of a browser path: the base taken off ('/workspace/brief' -> '/brief', '/workspace' -> '/');
 *  null for a path outside the base. */
export function stripBase(base: string, pathname: string): string | null {
  if (!base) return pathname;
  if (pathname === base) return '/';
  return pathname.startsWith(`${base}/`) ? pathname.slice(base.length) : null;
}

/** The URL of an API path of the workspace server ('/api/kb/…', '/api/variator/…'). */
export const apiUrl = (path: string) => joinBase(BASE, path);
/** The href of an in-app path ('/brief?asOf=…'), as a link shows it. */
export const appHref = (to: string) => joinBase(BASE, to);
/** The in-app path of `window.location.pathname` (a path outside the base stays as it is). */
export const appPath = (pathname: string) => stripBase(BASE, pathname) ?? pathname;
