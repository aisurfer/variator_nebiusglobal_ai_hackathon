// Engine client for the UI: promises over the Web Worker (engine.worker.ts), with an in-thread fallback when no
// Worker is available (tests, server-side render). Each call resolves with its own result; a caller that only wants
// the latest result compares the `seq` it gets back with its own last request.
import { loadCase, simulateCase, solvePlan } from './index.js';
import type { CaseConfig, CaseState, EngineRequest, EngineResponse, Plan, SimRun } from './types.js';

export interface EngineClient {
  /** Simulate the state (heavy). `seq` grows with each request. */
  simulate(config: CaseConfig, state: CaseState): { seq: number; result: Promise<SimRun> };
  /** Plan of levers for option j (heavy: several small simulations). */
  solve(config: CaseConfig, state: CaseState, option: number, opts?: { n?: number; seed?: number }): { seq: number; result: Promise<Plan> };
  dispose(): void;
}

/** The module worker (Vite bundles it). Returns null where Worker does not exist. */
export function createEngineWorker(): Worker | null {
  if (typeof Worker === 'undefined') return null;
  return new Worker(new URL('./engine.worker.ts', import.meta.url), { type: 'module' });
}

type Pending = { resolve: (v: never) => void; reject: (e: Error) => void };

export function createEngineClient(worker: Worker | null = createEngineWorker()): EngineClient {
  let seq = 0;
  const pending = new Map<number, Pending>();
  if (worker) {
    worker.onmessage = (e: MessageEvent<EngineResponse>) => {
      const msg = e.data, p = pending.get(msg.id);
      if (!p) return;
      pending.delete(msg.id);
      if (msg.type === 'error') p.reject(new Error(msg.message));
      else p.resolve((msg.type === 'run' ? msg.run : msg.plan) as never);
    };
    worker.onerror = (e: ErrorEvent) => {
      for (const p of pending.values()) p.reject(new Error(e.message || 'engine worker failed'));
      pending.clear();
    };
  }
  const send = <T>(req: EngineRequest, inline: () => T): Promise<T> => {
    if (!worker) {
      return new Promise<T>((resolve, reject) => {
        setTimeout(() => {
          try {
            resolve(inline());
          } catch (err) {
            reject(err instanceof Error ? err : new Error(String(err)));
          }
        }, 0);
      });
    }
    return new Promise<T>((resolve, reject) => {
      pending.set(req.id, { resolve: resolve as (v: never) => void, reject });
      worker.postMessage(req);
    });
  };
  return {
    simulate(config, state) {
      const id = ++seq;
      return { seq: id, result: send<SimRun>({ type: 'simulate', id, config, state }, () => simulateCase(loadCase(config), state)) };
    },
    solve(config, state, option, opts = {}) {
      const id = ++seq;
      return {
        seq: id,
        result: send<Plan>({ type: 'solve', id, config, state, option, n: opts.n, seed: opts.seed }, () => solvePlan(loadCase(config), state, option, opts)),
      };
    },
    dispose() {
      for (const p of pending.values()) p.reject(new Error('engine client disposed'));
      pending.clear();
      worker?.terminate();
    },
  };
}
