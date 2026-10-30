// Optional runtime services for primitives, provided by the page that hosts them (a real API or
// fixtures). Primitives work without it: they just skip the extras.
import { createContext, useContext, useEffect, useState } from 'react';
import type { MetricResult, RecordRef } from '../contracts';

export interface PrimitiveRuntime {
  /** Open an atomic record (directive, issue, project, …) in the detail panel. */
  onDetail?: (detail: NonNullable<RecordRef['detail']>) => void;
  /** Full result of a metric on another morning, e.g. to show how index components moved. */
  loadResult?: (metricId: string, asOf: string) => Promise<MetricResult | null>;
}
export const PrimitiveRuntimeContext = createContext<PrimitiveRuntime>({});
export const usePrimitiveRuntime = () => useContext(PrimitiveRuntimeContext);

/** Result of the same metric on the comparison morning; null while loading or unavailable. */
export function useComparedResult(metricId: string, asOf: string | null): MetricResult | null {
  const { loadResult } = usePrimitiveRuntime();
  const [state, setState] = useState<{ key: string; result: MetricResult | null } | null>(null);
  const key = `${metricId}@${asOf}`;
  useEffect(() => {
    if (!loadResult || !asOf) return;
    let live = true;
    loadResult(metricId, asOf).then(result => { if (live) setState({ key, result }); }).catch(() => { if (live) setState({ key, result: null }); });
    return () => { live = false; };
  }, [loadResult, metricId, asOf, key]);
  return state?.key === key ? state.result : null;
}
