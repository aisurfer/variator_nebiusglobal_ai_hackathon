// The actor cards through the VARIATOR API proxy (/api/variator/artifacts): the set list, then cards.json and
// risks.json of the set picked for the view date (src/variator/web/app.py serves them read-only).
import { useEffect, useState } from 'react';
import { cardSets, pickCardSet } from './model';
import type { CardSet, CardsFile, RisksFile } from './types';
import { apiUrl } from '../../appUrl';
import { earlyFetch } from '../../early';

const API = apiUrl('/api/variator/artifacts');
/** The URL of a file of a card set (cards.json, risks.json). */
export const cardFileUrl = (set: string, file: string) => `${API}/${encodeURIComponent(set)}/${file}`;
const TIMEOUT_MS = 8000;

async function getJson(url: string): Promise<unknown> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    // the first view of the actors starts the list and the card files early (src/firstView.ts)
    const res = await earlyFetch(url, { signal: ctl.signal });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const msg = (body as { error?: { message?: string } | string } | null)?.error;
      throw new Error(typeof msg === 'string' ? msg : msg?.message ?? `HTTP ${res.status}`);
    }
    return body;
  } catch (e) {
    throw new Error((e as Error)?.name === 'AbortError' ? 'the VARIATOR API did not answer' : (e as Error).message);
  } finally {
    clearTimeout(timer);
  }
}

export type ActorData =
  | { status: 'loading' }
  | { status: 'error'; error: string }
  | { status: 'none' }
  | { status: 'later'; sets: CardSet[] }
  | { status: 'ready'; set: CardSet; cards: CardsFile; risks: RisksFile };

const cache = new Map<string, Promise<{ cards: CardsFile; risks: RisksFile }>>();

/** The cards and the risk register for a view date (the last day of data when undefined). */
export function useActorData(viewDate: string | undefined): ActorData {
  const [data, setData] = useState<ActorData>({ status: 'loading' });
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const sets = cardSets(await getJson(API));
        if (!sets.length) { if (live) setData({ status: 'none' }); return; }
        const set = pickCardSet(sets, viewDate);
        if (!set) { if (live) setData({ status: 'later', sets }); return; }
        const key = `${set.id}@${set.builtAt}`;
        if (!cache.has(key)) {
          const p = Promise.all([getJson(cardFileUrl(set.id, 'cards.json')), getJson(cardFileUrl(set.id, 'risks.json'))])
            .then(([c, r]) => ({ cards: c as CardsFile, risks: r as RisksFile }));
          p.catch(() => cache.delete(key));
          cache.set(key, p);
        }
        const { cards, risks } = await cache.get(key)!;
        if (!Array.isArray(cards?.cards) || !Array.isArray(risks?.risks)) throw new Error('the card files have an unknown shape');
        if (live) setData({ status: 'ready', set, cards, risks });
      } catch (e) {
        if (live) setData({ status: 'error', error: (e as Error).message });
      }
    })();
    return () => { live = false; };
  }, [viewDate]);
  return data;
}
