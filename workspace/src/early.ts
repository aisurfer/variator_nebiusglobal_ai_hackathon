// The first view's requests, started before the code that reads them has arrived (WP29, NFR7: the first view in under
// 3 s). Without this a page asks for its data only after its code has loaded and run, one round trip after another
// (the company, then its dates and the artifact sets, then the brief). Now:
//   1. the server names the requests it can name in index.html (<link rel="preload" as="fetch">, server/preload.ts):
//      the browser sends them with the page, while the code downloads;
//   2. src/firstView.ts asks for them before the first render (the browser hands over the preloaded answers) and for
//      the requests that need one of those answers first (the brief file of the view date, the month file of a case);
//   3. a page reads through `earlyFetch`: a read of a URL started here takes that answer, any other read (or one after
//      the first seconds of the visit) goes to the network as before. An answer is taken once, except a `shared` one
//      (the list of artifact sets, read by several parts of a page), which each reader takes in those seconds.
interface Early { res: Promise<Response>; shared: boolean; until: number; json?: Promise<unknown> }
const early = new Map<string, Early>();
/** How long an early answer may stand for a read: the first view; later reads go to the network. */
const FRESH_MS = 15_000;

/** Starts a GET of `url` for a later `earlyFetch` (once per URL). `peek`: also returns its JSON body (null when the
 *  request fails or answers an error) without taking the answer from the page. */
export function prefetch(url: string, opts: { shared?: boolean; peek?: boolean; priority?: 'high' | 'low' | 'auto' } = {}): Promise<unknown> {
  let e = early.get(url);
  if (!e) {
    const res = fetch(url, opts.priority ? { priority: opts.priority } as RequestInit : undefined);
    res.catch(() => undefined); // a failed early request: the page's own read goes to the network
    e = { res, shared: !!opts.shared, until: performance.now() + FRESH_MS };
    early.set(url, e);
    const mine = e;
    const timer: unknown = setTimeout(() => { if (early.get(url) === mine) early.delete(url); }, FRESH_MS); // no answer is kept past its time
    (timer as { unref?: () => void }).unref?.(); // (node tests: the timer does not hold the process)
  }
  // the copy is made in the first callback on the answer: always before a reader takes the body
  if (opts.peek) e.json ??= e.res.then(r => r.ok ? r.clone().json() : null).catch(() => null);
  return e.json ?? Promise.resolve(null);
}

/** `fetch`, taking the answer of a request started early for this URL when there is one (a plain GET only). */
export function earlyFetch(url: string, init?: RequestInit): Promise<Response> {
  const e = early.get(url);
  const plain = !init || ((!init.method || init.method.toUpperCase() === 'GET') && init.body == null);
  if (!e || !plain || performance.now() > e.until) {
    if (e && performance.now() > e.until) early.delete(url);
    return fetch(url, init);
  }
  if (!e.shared) early.delete(url);
  // a shared answer that is an error is not handed on: each reader asks again, as it did before (a retry stays a retry)
  const taken = (e.shared ? e.res.then(r => r.ok ? r.clone() : fetch(url, init)) : e.res).catch(() => fetch(url, init));
  const signal = init?.signal;
  if (!signal) return taken;
  // the reader's signal still stops its wait (a timeout, a page that closed), as it would stop its own request
  return new Promise<Response>((resolve, reject) => {
    const stop = () => reject(signal.reason ?? new DOMException('The operation was aborted.', 'AbortError'));
    if (signal.aborted) return stop();
    signal.addEventListener('abort', stop, { once: true });
    taken.then(resolve, reject).finally(() => signal.removeEventListener('abort', stop));
  });
}

/** Test hook: forget every early request. */
export function resetEarly(): void { early.clear(); }
