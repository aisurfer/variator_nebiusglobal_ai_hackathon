// The app's base on its site: the frontend's Vite `base` (vite.config.ts) without the last slash. Caddy takes it off
// before a request reaches this server (handle_path /workspace/*); a direct visit (the Vite dev proxy, a check on
// port 3002) keeps it. The server answers both the same way.
export const BASE_PATH = '/workspace';

/** The request URL without the app's base ('/workspace/brief' -> '/brief', '/workspace?x' -> '/?x'); null when the
 *  URL is outside the base. */
export function stripBasePath(url: string, base = BASE_PATH): string | null {
  if (url !== base && !url.startsWith(`${base}/`) && !url.startsWith(`${base}?`)) return null;
  const rest = url.slice(base.length);
  return rest.startsWith('/') ? rest : `/${rest}`;
}
