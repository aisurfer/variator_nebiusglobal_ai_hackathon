"""Clients and people metrics: ARR, churn, new ARR, NPS, headcount.

ARR comes from the client contracts (KB ``sim`` Contract records): the ACV in force on the date from the
``acv_schedule`` of the latest known version. NPS reads the v2 dataset ``nps_responses.csv``; without it the
metric is unknown. Headcount reads the people table and the employee events of the KB.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from variator.metrics.catalog.asof import parse_date
from variator.metrics.catalog.core import (
    Breakdown,
    Computation,
    Ctx,
    D,
    Item,
    aggregate,
    chains,
    fmt_money,
    money,
    operation,
    rnd,
    tile_value,
    top_breakdown,
)
from variator.metrics.catalog.datasets import Version
from variator.metrics.catalog.finance import contract_active
from variator.metrics.catalog.ids import cause_ref, org_id, person_id, sim_id

ZERO = Decimal(0)


# ---------------------------------------------------------------------------------------------- contracts
def acv_at(v: Version, d: date) -> Decimal:
    acv = ZERO
    for step in v.f.get("acv_schedule") or []:
        since = parse_date(step.get("from"))
        if since is not None and since <= d:
            acv = D(step.get("acv"))
    return acv


def client_contracts(ctx: Ctx, cut=None) -> list[tuple[str, Version]]:
    return [
        (r, v)
        for r, v in ctx.data.table("contract").known(cut or ctx.cut)
        if v.f.get("contract_type") != "vendor"
    ]


def contract_item(ctx: Ctx, rid: str, v: Version, acv: Decimal, **facts) -> Item:
    f = v.f
    return Item(
        kb_id=sim_id(rid),
        dataset="contracts",
        kind="Contract",
        title=str(f.get("title") or f"{f.get('client_name')}: contract {rid}"),
        date=str(f.get("start_date") or ""),
        facts={
            "client": str(f.get("client_name") or f.get("client") or ""),
            "ACV": money(acv),
            "status": str(f.get("status") or ""),
            "term end": str(f.get("term_end") or ""),
            "billing": str(f.get("billing_frequency") or ""),
            **facts,
        },
        event=(v.kind, v.ts.isoformat()),
        causes=v.causes,
    )


def arr_in_force(ctx: Ctx, d: date, cut=None) -> list[tuple[str, Version, Decimal]]:
    out = []
    for rid, v in client_contracts(ctx, cut):
        if contract_active(v, d):
            acv = acv_at(v, d)
            if acv > 0:
                out.append((rid, v, acv))
    return out


def arr(ctx: Ctx) -> Computation:
    rows = arr_in_force(ctx, ctx.as_of)
    if not rows:
        return Computation.unknown(
            f"No client contract in force is known on {ctx.as_of}.", components=["sim_contract"]
        )
    total = sum((a for _, _, a in rows), ZERO)
    items = sorted(
        (contract_item(ctx, r, v, a) for r, v, a in rows), key=lambda i: -float(i.facts["ACV"] or 0)
    )
    notice = [(r, v, a) for r, v, a in rows if v.f.get("status") == "notice"]
    risk = sum((a for _, _, a in notice), ZERO)
    key_arr = sum((a for _, v, a in rows if v.f.get("key_account")), ZERO)
    by_client: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for _, v, a in rows:
        by_client[str(v.f.get("client"))] += a
    caveats = [
        f"{len(notice)} contracts on notice hold {fmt_money(risk)} of this ARR.",
        f"Key accounts: {fmt_money(key_arr)} ({float(key_arr / total * 100):.0f}%).",
    ]
    return Computation(
        value=money(total),
        items=items,
        breakdown=top_breakdown(
            ((c, ctx.data.name(c), float(a), org_id(c)) for c, a in by_client.items()),
            n=12,
            other="Other clients",
        ),
        sample=len(items),
        caveats=caveats,
        lineage=[
            aggregate(
                "Client contracts in force",
                money(total),
                "usd",
                f"{len(items)} contracts started by {ctx.as_of} and not ended;"
                " ACV of the schedule step in force",
                items,
                limit=ctx.records_max,
                formula="sum(acv_schedule[from <= date].acv)",
            )
        ],
        formula_text="sum(ACV in force) over client contracts",
        records_total=len(items),
    )


def _reason(ctx: Ctx, v: Version) -> tuple[str, str | None, str | None]:
    """The churn reason from the declared life-event causes: (reason, life event KB id, its statement).

    A contract keeps every cause that still holds (an old upsell request too): the churn or downgrade event
    wins; else the latest life event.
    """
    found = []
    for raw in v.causes:
        ctype, kb = cause_ref(raw)
        if ctype != "life_event":
            continue
        le = ctx.data.table("life_event").at(kb[4:], ctx.cut)
        if le is not None:
            found.append((le.f.get("event_type") in ("churn", "downgrade"), le.ts, kb, le))
    if not found:
        return "no reason recorded", None, None
    _, _, kb, le = max(found, key=lambda x: (x[0], x[1]))
    text = str(le.f.get("statement") or le.f.get("title") or "")
    reason = (
        text.split(":", 1)[1].strip().rstrip(".") if ":" in text else str(le.f.get("event_type") or "other")
    )
    return reason or "other", kb, text


def churn(ctx: Ctx) -> Computation:
    days = ctx.window
    start = max(ctx.window_start(days), ctx.data.start)
    covered = (ctx.as_of - start).days + 1
    if covered < int(ctx.opt("min_days", 90)):
        return Computation.unknown(
            f"Only {covered} days of history before {ctx.as_of}; churn needs {ctx.opt('min_days', 90)}.",
            components=["sim_contract"],
        )
    base_rows = arr_in_force(ctx, start, ctx.end_of(start))
    base = sum((a for _, _, a in base_rows), ZERO)
    if base <= 0:
        return Computation.unknown(
            f"No ARR is known at the window start {start}.", components=["sim_contract"]
        )
    lost_items: list[Item] = []
    lost = ZERO
    by_reason: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for rid, v in client_contracts(ctx):
        # terminations effective in the window
        end = parse_date(v.f.get("end_date"))
        if v.f.get("status") == "terminated" and end is not None and start <= end <= ctx.as_of:
            a = acv_at(v, end - timedelta(days=1))
            reason, le_id, statement = _reason(ctx, v)
            lost += a
            by_reason[reason] += a
            it = contract_item(ctx, rid, v, a, **{"lost ARR": money(a), "ended": str(end), "reason": reason})
            it.date = str(end)
            it.facts["notice given"] = str(v.f.get("notice_at") or "")
            if le_id:
                it.related_id, it.related_title, it.related_label = le_id, statement, "reason"
            lost_items.append(it)
        # downgrades effective in the window
        prev = None
        for x in ctx.data.table("contract").history(rid, ctx.cut):
            if x.kind == "contract_amended_downgrade" and prev is not None:
                steps = x.f.get("acv_schedule") or []
                eff = parse_date(steps[-1].get("from")) if steps else None
                if eff is not None and start <= eff <= ctx.as_of:
                    a = max(acv_at(prev, eff) - acv_at(x, eff), ZERO)
                    if a > 0:
                        lost += a
                        by_reason["downgrade"] += a
                        it = contract_item(
                            ctx, rid, x, acv_at(x, eff), **{"lost ARR": money(a), "downgrade from": str(eff)}
                        )
                        it.date = str(eff)
                        lost_items.append(it)
            prev = x
    scale = Decimal(365) / Decimal(covered) if covered < 365 and ctx.opt("annualise", True) else Decimal(1)
    value = lost / base * 100 * scale
    lost_items.sort(key=lambda i: str(i.date), reverse=True)
    notice = [(r, v, a) for r, v, a in arr_in_force(ctx, ctx.as_of) if v.f.get("status") == "notice"]
    # notices given in the same window (the cases count those): their contracts end later, so this churn,
    # which counts contracts that ended, is lower than the share of ARR that gave notice
    given, given_n = ZERO, 0
    for _rid, v in client_contracts(ctx):
        at = parse_date(v.f.get("notice_at"))
        if at is not None and start <= at <= ctx.as_of:
            given += acv_at(v, at)
            given_n += 1
    given_pct = given / base * 100 * scale
    note = (
        f"Counts contracts that ended in the last {'12 months' if days == 365 else f'{days} days'}. "
        "Notices given in the same period: "
        f"{tile_value(float(given_pct), 'percent')} of ARR ({given_n} notice{'' if given_n == 1 else 's'}, "
        f"{tile_value(float(given), 'money')}); those contracts end later."
    )
    caveats = [
        f"Lost {fmt_money(lost)} of {fmt_money(base)} ARR at {start}; "
        f"{sum(1 for i in lost_items if 'ended' in i.facts)} contracts ended.",
        f"On notice now: {len(notice)} contracts, {fmt_money(sum((a for _, _, a in notice), ZERO))} of ARR.",
        note,
    ]
    if scale != 1:
        caveats.append(f"Annualised from {covered} days (× {float(scale):.2f}).")
    lim = ctx.records_max
    base_items = [contract_item(ctx, r, v, a) for r, v, a in base_rows]
    lineage = [
        operation(
            "Gross ARR churn",
            rnd(value, 2),
            "percent",
            "ARR lost to terminations and downgrades / ARR at the window start × 100",
            f"{money(lost)} / {money(base)} × 100" + (f" × {float(scale):.3f}" if scale != 1 else ""),
            [
                aggregate(
                    "ARR lost",
                    money(lost),
                    "usd",
                    f"Effective from {start} to {ctx.as_of}",
                    lost_items,
                    limit=lim,
                    children=chains(lost_items, ctx.data),
                ),
                aggregate(
                    f"ARR at {start}",
                    money(base),
                    "usd",
                    "Client contracts in force at the window start",
                    base_items,
                    limit=lim,
                ),
            ],
        )
    ]
    return Computation(
        value=rnd(value, 2),
        items=lost_items,
        breakdown=top_breakdown(
            ((k.replace(" ", "_")[:40], k, float(a), None) for k, a in by_reason.items()), n=10
        ),
        sample=len(base_items),
        caveats=caveats,
        lineage=lineage,
        formula_text="lost ARR / ARR at window start × 100",
        records_total=len(lost_items),
        display={"note": note},
    )


def new_arr(ctx: Ctx) -> Computation:
    days = ctx.window
    items: list[Item] = []
    total = ZERO
    by_type: dict[str, Decimal] = defaultdict(lambda: ZERO)
    deals = ctx.data.table("deal")
    for rid in ctx.data.table("contract").ids():
        prev = None
        for v in ctx.data.table("contract").history(rid, ctx.cut):
            if v.f.get("contract_type") == "vendor":
                break
            known = ctx.local(v.ts)
            gain, kind = ZERO, None
            if v.kind == "contract_signed" and ctx.in_window(known, days):
                steps = v.f.get("acv_schedule") or []
                gain, kind = (D(steps[0].get("acv")) if steps else D(v.f.get("amount"))), "new client"
            elif v.kind == "contract_amended_upsell" and prev is not None and ctx.in_window(known, days):
                new_steps, old_steps = v.f.get("acv_schedule") or [], prev.f.get("acv_schedule") or []
                if new_steps and old_steps:
                    gain, kind = D(new_steps[-1].get("acv")) - D(old_steps[-1].get("acv")), "upsell"
            if kind and gain > 0:
                total += gain
                by_type[kind] += gain
                it = contract_item(
                    ctx, rid, v, acv_at(v, ctx.as_of) or gain, **{"new ARR": money(gain), "type": kind}
                )
                it.date = known.isoformat()
                it.title = f"{v.f.get('client_name')}: {kind}" + (
                    f" ({v.f.get('last_amendment')})"
                    if kind == "upsell" and v.f.get("last_amendment")
                    else ""
                )
                deal = v.f.get("deal")
                if deal:
                    d = deals.at(str(deal), ctx.cut)
                    it.related_id = sim_id(deal)
                    it.related_title = str(d.f.get("title")) if d else str(deal)
                    it.related_label = "won deal"
                items.append(it)
            prev = v
    items.sort(key=lambda i: str(i.date), reverse=True)
    won = [
        (r, v)
        for r, v in deals.known(ctx.cut)
        if v.f.get("status") == "won" and ctx.in_window(parse_date(v.f.get("closed_at")), days)
    ]
    caveats = [
        f"{len(won)} deals won in the window;"
        f" CRM amounts {fmt_money(sum((D(v.f.get('amount')) for _, v in won), ZERO))}.",
        "Renewal price uplifts are not new ARR.",
    ]
    if ctx.window_start(days) < ctx.data.start:
        caveats.append(f"The window starts before the history ({ctx.data.start}).")
    return Computation(
        value=money(total),
        items=items,
        breakdown=[Breakdown(k.replace(" ", "_"), k, money(a)) for k, a in sorted(by_type.items())],
        sample=len(items),
        caveats=caveats,
        lineage=[
            aggregate(
                f"Contracts signed and upsells in {days} days",
                money(total),
                "usd",
                "ACV of new contracts + ACV added by upsell amendments,"
                " by the date the contract change was recorded",
                items,
                limit=ctx.records_max,
                children=chains(items, ctx.data),
            )
        ],
        formula_text=f"sum(new contract ACV + upsell ACV delta) over {days} days",
        records_total=len(items),
    )


def nps(ctx: Ctx) -> Computation:
    if not ctx.data.has("nps_responses"):
        return Computation.unknown(
            "Survey answers (datasets/nps_responses.csv) are not in this history; NPS is unknown, not zero.",
            components=["nps_responses"],
        )
    days = ctx.window
    hi, lo = int(ctx.opt("promoter_min", 9)), int(ctx.opt("detractor_max", 6))
    known = [r for r in ctx.data.nps if r.date <= ctx.as_of]
    first: dict[str, date] = {}
    for r in known:
        first[r.survey] = min(first.get(r.survey, r.date), r.date)
    surveys = sorted(first, key=lambda s: (first[s], s))
    if str(ctx.opt("mode", "survey")) == "window":
        rows = [r for r in known if ctx.in_window(r.date, days)]
        scope = f"the last {days} days"
    else:
        rows = [r for r in known if surveys and r.survey == surveys[-1]]
        scope = f"survey {surveys[-1]}" if surveys else "the latest survey"
    if not rows:
        return Computation.unknown(
            f"No survey answer in {scope} by {ctx.as_of}.", components=["nps_responses"]
        )

    def score(xs: list[int]) -> float:
        return (sum(s >= hi for s in xs) - sum(s <= lo for s in xs)) / len(xs) * 100

    def group(score: int) -> str:
        return "promoter" if score >= hi else "detractor" if score <= lo else "passive"

    items = [
        Item(
            kb_id=sim_id(r.response_id),
            dataset="nps",
            kind="NpsResponse",
            title=f"{ctx.data.name(r.client)}: score {r.score}",
            date=r.date.isoformat(),
            facts={
                "client": ctx.data.name(r.client),
                "contact": ctx.data.name(r.contact),
                "survey": r.survey,
                "score": r.score,
                "group": group(r.score),
            },
        )
        for r in rows
    ]
    items.sort(key=lambda i: (int(i.facts["score"] or 0), str(i.date)))
    n = len(rows)
    p = sum(1 for r in rows if r.score >= hi)
    d = sum(1 for r in rows if r.score <= lo)
    value = (p - d) / n * 100
    per: dict[str, list[int]] = defaultdict(list)
    for r in known:
        per[r.survey].append(r.score)
    bd = [Breakdown(s, f"Survey {s} ({len(per[s])} answers)", round(score(per[s]), 1)) for s in surveys][-12:]
    caveats = [
        f"Answers of {scope}: {n}; {p} promoters (≥ {hi}), {n - p - d} passives, {d} detractors (≤ {lo}).",
        f"All answers so far ({len(known)}): NPS {score([r.score for r in known]):.1f}.",
    ]
    if n < int(ctx.opt("min_responses", 10)):
        caveats.append(f"Small sample: {n} answers.")
    lim = ctx.records_max
    by = {"promoter": [], "passive": [], "detractor": []}
    for it in items:
        by[str(it.facts["group"])].append(it)
    return Computation(
        value=rnd(value, 1),
        items=items,
        breakdown=bd,
        sample=n,
        caveats=caveats,
        lineage=[
            operation(
                "Net promoter score",
                rnd(value, 1),
                "index",
                "(promoters − detractors) / answers × 100",
                f"({p} − {d}) / {n} × 100",
                [
                    aggregate("Promoters", p, "count", f"Score {hi}–10", by["promoter"], limit=lim),
                    aggregate(
                        "Passives", n - p - d, "count", f"Score {lo + 1}–{hi - 1}", by["passive"], limit=lim
                    ),
                    aggregate("Detractors", d, "count", f"Score 0–{lo}", by["detractor"], limit=lim),
                ],
            )
        ],
        formula_text="(promoters − detractors) / answers × 100",
        records_total=n,
        display={"note": nps_range_note(hi, lo)},
    )


def nps_range_note(promoter_min: int, detractor_max: int) -> str:
    """NPS is not a 0–100 score: the brief shows the plain number and explains the range in a tooltip."""
    return (
        f"NPS runs from −100 to 100: the share of promoters (score {promoter_min}–10)"
        f" minus the share of detractors (0–{detractor_max})."
    )


def headcount(ctx: Ctx) -> Computation:
    if not ctx.data.has("people"):
        return Computation.unknown(
            "The people table (datasets/people.csv) is not in this history.", components=["people"]
        )
    d = ctx.as_of
    staff = [p for p in ctx.data.people.values() if p.start <= d and (p.last_day is None or d <= p.last_day)]
    notices = {}
    for _, v in ctx.data.table("employee_event").known(ctx.cut):
        if v.kind in ("resignation_received", "employee_left") or v.f.get("type") == "resignation":
            notices[str(v.f.get("employee"))] = str(v.f.get("end_date") or v.f.get("effective_date") or "")
    items = [
        Item(
            kb_id=person_id(p.id),
            dataset="people",
            kind="Person",
            title=p.name,
            date=p.start.isoformat(),
            facts={
                "title": p.title,
                "department": p.department,
                "start": p.start.isoformat(),
                **({"leaving": notices[p.id]} if p.id in notices else {}),
            },
        )
        for p in sorted(staff, key=lambda p: (p.department, p.name))
    ]
    by_dept: dict[str, int] = defaultdict(int)
    for p in staff:
        by_dept[p.department] += 1
    recent = ctx.as_of - timedelta(days=int(ctx.opt("recent_days", 90)))
    joined = [p for p in ctx.data.people.values() if recent < p.start <= d]
    left = [p for p in ctx.data.people.values() if p.last_day is not None and recent < p.last_day < d]
    caveats = [f"Last {ctx.opt('recent_days', 90)} days: {len(joined)} joined, {len(left)} left."]
    if any(p.id in notices for p in staff):
        caveats.append(f"{sum(1 for p in staff if p.id in notices)} people in the count gave notice.")
    lim = ctx.records_max
    return Computation(
        value=float(len(staff)),
        items=items,
        breakdown=[
            Breakdown(k, k, float(v)) for k, v in sorted(by_dept.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        sample=len(staff),
        caveats=caveats,
        lineage=[
            aggregate(
                "Employees on the date",
                len(staff),
                "count",
                "Started on or before the date and not past their last day",
                items,
                limit=lim,
            ),
        ],
        formula_text="count(people where start <= date <= last_day)",
        records_total=len(items),
    )
