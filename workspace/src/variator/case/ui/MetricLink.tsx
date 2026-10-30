// The name of a simulated metric, linked to its definition in the VARIATOR metric catalogue (the side sheet with
// the definition, the steps and the data sources). The simulation's own list is src/variator/future/export.py.
import { useLocation, useNavigate } from 'react-router-dom';

/** Simulated metric id → catalogue metric id (src/variator/metrics/catalog/registry.py). */
const CATALOGUE: Record<string, string> = {
  cash: 'fin.cash', load: 'ops.team_load', incidents: 'ops.incidents', arr: 'rev.arr', new_arr: 'rev.new_arr',
  headcount: 'ppl.headcount', churn: 'rev.churn', runway: 'fin.runway', burn: 'fin.burn', nps: 'cust.nps', sla: 'ops.sla',
};

export function MetricLink({ id, label }: { id: string; label: string }) {
  const location = useLocation(), navigate = useNavigate(), target = CATALOGUE[id];
  if (!target) return <span>{label}</span>;
  const open = () => { const q = new URLSearchParams(location.search); q.set('metric', target); q.delete('entity'); navigate(`${location.pathname}?${q}`, { state: { kbBack: true } }); };
  return <button type="button" className="cs-metric-link" onClick={open} title={`${label}: definition and data sources in the VARIATOR metric catalogue`}>{label}</button>;
}
