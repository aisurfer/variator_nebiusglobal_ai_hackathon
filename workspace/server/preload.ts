// The first view's API requests named in the page itself (WP29, NFR7: the first view in under 3 s). index.html gets a
// <link rel="preload" as="fetch"> per request the page of the address will make at once, so the browser sends them
// with the page instead of after the app's code has arrived and run; the app takes the answers (src/early.ts,
// src/firstView.ts). Every page reads the company, its dates and the state of the sources; the brief, the actors and
// a case page (not the list of cases) also read the list of artifact sets. The URLs are the app's own, byte for byte (src/kb/api.ts):
// a preload the app does not ask for is wasted, never wrong. A simulated future (URL parameter `branch` on a
// knowledge-base page) is checked by the app first: its reads are not preloaded.
import { BASE_PATH } from './basePath.js';

const BRANCH_PAGES = new Set(['brief', 'ask', 'sources']);
const OTHER_PAGES = new Set(['ask', 'sources', 'actors', 'cases']);

/** The API URLs the page of `url` (an in-app URL: '/brief?tour=reviewer', '/cases/x') asks for first. */
export function firstViewRequests(url: string, company: { id: string } | null, base = BASE_PATH): string[] {
  const [path, query = ''] = url.split('#')[0].split('?');
  const params = new URLSearchParams(query);
  const segment = path.split('/')[1] ?? '';
  const page = OTHER_PAGES.has(segment) ? segment : 'brief'; // the brief is the page of `/` and of unknown paths
  const out = [`${base}/api/kb/organizations`];
  const future = BRANCH_PAGES.has(page) && !!params.get('branch');
  if (company && !future) {
    const asOf = params.get('asOf');
    const q = new URLSearchParams();
    if (asOf) q.set('asOf', asOf);
    out.push(`${base}/api/kb/${company.id}/dates`, `${base}/api/kb/${company.id}/status${q.size ? `?${q}` : ''}`);
  }
  const sets = page === 'brief' || page === 'actors' || (page === 'cases' && !!path.split('/')[2]);
  if (sets && !future) out.push(`${base}/api/variator/artifacts`);
  return out;
}

const attr = (v: string) => v.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

/** index.html with the preloads of the first view of `url` in its head, before the app's code (unchanged without a
 *  head): the small API answers go out first and are back while the code is still downloading. */
export function withPreloads(html: string, url: string, company: { id: string } | null): string {
  const head = html.indexOf('</head>');
  if (head < 0) return html;
  const script = html.indexOf('<script');
  const at = script >= 0 && script < head ? script : head;
  const links = firstViewRequests(url, company).map(href => `<link rel="preload" as="fetch" crossorigin href="${attr(href)}">`).join('');
  return html.slice(0, at) + links + html.slice(at);
}
