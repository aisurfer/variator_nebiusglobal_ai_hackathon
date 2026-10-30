"""P1 clients and contracts: renewals, churn, upsells, downgrades, late payments, implementations.

Each client has its own habits and one contract. Life events (09 §2.4):

- churn: a one-time event; hazard = base churn x the client's factor x (1 + incident bumps)
  + the overdue-implementation term (``k_churn_overdue``) + the NPS term in the renewal
  window (``k_churn_nps``, ``processes.nps``). An incident adds a bump of
  ``k_incident_churn`` x severity weight x the client's sensitivity that decays with
  ``incident_churn_tau_days``. The client gives notice; the service ends at a billing boundary.
- upsell (positive): the client asks for more; a deal; if won, the contract grows and an
  implementation project adds work (tickets) and support load.
- downgrade and late payment (negative): a smaller contract; a spell of late payments.

Amounts here are raw: the generator scales every client amount so that ARR at T is
``Truth.arr_target`` (``model.Money``).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import TYPE_CHECKING

from variator.orgsim.calendar import add_months
from variator.orgsim.company import templates as T
from variator.orgsim.company.habits import ClientProfile, NamePool, client_habits, expand_cohort
from variator.orgsim.company.model import (
    ClientState,
    FirstPassage,
    Issue,
    Money,
    Record,
    World,
    annual_hazard,
)

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

SEVERITY_WEIGHT = {"SEV1": 1.0, "SEV2": 0.5, "SEV3": 0.15}
CHURN_REASONS = [
    "budget cuts",
    "a move to an in-house team",
    "a lower price from a competitor",
    "a merger with another company",
    "a change of strategy",
]
STEP_COMPONENTS = {
    "tenant setup": "infrastructure",
    "SSO configuration": "auth",
    "data migration": "data-pipeline",
    "integration with the client systems": "integrations",
}
SIMPLE_STEPS = {
    "kick-off and requirements": "analyst_designer",
    "user training": "analyst_designer",
    "go-live checklist": "pm",
    "hypercare after go-live": "support",
}
UPSELL_COMPONENT = {
    "advanced analytics": "data-pipeline",
    "an extra region": "infrastructure",
    "the data export module": "api",
    "premium support": "web-app",
    "the integrations pack": "integrations",
    "GPU inference capacity": "ml-inference",
    "single sign-on for partners": "auth",
    "a custom reporting suite": "web-app",
}


def first_of_month(d: date) -> date:
    return d.replace(day=1)


def boundary_after(d: date, billing: str) -> date:
    """The first billing-period start strictly after ``d`` (months; quarters start in Jan, Apr, Jul, Oct)."""
    m = add_months(first_of_month(d), 1)
    if billing == "quarterly":
        while m.month not in (1, 4, 7, 10):
            m = add_months(m, 1)
    return m


def boundary_on_or_after(d: date, billing: str) -> date:
    if d.day == 1 and (billing == "monthly" or d.month in (1, 4, 7, 10)):
        return d
    return boundary_after(d, billing)


class Clients:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        self.names = NamePool(w.streams.scoped("clients"))
        self.tau_bdays = w.spec.truth.incident_churn_tau_days * 252 / 365
        self.implementations: list[ClientState] = []
        self.impl_log: list[
            tuple[ClientState, Issue, date]
        ] = []  # every implementation and its promised date

    def rng(self, cid: str, purpose: str):
        return self.w.rng("clients", cid, purpose)

    # ================================================================================== setup
    def setup(self) -> None:
        w = self.w
        names = NamePool(
            w.streams,
            {c.name for c in w.spec.key_clients}
            | {ct.name for c in w.spec.key_clients for ct in c.contacts}
            | {p.name for p in w.spec.people},
        )
        self.names = names
        profiles = expand_cohort(w.spec, w.streams, names)
        t0 = w.at(w.days[0], "06:00")
        for n, prof in enumerate(profiles):
            c = self.add(prof)
            r = self.rng(c.id, "opening")
            back = int(r.integers(2, 40))
            start = boundary_on_or_after(add_months(first_of_month(w.start), -back), prof.billing)
            if prof.renewal_in_months is not None:
                term_end = boundary_on_or_after(
                    add_months(first_of_month(w.start), prof.renewal_in_months), prof.billing
                )
            else:
                term_end = boundary_on_or_after(add_months(w.anchor, int(r.integers(1, 19))), prof.billing)
            c.start, c.term_end, c.acv = start, term_end, prof.acv
            c.schedule = [(start, prof.acv)]
            signed = start - timedelta(days=int(r.integers(10, 40)))
            c.contract = w.record(
                "contract",
                "sales",
                t0 + timedelta(seconds=5 * n),
                "clients",
                "opening_balance",
                (self.eng.proc("PROC-OPENING"),),
                prefix="CTR",
                **self._contract_fields(c, "active", signed),
            )

    def add(self, prof: ClientProfile, *, new: bool = False) -> ClientState:
        w = self.w
        c = ClientState(prof, client_habits(w.spec, prof, w.streams), new=new)
        c.churn_clock = FirstPassage(self.rng(c.id, "churn"))
        for k in ("upsell", "downgrade", "late"):
            c.clocks[k] = w.clock("clients", c.id, k)
        w.clients[c.id] = c
        return c

    def _contract_fields(self, c: ClientState, status: str, signed: date) -> dict:
        p = c.profile
        return dict(
            title=f"{p.name}: platform services agreement",
            client=p.id,
            status=status,
            amount=Money(c.acv, 1200),
            currency="USD",
            signed_at=signed.isoformat(),
            start_date=c.start.isoformat(),
            end_date=None,
            term_end=c.term_end.isoformat(),
            term_months=p.term_months,
            billing_frequency=p.billing,
            payment_terms_days=30,
            notice_days=int(c.habits.notice_days),
            sla_target=self.w.spec.truth.sla_target,
            owner=p.account_manager,
            acv_schedule=[{"from": s.isoformat(), "acv": Money(a, 1200)} for s, a in c.schedule],
            key_account=p.key,
        )

    def _amend(self, c: ClientState, ts, kind: str, causes: tuple, **extra) -> None:
        c.contract.update(
            ts,
            "clients",
            kind,
            causes,
            amount=Money(c.acv, 1200),
            acv_schedule=[{"from": s.isoformat(), "acv": Money(a, 1200)} for s, a in c.schedule],
            term_end=c.term_end.isoformat() if c.term_end else None,
            **extra,
        )

    # ================================================================================== the day
    def step(self, i: int, d: date) -> None:
        w, t = self.w, self.w.spec.truth
        for cid in sorted(w.clients):
            c = w.clients[cid]
            if c.status == "signed" and c.start and d >= c.start:
                c.status = "active"
                c.contract.update(
                    w.at(d, "06:50", 20, ("clients", cid)), "clients", "contract_activated", status="active"
                )
            if c.status == "notice" and c.end and d >= c.end:
                c.status = "churned"
                c.contract.update(
                    w.at(d, "06:55", 20, ("clients", cid)),
                    "clients",
                    "contract_terminated",
                    status="terminated",
                )
                continue
            if c.status != "active":
                continue
            if c.implementation is not None and not c.implementation.closed and c.promised and d > c.promised:
                c.overdue = True
            if c.term_end and d >= c.term_end:
                self._renew(c, d)
            elif c.term_end and w.bday(c.term_end, -20) == d:
                am = c.profile.account_manager
                self.eng.work.biz_ticket(am, d, i, f"Renewal prep for {c.profile.name}", days=(3, 10))
            h = c.habits
            bump = sum(
                t.k_incident_churn * wgt * h.incident_sensitivity * math.exp(-(i - k) / self.tau_bdays)
                for k, wgt, _ in c.bumps
                if k <= i
            )
            hz = annual_hazard(min(t.base_churn * h.churn_mult, 0.9)) * (1 + bump)
            if c.overdue:
                hz += annual_hazard(t.k_churn_overdue)
            hz += self.eng.nps.churn_term(c, d)[0]
            if c.churn_clock.tick(hz):
                self._churn(c, i, d, bump)
                continue
            if c.clocks["upsell"].tick(h.upsell_rate / 252.0) and not c.upsell_busy:
                self._upsell_request(c, i, d)
            if c.clocks["downgrade"].tick(h.downgrade_rate / 252.0):
                self._downgrade(c, d)
            spell = any(a <= d <= b for a, b, _, _ in c.late_spells)
            if c.clocks["late"].tick(h.late_payment_rate / 252.0) and not spell:
                self._late(c, d)

    def _renew(self, c: ClientState, d: date) -> None:
        w = self.w
        old_end = c.term_end
        c.acv = c.acv * (1 + c.habits.renewal_uplift)
        c.schedule.append((old_end, c.acv))
        c.term_end = boundary_on_or_after(add_months(old_end, c.profile.term_months), c.profile.billing)
        ts = w.at(d, "07:20", 30, ("clients", c.id))
        self._amend(
            c,
            ts,
            "contract_renewed",
            (self.eng.proc("PROC-RENEWAL"),),
            renewed_at=d.isoformat(),
            uplift=round(c.habits.renewal_uplift, 4),
        )

    def _client_mail(self, c: ClientState, d: date, subject: str, body: str, kind: str) -> object:
        w = self.w
        main = c.profile.contacts[0]
        ts = w.at(d, "08:30", 480, ("clients", c.id))
        am = c.profile.account_manager
        if not w.people[am].employed(d):
            am = self.eng.role_ids("sales", d)[0] if self.eng.role_ids("sales", d) else self.eng.head("ceo")
        return w.mail(
            ts,
            (main.name, self.contact_email(c, 0)),
            [w.addr(am)],
            subject,
            body,
            process="clients",
            kind=kind,
            slot_kind=kind,
        )

    def contact_email(self, c: ClientState, k: int) -> str:
        first, _, last = c.profile.contacts[k].name.replace("Dr. ", "").partition(" ")
        return f"{first[0].lower()}.{last.lower().replace(' ', '').replace(chr(39), '')}@{c.profile.domain}"

    def _churn(self, c: ClientState, i: int, d: date, bump: float) -> None:
        w, t = self.w, self.w.spec.truth
        r = self.rng(c.id, "churn_reason")
        hits = [
            (
                inc,
                t.k_incident_churn
                * wgt
                * c.habits.incident_sensitivity
                * math.exp(-(i - k) / self.tau_bdays),
            )
            for k, wgt, inc in c.bumps
            if k <= i
        ]
        hits = [(inc, x) for inc, x in hits if x > 0.1]
        base = annual_hazard(min(t.base_churn * c.habits.churn_mult, 0.9))
        nps_hz, signal, low = self.eng.nps.churn_term(c, d)
        total = base * (1 + bump) + (annual_hazard(t.k_churn_overdue) if c.overdue else 0) + nps_hz
        share_inc = base * bump / total
        share_nps = nps_hz / total
        if c.overdue and c.implementation is not None and r.random() < 0.6:
            reason = "the implementation is late"
        elif hits and r.random() < max(share_inc, 0.5):
            reason = "service problems after recent incidents"
        elif nps_hz > 0 and low and r.random() < max(share_nps, 0.5):
            reason = "low satisfaction with the service in the last NPS survey"
        else:
            reason = str(T.pick(r, CHURN_REASONS))
        causes: tuple = tuple(("process", inc) for inc, _ in sorted(hits, key=lambda x: -x[1])[:3])
        if c.overdue and c.implementation is not None:
            causes += (("process", c.implementation),)
        if nps_hz > 0:
            causes += tuple(("process", x) for x in low[:2])
        extra = {"nps_signal": round(signal, 1)} if signal is not None else {}
        le = w.record(
            "life_event",
            "sales",
            w.at(d, "07:40", 40, ("clients", c.id)),
            "clients",
            "life_event_happened",
            causes,
            prefix="LE",
            title=f"{c.profile.name} decides to leave",
            statement=f"{c.profile.name} decides to leave: {reason}.",
            client=c.id,
            effective_date=d.isoformat(),
            event_type="churn",
            polarity="negative",
            incident_bump=round(bump, 3),
            overdue_implementation=c.overdue,
            **extra,
        )
        notice = d + timedelta(days=int(c.habits.notice_days))
        c.end = boundary_after(notice, c.profile.billing)
        if c.term_end and c.end > c.term_end and c.term_end > d:
            c.end = c.term_end
        mail = self._client_mail(
            c,
            d,
            "Notice of termination",
            f"We have decided not to continue our agreement "
            f"with Larchwood Systems. Please treat this as our notice; the service can end on "
            f"{c.end:%d %B %Y}.",
            "termination_notice",
        )
        c.status = "notice"
        self._amend(
            c,
            mail.sent + timedelta(minutes=45),
            "notice_given",
            (("life_event", le), self.eng.rule("RULE-CLIENT-NOTICE")),
            status="notice",
            end_date=c.end.isoformat(),
            notice_at=d.isoformat(),
            related=[mail],
        )
        am = c.profile.account_manager
        if w.people[am].employed(d):
            self.eng.work.biz_ticket(
                am, d, i, f"Exit review with {c.profile.name}", client=c.id, days=(2, 8), after=mail.sent
            )

    def _upsell_request(self, c: ClientState, i: int, d: date) -> None:
        w, t = self.w, self.w.spec.truth
        r = self.rng(c.id, "upsell_size")
        feature = str(T.pick(r, T.UPSELL_FEATURES))
        share = t.upsell_share[0] + (t.upsell_share[1] - t.upsell_share[0]) * float(r.random())
        le = w.record(
            "life_event",
            "sales",
            w.at(d, "07:45", 40, ("clients", c.id)),
            "clients",
            "life_event_happened",
            (),
            prefix="LE",
            title=f"{c.profile.name} asks for {feature}",
            statement=f"{c.profile.name} asks for {feature}.",
            client=c.id,
            effective_date=d.isoformat(),
            event_type="upsell_request",
            polarity="positive",
            size_share=round(share, 3),
        )
        mail = self._client_mail(
            c,
            d,
            f"Request: {feature}",
            f"We would like to add {feature} to our agreement. Could you send a proposal?",
            "upsell_request",
        )
        c.upsell_busy = True
        self.eng.sales.open_upsell(c, le, mail, feature, share, i, d)

    def apply_upsell(
        self, c: ClientState, deal: Record, le: Record, feature: str, share: float, d: date, ts
    ) -> None:
        """A won upsell: the contract grows from the next billing period; an implementation starts."""
        w = self.w
        c.upsell_busy = False
        if c.status != "active":
            return
        eff = boundary_after(d, c.profile.billing)
        c.acv = c.acv * (1 + share)
        c.schedule.append((eff, c.acv))
        c.upsell_until, c.upsell_deal = w.bday(eff, 60), deal
        self._amend(
            c,
            ts,
            "contract_amended_upsell",
            (("life_event", le), ("process", deal)),
            deal=deal,
            last_amendment=f"upsell: {feature} from {eff.isoformat()}",
        )
        self.start_implementation(c, d, f"{c.profile.name}: {feature} rollout", feature, share, deal)

    def upsell_lost(self, c: ClientState) -> None:
        c.upsell_busy = False

    def _downgrade(self, c: ClientState, d: date) -> None:
        w, t = self.w, self.w.spec.truth
        r = self.rng(c.id, "downgrade_size")
        share = t.downgrade_share[0] + (t.downgrade_share[1] - t.downgrade_share[0]) * float(r.random())
        le = w.record(
            "life_event",
            "sales",
            w.at(d, "07:50", 40, ("clients", c.id)),
            "clients",
            "life_event_happened",
            (),
            prefix="LE",
            title=f"{c.profile.name} reduces its scope",
            statement=f"{c.profile.name} cuts its budget and reduces the scope by {share:.0%}.",
            client=c.id,
            effective_date=d.isoformat(),
            event_type="downgrade",
            polarity="negative",
            size_share=round(share, 3),
        )
        mail = self._client_mail(
            c,
            d,
            "Scope reduction",
            f"Because of budget cuts we need to reduce our scope "
            f"by about {share:.0%} from the next billing period.",
            "downgrade_request",
        )
        eff = boundary_after(d + timedelta(days=20), c.profile.billing)
        if c.end and eff >= c.end:
            return
        c.acv = c.acv * (1 - share)
        c.schedule.append((eff, c.acv))
        self._amend(
            c,
            mail.sent + timedelta(hours=2),
            "contract_amended_downgrade",
            (("life_event", le),),
            last_amendment=f"downgrade from {eff.isoformat()}",
            related=[mail],
        )

    def _late(self, c: ClientState, d: date) -> None:
        w, t = self.w, self.w.spec.truth
        r = self.rng(c.id, "late_spell")
        days = t.late_spell_days[0] + (t.late_spell_days[1] - t.late_spell_days[0]) * float(r.random())
        extra = t.late_extra_days[0] + (t.late_extra_days[1] - t.late_extra_days[0]) * float(r.random())
        end = d + timedelta(days=int(days))
        le = w.record(
            "life_event",
            "finance",
            w.at(d, "07:55", 40, ("clients", c.id)),
            "clients",
            "life_event_happened",
            (),
            prefix="LE",
            title=f"{c.profile.name} has cash-flow problems",
            statement=f"{c.profile.name} has cash-flow problems; its payments run about {int(extra)} days "
            f"late until {end:%B %Y}.",
            client=c.id,
            effective_date=d.isoformat(),
            end_date=end.isoformat(),
            event_type="late_payment",
            polarity="negative",
            extra_days=round(extra, 1),
        )
        c.late_spells.append((d, end, extra, le))

    # ================================================================================== new clients
    def onboard(self, prof: ClientProfile, deal: Record, value: float, d: date, ts) -> ClientState:
        """A won deal: a new client, a signed contract and an implementation project."""
        w = self.w
        c = self.add(prof, new=True)
        c.won_deal = deal
        c.status = "signed"
        c.start = boundary_after(w.bday(d, 8), "monthly")
        c.term_end = add_months(c.start, prof.term_months)
        c.acv = value
        c.schedule = [(c.start, value)]
        c.upsell_until, c.upsell_deal = w.bday(c.start, 60), deal
        c.contract = w.record(
            "contract",
            "sales",
            ts,
            "sales",
            "contract_signed",
            (("process", deal), self.eng.proc("PROC-SALES")),
            prefix="CTR",
            **self._contract_fields(c, "signed", d),
            deal=deal,
        )
        self.start_implementation(c, d, f"Onboarding: {prof.name}", None, 1.0, deal)
        return c

    def start_implementation(
        self, c: ClientState, d: date, title: str, feature: str | None, share: float, deal: Record
    ) -> None:
        w, eng = self.w, self.eng
        i = w.index.get(d, 0)
        pm = (eng.role_ids("pm", d) or [eng.head("delivery")])[0]
        who = pm if w.present(pm, d) else eng.head("delivery")
        ts = w.stamp(who, d, lo=0.4) if w.present(who, d) else w.at(d, "16:00", 60, ("clients", c.id))
        epic = eng.work.new_issue(
            "IMPL",
            "Epic",
            title,
            ts,
            who,
            assignee=pm,
            client=c.id,
            priority="High",
            labels=("onboarding" if feature is None else "upsell",),
            causes=(("process", deal), eng.proc("PROC-IMPLEMENTATION")),
            slot_kind="epic",
            process="clients",
        )
        lo, hi = w.spec.truth.implementation_bdays
        if feature is not None:  # an upsell rollout is a smaller project than an onboarding
            k = w.spec.truth.upsell_implementation_share
            lo, hi = round(lo * k), round(hi * k)
        r = self.rng(c.id, f"impl-{epic.uid}")
        c.implementation = epic
        c.promised = w.bday(d, int(r.integers(lo, hi + 1)))
        c.overdue = False
        self.impl_log.append((c, epic, c.promised))
        epic.on_done.append(lambda issue, when, c=c: self._implemented(c, issue, when))
        size = max(c.acv / 100_000, 0.3) ** 0.3
        components = dict(STEP_COMPONENTS)
        if feature is None:
            steps = [(name, max(1, round(pts * size))) for name, pts in T.ONBOARDING_STEPS]
        else:
            comp = UPSELL_COMPONENT.get(feature, "api")
            base = 3 + 20 * share
            steps = [
                (f"configure {feature}", round(base * 0.3) or 1),
                (f"build {feature} for the client", round(base * 0.5) or 2),
                (f"enable {feature} in production", 2),
                ("user training", 2),
            ]
            components.update(
                {
                    f"configure {feature}": comp,
                    f"build {feature} for the client": comp,
                    f"enable {feature} in production": "infrastructure",
                }
            )
        name = c.profile.name
        for k, (step, pts) in enumerate(steps):
            summary = f"{name}: {step}"
            if step in SIMPLE_STEPS:
                role = SIMPLE_STEPS[step]
                pid = (eng.role_ids(role, d) or [pm])[0]
                start = i + 2 + 4 * k
                issue = eng.work.new_issue(
                    "IMPL",
                    "Task",
                    summary,
                    ts + timedelta(minutes=2 + k),
                    who,
                    assignee=pid,
                    client=c.id,
                    parent=epic,
                    points=pts,
                    causes=(("process", deal),),
                    slot_kind="issue",
                    process="clients",
                )
                eng.work.add_simple(issue, start, start + max(pts, 1))
                continue
            comp = components.get(step, "integrations")
            issue = eng.work.new_issue(
                "IMPL",
                "Task" if k else "Story",
                summary,
                ts + timedelta(minutes=2 + k),
                who,
                component=comp,
                client=c.id,
                parent=epic,
                points=min(pts, 8),
                priority="High",
                causes=(("process", deal),),
                slot_kind="issue",
                process="clients",
            )
            eng.work.commit(issue)
        self.implementations.append(c)

    def _implemented(self, c: ClientState, epic: Issue, when) -> None:
        if c.implementation is not epic:
            return
        c.overdue = False
        if c.contract is not None:
            c.contract.update(
                when + timedelta(minutes=5),
                "clients",
                "implementation_completed",
                go_live=when.date().isoformat(),
                go_live_late_days=max((when.date() - c.promised).days, 0) if c.promised else 0,
            )

    # ================================================================================== queries
    def arr_on(self, d: date) -> float:
        return sum(c.acv_on(d) for c in self.w.clients.values() if c.active_on(d))
