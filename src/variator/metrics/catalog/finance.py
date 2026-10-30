"""Finance metrics: cash, runway, burn, revenue, costs, gross margin, DSO, taxes, budget variance.

Sources are the KB ``sim`` business records: bank entries, invoices and vendor bills, cloud bills, payroll
and tax payments. Money sums are ``Decimal``. The monthly P&L metrics (revenue, costs, gross margin, budget
variance) use the "reporting month": the latest calendar month before the as-of month whose cost records are
all known (payroll, the cloud bill and a bill of every vendor that bills around that month).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from variator.metrics.catalog.asof import DAYS_PER_MONTH, add_months, month_key, parse_date, period_months
from variator.metrics.catalog.core import (
    Breakdown,
    Computation,
    Ctx,
    D,
    Item,
    aggregate,
    fmt_money,
    money,
    operation,
    rnd,
    tile_value,
    top_breakdown,
)
from variator.metrics.catalog.datasets import Version
from variator.metrics.catalog.ids import org_id, sim_id
from variator.metrics.results import LineageNode

ZERO = Decimal(0)


# ---------------------------------------------------------------------------------------------- bank
def signed(v: Version) -> Decimal:
    a = D(v.f.get("amount"))
    return a if v.f.get("direction") == "credit" else -a


def bank_known(ctx: Ctx, cut=None) -> list[tuple[str, Version]]:
    rows = ctx.data.table("bank_entry").known(cut or ctx.cut)
    return sorted(rows, key=lambda r: (r[1].ts, r[0]))


def latest_balance(ctx: Ctx, cut=None) -> tuple[str, Version] | None:
    rows = bank_known(ctx, cut)
    return rows[-1] if rows else None


def bank_category(ctx: Ctx, rid: str, v: Version) -> tuple[str, str]:
    kind = ctx.data.bank_kind.get(rid, "")
    rec = str(v.f.get("records") or "")
    if v.kind == "opening_balance":
        return "opening", "Opening balance"
    if v.f.get("direction") == "credit":
        return "receipts", "Client receipts"
    if rec.startswith("PR-") or kind == "payroll":
        return "payroll", "Payroll (net pay)"
    if rec.startswith("TAX-") or kind == "tax":
        ref = str(v.f.get("reference") or "")
        return ("tax:paye", "PAYE and NIC") if ref.startswith("PAYE") else ("tax:other", "Corporation tax")
    name = str(v.f.get("counterparty_name") or v.f.get("counterparty") or "Other")
    return f"vendor:{v.f.get('counterparty') or name}", name


def bank_item(ctx: Ctx, rid: str, v: Version) -> Item:
    amount = signed(v)
    who = v.f.get("counterparty_name") or ("Payroll" if str(v.f.get("records", "")).startswith("PR-") else "")
    title = f"{v.f.get('reference') or rid}" + (f" · {who}" if who else "")
    return Item(
        kb_id=sim_id(rid),
        dataset="bank",
        kind="BankEntry",
        title=title,
        date=str(v.f.get("booked_at") or ctx.local(v.ts).isoformat()),
        facts={
            "amount": money(amount),
            "direction": str(v.f.get("direction") or ""),
            "counterparty": str(who or ""),
            "reference": str(v.f.get("reference") or ""),
            "balance after": money(D(v.f.get("balance_after"))),
        },
        event=(v.kind, v.ts.isoformat()),
        causes=v.causes,
    )


def bank_window(ctx: Ctx, days: int) -> tuple[list[tuple[str, Version]], int]:
    """Bank entries booked in the trailing window (no opening balance) and the days of history it covers."""
    start = max(ctx.window_start(days), ctx.data.start)
    rows = []
    for rid, v in bank_known(ctx):
        if v.kind == "opening_balance":
            continue
        booked = parse_date(v.f.get("booked_at")) or ctx.local(v.ts)
        if start <= booked <= ctx.as_of:
            rows.append((rid, v))
    return rows, (ctx.as_of - start).days + 1


@dataclass
class Burn:
    monthly: Decimal
    credits: Decimal
    debits: Decimal
    covered: int
    rows: list[tuple[str, Version]]


def net_burn(ctx: Ctx, days: int) -> Burn | None:
    rows, covered = bank_window(ctx, days)
    if covered < int(ctx.opt("min_days", 28)):
        return None
    credits = sum((D(v.f["amount"]) for _, v in rows if v.f.get("direction") == "credit"), ZERO)
    debits = sum((D(v.f["amount"]) for _, v in rows if v.f.get("direction") != "credit"), ZERO)
    monthly = (debits - credits) * DAYS_PER_MONTH / Decimal(covered)
    return Burn(monthly=monthly, credits=credits, debits=debits, covered=covered, rows=rows)


def flow_breakdown(
    ctx: Ctx, rows: list[tuple[str, Version]], scale: Decimal = Decimal(1), sign: int = 1
) -> list[Breakdown]:
    sums: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for rid, v in rows:
        sums[bank_category(ctx, rid, v)] += signed(v) * sign
    return top_breakdown(((k, label, float(s * scale), None) for (k, label), s in sums.items()), n=12)


def _no_cash(ctx: Ctx) -> Computation:
    return Computation.unknown(f"No bank entry is known by {ctx.as_of}.", components=["sim_bank_entry"])


def cash(ctx: Ctx) -> Computation:
    last = latest_balance(ctx)
    if last is None:
        return _no_cash(ctx)
    rid, v = last
    value = D(v.f.get("balance_after"))
    days = ctx.window
    before_day = ctx.as_of - timedelta(days=days)
    prior = latest_balance(ctx, ctx.end_of(before_day))
    start_bal = D(prior[1].f.get("balance_after")) if prior else ZERO
    flows = [
        (r, x)
        for r, x in bank_known(ctx)
        if (prior is None or (x.ts, r) > (prior[1].ts, prior[0])) and (x.ts, r) <= (v.ts, rid)
    ]
    credits = [(r, x) for r, x in flows if x.f.get("direction") == "credit"]
    debits = [(r, x) for r, x in flows if x.f.get("direction") != "credit"]
    c_sum = sum((D(x.f["amount"]) for _, x in credits), ZERO)
    d_sum = sum((D(x.f["amount"]) for _, x in debits), ZERO)
    caveats = [f"Net flow in the last {days} days: {fmt_money(c_sum - d_sum)}."]
    if abs(start_bal + c_sum - d_sum - value) > Decimal("0.05"):
        caveats.append("The running balance of the bank entries does not add up; check the bank records.")
    items = [bank_item(ctx, r, x) for r, x in reversed(flows)]
    lim = ctx.records_max
    last_item = bank_item(ctx, rid, v)
    lineage = [
        aggregate(
            "Latest bank entry by the date",
            money(value),
            "usd",
            f"Balance after {rid} booked {v.f.get('booked_at')}: {fmt_money(value)}",
            [last_item],
            limit=lim,
        ),
        operation(
            f"Balance check over the last {days} days",
            money(start_bal + c_sum - d_sum),
            "usd",
            f"Cash {days} days earlier {fmt_money(start_bal)} + credits {fmt_money(c_sum)}"
            f" − debits {fmt_money(d_sum)}",
            f"{money(start_bal)} + {money(c_sum)} − {money(d_sum)}",
            [
                aggregate(
                    f"Cash on {before_day}",
                    money(start_bal),
                    "usd",
                    "Balance after the last entry known at the end of that day (0 before the history)",
                    [bank_item(ctx, *prior)] if prior else [],
                    limit=lim,
                ),
                aggregate(
                    f"Credits in {days} days",
                    money(c_sum),
                    "usd",
                    f"{len(credits)} entries",
                    [bank_item(ctx, *x) for x in credits],
                    limit=lim,
                ),
                aggregate(
                    f"Debits in {days} days",
                    money(d_sum),
                    "usd",
                    f"{len(debits)} entries",
                    [bank_item(ctx, *x) for x in debits],
                    limit=lim,
                ),
            ],
        ),
    ]
    return Computation(
        value=money(value),
        items=items,
        breakdown=flow_breakdown(ctx, flows),
        caveats=caveats,
        lineage=lineage,
        formula_text=f"balance_after({rid})",
        records_total=len(items),
    )


def _burn_nodes(ctx: Ctx, b: Burn, days: int) -> LineageNode:
    lim = ctx.records_max
    credits = [bank_item(ctx, r, v) for r, v in b.rows if v.f.get("direction") == "credit"]
    debits = [bank_item(ctx, r, v) for r, v in b.rows if v.f.get("direction") != "credit"]
    return operation(
        "Monthly net burn",
        money(b.monthly),
        "usd",
        f"(debits − credits) over {b.covered} days, per month of 30.44 days",
        f"({money(b.debits)} − {money(b.credits)}) × 30.4375 / {b.covered}",
        [
            aggregate(
                f"Debits in {b.covered} days",
                money(b.debits),
                "usd",
                f"{len(debits)} bank entries",
                debits,
                limit=lim,
            ),
            aggregate(
                f"Credits in {b.covered} days",
                money(b.credits),
                "usd",
                f"{len(credits)} bank entries",
                credits,
                limit=lim,
            ),
        ],
    )


def burn_display(monthly: Decimal) -> dict[str, Any] | None:
    """A negative burn is a net inflow: the brief shows it negated, as the net cash flow of a month."""
    if monthly >= 0:
        return None
    inflow = fmt_money(-monthly)
    return {
        "label": "Net cash flow per month",
        "negate": True,
        "note": f"The company earns cash: {inflow} a month more came in than went out."
        " A negative burn is shown as a positive net cash flow.",
        "phrase": f"net cash flow +{tile_value(float(-monthly), 'money')} a month",  # the number of the tile
    }


def runway_display(value: Decimal, cap: Decimal, growing: bool) -> dict[str, Any] | None:
    """The runway at its cap is a lower bound: «60+ mo» on the tile, «over 5 years» in the headline."""
    if value < cap:
        return None
    months = int(cap) if cap == cap.to_integral_value() else float(cap)
    span = f"{months // 12} years" if isinstance(months, int) and months % 12 == 0 else f"{months} months"
    why = "The company is profitable: cash grows" if growing else "Cash lasts longer than the cap"
    return {
        "value": f"{months}+ mo",
        "note": f"{why}, so the runway is capped at {months} months ({span}). The true runway is longer.",
        "phrase": f"runway over {span}",
    }


def burn(ctx: Ctx) -> Computation:
    days = ctx.window
    b = net_burn(ctx, days)
    if b is None:
        return Computation.unknown(
            f"Less than {ctx.opt('min_days', 28)} days of bank history before {ctx.as_of}.",
            components=["sim_bank_entry"],
        )
    scale = DAYS_PER_MONTH / Decimal(b.covered)
    items = sorted(
        (bank_item(ctx, r, v) for r, v in b.rows), key=lambda i: -abs(float(i.facts["amount"] or 0))
    )
    caveats = [
        f"Average month over {b.covered} days: debits {fmt_money(b.debits * scale)},"
        f" credits {fmt_money(b.credits * scale)}.",
    ]
    if b.monthly < 0:
        caveats.insert(0, "Negative burn: more cash came in than went out.")
    if b.covered < days:
        caveats.append(f"The history covers only {b.covered} of the {days} days of the window.")
    return Computation(
        value=money(b.monthly),
        items=items,
        breakdown=flow_breakdown(ctx, b.rows, scale, sign=-1),
        caveats=caveats,
        lineage=[_burn_nodes(ctx, b, days)],
        formula_text=f"(debits − credits) × 30.4375 / {b.covered}",
        records_total=len(items),
        display=burn_display(D(money(b.monthly))),
    )


def runway(ctx: Ctx) -> Computation:
    last = latest_balance(ctx)
    if last is None:
        return _no_cash(ctx)
    days = ctx.window
    b = net_burn(ctx, days)
    if b is None:
        return Computation.unknown(
            f"Less than {ctx.opt('min_days', 28)} days of bank history before {ctx.as_of}.",
            components=["sim_bank_entry"],
        )
    cap = Decimal(str(ctx.opt("cap_months", 60)))
    rid, v = last
    cash_now = D(v.f.get("balance_after"))
    if cash_now <= 0:
        value = ZERO
    else:
        value = min(cash_now / max(b.monthly, Decimal(1)), cap)
    caveats = [
        f"Cash {fmt_money(cash_now)}; monthly net burn {fmt_money(b.monthly)} over the last {b.covered} days."
    ]
    if b.monthly <= 0:
        caveats.insert(
            0,
            f"Cash grows (net inflow of {fmt_money(-b.monthly)} a month):"
            f" the runway is capped at {cap} months.",
        )
    items = [bank_item(ctx, rid, v)] + sorted(
        (bank_item(ctx, r, x) for r, x in b.rows if x.f.get("direction") != "credit"),
        key=lambda i: float(i.facts["amount"] or 0),
    )
    lineage = [
        operation(
            "Runway",
            rnd(value, 1),
            "months",
            f"min(cash / max(monthly net burn, 1), {cap}) months",
            f"min({money(cash_now)} / max({money(b.monthly)}, 1), {cap})",
            [
                aggregate(
                    "Cash",
                    money(cash_now),
                    "usd",
                    f"Balance after {rid}",
                    [bank_item(ctx, rid, v)],
                    limit=ctx.records_max,
                ),
                _burn_nodes(ctx, b, days),
            ],
        )
    ]
    return Computation(
        value=rnd(value, 1),
        items=items,
        breakdown=flow_breakdown(ctx, b.rows, DAYS_PER_MONTH / Decimal(b.covered), sign=-1),
        caveats=caveats,
        lineage=lineage,
        formula_text=f"min(cash / max(burn, 1), {cap})",
        records_total=len(items),
        display=runway_display(value, cap, growing=b.monthly <= 0),
    )


# ---------------------------------------------------------------------------------------------- monthly P&L
@dataclass
class Month:
    key: str
    complete: bool
    missing: list[str] = field(default_factory=list)
    revenue: Decimal = ZERO
    revenue_items: list[tuple[Item, Decimal]] = field(default_factory=list)
    revenue_clients: dict[str, Decimal] = field(default_factory=lambda: defaultdict(lambda: ZERO))
    payroll: Decimal = ZERO  # gross pay, commission included
    employer_tax: Decimal = ZERO
    payroll_lines: list[tuple[str, Decimal]] = field(default_factory=list)
    payroll_item: Item | None = None
    bills: list[tuple[Item, Decimal, str]] = field(default_factory=list)  # (record, amount, vendor id)
    cloud: Decimal = ZERO
    cloud_item: Item | None = None
    uncovered: list[str] = field(default_factory=list)  # clients in force without an invoice for the month

    @property
    def costs(self) -> Decimal:
        return self.payroll + self.employer_tax + sum((a for _, a, _ in self.bills), ZERO) + self.cloud

    def cost_items(self) -> list[Item]:
        out = [x for x in (self.payroll_item, self.cloud_item) if x is not None]
        out += [i for i, _, _ in self.bills]
        return sorted(out, key=lambda i: -float(i.facts.get("amount") or 0))


def _invoice_item(ctx: Ctx, rid: str, v: Version, recognised: Decimal | None = None) -> Item:
    f = v.f
    who = f.get("vendor_name") if f.get("direction") == "payable" else f.get("client_name")
    facts: dict = {
        "client" if f.get("direction") != "payable" else "vendor": str(who or ""),
        "amount": money(D(f.get("amount"))),
        "period": str(f.get("period") or ""),
        "status": str(f.get("status") or ""),
    }
    if recognised is not None:
        facts["recognised in month"] = money(recognised)
    return Item(
        kb_id=sim_id(rid),
        dataset="invoices",
        kind="Invoice",
        title=str(f.get("title") or rid),
        date=str(f.get("issued_at") or ""),
        facts=facts,
        event=(v.kind, v.ts.isoformat()),
        causes=v.causes,
    )


def month_book(ctx: Ctx, key: str) -> Month:
    """Revenue and costs of a calendar month from the records known at the cut."""
    cut = ctx.cut
    m = Month(key=key, complete=True)
    # payroll of the month
    for rid, v in ctx.data.table("payroll").known(cut):
        if v.f.get("period") == key:
            m.payroll = D(v.f.get("amount"))
            m.employer_tax = D(v.f.get("employer_tax"))
            m.payroll_lines = [
                (str(x.get("employee")), D(x.get("gross")) + D(x.get("commission")))
                for x in v.f.get("lines") or []
            ]
            m.payroll_item = Item(
                kb_id=sim_id(rid),
                dataset="payroll",
                kind="Payroll",
                title=str(v.f.get("title") or rid),
                date=str(v.f.get("paid_at") or ctx.local(v.ts).isoformat()),
                facts={
                    "amount": money(m.payroll + m.employer_tax),
                    "gross pay": money(m.payroll),
                    "employer tax": money(m.employer_tax),
                    "headcount": int(v.f.get("headcount") or 0),
                    "status": str(v.f.get("status") or ""),
                },
                event=(v.kind, v.ts.isoformat()),
                causes=v.causes,
            )
    if m.payroll_item is None:
        m.complete = False
        m.missing.append("payroll")
    # cloud bill of the month
    for rid, v in ctx.data.table("cloud_bill").known(cut):
        if v.f.get("period") == key:
            m.cloud = D(v.f.get("amount"))
            m.cloud_item = Item(
                kb_id=sim_id(rid),
                dataset="cloud_bills",
                kind="CloudBill",
                title=f"{v.f.get('vendor_name') or 'Cloud'} bill {key}",
                date=ctx.local(v.ts).isoformat(),
                facts={
                    "amount": money(m.cloud),
                    "vendor": str(v.f.get("vendor_name") or ""),
                    "GPU share": float(v.f.get("gpu_share") or 0),
                    "status": str(v.f.get("status") or ""),
                },
                event=(v.kind, v.ts.isoformat()),
                causes=v.causes,
            )
    if m.cloud_item is None:
        m.complete = False
        m.missing.append("cloud bill")
    # invoices: client revenue spread over the service months; vendor bills of the month
    near = {add_months(key, k) for k in (-2, -1, 1)}
    active_vendors: set[str] = set()
    billed: set[str] = set()
    for rid, v in ctx.data.table("invoice").known(cut):
        f = v.f
        months = period_months(str(f.get("period") or ""))
        if f.get("direction") == "payable":
            vendor = str(f.get("vendor") or "")
            if months and months[0] in near:
                active_vendors.add(vendor)
            if key in months:
                billed.add(vendor)
                amount = D(f.get("amount")) / len(months)
                m.bills.append((_invoice_item(ctx, rid, v), amount, vendor))
        elif key in months and f.get("direction") == "receivable":
            share = D(f.get("amount")) / len(months)
            m.revenue += share
            m.revenue_items.append((_invoice_item(ctx, rid, v, share), share))
            m.revenue_clients[str(f.get("client") or "")] += share
    lacking = sorted(active_vendors - billed)
    if lacking:
        m.complete = False
        m.missing += [f"bill of {ctx.data.name(x)}" for x in lacking]
    # clients in force in the month without an invoice that covers it (history starts after it was paid)
    mid = date.fromisoformat(f"{key}-15")
    for _, v in ctx.data.table("contract").known(cut):
        if v.f.get("contract_type") == "vendor" or not contract_active(v, mid):
            continue
        if str(v.f.get("client") or "") not in m.revenue_clients:
            m.uncovered.append(str(v.f.get("client") or ""))
    m.revenue_items.sort(key=lambda x: -x[1])
    return m


def reporting_month(ctx: Ctx, back: int = 3) -> Month | None:
    """The latest complete month before the as-of month (None: no complete month in ``back`` months)."""
    key = add_months(month_key(ctx.as_of), -1)
    for _ in range(back):
        if date.fromisoformat(f"{key}-01") < date(ctx.data.start.year, ctx.data.start.month, 1):
            return None
        m = month_book(ctx, key)
        if m.complete:
            return m
        key = add_months(key, -1)
    return None


def contract_active(v: Version, d: date) -> bool:
    f = v.f
    start = parse_date(f.get("start_date"))
    end = parse_date(f.get("end_date"))
    if start is None or start > d:
        return False
    if f.get("status") in ("terminated", "cancelled") and (end is None or end <= d):
        return False
    return end is None or d < end


def _no_month(ctx: Ctx) -> Computation:
    return Computation.unknown(
        f"No complete month before {ctx.as_of}: payroll, the cloud bill or a vendor bill is not known yet.",
        components=["sim_payroll", "sim_cloud_bill", "sim_invoice"],
    )


def revenue(ctx: Ctx) -> Computation:
    m = reporting_month(ctx)
    if m is None:
        return _no_month(ctx)
    items = [i for i, _ in m.revenue_items]
    caveats = [
        f"Reporting month {m.key}: the latest month with all cost records known;"
        " invoices spread over their service months."
    ]
    if m.uncovered:
        caveats.append(
            f"{len(m.uncovered)} clients in force have no invoice for {m.key} in the history"
            " (paid before it starts)."
        )
    return Computation(
        value=money(m.revenue),
        items=items,
        breakdown=top_breakdown(
            ((c, ctx.data.name(c), float(a), org_id(c)) for c, a in m.revenue_clients.items()),
            n=12,
            other="Other clients",
        ),
        sample=len(items),
        caveats=caveats,
        lineage=[
            aggregate(
                f"Client invoices for {m.key}",
                money(m.revenue),
                "usd",
                f"{len(items)} invoices; a quarterly invoice counts one third per month",
                items,
                limit=ctx.records_max,
                formula="sum(amount / months of the period)",
            )
        ],
        formula_text=f"sum(invoice.amount / period months) for {m.key}",
        records_total=len(items),
    )


def _cost_breakdown(ctx: Ctx, m: Month) -> list[Breakdown]:
    rows: dict[tuple[str, str, str | None], Decimal] = defaultdict(lambda: ZERO)
    rows[("payroll", "Payroll (gross)", None)] += m.payroll
    rows[("payroll_tax", "Employer payroll tax", None)] += m.employer_tax
    if m.cloud_item is not None:
        rows[("cloud", str(m.cloud_item.facts.get("vendor") or "Cloud"), None)] += m.cloud
    for _, amount, vendor in m.bills:
        rows[(f"vendor:{vendor}", ctx.data.name(vendor), org_id(vendor))] += amount
    return top_breakdown(((k, label, float(a), det) for (k, label, det), a in rows.items()), n=12)


def _cost_nodes(ctx: Ctx, m: Month) -> list:
    lim = ctx.records_max
    bills = [i for i, _, _ in m.bills]
    return [
        aggregate(
            "Payroll and employer tax",
            money(m.payroll + m.employer_tax),
            "usd",
            f"Payroll {m.key}",
            [m.payroll_item] if m.payroll_item else [],
            limit=lim,
        ),
        aggregate(
            "Vendor bills",
            money(sum((a for _, a, _ in m.bills), ZERO)),
            "usd",
            f"{len(bills)} bills for {m.key}",
            bills,
            limit=lim,
        ),
        aggregate(
            "Cloud bill",
            money(m.cloud),
            "usd",
            f"Cloud usage of {m.key}",
            [m.cloud_item] if m.cloud_item else [],
            limit=lim,
        ),
    ]


def costs(ctx: Ctx) -> Computation:
    m = reporting_month(ctx)
    if m is None:
        return _no_month(ctx)
    items = m.cost_items()
    return Computation(
        value=money(m.costs),
        items=items,
        breakdown=_cost_breakdown(ctx, m),
        sample=len(items),
        caveats=[
            f"Reporting month {m.key}: payroll, employer tax, vendor bills and the cloud bill of that month."
        ],
        lineage=[
            operation(
                f"Costs of {m.key}",
                money(m.costs),
                "usd",
                "Sum of the cost records of the month",
                "payroll + employer tax + bills + cloud",
                _cost_nodes(ctx, m),
            )
        ],
        formula_text="payroll + employer tax + vendor bills + cloud bill",
        records_total=len(items),
    )


def gross_margin(ctx: Ctx) -> Computation:
    m = reporting_month(ctx)
    if m is None:
        return _no_month(ctx)
    if m.revenue <= 0:
        return Computation.unknown(f"No client revenue is known for {m.key}.", components=["sim_invoice"])
    depts = set(ctx.opt("delivery_departments", ["Delivery", "Engineering", "Support"]) or [])
    people = ctx.data.people
    gross = sum((g for pid, g in m.payroll_lines if pid in people and people[pid].department in depts), ZERO)
    rate = (m.employer_tax / m.payroll) if m.payroll else ZERO
    delivery = gross * (1 + rate)
    cogs_vendors = set(ctx.opt("cogs_vendors", []) or [])
    contractors = set(ctx.opt("contractor_vendors", []) or [])
    vendor_cogs = sum((a for _, a, vid in m.bills if vid in cogs_vendors), ZERO)
    # subcontractors deliver client work: their bills are cost of revenue (company_model counts them too)
    contractor_bills = [(i, a) for i, a, vid in m.bills if vid in contractors and vid not in cogs_vendors]
    contractor = sum((a for _, a in contractor_bills), ZERO)
    profit = m.revenue - m.cloud - delivery - vendor_cogs - contractor
    value = profit / m.revenue * 100
    caveats = [
        f"Reporting month {m.key}: revenue {fmt_money(m.revenue)}, cloud {fmt_money(m.cloud)},"
        f" delivery payroll {fmt_money(delivery)}, subcontractors {fmt_money(contractor)}."
    ]
    if contractor:
        without = float((profit + contractor) / m.revenue * 100)
        caveats.append(f"Without subcontractors the margin would be {without:.1f}%.")
    items = (
        [x for x in (m.cloud_item, m.payroll_item) if x is not None]
        + [i for i, _ in contractor_bills]
        + [i for i, _ in m.revenue_items]
    )
    lim = ctx.records_max
    pay_item = m.payroll_item
    if pay_item is not None:
        pay_item = Item(
            **{**pay_item.__dict__, "facts": {**pay_item.facts, "delivery payroll": money(delivery)}}
        )
    lineage = [
        operation(
            "Gross margin",
            rnd(value, 2),
            "percent",
            "(revenue − cloud − delivery payroll − subcontractors) / revenue × 100",
            f"({money(m.revenue)} − {money(m.cloud)} − {money(delivery)} − {money(contractor)}"
            + (f" − {money(vendor_cogs)}" if vendor_cogs else "")
            + f") / {money(m.revenue)} × 100",
            [
                aggregate(
                    "Revenue",
                    money(m.revenue),
                    "usd",
                    f"Client invoices for {m.key}",
                    [i for i, _ in m.revenue_items],
                    limit=lim,
                ),
                aggregate(
                    "Cloud (infrastructure)",
                    money(m.cloud),
                    "usd",
                    "The cloud bill of the month",
                    [m.cloud_item] if m.cloud_item else [],
                    limit=lim,
                ),
                aggregate(
                    "Delivery payroll",
                    money(delivery),
                    "usd",
                    f"Gross pay of {', '.join(sorted(depts))} × (1 + employer tax rate {float(rate):.3f})",
                    [pay_item] if pay_item else [],
                    limit=lim,
                ),
                aggregate(
                    "Subcontractors",
                    money(contractor),
                    "usd",
                    f"Bills of the delivery subcontractors ({', '.join(sorted(contractors)) or 'none set'})",
                    [i for i, _ in contractor_bills],
                    limit=lim,
                ),
            ]
            + (
                [
                    aggregate(
                        "Vendor cost of revenue",
                        money(vendor_cogs),
                        "usd",
                        "Bills of the vendors set in cogs_vendors",
                        [i for i, _, vid in m.bills if vid in cogs_vendors],
                        limit=lim,
                    )
                ]
                if cogs_vendors
                else []
            ),
        )
    ]
    shares = [
        ("cloud", "Cloud (infrastructure)", float(m.cloud / m.revenue * 100)),
        ("delivery", "Delivery payroll", float(delivery / m.revenue * 100)),
        ("subcontractors", "Subcontractors", float(contractor / m.revenue * 100)),
    ]
    if vendor_cogs:
        shares.append(("vendors", "Vendor cost of revenue", float(vendor_cogs / m.revenue * 100)))
    shares.append(("margin", "Gross margin", float(value)))
    return Computation(
        value=rnd(value, 2),
        items=items,
        breakdown=[Breakdown(k, label, round(v, 2)) for k, label, v in shares],
        caveats=caveats,
        lineage=lineage,
        formula_text="(revenue − cloud − delivery payroll − subcontractors) / revenue × 100",
        records_total=len(items),
    )


def dso(ctx: Ctx) -> Computation:
    days = ctx.window
    if ctx.window_start(days) < ctx.data.start:
        return Computation.unknown(
            f"The {days}-day billing window starts before the history ({ctx.data.start}):"
            " invoices paid before it are not known.",
            components=["sim_invoice"],
        )
    open_rows, billed = [], ZERO
    billed_rows = []
    for rid, v in ctx.data.table("invoice").known(ctx.cut):
        f = v.f
        if f.get("direction") != "receivable":
            continue
        issued = parse_date(f.get("issued_at"))
        if issued is None or issued > ctx.as_of:
            continue
        if f.get("status") != "paid":
            open_rows.append((rid, v))
        if ctx.in_window(issued, days):
            billed += D(f.get("amount"))
            billed_rows.append((rid, v))
    if billed <= 0:
        return Computation.unknown(
            f"No client invoice issued in the last {days} days.", components=["sim_invoice"]
        )
    ar = sum((D(v.f.get("amount")) for _, v in open_rows), ZERO)
    value = ar / (billed / Decimal(days))
    items = []
    for rid, v in open_rows:
        it = _invoice_item(ctx, rid, v)
        due = parse_date(v.f.get("due_date"))
        it.facts["due"] = str(v.f.get("due_date") or "")
        it.facts["days past due"] = max((ctx.as_of - due).days, 0) if due else None
        items.append(it)
    items.sort(key=lambda i: (-(i.facts.get("days past due") or 0), -float(i.facts.get("amount") or 0)))
    by_client: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for _, v in open_rows:
        by_client[(str(v.f.get("client")), str(v.f.get("client_name")))] += D(v.f.get("amount"))
    overdue = sum(1 for i in items if (i.facts.get("days past due") or 0) > 0)
    lim = ctx.records_max
    return Computation(
        value=rnd(value, 1),
        items=items,
        breakdown=top_breakdown(
            ((c, n, float(a), org_id(c)) for (c, n), a in by_client.items()), n=12, other="Other clients"
        ),
        sample=len(items),
        caveats=[
            f"Open receivables {fmt_money(ar)} in {len(items)} invoices, {overdue} past due;"
            f" billed in {days} days {fmt_money(billed)}."
        ],
        lineage=[
            operation(
                "Days sales outstanding",
                rnd(value, 1),
                "days",
                f"open receivables / (invoiced in {days} days / {days})",
                f"{money(ar)} / ({money(billed)} / {days})",
                [
                    aggregate(
                        "Open receivables",
                        money(ar),
                        "usd",
                        "Client invoices not paid at the date",
                        items,
                        limit=lim,
                    ),
                    aggregate(
                        f"Invoiced in {days} days",
                        money(billed),
                        "usd",
                        f"Client invoices issued from {ctx.window_start(days)}",
                        [_invoice_item(ctx, r, v) for r, v in billed_rows],
                        limit=lim,
                    ),
                ],
            )
        ],
        formula_text=f"AR / (invoiced_{days}d / {days})",
        records_total=len(items),
    )


def taxes(ctx: Ctx) -> Computation:
    days = ctx.window
    paid, scheduled = [], []
    ahead = int(ctx.opt("due_ahead_days", 30))
    for rid, v in ctx.data.table("tax_payment").known(ctx.cut):
        f = v.f
        item = Item(
            kb_id=sim_id(rid),
            dataset="tax_payments",
            kind="TaxPayment",
            title=str(f.get("title") or rid),
            date=str(f.get("paid_at") or f.get("due_date") or ""),
            facts={
                "tax": str(f.get("tax") or ""),
                "amount": money(D(f.get("amount"))),
                "period": str(f.get("period") or ""),
                "due": str(f.get("due_date") or ""),
                "status": str(f.get("status") or ""),
            },
            event=(v.kind, v.ts.isoformat()),
            causes=v.causes,
        )
        if f.get("status") == "paid" and ctx.in_window(parse_date(f.get("paid_at")), days):
            paid.append((item, D(f.get("amount")), str(f.get("tax") or "")))
        due = parse_date(f.get("due_date"))
        if (
            f.get("status") != "paid"
            and due is not None
            and ctx.as_of < due <= ctx.as_of + timedelta(days=ahead)
        ):
            scheduled.append((item, D(f.get("amount"))))
    total = sum((a for _, a, _ in paid), ZERO)
    by_tax: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for _, a, t in paid:
        by_tax[t] += a
    due_sum = sum((a for _, a in scheduled), ZERO)
    items = sorted((i for i, _, _ in paid), key=lambda i: str(i.date), reverse=True)
    lim = ctx.records_max
    caveats = [f"Due in the next {ahead} days: {fmt_money(due_sum)} in {len(scheduled)} scheduled payments."]
    if ctx.window_start(days) < ctx.data.start:
        caveats.append(
            f"The window starts before the history ({ctx.data.start}); earlier payments are not known."
        )
    return Computation(
        value=money(total),
        items=items,
        breakdown=top_breakdown(
            ((t.lower().replace(" ", "_"), t, float(a), None) for t, a in by_tax.items()), n=6
        ),
        sample=len(items),
        caveats=caveats,
        lineage=[
            aggregate(
                f"Taxes paid in {days} days",
                money(total),
                "usd",
                f"{len(items)} tax payments",
                items,
                limit=lim,
            ),
            aggregate(
                f"Scheduled, due in {ahead} days",
                money(due_sum),
                "usd",
                "Context: not in the value",
                [i for i, _ in scheduled],
                limit=lim,
            ),
        ],
        formula_text=f"sum(tax.amount where paid in {days} days)",
        records_total=len(items),
    )


def budget_variance(ctx: Ctx) -> Computation:
    n = int(ctx.opt("months", 3))
    annual = D(ctx.opt("annual_budget", 11_200_000))
    last = reporting_month(ctx)
    if last is None:
        return _no_month(ctx)
    months = [last]
    key = last.key
    for _ in range(n - 1):
        key = add_months(key, -1)
        m = month_book(ctx, key)
        if not m.complete:
            break
        months.append(m)
    months.reverse()
    budget_m = annual / 12
    actual = sum((m.costs for m in months), ZERO)
    budget = budget_m * len(months)
    value = (actual - budget) / budget * 100
    caveats = [
        f"Budget: {fmt_money(annual)} a year (parameter annual_budget; not a record of the history).",
        f"Costs {fmt_money(actual)} against {fmt_money(budget)} for {months[0].key}…{months[-1].key}.",
    ]
    if len(months) < n:
        caveats.append(f"Only {len(months)} complete months are known; the window asks for {n}.")
    items = sorted(
        (i for m in months for i in m.cost_items()), key=lambda i: -float(i.facts.get("amount") or 0)
    )
    return Computation(
        value=rnd(value, 2),
        items=items,
        breakdown=[
            Breakdown(m.key, m.key, round(float((m.costs - budget_m) / budget_m * 100), 2)) for m in months
        ],
        sample=len(months),
        caveats=caveats,
        lineage=[
            operation(
                "Budget variance",
                rnd(value, 2),
                "percent",
                "(actual costs − budget) / budget × 100",
                f"({money(actual)} − {money(budget)}) / {money(budget)} × 100",
                [
                    operation(
                        f"Costs of {m.key}",
                        money(m.costs),
                        "usd",
                        f"Budget for the month {fmt_money(budget_m)}",
                        None,
                        _cost_nodes(ctx, m),
                    )
                    for m in months
                ],
            )
        ],
        formula_text="(actual − annual_budget / 12 × months) / budget × 100",
        records_total=len(items),
    )


__all__ = [
    "budget_variance",
    "burn",
    "cash",
    "contract_active",
    "costs",
    "dso",
    "gross_margin",
    "month_book",
    "reporting_month",
    "revenue",
    "runway",
    "taxes",
]
