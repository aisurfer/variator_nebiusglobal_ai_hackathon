// Real connection state of a knowledge-base organization: sources, record counts, projection watermarks and the
// state of causal links, read from the knowledge base admin API.
import { Cable, RefreshCw } from 'lucide-react';
import type { KbOrganization, KbSourceStatus } from './types';
import { formatDate, KIND_LABEL } from './api';

export function KbSourcesPage({ org, status }: { org: KbOrganization; status: { data: KbSourceStatus | null; error: string; loading: boolean; reload: () => void } }) {
  const s = status.data;
  const journal = s?.watermark ?? 0;
  return <div className="kb-sources">
    <section className="page-intro"><div><div className="eyebrow intro-date">DATA SOURCES<span>·</span>TENANT {org.tenant.toUpperCase()}</div><h1>Where the brief comes from<span className="lime-period">.</span></h1>
      <p className="muted">Connectors write every change into the knowledge-base journal; search, graph and extraction follow it. The brief reads the journal through the API.</p></div>
      <button className="button" onClick={status.reload}><RefreshCw size={15} />Refresh</button></section>
    {status.error || s?.error ? <p className="kb-caveat">{status.error || s?.error}</p> : null}
    {s && <>
      <div className="kb-source-grid">{s.sources.map(x => <div key={x.id} className="card kb-source"><Cable size={18} /><div><strong>{x.name}</strong><small>{x.provider} · {x.id}</small></div><span className={`badge ${s.reachable ? 'badge-good' : 'badge-danger'}`}><i />{s.reachable ? 'Connected' : 'Unreachable'}</span></div>)}</div>
      <section className="card kb-panel"><div className="section-heading"><div><h2>Processing</h2><p>Journal position and how far each projection has caught up.</p></div></div>
        <div className="kb-bars kb-pad">{Object.entries(s.projections).map(([k, v]) => <div key={k} className="kb-bar"><span>{k}</span><i style={{ width: `${journal ? Math.max(3, (v / journal) * 100) : 0}%` }} /><b>{v.toLocaleString('en-US')}{k !== 'journal' && journal ? ` · ${Math.round((v / journal) * 100)}%` : ''}</b></div>)}</div></section>
      <section className="card kb-panel"><div className="section-heading"><div><h2>Records</h2><p>{s.asOf ? `Entities by kind known on ${formatDate(s.asOf)} (the view date)` : 'Entities by kind, as the knowledge base holds them now'}.</p></div></div>
        <div className="kb-bars kb-pad">{Object.entries(s.entitiesByKind).sort((a, b) => b[1] - a[1]).map(([k, v]) => <div key={k} className="kb-bar"><span>{KIND_LABEL[k] ?? k}</span><i style={{ width: `${Math.max(2, (v / Math.max(...Object.values(s.entitiesByKind))) * 100)}%` }} /><b>{v.toLocaleString('en-US')}</b></div>)}</div></section>
      <section className="card kb-panel"><div className="section-heading"><div><h2>Causal links</h2><p>Declared and inferred links by computed status (STALE: the source changed after the link was made). {s.causalAsOf ? `As of ${formatDate(s.causalAsOf)}.` : 'As of the end of the data.'}</p></div></div>
        <div className="kb-bars kb-pad">{Object.entries(s.causal).map(([k, v]) => <div key={k} className="kb-bar"><span>{k}</span><i style={{ width: `${Math.max(2, (v / Math.max(...Object.values(s.causal), 1)) * 100)}%` }} /><b>{v.toLocaleString('en-US')}</b></div>)}</div></section>
    </>}
  </div>;
}
