"""P2 sales: new-business deals from the pipeline and upsell deals of existing clients.

New deals arrive as a counting process with rate ``pipeline`` / mean deal value per year.
Each deal is a slot actor ``deal-slot-n`` (its prospect, value, stage times and outcome draw
come from the slot's streams). A deal is won when its uniform draw is below
``win_rate`` x (0.8 + 0.2 x roadmap), where roadmap = min(1, target load / current load):
a team over capacity wins less (company_model). A won deal becomes a client, a contract and
an implementation project.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from variator.orgsim.company import templates as T
from variator.orgsim.company.habits import BILLING_TITLES, MAIN_TITLES, ClientProfile, domain_of
from variator.orgsim.company.model import ClientState, Money, Record, World
from variator.orgsim.company.spec import Contact
from variator.orgsim.streams import slot_key

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

STAGES = ("lead", "qualified", "proposal", "negotiation")
LOST_REASONS = ["price", "a competitor", "no decision", "timing", "missing features"]
SOURCES = ["inbound", "outbound", "referral", "event"]


@dataclass
class Deal:
    rec: Record
    slot: str
    prospect: ClientProfile | None
    value: float
    owner: str
    plan: list[tuple[date, str]]  # (day, stage or outcome)
    u_win: float
    drop: str | None  # the stage after which a lost deal ends (None: decided at the close)
    client: ClientState | None = None  # upsell
    le: Record | None = None
    feature: str = ""
    share: float = 0.0
    thread: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class Sales:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        self.deals: list[Deal] = []  # open deals
        self.all: list[Deal] = []  # every new-business deal (their prospects go to people.yaml)
        self.won: list[Deal] = []
        self.slots = 0
        t = w.spec.truth
        self.mean_value = t.new_deal_value_median * math.exp(t.deal_value_sigma**2 / 2)
        self.arrivals = w.clock("sales", "pipeline", "arrivals")

    def owner_for(self, slot: str, d: date) -> str:
        reps = self.eng.role_ids("sales", d)
        if not reps:
            return self.eng.head("ceo")
        r = self.w.rng("sales", slot, "owner")
        return reps[int(r.integers(len(reps)))]

    # ================================================================================== setup
    def setup(self) -> None:
        w = self.w
        for k in range(w.spec.truth.open_deals_at_start):
            self._new_deal(w.days[0], opening=True, n=k)

    # ================================================================================== new business
    def _prospect(self, slot: str, d: date, value: float) -> ClientProfile:
        w = self.w
        r = w.rng("sales", slot, "prospect")
        industry = str(T.pick(r, w.spec.cohort.industries))
        names = self.eng.clients.names
        name = names.company(slot, industry)
        pid = f"PR-{int(slot.rsplit('-', 1)[1]):03d}"
        owner = self.owner_for(slot, d)
        return ClientProfile(
            id=pid,
            name=name,
            domain=domain_of(name),
            industry=industry,
            acv=round(value / 1200) * 1200,
            billing="monthly",
            term_months=int(r.choice([12, 24])),
            renewal_in_months=None,
            account_manager=owner,
            contacts=[
                Contact(name=names.person(f"{slot}-main"), title=str(T.pick(r, MAIN_TITLES)), kind="main"),
                Contact(
                    name=names.person(f"{slot}-billing"), title=str(T.pick(r, BILLING_TITLES)), kind="billing"
                ),
            ],
            key=False,
        )

    def _new_deal(self, d: date, *, opening: bool = False, n: int = 0) -> None:
        w, t = self.w, self.w.spec.truth
        self.slots += 1
        slot = slot_key("deal", self.slots)
        r = w.rng("sales", slot, "deal")
        value = t.new_deal_value_median * math.exp(r.normal(0, t.deal_value_sigma))
        prospect = self._prospect(slot, d, value)
        owner = prospect.account_manager
        total = t.deal_cycle_days[0] + (t.deal_cycle_days[1] - t.deal_cycle_days[0]) * float(r.random())
        cuts = sorted(float(x) for x in r.random(3))
        stage_days = [0.0] + [total * x for x in cuts]
        u_win = float(r.random())
        drop = str(r.choice(["qualified", "proposal", "negotiation"], p=[0.4, 0.3, 0.3]))
        start_stage = 0
        if opening:
            start_stage = int(r.integers(0, 4))
            shift = stage_days[start_stage] - 3
            stage_days = [x - shift for x in stage_days]
            total -= shift
        plan = [(d + timedelta(days=int(x)), STAGES[k]) for k, x in enumerate(stage_days) if k > start_stage]
        plan.append((d + timedelta(days=int(total)), "close"))
        plan = [(w.cal.roll_forward(x), s) for x, s in plan]
        ts = w.at(d, "06:45", 10, ("sales", slot)) if opening else self._ts(owner, d)
        kind = "opening_balance" if opening else "deal_created"
        causes = (self.eng.proc("PROC-OPENING" if opening else "PROC-SALES"),)
        rec = w.record(
            "deal",
            "sales",
            ts + timedelta(seconds=n),
            "sales",
            kind,
            causes,
            prefix="DEAL",
            title=f"New business: {prospect.name}",
            client=prospect.id,
            owner=owner,
            status=STAGES[start_stage],
            amount=Money(value, 100),
            currency="USD",
            expected_close=plan[-1][0].isoformat(),
            probability=round(t.win_rate, 2),
            deal_type="new",
            source=str(T.pick(r, SOURCES)),
            industry=prospect.industry,
            actor=owner if not opening and w.present(owner, d) else None,
        )
        self.deals.append(Deal(rec, slot, prospect, value, owner, plan, u_win, drop))
        self.all.append(self.deals[-1])

    def _ts(self, pid: str, d: date, after: datetime | None = None) -> datetime:
        w = self.w
        return w.stamp(pid, d, after=after) if w.present(pid, d) else w.at(d, "11:00", 300, ("sales", pid))

    # ================================================================================== the day
    def step(self, i: int, d: date) -> None:
        w, t = self.w, self.w.spec.truth
        for _ in range(self.arrivals.tick(t.pipeline / self.mean_value / 252.0)):
            self._new_deal(d)
        for deal in list(self.deals):
            due = [x for x in deal.plan if x[0] <= d]
            if not due:
                continue
            deal.plan = [x for x in deal.plan if x[0] > d]
            owner = deal.owner if w.people[deal.owner].employed(d) else self.owner_for(deal.slot + "x", d)
            if not w.present(owner, d):  # the owner is away: the next steps wait for the owner's return
                back = w.next_present_day(owner, w.bday(d, 1))
                if back is not None and back < w.anchor:
                    deal.plan = [(max(x, back), s) for x, s in due + deal.plan]  # keeps the stage order
                continue
            for _, stage in due:
                if deal.client is not None:
                    self._upsell_stage(deal, stage, i, d)
                else:
                    self._stage(deal, stage, i, d)
                if deal not in self.deals:
                    break

    def _stage(self, deal: Deal, stage: str, i: int, d: date) -> None:
        w = self.w
        owner = deal.owner if w.people[deal.owner].employed(d) else self.owner_for(deal.slot + "x", d)
        deal.owner = owner
        actor = owner if w.present(owner, d) else None
        ts = self._ts(owner, d)
        pr = deal.prospect
        main = (pr.contacts[0].name, self.contact_email(pr, 0))
        if stage != "close":
            if (
                deal.drop is not None
                and STAGES.index(stage) > STAGES.index(deal.drop)
                and deal.u_win >= self.p_win()
            ):
                self._close(deal, False, d, ts, actor)
                return
            deal.rec.update(ts, "sales", f"deal_{stage}", status=stage, owner=owner, actor=actor)
            if stage == "qualified":
                deal.thread = w.mail(
                    ts + timedelta(minutes=10),
                    w.addr(owner),
                    [main],
                    f"Larchwood Systems and {pr.name}",
                    f"Thank you for the call. As discussed, here is an overview of our platform "
                    f"services for {pr.industry} companies.",
                    process="sales",
                    kind="sales_intro",
                    slot_kind="sales_intro",
                )
            elif stage == "proposal":
                deal.thread = w.mail(
                    ts + timedelta(minutes=30),
                    w.addr(owner),
                    [main],
                    f"Proposal for {pr.name}",
                    "Please find our proposal attached. The annual fee and the implementation plan "
                    "are on page 2.",
                    process="sales",
                    kind="sales_proposal",
                    parent=deal.thread if deal.thread else None,
                    slot_kind="sales_proposal",
                )
                self.eng.work.biz_ticket(
                    owner, d, i, f"Proposal for {pr.name}", client=pr.id, days=(1, 4), after=ts
                )
            return
        self._close(deal, deal.u_win < self.p_win(), d, ts, actor)

    def p_win(self) -> float:
        t = self.w.spec.truth
        roadmap = min(1.0, t.team_load / max(self.w.team_load, 1e-9))
        return t.win_rate * (0.8 + 0.2 * roadmap)

    def contact_email(self, pr: ClientProfile, k: int) -> str:
        first, _, last = pr.contacts[k].name.partition(" ")
        return f"{first[0].lower()}.{last.lower().replace(' ', '').replace(chr(39), '')}@{pr.domain}"

    def _close(self, deal: Deal, won: bool, d: date, ts: datetime, actor: str | None) -> None:
        w = self.w
        pr = deal.prospect
        main = (pr.contacts[0].name, self.contact_email(pr, 0))
        self.deals.remove(deal)
        if not won:
            reason = str(T.pick(w.rng("sales", deal.slot, "lost"), LOST_REASONS))
            w.mail(
                ts - timedelta(minutes=40),
                main,
                [w.addr(deal.owner)],
                f"Re: Proposal for {pr.name}",
                f"Thank you for your time. We have decided not to go ahead for now ({reason}).",
                process="sales",
                kind="sales_lost",
                parent=deal.thread,
                slot_kind="sales_lost",
            )
            deal.rec.update(
                ts,
                "sales",
                "deal_lost",
                status="lost",
                closed_at=d.isoformat(),
                lost_reason=reason,
                probability=0.0,
                actor=actor,
            )
            return
        mail = w.mail(
            ts - timedelta(minutes=40),
            main,
            [w.addr(deal.owner)],
            f"Signed agreement: {pr.name}",
            "Please find the signed agreement attached. We look forward to the kick-off.",
            process="sales",
            kind="sales_won",
            parent=deal.thread,
            slot_kind="sales_won",
        )
        client = self.eng.clients.onboard(pr, deal.rec, deal.value, d, ts)
        deal.rec.update(
            ts + timedelta(minutes=1),
            "sales",
            "deal_won",
            status="won",
            closed_at=d.isoformat(),
            probability=1.0,
            contract=client.contract,
            related=[mail],
            actor=actor,
        )
        deal.extra["won_on"] = d
        self.won.append(deal)

    # ================================================================================== upsells
    def open_upsell(
        self, c: ClientState, le: Record, mail, feature: str, share: float, i: int, d: date
    ) -> None:
        w = self.w
        self.slots += 1
        slot = slot_key("deal", self.slots)
        r = w.rng("sales", slot, "upsell")
        owner = c.profile.account_manager
        if not w.people[owner].employed(d):
            owner = self.owner_for(slot, d)
        q = w.bday(d, int(r.integers(1, 4)))
        p = w.bday(q, int(r.integers(3, 9)))
        close = w.bday(p, int(r.integers(5, 16)))
        value = c.acv * share
        ts = self._ts(owner, d, after=mail.sent)
        rec = w.record(
            "deal",
            "sales",
            ts,
            "sales",
            "deal_created",
            (("life_event", le), self.eng.proc("PROC-SALES")),
            prefix="DEAL",
            title=f"Upsell: {feature} for {c.profile.name}",
            client=c.id,
            owner=owner,
            status="lead",
            amount=Money(value, 100),
            currency="USD",
            expected_close=close.isoformat(),
            probability=round(self.w.spec.truth.upsell_win, 2),
            deal_type="upsell",
            related=[mail],
            actor=owner if w.present(owner, d) else None,
        )
        plan = [(q, "qualified"), (p, "proposal"), (close, "close")]
        self.deals.append(
            Deal(
                rec,
                slot,
                None,
                value,
                owner,
                plan,
                float(r.random()),
                None,
                client=c,
                le=le,
                feature=feature,
                share=share,
                thread=mail,
            )
        )

    def _upsell_stage(self, deal: Deal, stage: str, i: int, d: date) -> None:
        w = self.w
        c = deal.client
        owner = deal.owner if w.people[deal.owner].employed(d) else self.owner_for(deal.slot + "x", d)
        deal.owner = owner
        actor = owner if w.present(owner, d) else None
        ts = self._ts(owner, d)
        if c.status != "active":
            self.deals.remove(deal)
            deal.rec.update(
                ts,
                "sales",
                "deal_lost",
                status="lost",
                closed_at=d.isoformat(),
                lost_reason="the client gave notice",
                probability=0.0,
                actor=actor,
            )
            self.eng.clients.upsell_lost(c)
            return
        main = (c.profile.contacts[0].name, self.eng.clients.contact_email(c, 0))
        if stage != "close":
            deal.rec.update(ts, "sales", f"deal_{stage}", status=stage, owner=owner, actor=actor)
            if stage == "proposal":
                deal.thread = w.mail(
                    ts + timedelta(minutes=20),
                    w.addr(owner),
                    [main],
                    f"Re: Request: {deal.feature}",
                    f"Here is our proposal for {deal.feature}. It adds {deal.share:.0%} to the annual fee.",
                    process="sales",
                    kind="upsell_proposal",
                    parent=deal.thread,
                    slot_kind="upsell_proposal",
                )
            return
        self.deals.remove(deal)
        if deal.u_win >= w.spec.truth.upsell_win:
            deal.rec.update(
                ts,
                "sales",
                "deal_lost",
                status="lost",
                closed_at=d.isoformat(),
                lost_reason="budget",
                probability=0.0,
                actor=actor,
            )
            self.eng.clients.upsell_lost(c)
            return
        mail = w.mail(
            ts - timedelta(minutes=30),
            main,
            [w.addr(owner)],
            f"Re: Request: {deal.feature}",
            "We accept the proposal. Please go ahead.",
            process="sales",
            kind="upsell_accepted",
            parent=deal.thread,
            slot_kind="upsell_accepted",
        )
        deal.rec.update(
            ts,
            "sales",
            "deal_won",
            status="won",
            closed_at=d.isoformat(),
            probability=1.0,
            contract=c.contract,
            related=[deal.rec.state.get("related", [None])[0], mail],
            actor=actor,
        )
        deal.extra["won_on"] = d
        self.eng.clients.apply_upsell(
            c, deal.rec, deal.le, deal.feature, deal.share, d, ts + timedelta(minutes=2)
        )
        self.won.append(deal)
