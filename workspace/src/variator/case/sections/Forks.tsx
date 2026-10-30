// ① Forks: world hypotheses (chips, layered), the fork cards, the timeline of interventions with lanes (decisions,
// people, clients and vendors, factors), the inspector of the selected item, «What stays as before», and the
// «Model settings» drawer for the analyst.
import { useMemo, useRef, useState, type PointerEvent as RPointerEvent } from 'react';
import { Anchor, ArrowUpRight, Ban, Building2, CalendarClock, ChevronDown, CloudLightning, Cpu, Flame, Gauge, Hourglass, Landmark, Layers, ListPlus, MapPin, Minus, Plus, Puzzle, Repeat, RotateCcw, Settings2, ShieldAlert, Shuffle, Snowflake, Swords, TrendingDown, TrendingUp, Truck, UserMinus, Users, Wallet, X, Zap , History , MousePointerClick } from 'lucide-react';
import type { PageContext } from '../../../pages';
import type { Dist, Intervention, InterventionValue, RecomputeClass } from '../model/types';
import { useCase } from '../state';
import { calMonth, displayScale, displaySuffix, fmtDist, fmtValue, forkColor, forkLabel, odds, pBestScope, pct, pertPdf } from '../ui/format';
import { HelpTip } from '../ui/HelpTip';
import { MonthTrack, type TrackBand, type TrackMarker, type TrackRow } from '../ui/MonthTrack';
import { ForkDot, ForkNote, ModifiedMark, SectionHeader, Segmented, sectionEntry, useForkNote } from '../ui/parts';
import { ModelSettings } from './ModelSettings';
import { useHypEffect } from './hypEffect';
import { RequestButton } from './Origin';
import { useNumbers } from '../numbers';
import { useActorData } from '../../actors/api';
import type { ActorCard, Risk } from '../../actors/types';
import { INSPECTOR_HINT, INSPECTOR_TITLE, actorLine, cardOf, groupLabels, historyLine, inspectorLine, riskOf } from '../model/inspector';

/** Lucide icons the configs name (`hypotheses[].icon`); an unknown name shows the layers icon. */
const HYP_ICON: Record<string, typeof Swords> = {
  swords: Swords, cpu: Cpu, 'user-minus': UserMinus, repeat: Repeat, snowflake: Snowflake, wallet: Wallet, 'trending-down': TrendingDown, 'trending-up': TrendingUp,
  'list-plus': ListPlus, flame: Flame, gauge: Gauge, hourglass: Hourglass, landmark: Landmark, truck: Truck, ban: Ban, 'shield-alert': ShieldAlert, 'cloud-lightning': CloudLightning, users: Users, shuffle: Shuffle, 'calendar-clock': CalendarClock,
};
export const RECOMPUTE: Record<RecomputeClass, { Icon: typeof Zap; label: string }> = {
  baseline: { Icon: Anchor, label: 'Baseline' }, overlay: { Icon: Zap, label: 'Overlay' }, from_date: { Icon: RotateCcw, label: 'Rerun from its date' }, structural: { Icon: Puzzle, label: 'Structural' },
};
/** Lanes of the timeline in their order; a lane a config names that is not here comes after them. */
const LANE_LABELS: Record<string, string> = { decisions: 'Decisions', people: 'People', clients_vendors: 'Clients and vendors', clients: 'Clients and vendors', vendors: 'Vendors', scope: 'Scope', factors: 'Factors' };
const laneLabel = (id: string) => LANE_LABELS[id] ?? id.charAt(0).toUpperCase() + id.slice(1);
const lanesOf = (items: Intervention[]) => [...new Set(items.map(i => i.lane))].sort((a, b) => (Object.keys(LANE_LABELS).indexOf(a) + 99) % 99 - (Object.keys(LANE_LABELS).indexOf(b) + 99) % 99 || 0);
/** Colour of a lever: its fork's colour, or the decision ink when it belongs to every fork. */
export const LEVER_INK = '#33424a';
export const leverColor = (cfg: Parameters<typeof forkColor>[0], i: Intervention) => i.forks?.length === 1 ? forkColor(cfg, i.forks[0]) : LEVER_INK;
const fixed = (v: number): Dist => ({ kind: 'fixed', value: v });
export const num = (d: unknown, fallback = 0): number => { if (typeof d === 'number') return d; const x = d as Partial<{ kind: string; value: number; mode: number }> | null; return x && typeof x === 'object' && 'kind' in x ? (x.kind === 'fixed' ? x.value ?? fallback : x.mode ?? fallback) : fallback; };

export function WorldSection() {
  const c = useCase(), cfg = c.cfg, sec = cfg.sections.find(s => s.id === 'world')!;
  return <section id="world" className="cs-section" aria-labelledby="world-title">
    <SectionHeader id="world" title="World hypotheses" purpose={sec.purpose} help={sec.help} paths={p => p.startsWith('hyp|')} simulation={false}
      more={['Hypotheses add up as layers in their order. Values a hypothesis changes carry a lime mark; your own edits an amber one.', 'No hypothesis on: the world as the data shows it.']}
      badges={c.hypotheses.length > 0 && <span className="cs-badge is-on" data-testid="hyp-count">{c.hypotheses.length} on</span>} />
    <div className="cs-hyps" role="group" aria-label="World hypotheses">
      {cfg.hypotheses.map(h => {
        const path = `hyp|${h.id}`, on = c.value(path) === true, Icon = HYP_ICON[h.icon ?? ''] ?? Layers;
        return <ModifiedMark key={h.id} path={path} unit="" label={h.label} format={v => v ? 'on' : 'off'}>
          <button type="button" className={`cs-hyp ${on ? 'is-on' : ''}`} aria-pressed={on} aria-describedby={`hyp-${h.id}`} onClick={() => c.change({ [path]: !on }, `Hypothesis «${h.label}» ${on ? 'off' : 'on'}`, 'sim')}>
            <Icon size={15} aria-hidden /><span>{h.label}</span>
            <span className="cs-hyp-tip" role="tooltip" id={`hyp-${h.id}`}><b>{h.label}</b><span>{h.line}</span>{h.changes.map(x => <span key={x} className="cs-hyp-op">{x}</span>)}<em>Hypotheses add up in their order.</em></span>
          </button>
        </ModifiedMark>;
      })}
    </div>
  </section>;
}

export function ForksSection({ ctx, onOrigin }: { ctx: PageContext; onOrigin: () => void }) {
  const c = useCase(), cfg = c.cfg, sec = cfg.sections.find(s => s.id === 'forks')!;
  const [settings, setSettings] = useState(false);
  // nothing is selected until you click a marker: the inspector is not «the settings of the best fork»
  const [selected, setSelected] = useState<string | null>(null);
  const sel = cfg.interventions.find(i => i.id === selected) ?? null;
  const actorData = useActorData(ctx.datesLoading ? undefined : ctx.asOf ?? ctx.dates?.default ?? undefined);
  const known = actorData.status === 'ready' ? { cards: actorData.cards.cards, risks: actorData.risks.risks } : null;
  const paths = useMemo(() => new Set(cfg.interventions.flatMap(i => [i.monthPath, i.placePath, ...i.values.map(v => v.path)].filter(Boolean) as string[])), [cfg]);
  const settingsEdits = c.editedPaths(p => /^(v|r|rm|o|sim)\|/.test(p) && !paths.has(p)).length;
  return <section id="forks" className="cs-section" aria-labelledby="forks-title">
    <SectionHeader id="forks" title="Forks and interventions" purpose={sec.purpose} help={sec.help} paths={p => p.startsWith('fork|') || paths.has(p)} simulation={false}
      actions={<>{cfg.origin && <RequestButton compact onOpen={onOrigin} />}<button type="button" className="button cs-settings-btn" onClick={() => setSettings(true)}><Settings2 size={15} />Model settings{settingsEdits > 0 && <span className="cs-count">{settingsEdits}</span>}</button></>} />
    <ForkCards />
    {cfg.interventions.length > 0 && <div className="cs-forks-work">
      <article className="card cs-card cs-interventions" data-testid="decisions-events">
        <div className="cs-card-head"><h3>Timeline: your decisions and events</h3><HelpTip entry={cfg.help.interventions} label="Timeline: your decisions and events" more={['Drag a marker to another month; Esc during a drag puts it back.', 'A dashed marker is a possible event: click it to place it on the timeline.']} /></div>
        <InterventionTimeline selected={sel?.id ?? null} onSelect={setSelected} />
      </article>
      {sel ? <Inspector key={sel.id} item={sel} ctx={ctx} known={known} /> : <aside className="card cs-inspector is-empty" data-testid="inspector" aria-label={INSPECTOR_TITLE}>
        <span className="cs-eyebrow">{INSPECTOR_TITLE}</span>
        <p className="cs-inspector-empty"><MousePointerClick size={16} aria-hidden />{INSPECTOR_HINT}</p>
      </aside>}
    </div>}
    <ModelSettings open={settings} onClose={() => setSettings(false)} />
  </section>;
}

function ForkCards() {
  const c = useCase(), cfg = c.cfg, nums = useNumbers();
  return <div id="fork-cards" className="cs-fork-cards" role="list" data-tour="fork-cards" style={{ '--cards': cfg.forks.length } as React.CSSProperties}>
    {cfg.forks.map(f => {
      const path = `fork|${f.id}`, on = f.baseline || c.value(path) !== false, R = RECOMPUTE[f.recompute] ?? RECOMPUTE.from_date;
      const place = nums?.order.indexOf(f.id) ?? -1;
      return <article key={f.id} role="listitem" className={`card cs-fork-card ${on ? '' : 'is-off'}`} style={{ '--fork': forkColor(cfg, f.id) } as React.CSSProperties}>
        <div className="cs-fork-top">
          <ForkDot fork={f.id} size={10} />
          <span className="cs-recompute" title={`${R.label}: ${cfg.help[`recompute.${f.recompute}`]?.short ?? ''}`}><R.Icon size={13} aria-hidden /><span className="sr-only">{R.label}</span></span>
          <HelpTip variant="info" size={14} label={f.label} entry={{ title: f.label, short: f.line, full: '' }} more={[`${R.label}: ${cfg.help[`recompute.${f.recompute}`]?.short ?? ''}`]} />
          {f.baseline ? <span className="cs-fork-lock" title="The baseline is always compared">Always on</span>
            : <ModifiedMark path={path} unit="" label={f.label} format={v => v === false ? 'off' : 'on'}>
              <label className={`cs-switch ${on ? 'is-on' : ''}`}><input type="checkbox" checked={on} aria-label={`Compare ${f.label}`} onChange={() => c.change({ [path]: !on }, `${f.label} turned ${on ? 'off' : 'on'}`, 'sim')} /><span className="cs-switch-track" aria-hidden><i /></span></label>
            </ModifiedMark>}
        </div>
        <h3>{f.label}<ForkCardNote fork={f.id} /></h3>
        <div className="cs-fork-foot" data-source={nums?.source}>{!on ? 'Not compared' : place < 0 ? (c.stale ? 'Recomputing…' : '—') : <>Rank {place + 1}{nums && <> · best in {pct(nums.pBest[f.id] ?? 0)} <span className="cs-pscope">· {pBestScope(nums.pBestOf)}</span></>}</>}</div>
      </article>;
    })}
  </div>;
}

function ForkCardNote({ fork }: { fork: string }) { const note = useForkNote(fork); return note ? <ForkNote note={note} /> : null; }

/** The value that draws the «ready» band after a marker: a lead time in months (a PERT range). */
const leadOf = (i: Intervention) => i.lead ? i.values.find(v => v.path === i.lead) : undefined;

/** The «?» of the two groups: forks are your decisions, events are dated what-ifs, world hypotheses are likelihoods. */
const GROUP_HELP = {
  decisions: { title: 'Your decisions', short: 'Each fork is a decision of yours. Its lever here has a date and a size; changing them changes that decision.', full: 'Forks are your decisions: the lever of a fork changes only that fork. Events below are specific dated what-ifs that hit every fork the same way. The world hypotheses (the chips above) change how likely such things are across all scenarios.' },
  events: { title: 'Events you can place', short: 'Specific dated what-ifs. Placed on the timeline, an event hits every fork the same way: a stress test of your decisions.', full: 'Each event stands for a risk that already happened in the company\'s history, bound to a real actor. Without placing it, the model still draws it at random with its yearly chance. The world hypotheses (the chips above) change how likely such things are across all scenarios; an event says exactly when one happens.' },
};

function InterventionTimeline({ selected, onSelect }: { selected: string | null; onSelect: (id: string) => void }) {
  const c = useCase(), cfg = c.cfg, H = cfg.horizonMonths, groups = groupLabels(cfg);
  const rows: TrackRow[] = [], markers: TrackMarker[] = [], bands: TrackBand[] = [];
  const isOn = (i: Intervention) => !i.forks || i.forks.some(f => c.forksOn.includes(f));
  const sections: { head?: TrackRow; lane?: string; items: Intervention[] }[] = [];
  const levers = cfg.interventions.filter(i => i.kind === 'lever'), events = cfg.interventions.filter(i => i.kind === 'event');
  if (levers.length) sections.push({ head: { id: 'group-decisions', kind: 'group', label: <span className="cs-tk-group">{groups.decisions}<HelpTip entry={GROUP_HELP.decisions} label={groups.decisions} size={13} /></span> }, items: levers });
  if (events.length) {
    sections.push({ head: { id: 'group-events', kind: 'group', label: <span className="cs-tk-group">{groups.events}<HelpTip entry={GROUP_HELP.events} label={groups.events} size={13} /></span> }, items: [] });
    for (const lane of lanesOf(events)) sections.push({ lane, items: events.filter(i => i.lane === lane) });
  }
  for (const sec of sections) {
    if (sec.head) rows.push(sec.head);
    if (sec.lane) rows.push({ id: `lane-${sec.lane}`, kind: 'lane', label: laneLabel(sec.lane) });
    for (const i of sec.items) {
      const m = num(c.value(i.monthPath)), on = isOn(i), placed = !i.placePath || c.value(i.placePath) === true;
      const fork = i.forks?.length === 1 ? i.forks[0] : undefined, edited = [i.monthPath, ...i.values.map(v => v.path)].some(p => c.isEdited(p));
      rows.push({ id: i.id, muted: !on, label: <span className="cs-tk-rowlabel">{i.kind === 'lever' ? (fork ? <ForkDot fork={fork} /> : <i className="cs-dot" style={{ background: LEVER_INK }} aria-hidden />) : <i className="cs-ev-dot" />}<span>{i.label}{edited && <i className="cs-mod-dot is-small" title="Changed by you" />}</span></span> });
      const color = i.kind === 'lever' ? leverColor(cfg, i) : 'var(--cs-event)';
      markers.push({ id: i.id, row: i.id, month: m, shape: 'circle', color, draggable: on && placed, ghost: !placed, selected: selected === i.id, dim: !on,
        text: !on ? 'fork off' : placed ? (i.kind === 'lever' ? `starts ${calMonth(cfg, m)}` : calMonth(cfg, m)) : 'possible · click to place', title: `${i.label}: ${placed ? calMonth(cfg, m) : 'not placed'}${on ? '' : ' (fork off)'}` });
      const lead = leadOf(i);
      if (lead && on && placed) {
        const r = c.value(lead.path) as Dist;
        if (r.kind === 'pert') bands.push({ row: i.id, from: m + r.low, to: m + r.high, mid: m + r.mode, kind: 'range', color: i.kind === 'lever' ? `${leverColor(cfg, i)}26` : '#d9558f26', title: `${lead.label}: ${+r.low.toFixed(1)}–${+r.high.toFixed(1)} months, likely ${+r.mode.toFixed(1)}. Ready between ${calMonth(cfg, m + r.low)} and ${calMonth(cfg, m + r.high)}.`, text: `ready ${calMonth(cfg, m + r.low, { year: 'never' })}–${calMonth(cfg, m + r.high, { year: 'never' })}` });
      }
    }
  }
  for (const mk of markers) { const b = bands.find(x => x.row === mk.row); if (b) mk.textAt = b.to; }
  // the months where the last change of the hypotheses moved the curves of the leading fork most
  const effect = useHypEffect();
  if (effect.length) {
    rows.push({ id: 'hyp-effect', kind: 'lane', label: <span className="cs-tk-rowlabel cs-hyp-effect-label"><Layers size={13} aria-hidden /><span>Hypothesis effect</span></span> });
    for (const e of effect) bands.push({ row: 'hyp-effect', from: Math.max(0, e.month - 0.5), to: Math.min(H, e.month + 0.5), kind: 'band', color: '#c6e36b88',
      text: `${Math.round(e.share * 100)}%`, title: `${calMonth(cfg, e.month)}, ${forkLabel(cfg, c.best!.leader)}: ${e.lines.join('; ')}` });
  }
  return <>
  <p className={`cs-hyp-on ${c.hypotheses.length ? 'is-on' : ''}`} data-testid="hyp-on"><Layers size={13} aria-hidden />{c.hypotheses.length
    ? <>Hypotheses on: {c.hypotheses.map(id => <b key={id}>{cfg.hypotheses.find(h => h.id === id)?.label ?? id}</b>)}<span>They change values for the whole period, not one month.</span></>
    : <>No hypothesis on: the world as the data shows it.</>}</p>
  <MonthTrack rows={rows} markers={markers} bands={bands} horizon={H} labelWidth={196} ariaLabel="Timeline: your decisions and events" monthLabel={m => calMonth(cfg, m, { year: 'jan' })}
    onSelect={id => {
      onSelect(id);
      const i = cfg.interventions.find(x => x.id === id);
      if (i?.placePath && c.value(i.placePath) !== true) c.change({ [i.placePath]: true }, `${i.label}: placed at ${calMonth(cfg, num(c.value(i.monthPath)))}`, 'sim');
    }}
    onMove={(id, to, from) => { const i = cfg.interventions.find(x => x.id === id)!; onSelect(id); c.change({ [i.monthPath]: fixed(to) }, `${i.label}: ${calMonth(cfg, from)} → ${calMonth(cfg, to)}`, 'sim', i.forks?.[0]); }} /></>;
}

function Inspector({ item: i, ctx, known }: { item: Intervention; ctx: PageContext; known: { cards: ActorCard[]; risks: Risk[] } | null }) {
  const c = useCase(), cfg = c.cfg;
  const m = num(c.value(i.monthPath)), placed = !i.placePath || c.value(i.placePath) === true;
  const fork = i.forks?.length === 1 ? i.forks[0] : undefined, forkOn = !i.forks || i.forks.some(f => c.forksOn.includes(f));
  const shock = i.shock ? cfg.shocks.find(s => s.id === i.shock) : null;
  const who = (id?: string) => id ? cfg.people.find(p => p.id === id || p.name === id) : undefined;
  const person = who(i.person), owner = who(i.owner);
  const kb = i.entityId ?? person?.entityId ?? owner?.entityId;
  const setMonth = (to: number) => { const t = Math.min(cfg.horizonMonths, Math.max(0, Math.round(to * 2) / 2)); if (t !== m) c.change({ [i.monthPath]: fixed(t) }, `${i.label}: ${calMonth(cfg, m)} → ${calMonth(cfg, t)}`, 'sim', i.forks?.[0]); };
  const paths = [i.monthPath, i.placePath, ...i.values.map(v => v.path)].filter(Boolean) as string[];
  const edited = paths.filter(p => c.isEdited(p));
  return <aside className="card cs-inspector" aria-labelledby="inspector-title" data-testid="inspector">
    <div className="cs-card-head"><span className="cs-eyebrow">{INSPECTOR_TITLE}</span><HelpTip entry={cfg.help.inspector} label="Inspector" />
      {edited.length > 0 && <button type="button" className="cs-text-btn cs-inspector-reset" onClick={() => c.resetPaths(edited, `${i.label}: reset`)}><RotateCcw size={13} />Reset item</button>}</div>
    <h3 id="inspector-title" className="cs-inspector-title">{i.kind === 'lever' ? (fork ? <ForkDot fork={fork} size={10} /> : <i className="cs-dot" style={{ background: LEVER_INK, width: 10, height: 10 }} aria-hidden />) : <i className="cs-ev-dot is-big" />}{i.label}</h3>
    <p className={`cs-inspector-kind is-${i.kind}`} data-testid="inspector-kind">{inspectorLine(cfg, i)}{i.kind === 'lever' && !forkOn && <span className="badge"><i />fork off</span>}</p>
    {i.kind === 'event' && <EventOrigin item={i} ctx={ctx} known={known} />}
    <p className="cs-inspector-text">{i.description}</p>
    {(person || owner || kb) && <div className="cs-inspector-who">
      {person && <p><Users size={14} /><span>Who: <strong>{person.name}</strong>, {person.title}</span></p>}
      {owner && <p><Users size={14} /><span>{i.kind === 'lever' ? 'Owner' : 'Handled by'}: <strong>{owner.name}</strong>, {owner.title}</span></p>}
      {!person && !owner && <p><Building2 size={14} /><span>In the knowledge base</span></p>}
      {kb && <a className="cs-kb-link" data-entity={kb} href={ctx.link(ctx.path, { entity: kb })} onClick={e => { e.preventDefault(); ctx.openEntity(kb); }}>{i.entityId && !i.entityId.startsWith('person.') ? 'Open in the knowledge base' : 'Dossier'}<ArrowUpRight size={12} /></a>}
    </div>}
    <div className="cs-field">
      <span className="cs-field-label">{i.kind === 'lever' ? 'Starts in' : 'Happens in'}</span>
      <ModifiedMark path={i.monthPath} unit="month" label={`${i.label} month`} format={v => calMonth(cfg, num(v))}>
        <span className="cs-stepper">
          <button type="button" aria-label="Half a month earlier" disabled={!placed || m <= 0} onClick={() => setMonth(m - 0.5)}><Minus size={14} /></button>
          <strong>{placed ? calMonth(cfg, m) : 'Not placed'}</strong><small>month {m}</small>
          <button type="button" aria-label="Half a month later" disabled={!placed || m >= cfg.horizonMonths} onClick={() => setMonth(m + 0.5)}><Plus size={14} /></button>
        </span>
      </ModifiedMark>
    </div>
    {i.values.map(v => v.kind === 'range' ? <RangeField key={v.path} path={v.path} label={v.label} unit={v.unit} what={i.label} fork={i.forks?.[0]} />
      : v.kind === 'choice' ? <ChoiceField key={v.path} item={i} v={v} /> : <ValueField key={v.path} item={i} v={v} />)}
    {i.placePath && <div className="cs-field">
      <span className="cs-field-label">On the timeline{shock && <small> · chance in the model {odds(num(c.value(shock.probabilityPath)))} a year</small>}</span>
      <ModifiedMark path={i.placePath} unit="" label={`${i.label} placement`} format={v => v === true ? 'placed' : 'not placed'}>
        <button type="button" className={`button ${placed ? '' : 'button-primary'}`} onClick={() => c.change({ [i.placePath!]: !placed }, `${i.label}: ${placed ? 'removed' : `placed at ${calMonth(cfg, m)}`}`, 'sim')}>{placed ? <><X size={14} />Remove from the timeline</> : <><MapPin size={14} />Place at {calMonth(cfg, m)}</>}</button>
      </ModifiedMark>
    </div>}
    <dl className="cs-inspector-meta">
      <div><dt>Source</dt><dd><span className="cs-source">{i.source}</span></dd></div>
      {i.affects.length > 0 && <div><dt>What it affects</dt><dd className="cs-affects">{i.affects.map(a => <span key={a}>{a}</span>)}</dd></div>}
    </dl>
    <p className="cs-inspector-hint"><MousePointerClick size={13} aria-hidden />{INSPECTOR_HINT}</p>
  </aside>;
}

/** Where an event comes from: its actor (from the actor cards) and the risk of the register that already fired in the
 *  history, with a link to that risk. */
function EventOrigin({ item: i, ctx, known }: { item: Intervention; ctx: PageContext; known: { cards: ActorCard[]; risks: Risk[] } | null }) {
  const cfg = useCase().cfg;
  if (!known) return null;
  const actor = actorLine(cardOf(i, known.cards), known.cards), risk = riskOf(cfg, i, known.risks);
  if (!actor && !risk) return null;
  const href = risk ? ctx.link('/actors', { tab: 'risks', risk: risk.id, actor: null, entity: null, metric: null, tour: null, step: null }) : '';
  return <div className="cs-event-origin" data-testid="event-origin">
    {actor && <p data-testid="event-actor">{actor}</p>}
    {risk && <p className="cs-event-history"><History size={13} aria-hidden /><span>{historyLine(risk)}
      <a className="cs-kb-link" href={href} data-testid="event-risk" title={`${risk.id} · ${risk.title}: the register of the risks that already fired`}
        onClick={e => { e.preventDefault(); ctx.go('/actors', { tab: 'risks', risk: risk.id, actor: null, entity: null, metric: null, tour: null, step: null }); }}>{risk.title}<ArrowUpRight size={12} /></a></span></p>}
  </div>;
}

function ValueField({ item: i, v }: { item: Intervention; v: Extract<InterventionValue, { kind: 'number' }> }) {
  const c = useCase(), cur = num(c.value(v.path)), [draft, setDraft] = useState<number | null>(null);
  const shown = draft ?? cur;
  const commit = (x: number) => { setDraft(null); if (x !== cur) c.change({ [v.path]: fixed(x) }, `${i.label} · ${v.label}: ${fmtValue(cur, v.unit)} → ${fmtValue(x, v.unit)}`, 'sim', i.forks?.[0]); };
  return <div className="cs-field" data-value={v.path}>
    <span className="cs-field-label">{v.label}</span>
    <ModifiedMark path={v.path} unit={v.unit} label={`${i.label} ${v.label}`} format={x => fmtValue(num(x), v.unit)}>
      <span className="cs-value-field">
        <input type="range" min={v.min} max={v.max} step={v.step} value={shown} aria-label={v.label} style={{ '--fill': `${(shown - v.min) / (v.max - v.min) * 100}%` } as React.CSSProperties}
          onChange={e => setDraft(Number(e.target.value))} onPointerUp={e => commit(Number((e.target as HTMLInputElement).value))} onKeyUp={e => commit(Number((e.target as HTMLInputElement).value))} onBlur={() => draft != null && commit(draft)} />
        <strong>{fmtValue(shown, v.unit)}</strong>
      </span>
    </ModifiedMark>
  </div>;
}

function ChoiceField({ item: i, v }: { item: Intervention; v: Extract<InterventionValue, { kind: 'choice' }> }) {
  const c = useCase(), cur = num(c.value(v.path)), name = (x: number) => v.options.find(o => o.value === x)?.label ?? String(x);
  return <div className="cs-field" data-value={v.path}>
    <span className="cs-field-label">{v.label}</span>
    <ModifiedMark path={v.path} unit={v.unit} label={`${i.label} ${v.label}`} format={x => name(num(x))}>
      <Segmented<string> size="sm" label={v.label} value={String(cur)} options={v.options.map(o => ({ id: String(o.value), label: o.label }))}
        onChange={x => { const to = Number(x); if (to !== cur) c.change({ [v.path]: fixed(to) }, `${i.label} · ${v.label}: ${name(cur)} → ${name(to)}`, 'sim', i.forks?.[0]); }} />
    </ModifiedMark>
  </div>;
}

/** Low / likely / high with three handles and the PERT curve above them. */
export function RangeField({ path, label, unit, what, fork }: { path: string; label: string; unit: string; what?: string; fork?: string }) {
  const c = useCase();
  const cur = c.value(path) as Dist, base = c.defaultOf(path) as Dist;
  const [draft, setDraft] = useState<{ low: number; mode: number; high: number } | null>(null);
  const svg = useRef<SVGSVGElement>(null), drag = useRef<'low' | 'mode' | 'high' | null>(null);
  if (cur.kind !== 'pert' || base?.kind !== 'pert') return null;
  const r = draft ?? { low: cur.low, mode: cur.mode, high: cur.high };
  const lo = Math.min(0, base.low * 2, r.low * 1.15), hi = Math.max(base.high * 2, r.high * 1.15), W = 260, Hh = 64;
  const step = unit === 'months' || unit === 'month' ? 0.5 : ['people', 'FTE', 'count', 'story points', 'points'].includes(unit) ? 1 : unit === 'share' ? 0.005 : +((hi - lo) / 100).toPrecision(1);
  const scale = displayScale(unit), show = (v: number) => +(v * scale).toFixed(scale === 100 ? 1 : 2);
  const x = (v: number) => 10 + (v - lo) / (hi - lo) * (W - 20), vAt = (px: number) => Math.round((lo + (px - 10) / (W - 20) * (hi - lo)) / step) * step;
  const pts = Array.from({ length: 61 }, (_, k) => r.low + (r.high - r.low) * k / 60), dens = pts.map(p => pertPdf(r.low, r.mode, r.high, p)), mx = Math.max(...dens, 1e-9);
  const d = `M${x(r.low)},${Hh - 18} ` + pts.map((p, k) => `L${x(p).toFixed(1)},${(Hh - 18 - dens[k] / mx * 34).toFixed(1)}`).join(' ') + ` L${x(r.high)},${Hh - 18} Z`;
  const move = (e: RPointerEvent) => {
    if (!drag.current || !svg.current) return;
    const b = svg.current.getBoundingClientRect(), v = Math.max(lo, vAt((e.clientX - b.left) / b.width * W)), n = { ...r };
    if (drag.current === 'low') n.low = Math.min(v, r.mode); else if (drag.current === 'high') n.high = Math.max(v, r.mode); else n.mode = Math.min(Math.max(v, r.low), r.high);
    setDraft(n);
  };
  const end = () => {
    if (!drag.current) return;
    drag.current = null;
    if (draft && (draft.low !== cur.low || draft.mode !== cur.mode || draft.high !== cur.high)) c.change({ [path]: { kind: 'pert', ...draft } }, `${what ? `${what} · ` : ''}${label}: ${fmtDist(cur, unit)} → ${fmtDist({ kind: 'pert', ...draft }, unit)}`, 'sim', fork);
    setDraft(null);
  };
  return <div className="cs-field" data-value={path}>
    <span className="cs-field-label">{label}<HelpTip entry={c.cfg.help.range} size={12} /></span>
    <ModifiedMark path={path} unit={unit} label={label}>
      <span className="cs-range-field">
        <svg ref={svg} viewBox={`0 0 ${W} ${Hh}`} width="100%" height={Hh} onPointerMove={move} onPointerUp={end} onPointerLeave={end} role="group" aria-label={`${label}: low ${show(r.low)}, likely ${show(r.mode)}, high ${show(r.high)} ${displaySuffix(unit)}`}>
          <path d={d} className="cs-pert" />
          <line x1={10} x2={W - 10} y1={Hh - 18} y2={Hh - 18} className="cs-pert-axis" />
          {(['low', 'mode', 'high'] as const).map(k => <g key={k} className={`cs-pert-handle is-${k}`} onPointerDown={e => { e.preventDefault(); try { (e.currentTarget as Element).setPointerCapture?.(e.pointerId); } catch { /* a pointer that is gone */ } drag.current = k; setDraft(r); }}>
            <rect x={x(r[k]) - 10} y={Hh - 30} width={20} height={26} fill="transparent" />
            <circle cx={x(r[k])} cy={Hh - 18} r={k === 'mode' ? 6 : 5} />
            <text x={x(r[k])} y={Hh - 3} textAnchor="middle">{show(r[k])}</text>
          </g>)}
        </svg>
        <span className="cs-range-legend"><span>low</span><span>likely</span><span>high</span><small>{displaySuffix(unit)}</small></span>
      </span>
    </ModifiedMark>
  </div>;
}

export function StaysSection() {
  const c = useCase(), cfg = c.cfg, sec = cfg.sections.find(s => s.id === 'stays')!;
  const inputs = cfg.inputs.filter(i => i.untouched);
  const blocks = cfg.blocks.filter(b => inputs.some(i => i.block === b.id));
  const actors = cfg.people.filter(p => !cfg.interventions.some(i => i.person === p.id || i.owner === p.id) && !cfg.shocks.some(x => x.person === p.id || x.owner === p.id));
  return <section id="stays" className="cs-section" aria-labelledby="stays-title">
    <details className="cs-stays">
      <summary><ChevronDown size={16} aria-hidden /><span><strong id="stays-title">What stays as before</strong><small>{inputs.length} input{inputs.length === 1 ? '' : 's'}{actors.length ? <> · {actors.length} {actors.length === 1 ? 'person' : 'people'}</> : ''}</small></span><HelpTip entry={sectionEntry(cfg, 'What stays as before', sec.purpose, sec.help)} label="What stays as before" more={['They keep their habits from the data.']} /></summary>
      <div className="cs-stays-body">
        {actors.length > 0 && <div className="cs-stays-people"><span className="cs-eyebrow">People</span>{actors.map(p => <span key={p.id} className="cs-person"><strong>{p.name}</strong><small>{p.title}</small></span>)}</div>}
        <div className="cs-stays-grid">{blocks.map(b => <div key={b.id}><span className="cs-eyebrow">{b.label}</span><ul>{inputs.filter(i => i.block === b.id).map(i => <li key={i.path}><span>{i.label}</span><b>{fmtDist(c.value(i.path), i.unit)}</b><small>{i.source}</small></li>)}</ul></div>)}</div>
      </div>
    </details>
  </section>;
}
