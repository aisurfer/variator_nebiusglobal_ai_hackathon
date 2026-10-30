// S6 «Actors and risks»: the company knows its actors. Left, the list (people by department, key clients, vendors,
// cohorts) with a segmented filter and a search; right, the selected card: a cited profile, the habits with their
// ranges and source tags, what changes the actor in the cases, and links to the dossiers. A second tab holds the
// register of the risks that already fired. Data: the actor-card artifact set through /api/variator/artifacts.
// Clean-page rule: names, titles, numbers, controls and the profile stay visible; the rest is behind «?» or closed.
import './actors.css';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { AlertTriangle, ArrowLeft, BookOpen, ChevronDown, FileText, Search, Telescope, X } from 'lucide-react';
import type { PageContext } from '../../pages';
import { KnowMore } from '../../genui/detail/shared';
import { useActorData } from './api';
import { HELP } from './help';
import { CHANGE_KIND, FILTERS, KIND_LABEL, filterCards, isFilter, listSections, moneyParts, moneyText, rangeText, shortDate, sourceText, sparkPath, type Filter } from './model';
import type { ActorCard, Cite, ExternalFact, HabitRow, Risk } from './types';
import { ReviewerTour } from '../tour/ReviewerTour';

type Go = PageContext['go'];

export function ActorsPage({ ctx }: { ctx: PageContext }) {
  const loc = useLocation();
  const params = new URLSearchParams(loc.search);
  const tab = params.get('tab') === 'risks' ? 'risks' : 'actors';
  const viewDate = ctx.asOf ?? ctx.dates?.default;
  const data = useActorData(ctx.datesLoading ? undefined : viewDate);
  const go: Go = (path, patch) => ctx.go(path, patch);

  if (data.status === 'loading') return <State title="Loading the actors…" />;
  if (data.status === 'error') return <State title="The actor cards could not be read" text={data.error} />;
  if (data.status === 'none') return <State title="No actor cards yet" text="Build them with python -m variator.actors.cards.cli all, then copy the set to the VARIATOR API." />;
  if (data.status === 'later') {
    const first = [...data.sets].sort((a, b) => a.asOf.localeCompare(b.asOf))[0];
    return <State title="The cards are learned from a later date" text={`They use the history up to ${shortDate(first?.asOf)}, after this view date. Nothing after the view date may count.`}
      action={<button className="button" onClick={() => go(ctx.path, { asOf: null })}>Go to the latest data</button>} />;
  }
  const { cards, risks } = data;
  const tabs = [
    { id: 'actors', label: 'Actors', short: 'Actors', count: cards.cards.length },
    { id: 'risks', label: 'Risks that already fired', short: 'Risks that fired', count: risks.risks.length },
  ];
  return <div className="ac-page" data-tour="actors-page">
    <ReviewerTour />
    <section className="page-intro ac-intro">
      <div>
        <div className="eyebrow intro-date">COMPANY<span>·</span>LEARNED FROM THE HISTORY UP TO {shortDate(cards.asOf).toUpperCase()}</div>
        <div className="kb-title-row"><h1>Actors and risks</h1><KnowMore label="About this page">{HELP.page}</KnowMore></div>
      </div>
      <div className="ac-tabs" role="tablist" aria-label="View">
        {tabs.map(t => <button key={t.id} role="tab" aria-selected={tab === t.id} className={tab === t.id ? 'is-on' : ''} data-tour={`actors-tab-${t.id}`}
          onClick={() => go(ctx.path, { tab: t.id === 'risks' ? 'risks' : null, entity: null })}>
          <span className="ac-tab-long">{t.label}</span><span className="ac-tab-short">{t.short}</span><span className="count-pill">{t.count}</span></button>)}
      </div>
    </section>
    {tab === 'risks'
      ? <RiskRegister risks={risks.risks} cards={cards.cards} selected={params.get('risk')} ctx={ctx} />
      : <Actors cards={cards.cards} risks={risks.risks} selectedId={params.get('actor')} ctx={ctx} />}
  </div>;
}

function State({ title, text, action }: { title: string; text?: string; action?: ReactNode }) {
  return <div className="ac-state card" role="status"><h1>{title}</h1>{text && <p>{text}</p>}{action}</div>;
}

// ------------------------------------------------------------------------------------------------ actors
function Actors({ cards, risks, selectedId, ctx }: { cards: ActorCard[]; risks: Risk[]; selectedId: string | null; ctx: PageContext }) {
  const [filter, setFilter] = useState<Filter>(() => {
    const g = new URLSearchParams(window.location.search).get('group');
    return isFilter(g) ? g : 'all';
  });
  const [query, setQuery] = useState('');
  const shown = useMemo(() => filterCards(cards, filter, query), [cards, filter, query]);
  const sections = useMemo(() => listSections(shown), [shown]);
  const selected = cards.find(c => c.id === selectedId) ?? cards[0];
  useEffect(() => {
    if (selectedId && window.matchMedia?.('(max-width: 760px)').matches) window.scrollTo({ top: 0 });
  }, [selectedId]);
  const select = (id: string) => ctx.go(ctx.path, { actor: id, entity: null });
  return <div className={`ac-layout ${selectedId ? 'has-card' : ''}`}>
    <aside className="card ac-list" aria-label="Actors" data-tour="actors-list">
      <div className="ac-seg" role="radiogroup" aria-label="Show" data-tour="actors-filter">
        {FILTERS.map(f => <button key={f.id} role="radio" aria-checked={filter === f.id} className={filter === f.id ? 'is-on' : ''} onClick={() => setFilter(f.id)}>{f.label}</button>)}
      </div>
      <label className="ac-search" data-tour="actors-search"><Search size={15} aria-hidden />
        <input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search by name or role" aria-label="Search actors" />
        {query && <button type="button" className="ac-clear" aria-label="Clear the search" onClick={() => setQuery('')}><X size={14} /></button>}
      </label>
      <nav className="ac-sections">
        {sections.map(s => <div className="ac-section" key={s.key}>
          <h3>{s.label}<span className="count-pill">{s.cards.length}</span>{s.group === 'cohorts' && s.label === 'Future hires' && <KnowMore label="About cohorts">{HELP.cohort}</KnowMore>}</h3>
          <ul>{s.cards.map(c => <li key={c.id}>
            <button className={`ac-item ${c.id === selected?.id ? 'is-selected' : ''}`} aria-current={c.id === selected?.id ? 'true' : undefined} onClick={() => select(c.id)} data-actor={c.id}>
              <strong>{c.name}</strong><small>{c.group === 'people' ? c.title : c.subtitle ?? c.title}</small>
            </button></li>)}</ul>
        </div>)}
        {!sections.length && <p className="ac-empty">No actor matches.</p>}
      </nav>
    </aside>
    {selected && <Card card={selected} risks={risks} ctx={ctx} />}
  </div>;
}

/** «Key clients» after «Key client»: the section only repeats the kind. */
const sameWord = (section: string, kind: string) => section.toLowerCase().startsWith(kind.toLowerCase());

/** The chips after each sentence: at most two records not cited earlier in the profile (else the first one), so a
 * record that backs several sentences does not repeat after each of them. */
function profileChips(card: ActorCard): { s: ActorCard['profile']['sentences'][number]; chips: Cite[] }[] {
  const seen = new Set<string>();
  return card.profile.sentences.map(s => {
    const fresh = s.cites.filter(c => !seen.has(c.id));
    const chips = fresh.length ? fresh.slice(0, 2) : s.cites.slice(0, 1);
    chips.forEach(c => seen.add(c.id));
    return { s, chips };
  });
}

/** A card text in people's words: amounts in dollars, short, with the exact value as a tooltip. */
function Words({ text }: { text: string }) {
  return <>{moneyParts(text).map((p, i) => typeof p === 'string' ? p : <span key={i} className="ac-money" title={p.exact}>{p.money}</span>)}</>;
}

function CiteChip({ c, onOpen }: { c: Cite; onOpen: (id: string) => void }) {
  return <button type="button" className="ac-cite" title={c.title} onClick={() => onOpen(c.id)} data-entity={c.id}>{c.label}</button>;
}

function Card({ card, risks, ctx }: { card: ActorCard; risks: Risk[]; ctx: PageContext }) {
  const primary = card.habits.filter(h => h.primary), more = card.habits.filter(h => !h.primary);
  const riskById = new Map(risks.map(r => [r.id, r]));
  const own = card.risks.map(id => riskById.get(id)).filter((r): r is Risk => !!r);
  const open = ctx.openEntity;
  return <article className="card ac-card" data-tour="actor-card" data-card={card.id} aria-labelledby="ac-card-name">
    <header className="ac-head">
      <button className="text-button ac-back" onClick={() => ctx.go(ctx.path, { actor: null })}><ArrowLeft size={14} />All actors</button>
      <div className="ac-head-main">
        <div className="eyebrow ac-kind">{KIND_LABEL[card.kind].toUpperCase()}
          {!sameWord(card.section, KIND_LABEL[card.kind]) && <><span>·</span>{card.section.toUpperCase()}</>}
          {card.kind === 'cohort' && <KnowMore label="About cohorts">{HELP.cohort}</KnowMore>}
          {card.kind === 'person' && <KnowMore label="About person cards">{HELP.people}</KnowMore>}</div>
        <h2 id="ac-card-name">{card.name}</h2>
        <p className="ac-title">{card.title}{card.subtitle && card.group !== 'people' && card.subtitle !== card.title ? ` · ${card.subtitle}` : ''}</p>
      </div>
      <div className="ac-actions">
        {card.dossier && <button className="button" data-tour="actor-dossier" onClick={() => open(card.dossier!)}><FileText size={15} />Dossier</button>}
        <button className="button" data-tour="actor-kb-page" title="This card as a page of the knowledge base" onClick={() => open(card.kbId)}><BookOpen size={15} />KB page</button>
      </div>
    </header>

    <section className="ac-block ac-profile" data-tour="actor-profile" aria-label="Profile">
      <p>{profileChips(card).map(({ s, chips }, i) => <span key={i} className="ac-sentence"><Words text={s.text} />{' '}
        {chips.map(c => <CiteChip key={c.id} c={c} onOpen={open} />)}{' '}</span>)}</p>
      <div className="ac-profile-note">{card.profile.source === 'facts' && <span className="badge badge-warn">Facts only</span>}
        <KnowMore label="How the profile is written" align="end">{card.profile.source === 'facts' ? `${HELP.profileFacts} ${HELP.profile}` : HELP.profile}</KnowMore></div>
    </section>

    {card.external?.length ? <ExternalContext facts={card.external} /> : null}

    {card.habits.length > 0 && <section className="ac-block" data-tour="actor-habits" aria-labelledby="ac-habits-h">
      <h3 className="ac-h3" id="ac-habits-h">Habits<KnowMore label="About habits">{HELP.habit}</KnowMore></h3>
      <div className="ac-habits" role="table" aria-label="Habits">
        <div className="ac-hrow ac-hhead" role="row"><span role="columnheader">Habit</span><span role="columnheader">Typical</span>
          <span role="columnheader">Range<KnowMore label="About the range">{HELP.interval}</KnowMore></span>
          <span role="columnheader">Source<KnowMore label="About source tags" align="end">{HELP.source}</KnowMore></span><span role="columnheader" className="ac-hspark">Over time</span></div>
        {primary.map(h => <Habit key={h.key} h={h} />)}
      </div>
      {more.length > 0 && <details className="ac-more"><summary><ChevronDown size={15} aria-hidden />More habits<span className="count-pill">{more.length}</span></summary>
        <div className="ac-habits" role="table" aria-label="More habits">{more.map(h => <Habit key={h.key} h={h} />)}</div></details>}
    </section>}

    {card.changes.length > 0 && <section className="ac-block" data-tour="actor-changes" aria-labelledby="ac-changes-h">
      <h3 className="ac-h3" id="ac-changes-h">What changes this actor<KnowMore label="About what changes this actor">{HELP.changes}</KnowMore></h3>
      <ul className="ac-changes">{card.changes.map(ch => <li key={`${ch.caseId}-${ch.id}`} title={ch.text ? `${ch.caseTitle}: ${ch.text}` : ch.caseTitle}>
        <span className="ac-case" aria-label={`Case ${ch.case}`}>{ch.case}</span><span className="ac-change">{ch.label}</span>
        <span className={`badge ac-ck ac-ck-${ch.kind}`}>{CHANGE_KIND[ch.kind] ?? ch.kind}</span></li>)}</ul>
    </section>}

    {own.length > 0 && <section className="ac-block" aria-labelledby="ac-risks-h">
      <h3 className="ac-h3" id="ac-risks-h">Risks that fired{card.kind === 'company' ? '' : ' here'}</h3>
      {card.kind === 'company'
        ? <button className="text-button ac-open-register" onClick={() => ctx.go(ctx.path, { tab: 'risks', risk: null })}><AlertTriangle size={14} />{own.length} risks, {own.reduce((a, r) => a + r.count, 0)} events: open the register</button>
        : <div className="ac-risk-chips">{own.map(r => <button key={r.id} className={`ac-risk-chip ${r.polarity === 'positive' ? 'is-positive' : ''}`} onClick={() => ctx.go(ctx.path, { tab: 'risks', risk: r.id })}>
          <b>{r.id}</b>{r.title}</button>)}</div>}
    </section>}

    {card.events.length > 0 && <details className="ac-more ac-block"><summary><ChevronDown size={15} aria-hidden />Life events<span className="count-pill">{card.events.length}</span></summary>
      <ul className="ac-events">{card.events.map((e, i) => <li key={i}><span>{shortDate(e.date)}</span><p><Words text={e.text} /> {e.cites.map(c => <CiteChip key={c.id} c={c} onOpen={open} />)}</p></li>)}</ul></details>}
  </article>;
}

function Habit({ h }: { h: HabitRow }) {
  const pts = h.series?.points ?? [];
  const d = pts.length >= 2 ? sparkPath(pts.map(p => p[1]), 84, 22) : '';
  return <div className="ac-hrow" role="row" data-habit={h.key}>
    <span role="cell" className="ac-hlabel" title={[moneyText(h.help), h.method && `Method: ${moneyText(h.method)}`, h.n ? `${h.n} observations` : ''].filter(Boolean).join('\n')}>{h.label}</span>
    <span role="cell" className="ac-hvalue"><b>{h.value}</b>{h.suffix && <small>{h.suffix}</small>}</span>
    <span role="cell" className="ac-hrange" title={h.interval && rangeText(h) !== h.interval ? h.interval : undefined}>{rangeText(h) || '—'}</span>
    <span role="cell"><span className={`ac-src is-${h.sourceKind}`} title={sourceText(h.source.replace(/^group:/, 'group · '))}>{sourceText(h.sourceLabel)}</span></span>
    <span role="cell" className="ac-hspark">{d ? <svg className="ac-spark" viewBox="0 0 84 22" role="img" aria-label={`${h.series!.label}: ${pts.length} points`}><title>{`${h.series!.label}, ${pts[0][0]} to ${pts[pts.length - 1][0]}`}</title><path d={d} /></svg> : null}</span>
  </div>;
}

function ExternalContext({ facts }: { facts: ExternalFact[] }) {
  return <details className="ac-block ac-external" open={facts.length <= 3}>
    <summary className="ac-h3"><Telescope size={14} aria-hidden />External context<span className="count-pill">{facts.length}</span></summary>
    <ul>
      {facts.map(f => <li key={f.id}>
        <p>{f.statement}</p>
        {f.evidence && <small>{f.evidence}</small>}
        <a href={f.source} target="_blank" rel="noreferrer">{f.title || f.source}</a>
      </li>)}
    </ul>
  </details>;
}

// ------------------------------------------------------------------------------------------------ risks
function RiskRegister({ risks, cards, selected, ctx }: { risks: Risk[]; cards: ActorCard[]; selected: string | null; ctx: PageContext }) {
  const [openIds, setOpen] = useState<Set<string>>(() => new Set(selected ? [selected] : []));
  useEffect(() => {
    if (!selected) return;
    setOpen(s => new Set([...s, selected]));
    requestAnimationFrame(() => document.getElementById(`risk-${selected}`)?.scrollIntoView({ block: 'center' }));
  }, [selected]);
  const toggle = (id: string) => setOpen(s => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const names = new Map(cards.map(c => [c.id, c.name]));
  const events = risks.reduce((a, r) => a + r.count, 0);
  const shocked = risks.filter(r => r.shocks.length).length;
  return <section className="card ac-register" data-tour="risks-register" aria-labelledby="ac-register-h">
    <div className="section-heading"><div><h2 id="ac-register-h">Risks that already fired<KnowMore label="About the register">{HELP.risks}</KnowMore></h2></div>
      <div className="ac-register-sum"><span className="badge">{risks.length} risks</span><span className="badge">{events} events</span><span className="badge badge-warn">{shocked} used as shocks</span></div></div>
    <div className="ac-rtable" role="table" aria-label="Risks that already fired">
      <div className="ac-rrow ac-rhead" role="row"><span role="columnheader">Risk</span><span role="columnheader">Per month</span><span role="columnheader">Events</span>
        <span role="columnheader">Last</span><span role="columnheader">Examples</span><span role="columnheader">Shock in the cases</span></div>
      {risks.map(r => {
        const isOpen = openIds.has(r.id);
        return <div key={r.id} id={`risk-${r.id}`} className={`ac-rrow ${selected === r.id ? 'is-selected' : ''}`} role="row" data-risk={r.id} data-tour={selected === r.id ? 'risk-selected' : undefined}>
          <span role="cell" className="ac-rname"><span className="ac-rid">{r.id}</span>
            <button className="ac-rtitle" aria-expanded={isOpen} onClick={() => toggle(r.id)}>{r.title}<ChevronDown size={14} aria-hidden /></button>
            {r.polarity === 'positive' && <span className="badge badge-good">Positive</span>}</span>
          <span role="cell" className="ac-rnum" data-label="Per month"><b>{r.perMonth.value}</b>{r.perMonth.interval && <small>{r.perMonth.interval}</small>}</span>
          <span role="cell" className="ac-rnum" data-label="Events"><b>{r.count}</b></span>
          <span role="cell" data-label="Last">{shortDate(r.last)}</span>
          <span role="cell" className="ac-rchips" data-label="Examples">{r.examples.slice(0, 2).map(c => <CiteChip key={c.id} c={c} onOpen={ctx.openEntity} />)}
            {r.evidenceCount > 2 && <span className="count-pill" title={`${r.evidenceCount} records behind this risk`}>+{r.evidenceCount - 2}</span>}</span>
          <span role="cell" className="ac-rshocks" data-label="Shock in the cases">{r.shocks.length
            ? r.shocks.slice(0, 2).map(s => <span key={`${s.caseId}-${s.id}`} className="ac-shock" title={`${s.caseTitle}: ${CHANGE_KIND[s.kind]?.toLowerCase()}`}><span className="ac-case">{s.case}</span>{s.label}</span>)
            : <span className="muted">Not used</span>}{r.shocks.length > 2 && <span className="count-pill">+{r.shocks.length - 2}</span>}</span>
          {isOpen && <div className="ac-rdetail" role="cell">
            <p><Words text={r.effect.charAt(0).toUpperCase() + r.effect.slice(1)} />.</p>
            <dl>
              <div><dt>First and last</dt><dd>{shortDate(r.first)} – {shortDate(r.last)}</dd></div>
              <div><dt>Actors involved</dt><dd>{r.actors}{r.cards.length > 0 && r.cards.length <= 6 ? `: ${r.cards.map(id => names.get(id) ?? id).join(', ')}` : ''}</dd></div>
              {r.size.map(s => <div key={s.label}><dt>{s.label}</dt><dd><Words text={`${s.value}${s.suffix ? ` ${s.suffix}` : ''}${s.interval ? ` (${s.interval})` : ''}`} /></dd></div>)}
              {r.shocks.length > 2 && <div><dt>All case items</dt><dd>{r.shocks.map(s => `${s.case} · ${s.label}`).join('; ')}</dd></div>}
            </dl>
            {r.note && <p className="ac-rnote"><Words text={r.note.charAt(0).toUpperCase() + r.note.slice(1)} />.</p>}
            {r.external?.length ? <ExternalContext facts={r.external} /> : null}
          </div>}
        </div>;
      })}
    </div>
  </section>;
}
