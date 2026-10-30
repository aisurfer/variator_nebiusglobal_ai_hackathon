// The workspace shell of the company: sidebar (brand, company, navigation, data sources, profile), top bar (breadcrumb,
// view date, knowledge-base state) and the side sheets of metrics and dossiers, all read live from /api/kb/*. One
// company, one user role (the CEO, the first role of the company file), one view date for every page (URL parameter
// asOf). URL parameter branch: a simulated future of a case (its own knowledge-base tenant); the knowledge-base pages
// read it, a Simulation banner says so, and «Back to the company» leaves it. Pages are registered in src/pages.tsx.
// Stylesheets first, in cascade order (styles.css is loaded by main.tsx before this module).
import './shell.css';
import './domain.css';
import './genui/genui.css';
import './kb/kb.css';
import { Component, Fragment, lazy, Suspense, useEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowLeft, BookOpen, Building2, Cable, CalendarDays, ChevronDown, ChevronRight, FlaskConical, Menu, X } from 'lucide-react';
import type { KbBranch, KbOrganization, KbTrailStep } from './kb/types';
import { formatDate, formatDay, kbApi, setKbBranch, setKbFormat, useKb } from './kb/api';
import { loadBranchPosition, placeText, type BranchPosition } from './kb/branchExamples';
import { navHref } from './kb/nav';
import { apiUrl, appHref } from './appUrl';
import { KnowMore } from './genui/detail/shared';
import { earlyFetch } from './early';
import { DEFAULT_PAGE, NAV, PageLoading, PAGES, type PageContext, type Patch } from './pages';
import { preloadPage, SHEETS_CODE } from './routes';

// the metric and dossier sheets load with the first sheet that opens (or after the brief's first view: KbBrief), then
// stay mounted: their dialog opens and closes as before
const KbSheets = lazy(() => SHEETS_CODE().then(m => ({ default: m.KbSheets })));

/** Pages that read the knowledge base: they keep the branch in their links; other pages (cases) leave it. */
const BRANCH_PAGES = new Set(['brief', 'ask', 'sources']);

export default function App() {
  const location = useLocation(), navigate = useNavigate();
  const params = new URLSearchParams(location.search);
  const pageId = location.pathname.split('/')[1] || DEFAULT_PAGE.id;
  const page = PAGES.find(p => p.id === pageId) ?? DEFAULT_PAGE;
  const [mobile, setMobile] = useState(false);
  const orgs = useKb(() => earlyFetch(apiUrl('/api/kb/organizations')).then(r => r.json() as Promise<KbOrganization[]>), []);
  // one company; the knowledge-base API keeps the organization path segment (/api/kb/<id>/…) for its tenant
  const org = orgs.data?.[0] ?? null;
  const organization = org?.id ?? '';
  if (org) setKbFormat(org); // before the pages render their amounts and dates
  // the user: the MVP has one role, the CEO (the company file keeps a list for later releases)
  const role = org?.roles[0];
  // a simulated future (URL parameter branch) must be one the knowledge base lists; every /api/kb call then reads it
  const asked = BRANCH_PAGES.has(page.id) ? params.get('branch') : null;
  const branch: KbBranch | null = asked ? org?.branches?.find(b => b.tenant === asked) ?? null : null;
  const missingBranch = !!asked && !!org && !branch;
  setKbBranch(branch);
  const tenant = branch?.tenant ?? org?.tenant ?? '';
  // one view date for the whole workspace (URL parameter asOf); without it every page shows the last day of data
  // (of the branch: its own dates, after the fork date)
  const asOf = params.get('asOf') ?? undefined;
  const dates = useKb(() => organization && !missingBranch ? kbApi.dates(organization) : Promise.resolve(null), [organization, tenant]);
  const status = useKb(() => organization && !missingBranch ? kbApi.status(organization, asOf) : Promise.resolve(null), [organization, tenant, asOf]);
  const mainRef = useRef<HTMLElement>(null);
  // `link`: the in-app path for navigate(); `href`: the same with the app's base, for an <a href>
  const link = (path: string, patch: Patch = {}) => {
    const next = new URLSearchParams(location.search);
    next.delete('role'); // a parameter of old links: there is one role now
    Object.entries(patch).forEach(([k, v]) => v === null ? next.delete(k) : next.set(k, v));
    return `${path}?${next}`;
  };
  const href = (path: string, patch: Patch = {}) => appHref(link(path, patch));
  const go = (path: string, patch: Patch = {}) => { setMobile(false); navigate(link(path, patch)); };
  useEffect(() => { document.title = `${branch ? 'Simulation · ' : ''}${page.label} · ${org?.name ?? 'Knowledge base'} · VARIATOR`; }, [page, org?.name, branch]);
  const initials = (role?.name ?? 'KB').split(' ').filter(Boolean).slice(0, 2).map(v => v[0]).join('');
  // the knowledge-base pages keep the branch; a page outside them leaves the simulated future
  // a navigation link carries only the view date and the branch, never the parameters of the page it leaves
  // (metric, entity, actor, tab…); leaving the simulated future also leaves its dates
  const pagePath = (id: string) => navHref(id, location.search, BRANCH_PAGES.has(id));
  const pageHref = (id: string) => appHref(pagePath(id));
  const nav = (id: string) => (e: MouseEvent) => { e.preventDefault(); setMobile(false); navigate(pagePath(id)); };
  // a link under the pointer or the focus starts loading the code of its page
  const warm = (id: string) => ({ onPointerEnter: () => preloadPage(`/${id}`), onFocus: () => preloadPage(`/${id}`) });
  // the sheets: mounted from the first metric or dossier asked for
  const sheetAsked = !!(params.get('metric') || params.get('entity'));
  const [sheets, setSheets] = useState(sheetAsked);
  useEffect(() => { if (sheetAsked) setSheets(true); }, [sheetAsked]);
  // the dossiers opened before the current one, from the history state (the breadcrumbs of the sheet)
  const trail = ((location.state as { kbTrail?: KbTrailStep[] } | null)?.kbTrail) ?? [];
  const toCompany: Patch ={ branch: null, asOf: null, metric: null, entity: null };
  const ctx: PageContext | null = org && role ? {
    org, role, asOf, dates: dates.data, datesLoading: dates.loading, status, link: href, go, path: location.pathname,
    openEntity: id => go(location.pathname, { entity: id }), openMetric: id => go(location.pathname, { metric: id, entity: null }),
  } : null;

  return <div className="app-shell kb-shell"><a className="skip-link" href="#main-content">Skip to workspace</a>
    {mobile && <button className="sidebar-scrim" aria-label="Close navigation" onClick={() => setMobile(false)} />}
    <aside className={`sidebar ${mobile ? 'mobile-open' : ''}`} aria-label="Workspace navigation">
      <a className="brand" href={pageHref(DEFAULT_PAGE.id)} onClick={nav(DEFAULT_PAGE.id)} aria-label="VARIATOR brief"><span className="brand-mark"><span /><span /><span /></span>VARIATOR</a>
      <button className="mobile-close icon-button" onClick={() => setMobile(false)} aria-label="Close navigation"><X size={20} /></button>
      <div className="workspace-switch organization-switch"><span className="company-mark">{org?.name[0] ?? 'C'}</span><span><small>COMPANY</small>
        <strong>{org?.name ?? '…'}</strong></span></div>
      <nav>{NAV.map(group => <div className="nav-group" key={group.heading}><div className="nav-heading">{group.heading}</div>
        {group.pages.map(({ id, label, Icon }) => <a key={id} href={pageHref(id)} onClick={nav(id)} {...warm(id)} className={`nav-item ${page.id === id ? 'active' : ''}`} aria-current={page.id === id ? 'page' : undefined}><Icon size={18} strokeWidth={1.7} /><span>{label}</span></a>)}
      </div>)}</nav>
      <div className="sidebar-bottom">
        <a href={pageHref('sources')} onClick={nav('sources')} {...warm('sources')} className={`nav-item ${page.id === 'sources' ? 'active' : ''}`}><Cable size={18} /><span>Data sources</span><span className={`connected-dot ${status.data && !status.data.reachable ? 'kb-dot-down' : ''}`} /></a>
        <a href={pageHref('docs')} onClick={nav('docs')} {...warm('docs')} className={`nav-item ${page.id === 'docs' ? 'active' : ''}`}><BookOpen size={18} /><span>Documentation</span></a>
        <div className="profile"><div className="profile-avatar">{initials}</div><div><strong>{role?.name ?? '—'}</strong><small>{role?.title ?? ''}</small></div><span className="demo-tag">LIVE</span></div>
      </div>
    </aside>
    <div className="main-shell" inert={mobile || undefined}>
      <header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-toggle" onClick={() => setMobile(true)} aria-label="Open navigation"><Menu size={21} /></button><Building2 size={16} /><span>{org?.name ?? 'Knowledge base'}</span><ChevronRight size={13} />{page.crumbs && ctx ? page.crumbs(ctx) : <strong>{page.label}</strong>}</div>
        <div className="header-tools">{page.tools && ctx ? page.tools(ctx) : dates.data && <div className="kb-asof"><CalendarDays size={15} /><label><span>As of</span>
          {/* the date reads as in the rest of the app («26 Oct 2026»), whatever the browser's locale: the native date
              field lies transparent over the text, so a click, a tap or Enter still opens the browser's own picker */}
          <span className="kb-asof-pick"><span className="kb-asof-text" aria-hidden>{formatDay(asOf ?? dates.data.default)}</span><ChevronDown size={14} aria-hidden />
          <input type="date" aria-label="View date" min={dates.data.start} max={dates.data.max} value={asOf ?? dates.data.default}
            onClick={e => openPicker(e.currentTarget)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPicker(e.currentTarget); } }}
            onChange={e => { const d = e.target.value; if (d && d >= dates.data!.start && d <= dates.data!.max) go(location.pathname, { asOf: d === dates.data!.default ? null : d }); }} /></span></label>
          {asOf && asOf !== dates.data.default && <button className="text-button" onClick={() => go(location.pathname, { asOf: null })} title={`Back to the last day of data, ${formatDay(dates.data.default)}`}>Latest data</button>}</div>}
<span className="snapshot-label"><span className={`tiny-dot ${status.data?.reachable === false ? 'red' : branch ? 'amber' : ''}`} /><span title={`Knowledge-base tenant ${tenant || '…'}`}>{status.data?.reachable === false ? 'Knowledge base unreachable' : branch ? `Simulated future · ${branch.label}` : 'Live knowledge base'}</span></span><span className="header-avatar">{initials}</span></div>
      </header>
      <main id="main-content" ref={mainRef} className="main-content kb-main" tabIndex={-1}>
        {branch && <BranchBanner branch={branch} href={href(location.pathname, toCompany)} onBack={e => { e.preventDefault(); go(location.pathname, toCompany); }} />}
        {orgs.error ? <div className="workspace-state"><h1>We couldn’t load the company</h1><p>{orgs.error}</p></div>
          : orgs.data && !org ? <div className="workspace-state"><h1>No company is configured</h1><p>Add config/company.json (or set COMPANY_FILE) and restart the workspace.</p></div>
          : missingBranch ? <div className="workspace-state"><FlaskConical size={30} /><h1>This simulated future is not in the knowledge base</h1>
            <p>It may still be loading. The case page enables its link once the knowledge base lists it.</p>
            <a className="button" href={href(location.pathname, toCompany)} onClick={e => { e.preventDefault(); go(location.pathname, toCompany); }}><ArrowLeft size={15} />Back to the company</a></div>
          : !ctx ? <div className="workspace-state"><h1>Connecting to the knowledge base…</h1></div>
          // a new branch is a new knowledge base: the page loads again from scratch
          : <Fragment key={tenant}><PageBoundary key={page.id}><Suspense fallback={page.fallback ? page.fallback(ctx) : <PageLoading />}>{page.render(ctx)}</Suspense></PageBoundary></Fragment>}
      </main>
    </div>
    {org && role && !missingBranch && sheets && <Suspense fallback={null}><KbSheets key={tenant} org={org} role={role} asOf={asOf} defaultAsOf={dates.data?.default} datesLoading={dates.loading} metric={params.get('metric')} entity={params.get('entity')}
      trail={trail}
      onEntity={(id, from) => {
        // a step to a dossier already on the trail goes back to it; any other step adds the current dossier to the trail
        const at = trail.findIndex(t => t.id === id);
        if (at >= 0) navigate(at - trail.length);
        else navigate(link(location.pathname, { entity: id }), { state: { kbBack: true, kbTrail: from ? [...trail, from] : trail } });
      }}
      onCrumb={i => navigate(i - trail.length)}
      onMetric={(id, fromTitle) => navigate(link(location.pathname, { metric: id, entity: null }), { state: { kbBack: true, kbFromTitle: fromTitle } })}
      backTo={(location.state as { kbFromTitle?: string } | null)?.kbFromTitle}
      onClose={key => navigate(link(location.pathname, key === 'entity' ? { entity: null } : { metric: null, entity: null }), { replace: true })}
      onBack={() => {
        // Back: one step of the sheet history; opened from a link, Back leaves the dossier or goes to the whole metric
        const metric = params.get('metric');
        if ((location.state as { kbBack?: boolean } | null)?.kbBack) navigate(-1);
        else if (params.get('entity') || !metric?.includes('~')) navigate(link(location.pathname, { entity: null }), { replace: true });
        else navigate(link(location.pathname, { metric: metric.split('~')[0] }), { replace: true });
      }} /></Suspense>}
  </div>;
}

/** Opens the browser's date picker of a date field (a click on its text would otherwise only select a part of the
 *  date). A browser without showPicker, or one that refuses it, keeps the field as it is: typing a date still works. */
function openPicker(input: HTMLInputElement) {
  try { input.showPicker?.(); } catch { /* no picker here: the field stays editable */ }
}

/** A page whose code could not be loaded (the network dropped, or a new release replaced the files: main.tsx reloads
 *  the page once for that) says so instead of leaving the workspace blank. */
class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (!this.state.failed) return this.props.children;
    return <div className="workspace-state" role="alert"><h1>This page could not be loaded</h1><p>The connection may have dropped, or a new version of the workspace was released.</p>
      <button className="button button-primary" onClick={() => window.location.reload()}>Reload the page</button></div>;
  }
}

/** The Simulation banner over every knowledge-base page of a branch: a simulated future, never real history. */
function BranchBanner({ branch, href, onBack }: { branch: KbBranch; href: string; onBack: (e: MouseEvent) => void }) {
  // its true place among the scenarios of the fork, once WP19's day set gives it; until then its order among the three
  const [position, setPosition] = useState<BranchPosition | null>(null);
  useEffect(() => {
    let live = true;
    setPosition(null);
    loadBranchPosition(branch.tenant).then(p => { if (live) setPosition(p); });
    return () => { live = false; };
  }, [branch.tenant]);
  const place = placeText(branch.place ?? 'middle', position);
  return <div className="branch-banner" role="status" aria-label="Simulation" data-branch={branch.tenant} data-place={place}>
    <FlaskConical size={16} aria-hidden /><span className="branch-chip">SIMULATION</span>
    <strong>{branch.label}</strong><span className="branch-warning">a simulated future, not real history</span>
    <KnowMore label="About this simulation">This example: {place}. Every record after {formatDate(branch.from)} {new Date(`${branch.from}T12:00:00Z`).getUTCFullYear()} was made by the simulation of this case fork. The knowledge base of this branch holds the state of the company at that date and the records of the simulated months; its causes come from the graph of this branch.</KnowMore>
    <a className="branch-back" href={href} onClick={onBack}><ArrowLeft size={14} aria-hidden />Back to the company</a>
  </div>;
}
