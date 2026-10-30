"""Delivery and operations metrics: SLA, latency, incidents, MTTR, overdue implementations, team load,
throughput, open requisitions, deploy frequency.

Incidents, deploys and requisitions are KB ``sim`` records; implementations and throughput read Jira issues
(``issues.csv`` + ``transitions.csv``, status as of the date from the transitions); team load reads the sprint
load of the planning record with the sprint calendar of the Jira sprints. SLA and latency read the v2 dataset
``monitoring_daily.csv``; without it they are unknown (incident downtime alone is not the SLA).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, median

from variator.metrics.catalog.asof import bdays, parse_date, parse_ts
from variator.metrics.catalog.core import (
    Breakdown,
    Computation,
    Ctx,
    Item,
    aggregate,
    chains,
    operation,
    rnd,
    top_breakdown,
)
from variator.metrics.catalog.datasets import DONE, Version
from variator.metrics.catalog.ids import jira_id, person_id, sim_id

KIND_BY_TYPE = {"Epic": "Epic", "Story": "Story", "Task": "Task", "Bug": "Bug"}


# ---------------------------------------------------------------------------------------------- incidents
def _ts(v: Version, key: str) -> datetime | None:
    s = v.f.get(key)
    return parse_ts(str(s)) if s else None


def incident_item(ctx: Ctx, rid: str, v: Version) -> Item:
    f = v.f
    started = _ts(v, "started_at")
    facts = {
        "severity": str(f.get("severity") or ""),
        "component": str(f.get("component") or ""),
        "started": started.isoformat() if started else None,
        "status": str(f.get("status") or ""),
        "cause kind": str(f.get("cause_kind") or ""),
        "affected clients": int(f.get("affected_count") or 0),
    }
    if f.get("restore_minutes") is not None:
        facts["restore, h"] = round(float(f["restore_minutes"]) / 60, 2)
    if f.get("downtime_minutes") is not None:
        facts["downtime, min"] = float(f["downtime_minutes"])
    if f.get("services"):
        facts["services"] = ", ".join(str(s) for s in f["services"])
    return Item(
        kb_id=sim_id(rid),
        dataset="incidents",
        kind="Incident",
        title=str(f.get("title") or rid),
        date=ctx.local(started).isoformat() if started else None,
        facts=facts,
        event=(v.kind, v.ts.isoformat()),
        causes=v.causes,
    )


def incidents_started(ctx: Ctx, days: int) -> list[tuple[str, Version]]:
    out = []
    for rid, v in ctx.data.table("incident").known(ctx.cut):
        started = _ts(v, "started_at")
        if started is not None and ctx.in_window(ctx.local(started), days):
            out.append((rid, v))
    return sorted(out, key=lambda r: str(r[1].f.get("started_at")), reverse=True)


def incidents(ctx: Ctx) -> Computation:
    days = ctx.window
    rows = incidents_started(ctx, days)
    items = [incident_item(ctx, r, v) for r, v in rows]
    value = len(rows) * 30 / days
    sev: dict[str, int] = defaultdict(int)
    for _, v in rows:
        sev[str(v.f.get("severity") or "unknown")] += 1
    allrows = ctx.data.table("incident").known(ctx.cut)
    months = max(((ctx.as_of - ctx.data.start).days + 1) / 30.4375, 1)
    caveats = [f"Since the history start: {len(allrows)} incidents, {len(allrows) / months:.1f} a month."]
    if ctx.window_start(days) < ctx.data.start:
        caveats.append(f"The window starts before the history ({ctx.data.start}).")
    return Computation(
        value=rnd(value, 2),
        items=items,
        breakdown=[Breakdown(k, k, float(n)) for k, n in sorted(sev.items())],
        sample=len(rows),
        caveats=caveats,
        lineage=[
            aggregate(
                f"Incidents started in {days} days",
                len(rows),
                "count",
                f"Incidents with a start from {ctx.window_start(days)} to {ctx.as_of}"
                + ("" if days == 30 else f"; × 30 / {days}"),
                items,
                limit=ctx.records_max,
                children=chains(items, ctx.data),
            )
        ],
        formula_text=f"count(incidents started in {days} days) × 30 / {days}",
        records_total=len(items),
    )


def mttr(ctx: Ctx) -> Computation:
    days = ctx.window
    rows = []
    for rid, v in ctx.data.table("incident").known(ctx.cut):
        resolved = _ts(v, "resolved_at")
        if (
            v.f.get("restore_minutes") is None
            or resolved is None
            or not ctx.in_window(ctx.local(resolved), days)
        ):
            continue
        rows.append((rid, v, float(v.f["restore_minutes"]) / 60))
    if not rows:
        return Computation.unknown(
            f"No incident was resolved in the last {days} days.", components=["sim_incident"]
        )
    stat = str(ctx.opt("stat", "mean"))
    hours = [h for _, _, h in rows]
    value = median(hours) if stat == "median" else mean(hours)
    items = sorted(
        (incident_item(ctx, r, v) for r, v, _ in rows), key=lambda i: -float(i.facts.get("restore, h") or 0)
    )
    sev: dict[str, list[float]] = defaultdict(list)
    for _, v, h in rows:
        sev[str(v.f.get("severity") or "unknown")].append(h)
    caveats = [
        f"{len(rows)} incidents resolved in {days} days; median {median(hours):.2f} h,"
        f" longest {max(hours):.2f} h."
    ]
    if len(rows) < int(ctx.opt("min_sample", 5)):
        caveats.append(f"Small sample: {len(rows)} incidents.")
    return Computation(
        value=rnd(value, 2),
        items=items,
        breakdown=[Breakdown(k, k, round(mean(v), 2)) for k, v in sorted(sev.items())],
        sample=len(rows),
        caveats=caveats,
        lineage=[
            operation(
                f"{stat.capitalize()} time to restore",
                rnd(value, 2),
                "hours",
                f"{stat} of restore minutes / 60 over the incidents resolved in {days} days",
                f"{stat}({len(hours)} values)",
                [
                    aggregate(
                        "Resolved incidents",
                        len(rows),
                        "count",
                        "Restore time from start to service restored",
                        items,
                        limit=ctx.records_max,
                    )
                ],
            )
        ],
        formula_text=f"{stat}(restore_minutes / 60)",
        records_total=len(items),
    )


# ---------------------------------------------------------------------------------------- monitoring (v2)
def _monitoring_unknown(ctx: Ctx, what: str) -> Computation:
    days = ctx.window
    rows = incidents_started(ctx, days)
    down = sum(float(v.f.get("downtime_minutes") or 0) for _, v in rows)
    c = Computation.unknown(
        f"Daily monitoring (datasets/monitoring_daily.csv) is not in this history;"
        f" {what} is unknown, not zero.",
        caveats=[f"Context only: {len(rows)} incidents in {days} days, {down:.0f} min of downtime."],
        components=["monitoring_daily"],
    )
    c.items = [incident_item(ctx, r, v) for r, v in rows]
    c.records_total = len(c.items)
    return c


def _monitoring_rows(ctx: Ctx, days: int):
    return [r for r in ctx.data.monitoring if ctx.in_window(r.date, days)]


def _availability(row) -> float | None:
    if row.availability is not None:
        a = row.availability
        return a * 100 if a <= 1.0 else a
    if row.downtime_minutes is not None:
        return 100 * (1 - row.downtime_minutes / 1440)
    return None


def _weights(ctx: Ctx, services: list[str]) -> tuple[dict[str, float], list[str]]:
    """Traffic weights of the services, normalised to 1, and the services without a weight (left out).

    No ``service_weights`` option: every service weighs the same.
    """
    given = ctx.opt("service_weights") or {}
    known = {s: float(given[s]) for s in services if float(given.get(s, 0) or 0) > 0}
    if not known:
        return {s: 1 / len(services) for s in services}, []
    total = sum(known.values())
    return {s: w / total for s, w in known.items()}, sorted(set(services) - set(known))


def _platform_days(rows, weights: dict[str, float], field: str) -> dict:
    """Per day: the traffic-weighted value of the services that have a row that day."""
    acc: dict = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        v = _availability(r) if field == "availability" else getattr(r, field)
        w = weights.get(r.service, 0.0)
        if v is None or w <= 0:
            continue
        acc[r.date][0] += w * v
        acc[r.date][1] += w
    return {d: s / w for d, (s, w) in sorted(acc.items()) if w > 0}


def sla(ctx: Ctx) -> Computation:
    if not ctx.data.has("monitoring_daily"):
        return _monitoring_unknown(ctx, "availability")
    days = ctx.window
    rows = _monitoring_rows(ctx, days)
    services = sorted({r.service for r in rows})
    weights, unweighted = _weights(ctx, services) if services else ({}, [])
    avail = _platform_days(rows, weights, "availability")
    if not avail:
        return Computation.unknown(
            f"No monitoring row in the last {days} days.", components=["monitoring_daily"]
        )
    availability = mean(avail.values())
    p95 = _platform_days(rows, weights, "p95_ms")
    slo = float(ctx.opt("slo_ms", 250))
    k = float(ctx.opt("penalty_per_50ms", 0.002))
    use_penalty = bool(ctx.opt("latency_penalty", True)) and bool(p95)
    penalty = mean(100 * k * max(x - slo, 0.0) / 50 for x in p95.values()) if use_penalty else 0.0
    value = availability - penalty
    per: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        a = _availability(r)
        if a is not None:
            per[r.service].append(a)
    items = [incident_item(ctx, r, v) for r, v in incidents_started(ctx, days)]
    target = ctx.p.target
    below = sum(1 for a in avail.values() if target is not None and a < target)
    slow = sum(1 for x in p95.values() if x > slo)
    caveats = [
        f"Traffic-weighted availability {availability:.3f}% over {len(avail)} days"
        f" and {len(weights)} services;"
        f" {below} days below the target.",
        f"Latency penalty {penalty:.3f} points (platform p95 above {slo:.0f} ms on {slow} days),"
        " as in company_model."
        if use_penalty
        else "No latency penalty (option latency_penalty is off).",
    ]
    if unweighted:
        caveats.append(f"Services without a traffic weight are left out: {', '.join(unweighted)}.")
    return Computation(
        value=rnd(value, 3),
        items=items,
        breakdown=sorted(
            (
                Breakdown(s, f"{s} ({weights.get(s, 0) * 100:.0f}% of traffic)", round(mean(v), 3))
                for s, v in per.items()
            ),
            key=lambda b: (b.value, b.label),
        ),
        sample=len(rows),
        caveats=caveats,
        lineage=[
            operation(
                "SLA",
                rnd(value, 3),
                "percent",
                "traffic-weighted availability − latency penalty (company_model formula on the daily series)",
                f"{availability:.4f} − {penalty:.4f}",
                [
                    operation(
                        "Traffic-weighted availability",
                        rnd(availability, 4),
                        "percent",
                        f"mean over {len(avail)} days of sum(weight × availability) of the services"
                        " (monitoring rows are not KB records)",
                        "mean_d(sum_s w_s × availability_s,d) × 100",
                        [],
                    ),
                    operation(
                        "Latency penalty",
                        rnd(penalty, 4),
                        "percent",
                        f"mean over days of {k} × max(platform p95 − {slo:.0f} ms, 0) / 50, in points",
                        "mean_d(100 × k × max(p95_d − slo, 0) / 50)",
                        [],
                    ),
                    aggregate(
                        f"Incidents in {days} days",
                        len(items),
                        "count",
                        "The incidents behind the downtime",
                        items,
                        limit=ctx.records_max,
                    ),
                ],
            )
        ],
        formula_text="sum_s w_s × availability_s − latency penalty",
        records_total=len(items),
    )


def latency(ctx: Ctx) -> Computation:
    if not ctx.data.has("monitoring_daily"):
        return _monitoring_unknown(ctx, "latency")
    days = ctx.window
    rows = [r for r in _monitoring_rows(ctx, days) if r.p95_ms is not None]
    services = sorted({r.service for r in rows})
    weights, unweighted = _weights(ctx, services) if services else ({}, [])
    p95 = _platform_days(rows, weights, "p95_ms")
    if not p95:
        return Computation.unknown(
            f"No monitoring row in the last {days} days.", components=["monitoring_daily"]
        )
    value = mean(p95.values())
    per: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        per[r.service].append(float(r.p95_ms))  # type: ignore[arg-type]
    slo = float(ctx.opt("slo_ms", 250))
    over = sum(1 for x in p95.values() if x > slo)
    items = [incident_item(ctx, r, v) for r, v in incidents_started(ctx, days)]
    caveats = [
        f"Platform p95 above the objective of {slo:.0f} ms on {over} of {len(p95)} days;"
        f" max {max(p95.values()):.0f} ms."
    ]
    if unweighted:
        caveats.append(f"Services without a traffic weight are left out: {', '.join(unweighted)}.")
    return Computation(
        value=rnd(value, 1),
        items=items,
        breakdown=sorted(
            (Breakdown(s, s, round(mean(v), 1)) for s, v in per.items()),
            key=lambda b: (-(b.value or 0), b.label),
        ),
        sample=len(p95),
        caveats=caveats,
        lineage=[
            operation(
                "Platform p95 latency",
                rnd(value, 1),
                "ms",
                f"mean over {len(p95)} days of the traffic-weighted p95 (ms) of {len(weights)} services"
                " (monitoring rows are not KB records)",
                "mean_d(sum_s w_s × p95_s,d)",
                [
                    aggregate(
                        f"Incidents in {days} days",
                        len(items),
                        "count",
                        "Incidents in the same days",
                        items,
                        limit=ctx.records_max,
                    )
                ],
            )
        ],
        formula_text="mean_d(sum_s w_s × p95_ms)",
        records_total=len(items),
    )


# ---------------------------------------------------------------------------------------------- Jira
def issue_item(ctx: Ctx, key: str, **facts) -> Item:
    row = ctx.data.issues[key]
    kb = jira_id(key)
    return Item(
        kb_id=kb,
        dataset="issues",
        kind=KIND_BY_TYPE.get(row.type, "Task"),
        title=ctx.data.titles.get(kb, key),
        date=ctx.local(row.created).isoformat(),
        facts={"type": row.type, "points": row.points, **facts},
    )


def _need_jira(ctx: Ctx) -> Computation | None:
    if not (ctx.data.has("issues") and ctx.data.has("transitions")):
        return Computation.unknown(
            "Jira issues and transitions are not in this history.", components=["issues", "transitions"]
        )
    return None


def overdue_implementations(ctx: Ctx) -> Computation:
    missing = _need_jira(ctx)
    if missing:
        return missing
    days = ctx.window
    onboarding = int(ctx.opt("promise_bdays", 50))
    upsell = int(ctx.opt("upsell_promise_bdays", 28))
    prefix = str(ctx.opt("onboarding_prefix", "Onboarding:"))
    project, epic = str(ctx.opt("project", "IMPL")), str(ctx.opt("issue_type", "Epic"))
    # 1. went live after the promised date, in the window (exact: go_live_late_days of the contract record)
    late: list[Item] = []
    done = 0
    for rid, v in ctx.data.table("contract").versions(ctx.cut):
        go_live = parse_date(v.f.get("go_live"))
        if v.kind != "implementation_completed" or not ctx.in_window(go_live, days):
            continue
        done += 1
        late_days = int(v.f.get("go_live_late_days") or 0)
        if late_days > 0:
            late.append(
                Item(
                    kb_id=sim_id(rid),
                    dataset="contracts",
                    kind="Contract",
                    title=f"{v.f.get('client_name') or v.f.get('client')}: went live {late_days} days late",
                    date=str(go_live),
                    facts={
                        "client": str(v.f.get("client_name") or ""),
                        "go-live": str(go_live),
                        "late, days": late_days,
                    },
                    event=(v.kind, v.ts.isoformat()),
                    causes=v.causes,
                )
            )
    # 2. still open and older than the longest promise of its kind (certainly overdue)
    open_items, overdue = [], []
    for key, row in ctx.data.issues.items():
        if row.project != project or row.type != epic or row.created > ctx.cut:
            continue
        status = ctx.data.status_at(key, ctx.cut)
        if status in DONE:
            continue
        kind = "onboarding" if ctx.data.titles.get(jira_id(key), "").startswith(prefix) else "upsell"
        promise = onboarding if kind == "onboarding" else upsell
        age = bdays(ctx.local(row.created), ctx.as_of)
        it = issue_item(
            ctx,
            key,
            client=ctx.data.name(row.client),
            status=status,
            implementation=kind,
            **{"age, business days": age, "promise, business days": promise},
        )
        open_items.append(it)
        if age > promise:
            overdue.append(it)
    overdue.sort(key=lambda i: -int(i.facts["age, business days"] or 0))
    open_items.sort(key=lambda i: -int(i.facts["age, business days"] or 0))
    late.sort(key=lambda i: str(i.date), reverse=True)
    items = overdue + late
    caveats = [
        f"Went live in {days} days: {done}, {len(late)} after the promised date; open now: {len(open_items)},"
        f" {len(overdue)} past the longest promise.",
        f"The promised date of an open project is not in the records: overdue means older than {onboarding}"
        f" (onboarding) or {upsell} (upsell) business days, the longest promises (parameters).",
    ]
    lim = ctx.records_max
    return Computation(
        value=float(len(items)),
        items=items,
        breakdown=[
            Breakdown("late_go_live", "Went live late", float(len(late))),
            Breakdown("open_overdue", "Open past the promise", float(len(overdue))),
        ],
        sample=done + len(open_items),
        caveats=caveats,
        lineage=[
            operation(
                "Late implementations",
                len(items),
                "count",
                f"went live late in {days} days + open past the longest promise",
                f"{len(late)} + {len(overdue)}",
                [
                    aggregate(
                        f"Went live late in {days} days",
                        len(late),
                        "count",
                        "Implementation completed after its promised date (go_live_late_days > 0)",
                        late,
                        limit=lim,
                        children=chains(late, ctx.data),
                    ),
                    aggregate(
                        "Open past the promise",
                        len(overdue),
                        "count",
                        f"Open {project} epics older than {onboarding} / {upsell} business days",
                        overdue,
                        limit=lim,
                    ),
                    aggregate(
                        "Open implementations",
                        len(open_items),
                        "count",
                        f"{project} epics not Done at the date (context)",
                        open_items,
                        limit=lim,
                    ),
                ],
            )
        ],
        formula_text="count(late go-lives in the window) + count(open epics past the longest promise)",
        records_total=len(items),
    )


def done_in(ctx: Ctx, start: datetime, end: datetime, projects: set[str]) -> list[tuple[str, datetime]]:
    """Issues (no epics) of ``projects`` with the first move to Done in [start, end], known at the cut."""
    out = []
    for key, row in ctx.data.issues.items():
        if row.project not in projects or row.type == "Epic":
            continue
        at = ctx.data.done_at(key, ctx.cut)
        if at is not None and start <= at <= end:
            out.append((key, at))
    return out


def throughput(ctx: Ctx) -> Computation:
    missing = _need_jira(ctx)
    if missing:
        return missing
    days = ctx.window
    projects = set(ctx.opt("projects", ["PLAT", "IMPL"]) or [])
    start = ctx.data.clock.start_of_day(ctx.window_start(days))
    rows = done_in(ctx, start, ctx.cut, projects)
    items = [
        issue_item(
            ctx,
            k,
            done=ctx.local(at).isoformat(),
            worker=ctx.data.name(ctx.data.worker(k, ctx.cut)),
            project=ctx.data.issues[k].project,
        )
        for k, at in sorted(rows, key=lambda r: r[1], reverse=True)
    ]
    points = sum(ctx.data.issues[k].points for k, _ in rows)
    past = []
    for k in range(1, int(ctx.opt("baseline_weeks", 8)) + 1):
        e = ctx.cut - timedelta(days=7 * k)
        s = start - timedelta(days=7 * k)
        if s >= ctx.data.clock.start_of_day(ctx.data.start):
            past.append(len(done_in(ctx, s, e, projects)))
    by_type: dict[str, int] = defaultdict(int)
    for k, _ in rows:
        by_type[ctx.data.issues[k].type] += 1
    return Computation(
        value=float(len(rows)),
        items=items,
        breakdown=[
            Breakdown(k, k, float(n)) for k, n in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        sample=len(rows),
        caveats=[
            f"{points} story points done in {days} days."
            + (f" Average of the previous {len(past)} periods: {mean(past):.1f}." if past else "")
        ],
        baseline=round(mean(past), 1) if past else None,
        lineage=[
            aggregate(
                f"Issues done in {days} days",
                len(rows),
                "count",
                f"{', '.join(sorted(projects))} issues (no epics) with the first move to Done in the window",
                items,
                limit=ctx.records_max,
            )
        ],
        formula_text=f"count(first Done in {days} days)",
        records_total=len(items),
    )


def team_load(ctx: Ctx) -> Computation:
    if not ctx.data.has("sprint_load"):
        return Computation.unknown(
            "The sprint load (datasets/sprint_load.csv) or the sprint calendar of the Jira timeline"
            " is not in this history.",
            components=["sprint_load"],
        )
    done = [s for s in ctx.data.sprints if s.end <= ctx.cut and s.load is not None]
    if not done:
        return Computation.unknown(f"No sprint has ended by {ctx.as_of}.", components=["sprint_load"])
    s = done[-1]
    value = float(s.load) * 100  # type: ignore[arg-type]
    prev = [float(x.load) * 100 for x in done[-1 - int(ctx.opt("baseline_sprints", 6)) : -1]]  # type: ignore[arg-type]
    projects = set(ctx.opt("projects", ["PLAT", "IMPL"]) or [])
    rows = done_in(ctx, s.start, s.end, projects)
    per: dict[str, list[str]] = defaultdict(list)
    for k, _ in rows:
        per[ctx.data.worker(k, ctx.cut) or ctx.data.issues[k].reporter].append(k)
    items = []
    for pid, keys in sorted(per.items(), key=lambda kv: -sum(ctx.data.issues[k].points for k in kv[1])):
        p = ctx.data.people.get(pid)
        items.append(
            Item(
                kb_id=person_id(pid),
                dataset="people",
                kind="Person",
                title=ctx.data.name(pid),
                date=None,
                facts={
                    "department": p.department if p else "",
                    "points done": sum(ctx.data.issues[k].points for k in keys),
                    "issues done": len(keys),
                },
            )
        )
    issues = [
        issue_item(ctx, k, done=ctx.local(at).isoformat(), worker=ctx.data.name(ctx.data.worker(k, ctx.cut)))
        for k, at in rows
    ]
    caveats = [
        f"{s.name} ({ctx.local(s.start)} to {ctx.local(s.end)}): assigned work / capacity,"
        " from sprint planning; "
        "the sprint that runs at the date is left out.",
    ]
    if prev:
        caveats.append(f"Previous {len(prev)} sprints: {', '.join(f'{x:.0f}%' for x in prev)}.")
    return Computation(
        value=rnd(value, 1),
        items=items,
        breakdown=[
            Breakdown(
                person_id(pid),
                ctx.data.name(pid),
                float(sum(ctx.data.issues[k].points for k in keys)),
                person_id(pid),
            )
            for pid, keys in per.items()
        ],
        sample=len(items),
        caveats=caveats,
        baseline=round(mean(prev), 1) if prev else None,
        lineage=[
            operation(
                f"Load of {s.name}",
                rnd(value, 1),
                "percent",
                "assigned story points / capacity of the engineers (sprint planning record)",
                f"{s.load} × 100",
                [
                    aggregate(
                        "Issues done in the sprint",
                        len(issues),
                        "count",
                        "The work finished in the sprint, by who started it",
                        issues,
                        limit=ctx.records_max,
                    )
                ],
            )
        ],
        formula_text="sprint_load.load × 100 (last finished sprint)",
        records_total=len(items),
    )


# ---------------------------------------------------------------------------------------- people and releases
def open_requisitions(ctx: Ctx) -> Computation:
    closed = set(ctx.opt("closed_statuses", ["filled", "cancelled", "closed"]) or [])
    rows, filled = [], []
    for rid, v in ctx.data.table("requisition").known(ctx.cut):
        if v.f.get("status") in closed:
            if ctx.in_window(parse_date(v.f.get("filled_at")), 90):
                filled.append(rid)
            continue
        rows.append((rid, v))
    items = []
    for rid, v in sorted(rows, key=lambda r: str(r[1].f.get("opened_at"))):
        opened = parse_date(v.f.get("opened_at"))
        items.append(
            Item(
                kb_id=sim_id(rid),
                dataset="requisitions",
                kind="Requisition",
                title=f"{v.f.get('role')} ({v.f.get('department')})",
                date=str(v.f.get("opened_at") or ""),
                facts={
                    "role": str(v.f.get("role") or ""),
                    "department": str(v.f.get("department") or ""),
                    "status": str(v.f.get("status") or ""),
                    "target start": str(v.f.get("target_start") or ""),
                    "open, days": (ctx.as_of - opened).days if opened else None,
                    "replaces": ctx.data.name(v.f.get("replaces")) if v.f.get("replaces") else None,
                },
                event=(v.kind, v.ts.isoformat()),
                causes=v.causes,
            )
        )
    by_dept: dict[str, int] = defaultdict(int)
    for _, v in rows:
        by_dept[str(v.f.get("department") or "")] += 1
    return Computation(
        value=float(len(rows)),
        items=items,
        breakdown=[Breakdown(k, k, float(n)) for k, n in sorted(by_dept.items())],
        sample=len(rows),
        caveats=[f"Filled in the last 90 days: {len(filled)}."],
        lineage=[
            aggregate(
                "Open requisitions",
                len(rows),
                "count",
                "Requisitions not filled or cancelled at the date",
                items,
                limit=ctx.records_max,
                children=chains(items, ctx.data),
            )
        ],
        formula_text="count(requisitions with an open status)",
        records_total=len(items),
    )


def deploys(ctx: Ctx) -> Computation:
    days = ctx.window
    rows = []
    for rid, v in ctx.data.table("deploy").known(ctx.cut):
        at = _ts(v, "deployed_at")
        env = str(v.f.get("environment") or "production")
        if (
            at is not None
            and env == str(ctx.opt("environment", "production"))
            and ctx.in_window(ctx.local(at), days)
        ):
            rows.append((rid, v, at))
    rows.sort(key=lambda r: r[2], reverse=True)
    items = [
        Item(
            kb_id=sim_id(rid),
            dataset="deploys",
            kind="Deploy",
            title=str(v.f.get("title") or rid),
            date=ctx.local(at).isoformat(),
            facts={
                "version": str(v.f.get("version") or ""),
                "component": str(v.f.get("component") or ""),
                "status": str(v.f.get("status") or ""),
                "changes": int(v.f.get("changes") or 0),
                "by": str(v.f.get("author_name") or ""),
            },
            event=(v.kind, v.ts.isoformat()),
            causes=v.causes,
        )
        for rid, v, at in rows
    ]
    value = len(rows) * 7 / days
    rolled = sum(
        1 for _, v, _ in rows if v.kind == "deploy_rolled_back" or v.f.get("status") == "rolled_back"
    )
    caused = sum(1 for _, v in incidents_started(ctx, days) if v.f.get("cause_kind") == "deploy")
    by_comp: dict[str, int] = defaultdict(int)
    for _, v, _ in rows:
        by_comp[str(v.f.get("component") or "")] += 1
    caveats = [
        f"{len(rows)} deploys in {days} days; {rolled} rolled back; {caused} incidents caused by a deploy."
    ]
    if ctx.window_start(days) < ctx.data.start:
        caveats.append(f"The window starts before the history ({ctx.data.start}).")
    return Computation(
        value=rnd(value, 2),
        items=items,
        breakdown=top_breakdown(((k, k, float(n), None) for k, n in by_comp.items()), n=10),
        sample=len(rows),
        caveats=caveats,
        lineage=[
            aggregate(
                f"Production deploys in {days} days",
                len(rows),
                "count",
                f"Deploys from {ctx.window_start(days)}; × 7 / {days} per week",
                items,
                limit=ctx.records_max,
                formula=f"{len(rows)} × 7 / {days}",
            )
        ],
        formula_text=f"count(deploys in {days} days) × 7 / {days}",
        records_total=len(items),
    )


__all__ = [
    "deploys",
    "incidents",
    "latency",
    "mttr",
    "open_requisitions",
    "overdue_implementations",
    "sla",
    "team_load",
    "throughput",
]
