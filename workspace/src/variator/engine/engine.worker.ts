// Web Worker: the heavy steps (simulate, plan search) off the main thread. Protocol: EngineRequest -> EngineResponse
// (types.ts). The typed arrays of a run are transferred, not copied. Create it with createEngineWorker() (client.ts).
import { loadCase, simulateCase, solvePlan } from './index.js';
import { runBuffers } from './simulate.js';
import type { EngineRequest, EngineResponse } from './types.js';

interface WorkerScope {
  onmessage: ((e: MessageEvent<EngineRequest>) => void) | null;
  postMessage(message: EngineResponse, transfer?: Transferable[]): void;
}
const scope = self as unknown as WorkerScope;

scope.onmessage = (e: MessageEvent<EngineRequest>) => {
  const req = e.data;
  try {
    const c = loadCase(req.config);
    if (req.type === 'simulate') {
      const run = simulateCase(c, req.state);
      scope.postMessage({ type: 'run', id: req.id, run }, runBuffers(run));
    } else if (req.type === 'solve') {
      const plan = solvePlan(c, req.state, req.option, { n: req.n, seed: req.seed });
      scope.postMessage({ type: 'plan', id: req.id, plan });
    }
  } catch (err) {
    scope.postMessage({ type: 'error', id: req.id, message: err instanceof Error ? err.message : String(err) });
  }
};
