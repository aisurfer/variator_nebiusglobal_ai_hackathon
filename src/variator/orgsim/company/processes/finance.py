"""P3 billing and payments, P4 finance, P7 cloud bills: the money of the history.

This process runs after the day loop, when the contract schedules and the staff are known
and the ARR calibration factor is set (``Money.value``). It writes:

- client invoices in advance per billing period (amount = ACV in force / 12 x months),
  their e-mails, reminders and payments after the client's pay delay (+ the extra days of
  a late-payment spell);
- vendor bills (subcontractors, rent, software, professional services, marketing) paid on
  their due date; cloud bills from a daily cost series with a GPU share;
- payroll (salary in force, joiners and leavers pro rata, sales commission), PAYE and NIC
  by the 22nd of the next month, quarterly corporation tax instalments;
- bank entries with the running balance. The opening balance makes the cash at T equal to
  ``Truth.cash_target``.

Records that were open at the start of the history (invoices and bills issued before it
and paid inside it) are written at the start with the kind ``opening_balance``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from variator.orgsim.calendar import add_months
from variator.orgsim.company.model import Money, Record, World
from variator.orgsim.company.processes.clients import boundary_on_or_after, first_of_month
from variator.orgsim.company.processes.people import month_label

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

OWN_ORG = "LW"
SKU_SHARE = {"compute": 0.30, "storage": 0.12, "network": 0.08, "databases": 0.10, "support": 0.05}


@dataclass
class BankItem:
    ts: datetime
    amount: float  # signed: + credit, - debit
    counterparty: str | None
    records: Any
    reference: str
    causes: tuple
    kind: str
    extra: dict[str, Any] = field(default_factory=dict)


class Finance:
    def __init__(self, w: World, eng: Engine, scale: float):
        self.w, self.eng, self.scale = w, eng, scale
        self.bank: list[BankItem] = []
        self.cfo = eng.head("finance")
        self.vendor_contracts: dict[str, Record] = {}
        self.revenue_by_month: dict[str, float] = {}
        self.cost_by_month: dict[str, float] = {}
        self.ws = w.start
        self.lookback = add_months(first_of_month(w.start), -4)

    def rng(self, entity: str, purpose: str) -> np.random.Generator:
        return self.w.rng("finance", entity, purpose)

    def money(self, raw: float, step: float = 0.01) -> float:
        return Money(raw, step).value(self.scale)

    def run(self) -> None:
        self._vendor_contracts()
        self._client_invoices()
        self._vendor_bills()
        self._cloud_bills()
        self._payroll()
        self._corporation_tax()
        self._bank()

    # ------------------------------------------------------------------ helpers
    def _actor_ts(self, pid: str, d: date, lo: float = 0.0, hi: float = 1.0) -> tuple[datetime, str | None]:
        w = self.w
        if w.present(pid, d):
            return w.stamp(pid, d, lo=lo, hi=hi, purpose="finance"), pid
        return w.at(d, "10:00", 300, ("finance", pid)), None

    def _opening_ts(self, k: int) -> datetime:
        return self.w.at(self.ws, "06:50") + timedelta(seconds=3 * k)

    def _add_month(self, book: dict[str, float], d: date, x: float) -> None:
        key = f"{d.year}-{d.month:02d}"
        book[key] = book.get(key, 0.0) + x

    # ------------------------------------------------------------------ vendor agreements
    def _vendor_contracts(self) -> None:
        w = self.w
        t0 = w.at(self.ws, "05:50")
        for n, v in enumerate(w.spec.vendors):
            if v.kind == "bank":
                continue
            annual = w.spec.truth.infra_base if v.kind == "cloud" else v.monthly_fee * 12
            self.vendor_contracts[v.id] = w.record(
                "contract",
                "finance",
                t0 + timedelta(seconds=5 * n),
                "finance",
                "opening_balance",
                (self.eng.proc("PROC-OPENING"),),
                prefix="VA",
                ext=f"VA-{v.id}",
                number=f"VA-{v.id}",
                title=f"{v.name}: {v.kind} services agreement",
                client=OWN_ORG,
                vendor=v.id,
                status="active",
                amount=round(annual, 2),
                currency="USD",
                owner=self.cfo,
                contract_type="vendor",
                payment_terms_days=v.terms_days,
            )

    # ------------------------------------------------------------------ client invoices
    def _client_invoices(self) -> None:
        w = self.w
        items = []
        for cid in sorted(w.clients):
            c = w.clients[cid]
            if c.contract is None or c.start is None:
                continue
            months = 3 if c.profile.billing == "quarterly" else 1
            ps = boundary_on_or_after(max(c.start, self.lookback), c.profile.billing)
            while ps < w.anchor and (c.end is None or ps < c.end):
                acv = Money(c.acv_on(ps), 1200).value(self.scale)
                issue = w.cal.roll_forward(ps)
                if issue < w.anchor and acv > 0:
                    items.append((issue, cid, ps, months, round(acv * months / 12, 2)))
                ps = add_months(ps, months)
        items.sort()
        k_open = 0
        for n, (issue, cid, ps, months, amount) in enumerate(items, start=1):
            c = w.clients[cid]
            h = c.habits
            r = self.rng(cid, f"pay-{ps.isoformat()}")
            delay = max(5.0, r.normal(h.pay_delay_mean, h.pay_delay_sd))
            spell = next((s for s in c.late_spells if s[0] <= issue <= s[1]), None)
            if spell:
                delay += spell[2]
            due = issue + timedelta(days=30)
            paid = w.cal.roll_forward(issue + timedelta(days=int(delay)))
            if paid < self.ws:
                self._add_month(self.revenue_by_month, issue, amount)
                continue
            num = f"INV-{n:05d}"
            base = dict(
                number=num,
                title=f"Invoice {num}: {c.profile.name}, {ps:%B %Y}",
                client=cid,
                contract=c.contract,
                amount=amount,
                currency="USD",
                issued_at=issue.isoformat(),
                due_date=due.isoformat(),
                period=f"{ps:%Y-%m}" + (f"/{months}m" if months > 1 else ""),
                owner=self.cfo,
                direction="receivable",
                billing_frequency=c.profile.billing,
            )
            causes = (("process", c.contract), self.eng.proc("PROC-BILLING"), self.eng.rule("RULE-NET30"))
            self._add_month(self.revenue_by_month, issue, amount)
            if issue < self.ws:
                k_open += 1
                inv = w.record(
                    "invoice",
                    "finance",
                    self._opening_ts(k_open),
                    "finance",
                    "opening_balance",
                    causes + (self.eng.proc("PROC-OPENING"),),
                    prefix="INV",
                    ext=num,
                    status="overdue" if due < self.ws else "sent",
                    **base,
                )
            else:
                ts = w.at(issue, "07:30", 45, ("finance", cid))
                inv = w.record(
                    "invoice",
                    "finance",
                    ts,
                    "finance",
                    "invoice_issued",
                    causes,
                    prefix="INV",
                    ext=num,
                    status="issued",
                    **base,
                )
                ct = c.profile.contacts[1]
                to = (ct.name, self.eng.clients.contact_email(c, 1))
                sent = ts + timedelta(minutes=int(r.integers(20, 90)))
                w.mail(
                    sent,
                    w.mailbox("billing"),
                    [to],
                    f"Invoice {num} from Larchwood Systems",
                    f"Dear {ct.name.split()[0]}, please find attached invoice {num} for {ps:%B %Y}. "
                    f"It is due on {due:%d %B %Y}.",
                    process="finance",
                    kind="invoice_sent",
                    slot_kind="invoice_mail",
                )
                inv.update(
                    sent + timedelta(seconds=40),
                    "finance",
                    "invoice_sent",
                    status="sent",
                    sent_at=sent.isoformat(),
                )
            if paid > due + timedelta(days=7):
                rem = w.cal.roll_forward(due + timedelta(days=7))
                if self.ws <= rem < w.anchor and rem < paid:
                    ts, actor = self._actor_ts(self.cfo, rem, hi=0.5)
                    ct = c.profile.contacts[1]
                    sender = w.addr(self.cfo) if actor else w.mailbox("billing")
                    w.mail(
                        ts,
                        sender,
                        [(ct.name, self.eng.clients.contact_email(c, 1))],
                        f"Reminder: invoice {num} is overdue",
                        f"Dear {ct.name.split()[0]}, invoice {num} was due "
                        f"on {due:%d %B}. Could you let us know when we can expect the payment?",
                        process="finance",
                        kind="payment_reminder",
                        slot_kind="payment_reminder",
                    )
                    inv.update(
                        ts + timedelta(minutes=1),
                        "finance",
                        "invoice_overdue",
                        status="overdue",
                        reminded_at=ts.isoformat(),
                        actor=actor,
                    )
            if paid >= w.anchor:
                continue
            pts = w.at(paid, "10:00", 300, ("finance", cid))
            self.bank.append(
                BankItem(
                    pts,
                    amount,
                    cid,
                    inv,
                    num,
                    (self.eng.proc("PROC-BILLING"),),
                    "incoming",
                    {"invoice": inv, "client": cid, "late_spell": spell[3] if spell else None},
                )
            )

    # ------------------------------------------------------------------ vendor bills
    def _vendor_bills(self) -> None:
        w = self.w
        items = []
        for v in w.spec.vendors:
            if v.kind in ("cloud", "bank") or v.monthly_fee <= 0:
                continue
            m = self.lookback
            while m < w.anchor:
                advance = v.kind == "landlord"
                issue = w.cal.roll_forward(m if advance else add_months(m, 1))
                if issue >= w.anchor:
                    break
                r = self.rng(v.id, f"bill-{m.isoformat()}")
                noise = float(np.exp(r.normal(0, 0.05))) if v.kind in ("nearshore", "offshore") else 1.0
                items.append((issue, v.id, m, round(v.monthly_fee * noise, 2)))
                m = add_months(m, 1)
        items.sort()
        k_open = 0
        for n, (issue, vid, m, amount) in enumerate(items, start=1):
            v = next(x for x in w.spec.vendors if x.id == vid)
            due = issue + timedelta(days=v.terms_days)
            pay = w.cal.roll_forward(due)
            self._add_month(self.cost_by_month, m, amount)
            if pay < self.ws:
                continue
            num = f"BILL-{n:05d}"
            vnum = f"{''.join(x[0] for x in v.name.split())}-{m:%y%m}-{100 + n % 900}"
            base = dict(
                number=num,
                title=f"{v.name} bill for {month_label(m)}",
                client=OWN_ORG,
                vendor=vid,
                contract=self.vendor_contracts[vid],
                amount=amount,
                currency="USD",
                issued_at=issue.isoformat(),
                due_date=due.isoformat(),
                period=f"{m:%Y-%m}",
                owner=self.cfo,
                direction="payable",
                vendor_invoice_number=vnum,
                cost_class=v.cost_class,
            )
            causes = (("process", self.vendor_contracts[vid]), self.eng.proc("PROC-VENDOR-BILLS"))
            if issue < self.ws:
                k_open += 1
                bill = w.record(
                    "invoice",
                    "finance",
                    self._opening_ts(500 + k_open),
                    "finance",
                    "opening_balance",
                    causes + (self.eng.proc("PROC-OPENING"),),
                    prefix="BILL",
                    ext=num,
                    status="approved",
                    **base,
                )
            else:
                ts = w.at(issue, "09:00", 180, ("finance", vid))
                w.mail(
                    ts - timedelta(minutes=5),
                    (v.contact_name, f"billing@{v.domain}"),
                    [w.mailbox("billing")],
                    f"Invoice {vnum} for {month_label(m)}",
                    f"Please find attached our invoice {vnum} for {month_label(m)}.",
                    process="finance",
                    kind="vendor_bill",
                    slot_kind="vendor_bill_mail",
                )
                bill = w.record(
                    "invoice",
                    "finance",
                    ts,
                    "finance",
                    "bill_received",
                    causes,
                    prefix="BILL",
                    ext=num,
                    status="received",
                    **base,
                )
                appr = w.next_present_day(self.cfo, w.bday(issue, 1)) or w.bday(issue, 1)
                if appr < min(pay, w.anchor):
                    ats, actor = self._actor_ts(self.cfo, appr)
                    bill.update(
                        ats,
                        "finance",
                        "bill_approved",
                        status="approved",
                        approved_at=appr.isoformat(),
                        actor=actor,
                    )
            if pay >= w.anchor:
                continue
            pts = w.at(pay, "09:30", 120, ("finance", vid))
            self.bank.append(
                BankItem(
                    pts,
                    -amount,
                    vid,
                    bill,
                    vnum,
                    (self.eng.proc("PROC-VENDOR-BILLS"),),
                    "outgoing",
                    {"invoice": bill, "vendor": vid},
                )
            )

    # ------------------------------------------------------------------ cloud
    def _cloud_bills(self) -> None:
        w, t = self.w, self.w.spec.truth
        cloud = w.spec.vendor("cloud")
        first = add_months(first_of_month(self.ws), -1)
        days = [first + timedelta(days=k) for k in range((w.anchor - first).days)]
        r = self.rng(cloud.id, "usage")
        noise = np.exp(r.normal(0, 0.06, len(days)))
        gnoise = np.exp(r.normal(0, 0.10, len(days)))
        base = t.infra_base / 365.0
        per_month: dict[date, dict[str, float]] = {}
        for k, d in enumerate(days):
            trend = 0.94 + 0.12 * k / max(len(days) - 1, 1)
            week = 0.85 if d.weekday() >= 5 else 1.06
            total = base * trend * week * float(noise[k])
            gpu = total * t.gpu_share * float(gnoise[k])
            rest = total - total * t.gpu_share
            row = {"gpu": gpu, **{s: rest * x / sum(SKU_SHARE.values()) for s, x in SKU_SHARE.items()}}
            if d >= self.ws:
                w.row(
                    "cloud_cost_daily",
                    date=d.isoformat(),
                    **{s: round(x, 2) for s, x in row.items()},
                    total=round(sum(row.values()), 2),
                )
            m = first_of_month(d)
            acc = per_month.setdefault(m, {})
            for s, x in row.items():
                acc[s] = acc.get(s, 0.0) + x
        for n, (m, skus) in enumerate(sorted(per_month.items()), start=1):
            if add_months(m, 1) > w.anchor:
                break  # the month is not over at T
            issue = w.bday(w.cal.roll_forward(add_months(m, 1)), 1)
            if issue >= w.anchor or issue < self.ws:
                continue  # after T, or issued and paid before the history
            services = {s: round(x, 2) for s, x in skus.items()}
            amount = round(sum(services.values()), 2)
            due = issue + timedelta(days=cloud.terms_days)
            self._add_month(self.cost_by_month, m, amount)
            ts = w.at(issue, "07:05", 20, ("finance", "cloud"))
            bill = w.record(
                "cloud_bill",
                "finance",
                ts,
                "finance",
                "cloud_bill_received",
                (self.eng.proc("PROC-CLOUD"), ("process", self.vendor_contracts[cloud.id])),
                prefix="CB",
                ext=f"CB-{m:%Y-%m}",
                number=f"NC-{m:%y%m}-{4100 + n}",
                vendor=cloud.id,
                period=f"{m:%Y-%m}",
                amount=amount,
                currency="USD",
                status="issued",
                due_date=due.isoformat(),
                services=services,
                gpu_share=round(services["gpu"] / amount, 4),
                contract=self.vendor_contracts[cloud.id],
            )
            pay = w.cal.roll_forward(due)
            if pay < w.anchor:
                self.bank.append(
                    BankItem(
                        w.at(pay, "07:30", 20, ("finance", "cloud")),
                        -amount,
                        cloud.id,
                        bill,
                        bill.state["number"],
                        (self.eng.proc("PROC-CLOUD"),),
                        "direct_debit",
                        {"cloud_bill": bill},
                    )
                )

    # ------------------------------------------------------------------ payroll and taxes
    def _pay_day(self, m: date) -> date:
        return self.w.cal.roll_backward(m.replace(day=25))

    def _payroll(self) -> None:
        w, t = self.w, self.w.spec.truth
        tax_org = w.spec.tax_authority.id
        won = [(dl.extra.get("won_on"), dl.owner, dl.value) for dl in self.eng.sales.won]
        m = add_months(first_of_month(self.ws), -1)
        while m < w.anchor:
            pay = self._pay_day(m)
            due = w.cal.roll_backward(add_months(m, 1).replace(day=22))
            nxt = add_months(m, 1)
            if due < self.ws:
                m = nxt
                continue
            workdays = w.cal.workdays(m, nxt)
            lines = []
            for pid in sorted(w.people):
                p = w.people[pid]
                worked = sum(1 for x in workdays if p.employed(x))
                if worked == 0:
                    continue
                gross = p.salary_on(m) / 12 * worked / max(len(workdays), 1)
                prev = add_months(m, -1)
                commission = sum(
                    t.commission_rate * self.money(v)
                    for won_on, owner, v in won
                    if owner == pid and won_on and prev <= won_on < m
                )
                lines.append(
                    {
                        "employee": pid,
                        "gross": round(gross + commission, 2),
                        "commission": round(commission, 2),
                    }
                )
                w.row(
                    "payroll_lines",
                    period=f"{m:%Y-%m}",
                    employee=pid,
                    role=p.person.role,
                    gross=round(gross + commission, 2),
                    commission=round(commission, 2),
                    employer_tax=round((gross + commission) * t.payroll_tax_rate, 2),
                )
            gross = round(sum(x["gross"] for x in lines), 2)
            employer = round(gross * t.payroll_tax_rate, 2)
            deductions = round(gross * t.employee_deduction_rate, 2)
            net = round(gross - deductions, 2)
            self._add_month(self.cost_by_month, m, gross + employer)
            base = dict(
                title=f"Payroll {month_label(m)}",
                period=f"{m:%Y-%m}",
                amount=gross,
                currency="USD",
                headcount=len(lines),
                owner=self.cfo,
                employee=[x["employee"] for x in lines],
                employer_tax=employer,
                deductions=deductions,
                net_pay=net,
                lines=lines,
            )
            pr_id = f"PR-{m:%Y-%m}"
            if pay < self.ws:
                rec = w.record(
                    "payroll",
                    "people",
                    self._opening_ts(900),
                    "finance",
                    "opening_balance",
                    (self.eng.proc("PROC-PAYROLL"), self.eng.proc("PROC-OPENING")),
                    prefix="PR",
                    ext=pr_id,
                    status="paid",
                    paid_at=pay.isoformat(),
                    **base,
                )
            else:
                appr = w.bday(pay, -3)
                ats, actor = self._actor_ts(self.cfo, appr, lo=0.5)
                rec = w.record(
                    "payroll",
                    "people",
                    ats,
                    "finance",
                    "payroll_approved",
                    (self.eng.proc("PROC-PAYROLL"),),
                    prefix="PR",
                    ext=pr_id,
                    status="approved",
                    actor=actor,
                    **base,
                )
                if pay < w.anchor:
                    rec.update(
                        w.at(pay, "08:40", 10, ("finance", "payroll")),
                        "finance",
                        "payroll_paid",
                        status="paid",
                        paid_at=pay.isoformat(),
                        actor=None,
                    )
                    self.bank.append(
                        BankItem(
                            w.at(pay, "08:55", 4, ("finance", "payroll")),
                            -net,
                            None,
                            rec,
                            f"Payroll {m:%Y-%m}",
                            (self.eng.proc("PROC-PAYROLL"),),
                            "payroll",
                        )
                    )
            if due >= w.anchor or pay >= w.anchor:
                m = nxt
                continue
            amount = round(employer + deductions, 2)
            tax = w.record(
                "tax_payment",
                "finance",
                w.at(max(pay, self.ws), "09:10", 10, ("finance", "paye")),
                "finance",
                "tax_assessed",
                (("process", rec), self.eng.rule("RULE-PAYE")),
                prefix="TAX",
                ext=f"TAX-PAYE-{m:%Y-%m}",
                title=f"PAYE and NIC for {month_label(m)}",
                tax="PAYE and NIC",
                period=f"{m:%Y-%m}",
                amount=amount,
                currency="USD",
                authority=tax_org,
                status="scheduled",
                due_date=due.isoformat(),
                owner=self.cfo,
            )
            self.bank.append(
                BankItem(
                    w.at(due, "07:00", 30, ("finance", "paye")),
                    -amount,
                    tax_org,
                    tax,
                    f"PAYE {m:%Y-%m}",
                    (self.eng.rule("RULE-PAYE"),),
                    "tax",
                    {"tax": tax, "paid": due},
                )
            )
            m = nxt

    def _corporation_tax(self) -> None:
        w, t = self.w, self.w.spec.truth
        months = [k for k in sorted(self.revenue_by_month) if k >= f"{self.ws:%Y-%m}"]
        rev = sum(self.revenue_by_month.get(k, 0.0) for k in months)
        cost = sum(self.cost_by_month.get(k, 0.0) for k in months)
        annual_profit = max(rev - cost, 0.0) * 12 / max(len(months), 1)
        instalment = round(t.corporation_tax_rate * annual_profit / 4, 2)
        tax_org = w.spec.tax_authority.id
        for d in w.days:
            if d.month not in (1, 4, 7, 10) or d.day < 14 or w.cal.roll_forward(d.replace(day=14)) != d:
                continue
            q = (d.month - 1) // 3 or 4
            year = d.year if d.month > 1 else d.year - 1
            period = f"{year}-Q{q}"
            tax = w.record(
                "tax_payment",
                "finance",
                w.at(d, "07:20", 20, ("finance", "ct")),
                "finance",
                "tax_assessed",
                (self.eng.rule("RULE-CORP-TAX"),),
                prefix="TAX",
                ext=f"TAX-CT-{period}",
                title=f"Corporation tax instalment {period}",
                tax="Corporation tax",
                period=period,
                amount=instalment,
                currency="USD",
                authority=tax_org,
                status="scheduled",
                due_date=d.isoformat(),
                owner=self.cfo,
            )
            self.bank.append(
                BankItem(
                    w.at(d, "07:40", 20, ("finance", "ct")),
                    -instalment,
                    tax_org,
                    tax,
                    f"Corporation tax {period}",
                    (self.eng.rule("RULE-CORP-TAX"),),
                    "tax",
                    {"tax": tax, "paid": d},
                )
            )

    # ------------------------------------------------------------------ bank
    def _bank(self) -> None:
        w, t = self.w, self.w.spec.truth
        items = sorted((b for b in self.bank if b.ts < w.end), key=lambda b: (b.ts, b.reference))
        opening = round(t.cash_target - sum(b.amount for b in items), 2)
        self.opening = opening
        payments = sorted(
            (b for b in items if b.kind in ("incoming", "outgoing")), key=lambda b: (b.ts, b.reference)
        )
        pay_ids = {id(b): f"PAY-{n:05d}" for n, b in enumerate(payments, start=1)}
        balance = opening
        w.record(
            "bank_entry",
            "finance",
            w.at(self.ws, "06:30"),
            "finance",
            "opening_balance",
            (self.eng.proc("PROC-OPENING"),),
            prefix="BNK",
            ext="BNK-00000",
            amount=abs(opening),
            currency="USD",
            direction="credit" if opening >= 0 else "debit",
            booked_at=self.ws.isoformat(),
            account="Operating account",
            reference="Balance brought forward",
            balance_after=opening,
            counterparty=w.spec.vendor("bank").id,
        )
        for n, b in enumerate(items, start=1):
            balance = round(balance + b.amount, 2)
            records = b.records
            causes = b.causes
            if b.kind in ("incoming", "outgoing"):
                pid = pay_ids[id(b)]
                inv = b.extra["invoice"]
                party = {"client": b.counterparty} if b.kind == "incoming" else {"vendor": b.counterparty}
                actor = self.cfo if b.kind == "outgoing" and w.present(self.cfo, b.ts.date()) else None
                pay = w.record(
                    "payment",
                    "finance",
                    b.ts,
                    "finance",
                    "payment_received" if b.kind == "incoming" else "payment_sent",
                    (("process", inv),)
                    + ((("life_event", b.extra["late_spell"]),) if b.extra.get("late_spell") else ()),
                    prefix="PAY",
                    ext=pid,
                    number=pid,
                    amount=abs(b.amount),
                    currency="USD",
                    direction=b.kind,
                    settles=inv,
                    paid_at=b.ts.date().isoformat(),
                    method="bank transfer",
                    status="completed",
                    actor=actor,
                    title=f"Payment {pid}: {inv.state['number']}",
                    **party,
                )
                records = pay
                ts = b.ts + timedelta(minutes=3)
                inv.update(
                    b.ts + timedelta(minutes=30),
                    "finance",
                    "invoice_paid" if b.kind == "incoming" else "bill_paid",
                    status="paid",
                    paid_at=b.ts.date().isoformat(),
                    related=[pay],
                    actor=None,
                )
            elif b.kind == "tax":
                tax = b.extra["tax"]
                tax.update(
                    b.ts + timedelta(minutes=1),
                    "finance",
                    "tax_paid",
                    status="paid",
                    paid_at=b.extra["paid"].isoformat(),
                )
                ts = b.ts
            elif b.kind == "direct_debit":
                bill = b.extra["cloud_bill"]
                bill.update(
                    b.ts + timedelta(minutes=2),
                    "finance",
                    "cloud_bill_paid",
                    status="paid",
                    paid_at=b.ts.date().isoformat(),
                )
                ts = b.ts
            else:
                ts = b.ts
            w.record(
                "bank_entry",
                "finance",
                ts,
                "finance",
                "bank_entry_booked",
                causes,
                prefix="BNK",
                ext=f"BNK-{n:05d}",
                amount=abs(b.amount),
                currency="USD",
                direction="credit" if b.amount >= 0 else "debit",
                booked_at=b.ts.date().isoformat(),
                account="Operating account",
                counterparty=b.counterparty,
                records=records,
                reference=b.reference,
                balance_after=balance,
            )
            w.row(
                "bank",
                id=f"BNK-{n:05d}",
                ts=b.ts.isoformat(),
                date=b.ts.date().isoformat(),
                amount=round(b.amount, 2),
                kind=b.kind,
                counterparty=b.counterparty or "",
                reference=b.reference,
                balance_after=balance,
            )
        self.cash_at_t = balance
