// Shared pieces of the case page: modified marks with «was → now» and ↺, section headers with «Reset section»,
// the segmented control, the side sheet, fork chips and small status badges.
import { useState, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { ArrowLeft, FlaskConical, Hourglass, RotateCcw, Sparkles, X } from 'lucide-react';
import type { EditValue, HelpEntry, Unit } from '../model/types';
import { useCase } from '../state';
import { forkColor, forkLabel, fmtDist, forkNote } from './format';
import { HelpTip } from './HelpTip';

/** Marks a value that differs from its default: amber dot = your edit, lime = set by a hypothesis. «↺» returns to default. */
export function ModifiedMark({ path, unit, label, children, inline = false, format }: { path: string; unit: Unit | string; label: string; children?: ReactNode; inline?: boolean; format?: (v: EditValue | undefined) => string }) {
  const c = useCase();
  const edited = c.isEdited(path), preset = c.fromPreset(path);
  const f = format ?? ((v: EditValue | undefined) => fmtDist(v, unit));
  const state = edited ? 'edited' : preset ? 'preset' : null;
  return <span className={`cs-mod ${inline ? 'is-inline' : ''} ${state ? `is-${state}` : ''}`} data-mod={state ?? undefined}>
    {children}
    {state && <span className="cs-mod-tag">
      <span className="cs-mod-dot" aria-hidden />
      <span className="cs-mod-tip" role="note">{edited ? <>Your edit · was <b>{f(c.baseOf(path))}</b> → now <b>{f(c.value(path))}</b>{preset && <> · hypothesis value {f(c.presets[path])}</>}</> : <>Set by a hypothesis · default <b>{f(c.defaultOf(path))}</b> → now <b>{f(c.value(path))}</b></>}</span>
      {edited && <button type="button" className="cs-mod-reset" aria-label={`Return ${label} to ${preset ? 'the hypothesis value' : 'the default'}`} title={`Back to ${f(c.baseOf(path))}`} onClick={() => c.resetPaths([path], `${label}: back to ${preset ? 'the hypothesis value' : 'default'}`)}><RotateCcw size={12} /></button>}
    </span>}
  </span>;
}

/** The «?» entry of a section: its intro sentence (config `summary`) first, then the section's help text. */
export function sectionEntry(cfg: { help: Record<string, HelpEntry> }, title: string, purpose: string, help: string): HelpEntry {
  const h = cfg.help[help];
  return { title: h?.title ?? title, short: purpose || h?.short || title, full: h ? h.full || h.short : '' };
}

/** A section's title row: the step eyebrow, the title with its «?» (the intro and how to read it live there, not under
 *  the title), the badges and the tools. `more`: secondary lines of the section (how to read a chart, a footnote). */
export function SectionHeader({ id, title, purpose, help, paths, actions, badges, preview = false, simulation = true, more }: { id: string; title: string; purpose: string; help: string; paths?: (path: string) => boolean; actions?: ReactNode; badges?: ReactNode; preview?: boolean; simulation?: boolean; more?: (string | false | null | undefined)[] }) {
  const c = useCase();
  const edited = paths ? c.editedPaths(paths) : [];
  const st = c.cfg.steps.find(x => x.id === c.cfg.sections.find(s => s.id === id)?.step);
  return <header className="cs-section-head">
    <div className="cs-section-title">
      {st && <span className="cs-section-step">{st.number} · {st.label}</span>}
      <h2 id={`${id}-title`}>{title}<HelpTip entry={sectionEntry(c.cfg, title, purpose, help)} label={title} more={more} /></h2>
    </div>
    {/* what the user can do or see here, visible (the help keeps the details) */}
    {purpose && <p className="cs-section-purpose" title={purpose}>{purpose}</p>}
    <div className="cs-section-tools">
      {badges}
      {simulation && <SimBadge />}
      {preview && <PreviewBadge />}
      {c.stale && simulation && <span className="cs-badge is-stale"><Hourglass size={12} />Stale · recomputing</span>}
      {actions}
      {edited.length > 0 && <button type="button" className="cs-text-btn" onClick={() => c.resetPaths(edited, `${title}: section reset`)}><RotateCcw size={13} />Reset section<span className="cs-count">{edited.length}</span></button>}
    </div>
  </header>;
}
export function SimBadge() { const c = useCase(); return <span className="cs-badge is-sim" title={c.cfg.help.simulation?.short}><Sparkles size={12} />Simulation</span>; }
export function PreviewBadge() {
  const c = useCase(), h = c.cfg.help.preview;
  return <HelpTip variant="badge" className="cs-badge is-preview" entry={h ?? { title: 'Preview data', short: 'Preview data until the actor simulation is ready.', full: '' }} label="Preview data"><FlaskConical size={12} aria-hidden />Preview data</HelpTip>;
}

export function Segmented<T extends string>({ value, options, onChange, label, size = 'md' }: { value: T; options: { id: T; label: string; title?: string }[]; onChange: (v: T) => void; label: string; size?: 'sm' | 'md' }) {
  return <div className={`cs-seg is-${size}`} role="radiogroup" aria-label={label}>
    {options.map(o => <button key={o.id} type="button" role="radio" aria-checked={value === o.id} className={value === o.id ? 'is-on' : ''} title={o.title} onClick={() => onChange(o.id)}>{o.label}</button>)}
  </div>;
}

export function ForkDot({ fork, size = 8 }: { fork: string; size?: number }) {
  const c = useCase();
  return <i className="cs-dot" style={{ background: forkColor(c.cfg, fork), width: size, height: size }} aria-hidden />;
}
export function ForkName({ fork, strong = false }: { fork: string; strong?: boolean }) {
  const c = useCase();
  const T = strong ? 'strong' : 'span', note = useForkNote(fork);
  return <T className="cs-fork-name"><ForkDot fork={fork} />{forkLabel(c.cfg, fork)}{note && <ForkNote note={note} />}</T>;
}
/** The fork's name is fixed text («Hire 5 engineers»); when you edit a size of its own levers, say so next to it. */
export function useForkNote(fork: string): string | null {
  const c = useCase();
  return forkNote(c.cfg, fork, c.value, c.isEdited);
}
export function ForkNote({ note }: { note: string }) { return <span className="cs-fork-note" title="The fork's name is fixed; this value is your edit">· {note}</span>; }
/** Chips to pick forks for a chart (2–3 at a time). */
export function ForkPicker({ value, onChange, max = 3, min = 1, forks, label }: { value: string[]; onChange: (v: string[]) => void; max?: number; min?: number; forks: string[]; label: string }) {
  return <div className="cs-fork-picker" role="group" aria-label={label}>
    {forks.map(f => {
      const on = value.includes(f), full = !on && value.length >= max;
      return <button key={f} type="button" className={`cs-chip ${on ? 'is-on' : ''}`} aria-pressed={on} disabled={full || (on && value.length <= min)} title={full ? `Up to ${max} forks` : undefined}
        onClick={() => onChange(on ? value.filter(x => x !== f) : [...value, f])}><ForkDot fork={f} /><ForkLabel fork={f} /></button>;
    })}
  </div>;
}
function ForkLabel({ fork }: { fork: string }) { const c = useCase(); return <>{forkLabel(c.cfg, fork)}</>; }

/** Side sheet in the Plancy style (same frame as the dossier and the metric sheet). */
/** A side sheet; `back` (a step opened from another sheet) shows «Back to <label>» in place of the breadcrumb. */
export function Sheet({ open, onClose, eyebrow, title, children, foot, width, back }: { open: boolean; onClose: () => void; eyebrow: string; title: string; children: ReactNode; foot?: ReactNode; width?: number; back?: { label: string; onBack: () => void; href?: string } }) {
  const letter = useCase().cfg.letter;
  return <Dialog.Root open={open} onOpenChange={o => { if (!o) onClose(); }}>
    <Dialog.Portal>
      <Dialog.Overlay className="detail-overlay" />
      <Dialog.Content className="detail-panel cs-sheet" style={width ? { width: `min(${width}px, 100%)` } : undefined} aria-describedby={undefined}>
        <div className="detail-sticky-head">
          <div className="detail-topbar">{back ? <a className="detail-back" href={back.href} onClick={e => { e.preventDefault(); back.onBack(); }} aria-label={`Back to ${back.label}`} title={`Back to ${back.label}`}><ArrowLeft size={16} />Back to <b className="detail-back-to">{back.label}</b></a> : <span className="detail-breadcrumb"><Sparkles size={14} />Simulation · case {letter}</span>}<Dialog.Close className="domain-icon-button detail-close" aria-label="Close"><X size={18} /></Dialog.Close></div>
          <div className="detail-title-wrap"><span className="eyebrow">{eyebrow}</span><Dialog.Title>{title}</Dialog.Title></div>
        </div>
        <div className="detail-scroll-body">{children}</div>
        {foot && <div className="detail-bottom-bar">{foot}</div>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}

/** A small in-place confirm (no browser dialog). */
export function useConfirm() {
  const [open, setOpen] = useState(false);
  return { open, ask: () => setOpen(true), close: () => setOpen(false) };
}

export function Empty({ icon, title, text, action }: { icon: ReactNode; title: string; text: string; action?: ReactNode }) {
  return <div className="cs-empty">{icon}<strong>{title}</strong><p>{text}</p>{action}</div>;
}
