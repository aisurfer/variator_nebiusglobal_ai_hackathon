"""Habits of people (09 §2.3): pace, cycle time, absences, working hours, life events.

Each estimator reads only rows known at T. Per-person values are partially pooled with the
cohort (``stats.pool_poisson`` for counts, ``stats.pool_normal`` for levels).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from variator.actors import stats
from variator.actors.context import Context
from variator.actors.history import ENGINEER_ROLES, PersonInfo, parse_date
from variator.actors.stats import Param

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
RAMP_BDAYS = 40  # a new hire is not at full pace in the first two months
ROTATION_BDAYS = 20
LEAVING_BDAYS = 10
PROCESS_PREFIXES = (
    "Renewal prep for",
    "Exit review with",
    "Proposal for",
    "Offboarding:",
    "Hiring:",
    "Onboarding:",
    "Post-incident review:",
)
DESIGN_PREFIXES = ("Design:", "Spec:", "User research:", "Prototype:")


@dataclass
class PeopleResult:
    params: dict[str, dict[str, Param]] = field(default_factory=dict)  # pid -> name -> Param
    info: dict[str, dict[str, Any]] = field(default_factory=dict)  # pid -> descriptive values
    company: dict[str, Param] = field(default_factory=dict)  # cohort-level values (all staff)
    roles: dict[str, dict[str, Param]] = field(default_factory=dict)  # role -> name -> Param
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # kind -> event rows
    evidence: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # pid -> name -> kb ids
    hires: list[dict[str, Any]] = field(default_factory=list)


def _month_label(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.year}"


def _first_workday_of_month(ctx: Context, d: date) -> bool:
    first = ctx.h.cal.roll_forward(d.replace(day=1))
    return first == d


def learn_people(ctx: Context) -> PeopleResult:
    h = ctx.h
    res = PeopleResult()
    people = sorted(h.people.values(), key=lambda p: p.id)
    for p in people:
        res.params[p.id] = {}
        res.evidence[p.id] = defaultdict(list)
        a, b = ctx.employed_span(p)
        res.info[p.id] = {
            "employed_bdays": len(ctx.employed_days(p)),
            "present_bdays": len(ctx.present_days(p)),
            "window": [a.isoformat(), b.isoformat()],
        }
    _events(ctx, res)
    _absences(ctx, res, people)
    _life_events(ctx, res, people)
    _throughput(ctx, res)
    _biz_tickets(ctx, res, people)
    _response(ctx, res)
    _working_hours(ctx, res, people)
    _hires(ctx, res)
    res.evidence = {k: dict(v) for k, v in res.evidence.items()}
    return res


# ---------------------------------------------------------------------------------------------- events
def _events(ctx: Context, res: PeopleResult) -> None:
    """Employee events by kind (first version of each record), with KB ids."""
    h = ctx.h
    seen: set[str] = set()
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in h.records.get("employee_event", []):
        key = (e["id"], e["kind"])
        if key in seen:
            continue
        seen.add(key)
        out[e["kind"]].append({**e, "kb": h.kb("sim", e["id"])})
    for le in h.records.get("life_event", []):
        if le.get("employee") and le["id"] not in seen:
            seen.add(le["id"])
            out["decides_to_leave"].append({**le, "kb": h.kb("sim", le["id"])})
    res.events = dict(out)


# ---------------------------------------------------------------------------------------------- absences
def _absences(ctx: Context, res: PeopleResult, people: list[PersonInfo]) -> None:
    h = ctx.h
    tag = ctx.tag("sim_employee_event")
    ids, counts, expo, spell_len = [], [], [], []
    for p in people:
        sp = ctx.spells.get(p.id, [])
        sick = [s for s in sp if s.kind == "sick"]
        vac = [s for s in sp if s.kind == "vacation"]
        emp = ctx.employed_days(p)
        vac_days = sum(s.days for s in vac)
        sick_days = sum(s.days for s in sick)
        years = max(len(emp) - vac_days - sick_days, 0) / 252.0
        ids.append(p.id)
        counts.append(len(sick))
        expo.append(years)
        spell_len += [s.days for s in sick if s.last < h.as_of - timedelta(days=1)]
        res.evidence[p.id]["sick_spells_per_year"] = [x for s in sick for x in s.evidence]
        res.info[p.id].update(
            sick_spells=len(sick),
            sick_days=sick_days,
            vacation_days=vac_days,
            vacation_spells=[
                {"first": s.first.isoformat(), "last": s.last.isoformat(), "days": s.days} for s in vac
            ],
            planned_leave=[
                {
                    "first": s.first.isoformat(),
                    "last": s.last.isoformat(),
                    "days": s.days,
                    "evidence": s.evidence,
                }
                for s in ctx.planned.get(p.id, [])
            ],
        )
        res.evidence[p.id]["vacation"] = [x for s in vac for x in s.evidence] + [
            x for s in ctx.planned.get(p.id, []) for x in s.evidence
        ]
    pooled = stats.pool_poisson(
        ids, np.array(counts), np.array(expo), tag, ctx.group("staff"), unit="spells per year", label="sick"
    )
    res.company["sick_spells_per_year"] = pooled.group
    res.company["sick_spells_spread"] = pooled.spread
    for pid, prm in pooled.actors.items():
        res.params[pid]["sick_spells_per_year"] = prm
    if len(spell_len) >= 2:
        fit = stats.lognormal(np.array(spell_len, float), tag, "working days", "sick_len")
        res.company["sick_days_per_spell"] = Param(
            float(np.mean(spell_len)),
            fit.mean.low,
            float(np.median(spell_len)),
            fit.mean.high,
            len(spell_len),
            tag,
            "working days",
            "log-normal (mean)",
        )
        mean_len = float(np.mean(spell_len))
        for pid in ids:
            sp = res.params[pid]["sick_spells_per_year"]
            res.params[pid]["sick_days_per_year"] = Param(
                sp.point * mean_len,
                sp.low * mean_len,
                sp.mode * mean_len,
                sp.high * mean_len,
                sp.n,
                sp.source,
                "working days per year",
                "sick spells x mean spell length",
            )
    # vacation: the main block month, short breaks
    months = Counter()
    short, person_months = 0, 0
    for p in people:
        vac = [s for s in ctx.spells.get(p.id, []) if s.kind == "vacation"]
        vac_all = vac + ctx.planned.get(p.id, [])
        main = max(vac, key=lambda s: (s.days, s.first), default=None)
        if main is not None and main.days >= 5:
            res.info[p.id]["vacation_month"] = main.first.month
            res.info[p.id]["vacation_main_days"] = main.days
            months[main.first.month] += 1
        emp = ctx.employed_days(p)
        by_month: dict[tuple[int, int], int] = Counter((d.year, d.month) for d in emp)
        main_m = (main.first.year, main.first.month) if main is not None and main.days >= 5 else None
        brk = [s for s in vac_all if s.days <= 3 and s.first < h.as_of]
        for ym, n in by_month.items():
            if n >= 8 and ym != main_m:
                person_months += 1
        short += sum(1 for s in brk if (s.first.year, s.first.month) != main_m)
        years = len(emp) / 252.0
        if years > 0.2:
            res.params[p.id]["vacation_days_per_year"] = stats.rate(
                sum(s.days for s in vac), years, ctx.tag("sim_employee_event"), "working days per year"
            )
    if person_months:
        res.company["short_break_per_month"] = stats.share(short, person_months, tag)
    total = sum(months.values())
    if total:
        for m in range(1, 13):
            res.company[f"vacation_month_share_{m:02d}"] = stats.share(months.get(m, 0), total, tag)


# ---------------------------------------------------------------------------------------------- life events
def _life_events(ctx: Context, res: PeopleResult, people: list[PersonInfo]) -> None:
    h = ctx.h
    tag = ctx.tag("sim_employee_event")
    ev = res.events
    resign = {e["employee"]: e for e in ev.get("resignation_received", [])}
    growth: dict[str, list[dict]] = defaultdict(list)
    for e in ev.get("growth_recognised", []):
        growth[e["employee"]].append(e)
    rotation: dict[str, list[dict]] = defaultdict(list)
    for e in ev.get("rotation_started", []):
        rotation[e["employee"]].append(e)
    ids, cnt, expo = [], [], []
    g_ids, g_cnt, g_expo = [], [], []
    rot_cnt, rot_expo = 0, 0.0
    for p in people:
        a, b = ctx.employed_span(p)
        r = resign.get(p.id)
        end = min(parse_date(r["effective_date"]), b) if r else b
        years = h.years(a, end)
        if r:
            res.evidence[p.id]["resignation"] = [r["kb"]]
        if not p.executive:
            ids.append(p.id)
            cnt.append(1 if r else 0)
            expo.append(years)
            g_ids.append(p.id)
            g_cnt.append(len(growth.get(p.id, [])))
            g_expo.append(years)
        if p.role in ("backend", "frontend"):
            rot_cnt += len(rotation.get(p.id, []))
            rot_expo += years
        res.evidence[p.id]["growth"] = [e["kb"] for e in growth.get(p.id, [])]
        res.evidence[p.id]["rotation"] = [e["kb"] for e in rotation.get(p.id, [])]
    pooled = stats.pool_rate(
        ids, np.array(cnt), np.array(expo), tag, ctx.group("staff"), unit="1/year", label="resign"
    )
    f = lambda x: 1.0 - math.exp(-x)  # noqa: E731

    def as_prob(prm: Param) -> Param:
        return Param(
            f(prm.point),
            f(prm.low),
            f(prm.mode),
            f(prm.high),
            prm.n,
            prm.source,
            "probability per year",
            prm.method + ", annual probability",
            dict(prm.extra),
        )

    res.company["resignation_hazard"] = as_prob(pooled.group)
    res.company["resignation_hazard"].extra["realised_rate"] = stats.hazard(sum(cnt), sum(expo), tag).point
    res.company["resignation_hazard_crude"] = stats.hazard(sum(cnt), sum(expo), tag)
    for pid, prm in pooled.actors.items():
        res.params[pid]["resignation_hazard"] = as_prob(prm)
    for p in people:
        if p.executive:
            g = res.company["resignation_hazard"]
            res.params[p.id]["resignation_hazard"] = Param(
                g.point,
                g.low,
                g.mode,
                g.high,
                0,
                ctx.group("staff"),
                g.unit,
                "no own events: the staff value (assumption: executives share the staff hazard)",
            )
    gp = stats.pool_rate(
        g_ids, np.array(g_cnt), np.array(g_expo), tag, ctx.group("staff"), unit="1/year", label="growth"
    )
    res.company["growth_rate"] = gp.group
    for pid, prm in gp.actors.items():
        res.params[pid]["growth_rate"] = prm
    if rot_expo > 0:
        res.company["rotation_rate"] = stats.rate(rot_cnt, rot_expo, tag, "1/year")
    # salary reviews: granted vs declined, raise size
    granted = ev.get("salary_changed", [])
    declined = ev.get("salary_review_declined", [])
    if granted or declined:
        res.company["raise_grant_share"] = stats.share(len(granted), len(granted) + len(declined), tag)
    asks = [
        e["salary_expectation"] / e["salary"] - 1
        for e in ev.get("growth_recognised", [])
        if e.get("salary") and e.get("salary_expectation")
    ]
    if len(asks) >= 2:
        res.company["raise_ask_share"] = stats.normal_mean(np.array(asks), tag, "share of salary")
    for e in granted:
        res.evidence[e["employee"]].setdefault("salary_review", []).append(e["kb"])
    for e in declined:
        res.evidence[e["employee"]].setdefault("salary_review", []).append(e["kb"])
    for p in people:
        res.info[p.id].update(
            resigned=p.id in resign,
            resignation_date=resign[p.id]["effective_date"] if p.id in resign else None,
            growth_events=[e["effective_date"] for e in growth.get(p.id, [])],
            rotations=[e["effective_date"] for e in rotation.get(p.id, [])],
            salary_reviews=[
                {
                    "date": e["effective_date"],
                    "granted": e["kind"] == "salary_changed",
                    "salary": e.get("salary"),
                    "previous": e.get("previous_salary"),
                    "expectation": e.get("salary_expectation"),
                }
                for e in granted + declined
                if e["employee"] == p.id
            ],
        )


# ---------------------------------------------------------------------------------------------- pace
def _throughput(ctx: Context, res: PeopleResult) -> None:
    h = ctx.h
    tag = ctx.tag("jira")
    issues = {r["key"]: r for r in h.table("issues")}
    trans = sorted(h.table("transitions").rows, key=lambda r: r["ts"])
    first_review: dict[str, dict] = {}
    first_progress: dict[str, dict] = {}
    blocked: dict[str, list[tuple[datetime, datetime | None]]] = defaultdict(list)
    open_block: dict[str, tuple[str, datetime]] = {}
    for r in trans:
        k = r["key"]
        if r["to"] == "In Progress" and k not in first_progress:
            first_progress[k] = r
        if r["to"] == "In Review" and k not in first_review:
            first_review[k] = r
        if r["to"] == "Blocked":
            open_block[k] = (r["actor"], r["ts"])
        elif r["from"] == "Blocked" and k in open_block:
            actor, t0 = open_block.pop(k)
            blocked[actor].append((t0, r["ts"]))
    for actor, t0 in open_block.values():
        blocked[actor].append((t0, None))
    eng = sorted(ctx.engineers(), key=lambda p: p.id)
    sprint_of = {}
    for n, a, b in h.sprints:
        for d in h.workdays(a, b):
            sprint_of[d] = n
    rot = {p.id: [parse_date(x) for x in res.info[p.id].get("rotations", [])] for p in eng}
    ids, y, v = [], [], []
    for p in eng:
        excl: set[date] = set()
        if p.start >= h.start:
            excl.update(h.workdays(p.start, h.cal.add_business_days(p.start, RAMP_BDAYS)))
        for d0 in rot.get(p.id, []):
            excl.update(h.workdays(d0, h.cal.add_business_days(d0, ROTATION_BDAYS)))
        if p.last_day is not None:
            excl.update(
                h.workdays(h.cal.add_business_days(p.last_day, -LEAVING_BDAYS), p.last_day + timedelta(1))
            )
        present = [d for d in ctx.present_days(p) if d not in excl]
        days_by: Counter = Counter(sprint_of.get(d) for d in present)
        pts_by: Counter = Counter()
        cyc: list[float] = []
        keys: list[tuple[int, str]] = []
        for k, r in first_review.items():
            if r["actor"] != p.id:
                continue
            iss = issues.get(k)
            if iss is None or iss["project"] not in ("PLAT", "IMPL"):
                continue
            d = h.local(r["ts"]).date()
            if d in excl:
                continue
            pts_by[sprint_of.get(d)] += iss["points"] or 0
            keys.append((iss["points"] or 0, k))
            fp = first_progress.get(k)
            if fp is not None and fp["actor"] == p.id:
                cyc.append(max((r["ts"] - fp["ts"]).total_seconds() / 86400.0, 0.01))
        sprints = [s for s in days_by if s is not None and days_by[s] >= 3]
        days = np.array([days_by[s] for s in sprints], float)
        pts = np.array([pts_by.get(s, 0) for s in sprints], float)
        res.evidence[p.id]["throughput"] = [h.kb("jira", k) for _, k in sorted(keys, reverse=True)[:8]]
        res.info[p.id]["delivered_points"] = int(pts.sum())
        res.info[p.id]["delivered_issues"] = len(keys)
        res.info[p.id]["pace_days"] = int(days.sum())
        if days.sum() >= 15 and len(sprints) >= 2:
            rate = pts.sum() / days.sum() * 10
            resid = pts - rate / 10 * days
            var = len(sprints) / max(len(sprints) - 1, 1) * float(np.sum(resid**2)) / days.sum() ** 2 * 100
            var = max(var, (0.08 * rate) ** 2 / len(sprints))
            ids.append(p.id)
            y.append(math.log(max(rate, 0.1)))
            v.append(var / max(rate, 0.1) ** 2)
            res.info[p.id]["throughput_own"] = rate
            res.info[p.id]["throughput_sprints"] = len(sprints)
        if len(cyc) >= 3:
            fit = stats.lognormal(np.array(cyc), tag, "calendar days", f"cycle:{p.id}")
            res.params[p.id]["cycle_time_days"] = fit.median
        # ramp-up of a hire: points per present day in the first 20 and the next 20 working days
        if p.start >= h.start and days.sum() > 0:
            steady = pts.sum() / days.sum()
            first20 = set(h.workdays(p.start, h.cal.add_business_days(p.start, RAMP_BDAYS // 2)))
            next20 = set(
                h.workdays(
                    h.cal.add_business_days(p.start, RAMP_BDAYS // 2),
                    h.cal.add_business_days(p.start, RAMP_BDAYS),
                )
            )
            pres = set(ctx.present_days(p))
            ramp = {}
            for name, win in (("first_20_bdays", first20), ("next_20_bdays", next20)):
                dd = len(win & pres)
                pp = sum(
                    (issues[k]["points"] or 0)
                    for k, r in first_review.items()
                    if r["actor"] == p.id
                    and k in issues
                    and issues[k]["project"] in ("PLAT", "IMPL")
                    and h.local(r["ts"]).date() in win
                )
                if dd >= 5 and steady > 0:
                    ramp[name] = round(pp / dd / steady, 3)
            if ramp:
                res.info[p.id]["ramp_share_of_steady_pace"] = ramp
        # blocked share of the present days
        bl = 0.0
        for t0, t1 in blocked.get(p.id, []):
            a = h.local(t0).date()
            b = h.local(t1).date() if t1 else h.as_of
            bl += max(h.bdays(a, b), 0) + 0.5
        res.info[p.id]["blocked_days"] = bl
        res.info[p.id]["blocked_times"] = len(blocked.get(p.id, []))
    if ids:
        pooled = stats.pool_normal(
            ids,
            np.array(y),
            np.array(v),
            tag,
            ctx.group("engineers"),
            unit="points per sprint",
            label="throughput",
        )
        res.company["throughput_engineers"] = pooled.group
        res.company["throughput_spread"] = pooled.spread
        for pid, prm in pooled.actors.items():
            prm.n = res.info[pid]["throughput_sprints"]
            res.params[pid]["throughput_points_per_sprint"] = prm
            res.info[pid]["throughput_weight"] = pooled.weights[pid]
        # pace as now: after the last growth event the pace of a person rises
        ups = []
        for p in eng:
            g = [parse_date(x) for x in res.info[p.id].get("growth_events", [])]
            if not g or p.id not in pooled.actors:
                continue
            g0 = max(g)
            before = [s for s in h.sprints if s[2] <= g0]
            after = [s for s in h.sprints if s[1] >= g0]
            if before and after:
                ups.append((p.id, g0))
        res.info["_growth_people"] = {"ids": [x[0] for x in ups]}
        by_role: dict[str, list[str]] = defaultdict(list)
        for p in eng:
            if p.id in pooled.actors:
                by_role[p.role].append(p.id)
        for role, members in by_role.items():
            vals = np.array([math.log(pooled.actors[m].point) for m in members])
            res.roles.setdefault(role, {})["throughput_points_per_sprint"] = Param(
                float(np.exp(vals.mean())),
                float(np.exp(vals.min())),
                float(np.exp(vals.mean())),
                float(np.exp(vals.max())),
                len(members),
                ctx.group(f"role:{role}"),
                "points per sprint",
                "geometric mean of the role members (low/high: the members' range)",
            )
    # blocked share (company level)
    tot_b = sum(res.info[p.id].get("blocked_days", 0.0) for p in eng)
    tot_d = sum(len(ctx.present_days(p)) for p in eng)
    k = sum(res.info[p.id].get("blocked_times", 0) for p in eng)
    if tot_d:
        share = tot_b / tot_d
        # uncertainty from the count of blocked spells (Poisson), the mean spell length fixed
        r = stats.rate(k, tot_d, tag, "")
        per = tot_b / max(k, 1)
        res.company["blocked_time_share"] = Param(
            share,
            r.low * per,
            share,
            r.high * per,
            k,
            tag,
            "share",
            "blocked working days / present working days",
        )


# ----------------------------------------------------------------------------------------- business tasks
def _biz_tickets(ctx: Context, res: PeopleResult, people: list[PersonInfo]) -> None:
    h = ctx.h
    tag = ctx.tag("jira")
    if not h.issue_text:
        return  # no Jira summaries (timeline fixture missing): own tasks cannot be told apart
    by: dict[str, list[str]] = defaultdict(list)
    for r in h.table("issues"):
        if r["project"] not in ("BIZ", "PLAT"):
            continue
        txt = h.issue_text.get(r["key"], {})
        s = txt.get("summary", "")
        if not s:
            continue
        if r["project"] == "PLAT" and not s.startswith(DESIGN_PREFIXES):
            continue
        if s.startswith(PROCESS_PREFIXES) or "postmortem" in (txt.get("labels") or []):
            continue
        created = h.local(r["created"]).date()
        if _first_workday_of_month(ctx, created) and _is_recurring(s, created):
            continue
        by[r["reporter"]].append(r["key"])
    for p in people:
        if p.role in ENGINEER_ROLES:
            continue
        days = len(ctx.present_days(p))
        if days < 10:
            continue
        keys = by.get(p.id, [])
        res.params[p.id]["own_tasks_per_day"] = stats.rate(len(keys), days, tag, "tasks per present day")
        res.evidence[p.id]["own_tasks_per_day"] = [h.kb("jira", k) for k in keys[-6:]]
        res.info[p.id]["own_tasks"] = len(keys)


def _is_recurring(summary: str, d: date) -> bool:
    prev = d.replace(day=1) - timedelta(days=1)
    q = f"Q{(d.month - 1) // 3 + 1} {d.year}"
    return summary in (
        f"Month-end close for {_month_label(prev)}",
        f"Board pack for {_month_label(prev)}",
        f"Newsletter for {_month_label(d)}",
        f"Capacity plan for {q}",
    )


# --------------------------------------------------------------------------------------- support response
def _response(ctx: Context, res: PeopleResult) -> None:
    h = ctx.h
    tag = ctx.tag("sim_support_ticket")
    first: dict[str, dict] = {}
    opened: dict[str, dict] = {}
    for r in h.records.get("support_ticket", []):
        if r["kind"] == "ticket_opened":
            opened[r["id"]] = r
        elif r["kind"] == "first_response" and r["id"] not in first:
            first[r["id"]] = r
    per: dict[str, list[float]] = defaultdict(list)
    ev: dict[str, list[str]] = defaultdict(list)
    for tid, f in first.items():
        o = opened.get(tid)
        if o is None or o.get("priority") == "P1" or o.get("category") == "incident":
            continue
        t0 = parse_ts_safe(o.get("opened_at"))
        t1 = parse_ts_safe(f.get("first_response_at"))
        if t0 is None or t1 is None:
            continue
        hours = _work_hours(ctx, t0, t1)
        who = o.get("assignee")
        per[who].append(max(hours, 0.01))
        ev[who].append(h.kb("sim", tid))
    for pid, xs in per.items():
        if pid not in h.people or len(xs) < 5:
            continue
        x = np.array(xs)
        fit = stats.lognormal(x, tag, "working hours", f"response:{pid}")
        # robust level: the median of the delays (absences add a long right tail)
        med = float(np.median(x))
        lo, hi = _median_ci(x)
        res.params[pid]["first_response_hours"] = Param(
            med,
            lo,
            med,
            hi,
            len(xs),
            tag,
            "working hours",
            "median with an order-statistic interval",
            {"log_sd": fit.s},
        )
        res.evidence[pid]["first_response_hours"] = ev[pid][-6:]


def parse_ts_safe(x: Any) -> datetime | None:
    from variator.actors.history import parse_ts

    try:
        return parse_ts(x)
    except (TypeError, ValueError):
        return None


def _median_ci(x: np.ndarray, p_lo: float = stats.P_LO, p_hi: float = stats.P_HI) -> tuple[float, float]:
    """A distribution-free interval for the median from the binomial order statistics."""
    from scipy import stats as st

    xs = np.sort(np.asarray(x, float))
    n = xs.size
    lo = int(st.binom.ppf(p_lo, n, 0.5))
    hi = int(st.binom.ppf(p_hi, n, 0.5))
    return float(xs[max(lo - 1, 0)]), float(xs[min(hi, n - 1)])


def _work_hours(ctx: Context, t0: datetime, t1: datetime) -> float:
    """Working hours of the company calendar between two moments."""
    cal = ctx.h.cal
    if t1 <= t0:
        return 0.0
    a = cal.next_work_time(t0)
    b = cal.local(t1)
    if b <= a:
        return 0.0
    total = 0.0
    cur = a
    while cur.date() < b.date():
        end = cal.at(cur.date(), cal.day_end)
        total += max((end - cur).total_seconds(), 0) / 3600
        cur = cal.at(cal.add_business_days(cur.date(), 1), cal.day_start)
        if cur > b:
            return total
    start = max(cur, cal.at(b.date(), cal.day_start))
    end = min(b, cal.at(b.date(), cal.day_end))
    total += max((end - start).total_seconds(), 0) / 3600
    return total


# ---------------------------------------------------------------------------------------------- working hours
def _working_hours(ctx: Context, res: PeopleResult, people: list[PersonInfo]) -> None:
    h = ctx.h
    tag = ctx.tag("jira+mail")
    mins: dict[str, list[float]] = defaultdict(list)
    for r in h.table("transitions"):
        t = h.local(r["ts"])
        mins[r["actor"]].append(t.hour * 60 + t.minute + t.second / 60)
    addr = {}
    for pid, d in h.directory.items():
        if d.get("email"):
            addr[d["email"]] = pid
    for r in h.table("emails"):
        pid = addr.get(r["from"])
        if pid is None or r.get("kind") in ("support_reply", "support_resolved"):
            continue
        t = h.local(r["sent"])
        mins[pid].append(t.hour * 60 + t.minute + t.second / 60)
    for p in people:
        x = np.array(mins.get(p.id, []), float)
        if x.size < 20:
            continue
        start, end = np.quantile(x, [0.02, 0.98])
        earliest = float(x.min())
        # the working day starts at or before the first activity; the first activity comes
        # within an hour of the start for people with 20 or more stamps (assumption)
        res.params[p.id]["work_start_minute"] = Param(
            earliest,
            max(earliest - 60.0, 0.0),
            earliest,
            earliest,
            int(x.size),
            tag,
            "minutes after midnight (local)",
            "upper bound: the earliest activity; low = the bound - 60 min",
            {"activity_p2": float(start)},
        )
        res.params[p.id]["work_end_minute"] = Param(
            float(end),
            float(np.quantile(x, 0.95)),
            float(end),
            float(x.max()),
            int(x.size),
            tag,
            "minutes after midnight (local)",
            "p98 of the activity times",
        )
        res.info[p.id]["activity_window"] = [_hhmm(start), _hhmm(end)]
        res.info[p.id]["earliest_activity"] = _hhmm(earliest)


def _hhmm(m: float) -> str:
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


# ---------------------------------------------------------------------------------------------- hires
def _hires(ctx: Context, res: PeopleResult) -> None:
    """Requisitions: lead time, the salary of the hire vs the predecessor, the role."""
    h = ctx.h
    reqs = h.versions("requisition")
    out = []
    for rid, vers in sorted(reqs.items()):
        first, last = vers[0], vers[-1]
        opened = parse_date(first.get("opened_at"))
        filled = parse_date(last.get("filled_at")) if last.get("status") == "filled" else None
        prev = h.people.get(first.get("replaces", ""))
        new = h.people.get(last.get("employee", "")) if filled else None
        out.append(
            {
                "requisition": rid,
                "kb": h.kb("sim", rid),
                "role": first.get("role"),
                "department": first.get("department"),
                "replaces": first.get("replaces"),
                "opened": opened,
                "filled": filled,
                "target_start": parse_date(last.get("target_start")),
                "status": last.get("status"),
                "employee": last.get("employee") if filled else None,
                "salary_ratio": (new.salary / prev.salary)
                if (new and prev and new.salary and prev.salary)
                else None,
                "role_key": prev.role if prev else None,
            }
        )
    res.hires = out
