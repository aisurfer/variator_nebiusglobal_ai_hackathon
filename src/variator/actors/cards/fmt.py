"""Display forms of habit parameters: one value, one interval and one unit per parameter.

A parameter is the WP10 format ``{point, low, mode, high, n, source, unit, method}``. The
display row keeps the raw numbers too, so the page can draw them; the text forms are made
here once, so the page and the KB page show the same strings.
"""

from __future__ import annotations

from typing import Any

# parameter -> (label, short help); the label is what a manager reads in a habit row
LABELS: dict[str, tuple[str, str]] = {
    "arr": ("ARR", "Annual recurring revenue under the contract in force at T."),
    "arr_at_t": ("ARR", "Annual recurring revenue of all contracts in force at T."),
    "cash_at_t": ("Cash", "Bank balance at T."),
    "pay_delay_days": ("Pays an invoice after", "Days from the invoice date to the payment."),
    "pay_delay_sd_days": (
        "Spread of payment days",
        "How much the payment day varies from invoice to invoice.",
    ),
    "tickets_per_month": ("Support tickets", "Support tickets the client opens in a month."),
    "nps_level": ("NPS level", "The client's level on the NPS scale (-100 to 100), from its survey answers."),
    "nps": ("NPS", "Net promoter score of the cohort's survey answers."),
    "churn_hazard": ("Gives notice in a year", "Chance that the client gives notice within a year."),
    "churn_hazard_base": ("Gives notice in a year", "Chance that a client gives notice within a year."),
    "upsell_rate": ("Asks for more", "Upsell requests per year."),
    "late_payment_rate": ("Late-payment spells", "Spells of late payment (cash-flow problems) per year."),
    "downgrade_rate": ("Reduces its scope", "Scope reductions per year."),
    "notice_days": ("Notice period", "Days of notice in the contract."),
    "throughput_points_per_sprint": (
        "Pace",
        "Story points delivered in a two-week sprint at full availability.",
    ),
    "pace_throughput_points_per_sprint": ("Pace", "Story points in a two-week sprint at full pace."),
    "cycle_time_days": ("Ticket cycle time", "Median days from the start of a ticket to its review."),
    "own_tasks_per_day": ("Own business tasks", "Business tasks the person opens per working day."),
    "pace_own_tasks_per_day": ("Own business tasks", "Business tasks per working day at full pace."),
    "first_response_hours": (
        "First reply to a ticket",
        "Working hours to the first reply to a client ticket.",
    ),
    "pace_first_response_hours": ("First reply to a ticket", "Working hours to the first reply."),
    "work_start_minute": ("Starts work", "The time of the first activity of a working day."),
    "work_end_minute": ("Ends work", "The time of the last regular activity of a working day."),
    "sick_spells_per_year": ("Sick spells", "Spells of sickness absence per year."),
    "sick_days_per_year": ("Sick days", "Working days of sickness absence per year."),
    "vacation_days_per_year": ("Holiday", "Working days of holiday per year."),
    "growth_rate": ("Growth in the role", "Staff rate: recognised growth in the role per year."),
    "resignation_hazard": ("Resigns in a year", "Staff rate: chance that a person resigns within a year."),
    "monthly_fee": ("Monthly bill", "The median monthly bill of the vendor."),
    "monthly_fee_now": ("Last bill", "The last monthly bill before T."),
    "payroll_tax_rate": ("Employer payroll tax", "Employer tax as a share of gross pay."),
    "employee_deduction_rate": ("Employee deductions", "Deductions as a share of gross pay."),
    "incidents_per_month": ("Incidents", "Production incidents per month."),
    "mttr_hours": ("Time to restore", "Hours from the detection of an incident to the restore."),
    "deploys_per_week": ("Deploys", "Production deploys per five working days."),
    "win_rate": ("Win rate", "Share of new-business deals that are won."),
    "deals_per_month": ("New deals", "New-business deals opened per month."),
    "deal_value_median": ("Deal size", "Median value of a new-business deal."),
    "sales_cycle_days": ("Sales cycle", "Days from the start of a deal to its close."),
    "nps_now": ("NPS, latest survey", "Net promoter score of the latest quarterly survey."),
    "team_load": ("Sprint load", "Assigned work over capacity in a sprint (1 = full)."),
    "latency_p95_ms": ("p95 latency", "95th percentile of the response time."),
    "availability": ("Availability", "Daily availability of the platform."),
    "cloud_cost_per_year": ("Cloud cost", "Cloud bills per year."),
    "support_tickets_per_month": ("Support tickets", "Support tickets of all clients per month."),
    "dso_days_at_t": ("Days sales outstanding", "Receivables over daily revenue at T."),
    "hiring_lead_months": ("Time to hire", "Months from the requisition to the start of the hire."),
    "salary_vs_predecessor": (
        "Salary vs predecessor",
        "The salary of a hire over the salary of the person replaced.",
    ),
    "ramp_share_first_20_bdays": (
        "Pace, first 20 days",
        "Share of the full pace in the first 20 working days.",
    ),
    "ramp_share_next_20_bdays": ("Pace, next 20 days", "Share of the full pace in the next 20 working days."),
    "vacation_month_share_08": (
        "Holiday in August",
        "Share of the staff who take the main holiday in August.",
    ),
    "deal_value_log_sd": ("Spread of deal size", "Log standard deviation of the deal value."),
}

PCT_UNITS = {
    "probability per year",
    "share",
    "share of gross pay",
    "share of ACV",
    "share of salary",
    "share of the cloud cost",
    "share of requests",
    "share per year",
    "share of the steady pace",
}
# unit -> suffix after the value ("" = none)
SUFFIX = {
    "USD per year": "/ year",
    "USD per month": "/ month",
    "tickets per month": "/ month",
    "incidents per month": "/ month",
    "new deals per month": "/ month",
    "events per month": "/ month",
    "1/year": "/ year",
    "spells per year": "/ year",
    "working days per year": "days / year",
    "probability per year": "",  # the label says "in a year"
    "points per sprint": "pts / sprint",
    "tasks per present day": "/ day",
    "deploys per 5 working days": "/ week",
    "NPS points": "pts",
    "days": "days",
    "calendar days": "days",
    "working days": "days",
    "months": "months",
    "hours": "h",
    "working hours": "h",
    "ms": "ms",
    "clients": "clients",
}


def num(x: float | None) -> str:
    """A short number: 1,022,400 · 126 · 52.9 · 3.04 · 0.72."""
    if x is None:
        return "?"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.0f}"
    if a >= 20:
        return f"{x:.0f}"
    if a >= 2:
        s = f"{x:.1f}"
    else:
        s = f"{x:.2f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def money(x: float) -> str:
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    return f"${x:,.0f}"


def pct(x: float, digits: int = 0) -> str:
    s = f"{100 * x:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{s}%"


def hhmm(minutes: float) -> str:
    m = int(round(minutes))
    return f"{m // 60:02d}:{m % 60:02d}"


def _one(x: float, unit: str) -> str:
    if unit.startswith("USD"):
        return money(x)
    if unit in PCT_UNITS:
        if 0.99 < abs(x) < 1:
            return pct(x, 2)
        return pct(x, 0 if abs(x) >= 0.1 else 1)
    if unit == "share of the day":
        return pct(x, 2)
    if unit.startswith("minutes after midnight"):
        return hhmm(x)
    if unit == "ratio":
        return f"x{num(x)}"
    return num(x)


def display(p: dict[str, Any]) -> dict[str, str]:
    """value, interval and suffix of a parameter for a habit row."""
    unit = str(p.get("unit") or "")
    value = _one(float(p["point"]), unit)
    lo, hi = float(p["low"]), float(p["high"])
    interval = "" if abs(hi - lo) <= 1e-9 * max(1.0, abs(hi)) else f"{_one(lo, unit)} – {_one(hi, unit)}"
    return {"value": value, "interval": interval, "suffix": SUFFIX.get(unit, "")}


def source_kind(tag: str) -> str:
    """The class of a source tag: data, group, web, llm or assumption."""
    head = (tag or "").split(":", 1)[0].strip().lower()
    return head if head in ("data", "group", "web", "llm", "assumption") else "data"


def source_label(tag: str) -> str:
    """A readable source tag: data:sim_invoice@2026-10-27 -> data · invoice."""
    kind = source_kind(tag)
    rest = tag.split(":", 1)[1] if ":" in tag else ""
    rest = rest.split("@", 1)[0]
    rest = rest.removeprefix("sim_").replace("_", " ")
    return f"{kind} · {rest}" if rest and rest != "pending" else kind


def row(key: str, p: dict[str, Any], *, primary: bool, label: str | None = None) -> dict[str, Any]:
    """A habit row of a card."""
    lab, help_ = LABELS.get(key, (key.replace("_", " ").capitalize(), ""))
    d = display(p)
    return {
        "key": key,
        "label": label or lab,
        "help": help_,
        "value": d["value"],
        "interval": d["interval"],
        "suffix": d["suffix"],
        "point": p["point"],
        "low": p["low"],
        "high": p["high"],
        "n": int(p.get("n") or 0),
        "unit": p.get("unit") or "",
        "source": p.get("source") or "",
        "sourceKind": source_kind(str(p.get("source") or "")),
        "sourceLabel": source_label(str(p.get("source") or "")),
        "method": p.get("method") or "",
        "primary": primary,
    }


def pending_row(key: str, why: str, reason: str) -> dict[str, Any]:
    """A parameter that waits for general knowledge (llm:pending): no value yet."""
    return {
        "key": key,
        "label": key.replace("_", " ").capitalize(),
        "help": why,
        "value": "pending",
        "interval": "",
        "suffix": "",
        "point": None,
        "low": None,
        "high": None,
        "n": 0,
        "unit": "",
        "source": "llm:pending",
        "sourceKind": "llm",
        "sourceLabel": "llm · pending",
        "method": reason,
        "primary": False,
    }
