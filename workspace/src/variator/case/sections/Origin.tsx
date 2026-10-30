// Where the forks come from, in a modal: the request of the author (the CEO) in their own words, the clarifying
// questions, what the system drafted from it (forks, world hypotheses, criteria: chips that close the modal and scroll
// to them), and the review and freeze by the author. «Edit request and regenerate» (the footer) opens the request in an
// editor; «Regenerate» only explains what would happen: regeneration is not in this version, and nothing changes.
// Two buttons open it: «Your request» in the page header and a small one in the Forks section header.
import { useEffect, useRef, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { ArrowLeft, ArrowRight, ChevronDown, Lock, MessagesSquare, PencilLine, Quote, RotateCcw, ShieldCheck, TriangleAlert, X } from 'lucide-react';
import type { CaseOrigin } from '../model/types';
import { BAR_OFFSET } from '../Progress';
import { longDate } from '../ui/format';

/** Scrolls a target under the sticky bar (the fork cards, the world hypotheses, the criteria). */
export function scrollToId(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - BAR_OFFSET - 8, behavior: reduce ? 'auto' : 'smooth' });
}

/** «Your request»: opens the origin modal. `compact`: the small one of the Forks section header. */
export function RequestButton({ onOpen, compact = false, tour }: { onOpen: () => void; compact?: boolean; tour?: string }) {
  return <button type="button" className={compact ? 'cs-text-btn cs-request-btn is-compact' : 'button cs-request-btn'} onClick={onOpen} aria-haspopup="dialog" data-tour={tour}
    title="Where these forks come from: your request, the questions, what was drafted and your review">
    <Quote size={compact ? 13 : 14} aria-hidden />Your request
  </button>;
}

type View = 'origin' | 'edit' | 'notice';

/** The origin modal. `modal` false: opened by the guided tour, which keeps its own card next to it (no focus trap). */
export function OriginDialog({ origin, caseTitle, open, modal = true, onClose, returnTo }: { origin: CaseOrigin; caseTitle: string; open: boolean; modal?: boolean; onClose: () => void; returnTo?: { current: Element | null } }) {
  const [view, setView] = useState<View>('origin');
  const [text, setText] = useState(origin.request);
  const jump = useRef<string | null>(null), area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (open) { setView('origin'); setText(origin.request); } }, [open, origin.request]);
  useEffect(() => { if (view === 'edit') area.current?.focus(); }, [view]);
  const keep = () => { setText(origin.request); setView('origin'); };
  const changed = text.trim() !== origin.request.trim();
  const d = origin.drafted;
  const chips = [
    { n: d.forks.length, one: 'fork', many: 'forks', to: 'fork-cards' },
    { n: d.hypotheses.length, one: 'hypothesis', many: 'hypotheses', to: 'world' },
    { n: d.criteria.length, one: 'criterion', many: 'criteria', to: 'compare' },
  ];
  return <Dialog.Root open={open} modal={modal} onOpenChange={o => { if (!o) onClose(); }}>
    <Dialog.Portal>
      {modal && <Dialog.Overlay className="cs-modal-overlay" />}
      <Dialog.Content className={`cs-modal cs-origin-modal ${modal ? '' : 'is-toured'}`} data-testid="origin" data-tour="origin-modal" aria-describedby="origin-lead"
        onOpenAutoFocus={e => { if (!modal) e.preventDefault(); }}
        onInteractOutside={e => { if ((e.target as Element | null)?.closest?.('[data-tour-ui]')) e.preventDefault(); }}
        onCloseAutoFocus={e => {
          e.preventDefault();
          if (jump.current) { const to = jump.current; jump.current = null; window.setTimeout(() => scrollToId(to), 30); return; }
          // the button that opened it gets the focus back (there is no Radix trigger: two buttons open it)
          const el = returnTo?.current as HTMLElement | null | undefined;
          if (el?.isConnected && modal) el.focus({ preventScroll: true });
        }}>
        <div className="cs-modal-head"><span className="cs-eyebrow">{caseTitle} · your request</span><Dialog.Close className="cs-icon-btn" aria-label="Close"><X size={17} /></Dialog.Close></div>
        {view === 'origin' && <>
          <Dialog.Title className="cs-modal-title">Where these forks come from</Dialog.Title>
          <div className="cs-origin-scroll">
            <blockquote className="cs-origin-quote" id="origin-lead">
              <Quote size={18} aria-hidden className="cs-origin-mark" />
              <p data-testid="origin-request">{origin.request}</p>
              <footer>{origin.author.name}{origin.author.title && <span>, {origin.author.title}</span>}<span className="cs-origin-sep">·</span><time dateTime={origin.requestedOn}>{longDate(origin.requestedOn)}</time></footer>
            </blockquote>
            {origin.qa.length > 0 && <dl className="cs-origin-qa" aria-label="Clarifying questions">{origin.qa.map(x => <div key={x.q}><dt><MessagesSquare size={13} aria-hidden />{x.q}</dt><dd>{x.a}</dd></div>)}</dl>}
            <div className="cs-origin-flow">
              <ArrowRight size={15} aria-hidden className="cs-origin-arrow" />
              <span className="cs-origin-label">Drafted</span>
              <span className="cs-origin-chips">{chips.map(ch => <button key={ch.to} type="button" className="cs-chip cs-origin-chip" onClick={() => { jump.current = ch.to; onClose(); }} title={`Go to the ${ch.many}`}>
                <b>{ch.n}</b> {ch.n === 1 ? ch.one : ch.many}</button>)}</span>
            </div>
            <details className="cs-origin-review">
              <summary><ShieldCheck size={15} aria-hidden /><span>Reviewed and frozen by you on <time dateTime={origin.frozenOn}>{longDate(origin.frozenOn)}</time></span><span className="cs-origin-what">What you changed<ChevronDown size={13} aria-hidden /></span></summary>
              <ul>{origin.reviewEdits.map(e => <li key={e}>{e}</li>)}</ul>
            </details>
          </div>
          <div className="cs-origin-foot">
            <button type="button" className="button" onClick={() => setView('edit')}><PencilLine size={14} />Edit request and regenerate</button>
            <Dialog.Close className="button button-primary">Close</Dialog.Close>
          </div>
        </>}
        {view === 'edit' && <>
          <Dialog.Title className="cs-modal-title">Edit the request</Dialog.Title>
          <p id="origin-lead" className="cs-modal-lead">The forks, world hypotheses and criteria of this case were drafted from this text. Change it to draft the case again.</p>
          <textarea ref={area} className="cs-regen-text" value={text} onChange={e => setText(e.target.value)} rows={9} aria-label="Your request" />
          <div className="cs-regen-foot">
            <span className="cs-muted">{text.trim().split(/\s+/).filter(Boolean).length} words{changed && ' · edited'}</span>
            {changed && <button type="button" className="cs-text-btn" onClick={() => setText(origin.request)}><RotateCcw size={13} />Original text</button>}
            <button type="button" className="button" onClick={keep}><ArrowLeft size={14} />Cancel</button>
            <button type="button" className="button button-primary" onClick={() => setView('notice')}>Regenerate</button>
          </div>
        </>}
        {view === 'notice' && <div role="alertdialog" aria-labelledby="regen-title" aria-describedby="regen-points" className="cs-regen-notice">
          <Dialog.Title id="regen-title" className="cs-modal-title"><TriangleAlert size={18} aria-hidden />Regenerate this case?</Dialog.Title>
          <ul id="regen-points" className="cs-regen-points">
            <li>The forks of this case were already drafted from this request and reviewed by you on {longDate(origin.frozenOn)}.</li>
            <li>If the request changes, everything is drafted again: forks, world hypotheses and criteria. Your edits and the results are reset.</li>
            <li><strong>Regeneration is not available in this version of VARIATOR.</strong></li>
          </ul>
          <div className="cs-regen-foot">
            <button type="button" className="button button-primary" onClick={keep}>Keep current forks</button>
            <span className="cs-regen-off">
              <button type="button" className="button" disabled aria-describedby="regen-off-note"><Lock size={14} />Regenerate everything</button>
              <small id="regen-off-note">Not available in this version</small>
            </span>
          </div>
        </div>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
