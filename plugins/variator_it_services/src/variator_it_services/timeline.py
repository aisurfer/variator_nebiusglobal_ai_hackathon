"""Timeline of the ``delivery_model`` template: start positions, plan items, events, milestones, goal, levers.

The delivery page draws the same items and moves the same month variables
(``docs/playground/it-delivery-simulator.html``; parity: ``check_playground.py``).
"""

from __future__ import annotations

from variator.domain import Lever, Timeline, TimelineItem

# Month when the work w is done, with the extra people from t_add (as the derived values of delivery_model)
DONE = "minimum(ramp_up_months + {w} / cap, t_add + ({w} - cap * (t_add - ramp_up_months)) / (cap + add_cap))"


def done_at(w: str) -> str:
    """Month when the work ``w`` is done, plus 14% of the time after a key person leaves."""
    d = DONE.format(w=w)
    return f"{d} + ev_kp * 0.14 * maximum({d} - ev_kp_month, 0)"


DELIVERY_TIMELINE = Timeline(
    horizon=24,
    items=[
        TimelineItem(
            name="start_team", label="Team at the start", kind="start", emoji="👥", month=0, show="team_size"
        ),
        TimelineItem(
            name="start_scope",
            label="Scope at the start",
            kind="start",
            emoji="📋",
            month=0,
            show="scope_points",
        ),
        TimelineItem(
            name="env_work",
            label="Load-test environment work starts",
            kind="plan",
            emoji="🧪",
            month="env_start",
            description="Start earlier to have the environment sooner.",
        ),
        TimelineItem(
            name="extra_people",
            label="Extra people requested",
            kind="plan",
            emoji="➕",
            month="add_month",
            description="Hires start after 2-5 months; vendor people after 0.5-1.5 months (+20% rate).",
            show="t_add",
        ),
        TimelineItem(
            name="scope_change",
            label="Scope change (+100 points)",
            kind="event",
            emoji="🔁",
            month="ev_scope_month",
            description="The client adds 100 story points. It counts before the planned go-live only.",
            place={"ev_scope_points": 100},
        ),
        TimelineItem(
            name="key_person_leaves",
            label="A key person leaves",
            kind="event",
            emoji="🚪",
            month="ev_kp_month",
            description="The rest of the work takes 14% longer from that month.",
            place={"ev_kp": 1},
        ),
        TimelineItem(
            name="beta",
            label="Beta: half of the work done",
            kind="milestone",
            emoji="🧩",
            month="m_beta",
            condition="beta_time <= m_beta",
            show="beta_time",
        ),
        TimelineItem(
            name="load_test_env",
            label="Load-test environment ready",
            kind="milestone",
            emoji="🧪",
            month="m_env",
            condition="env_ready <= m_env",
            show="env_ready",
        ),
        TimelineItem(
            name="go_live",
            label="Go-live",
            kind="milestone",
            emoji="🚀",
            month="target_months",
            condition="duration <= target_months",
            show="duration",
        ),
        TimelineItem(
            name="money_goal",
            label="Money at month 24 at least the goal",
            kind="goal",
            emoji="💰",
            month=24,
            condition="money >= g_money",
        ),
    ],
    # Plan order: the environment, then how many people, then when to request them
    levers=[
        Lever(
            variable="env_start",
            label="Start the load-test environment work by month",
            low=0,
            high=6,
            step=0.5,
            unit="month",
            prefer="high",
        ),
        Lever(
            variable="add_fte",
            label="Extra people",
            low=0,
            high=8,
            step=1,
            unit="FTE",
            mode="add_outsourced",
            modes={"hire": 0, "outsource": 1},
        ),
        Lever(
            variable="add_month",
            label="Request the extra people by month",
            low=0,
            high=8,
            step=1,
            unit="month",
            prefer="high",
            needs="add_fte",
        ),
    ],
)

# Company year: each lever starts at a month and is ready after its lead time; then it acts until month 12.
# Events are certain (placed) events from a month; the random shocks stay as risks.
COMPANY_TIMELINE = Timeline(
    horizon=12,
    items=[
        TimelineItem(
            name="start_cash", label="Cash at the start", kind="start", emoji="💵", month=0, show="cash0"
        ),
        TimelineItem(
            name="start_arr", label="ARR at the start", kind="start", emoji="📈", month=0, show="arr0"
        ),
        TimelineItem(
            name="price_start",
            label="New prices announced",
            kind="plan",
            emoji="🏷️",
            month="m_price",
            options=["raise_prices_8"],
        ),
        TimelineItem(
            name="debt_start",
            label="Debt work starts",
            kind="plan",
            emoji="🧹",
            month="m_debt",
            options=["pay_tech_debt"],
        ),
        TimelineItem(
            name="hire_start",
            label="Hiring starts",
            kind="plan",
            emoji="👥",
            month="m_hire",
            options=["hire_5"],
        ),
        TimelineItem(
            name="gpu_start",
            label="GPU reservation ordered",
            kind="plan",
            emoji="🖥️",
            month="m_gpu",
            options=["reserve_gpu"],
        ),
        TimelineItem(
            name="gpu_event",
            label="GPU shortage",
            kind="event",
            emoji="⚡",
            month="ev_gpu_month",
            place={"ev_gpu": 1},
            description="40% shortage from that month: infra cost and latency rise.",
        ),
        TimelineItem(
            name="vendor_event",
            label="Supplier raises prices 10%",
            kind="event",
            emoji="📈",
            month="ev_vendor_month",
            place={"ev_vendor": 1},
        ),
        TimelineItem(
            name="account_event",
            label="Key account leaves",
            kind="event",
            emoji="🚪",
            month="ev_account_month",
            place={"ev_account": 1},
            description="10% of ARR from that month.",
        ),
        TimelineItem(
            name="kp_event",
            label="Key engineer leaves",
            kind="event",
            emoji="🧑‍💻",
            month="ev_kp_month",
            place={"ev_kp": 1},
        ),
        TimelineItem(
            name="price_live",
            label="New prices live",
            kind="milestone",
            emoji="🏷️",
            month="ms_price",
            condition="price_live <= ms_price",
            show="price_live",
            options=["raise_prices_8"],
            depends=["price_start"],
        ),
        TimelineItem(
            name="debt_done",
            label="Debt work shows an effect",
            kind="milestone",
            emoji="🧹",
            month="ms_debt",
            condition="debt_ready <= ms_debt",
            show="debt_ready",
            options=["pay_tech_debt"],
            depends=["debt_start"],
        ),
        TimelineItem(
            name="hires_ready",
            label="New hires productive",
            kind="milestone",
            emoji="👥",
            month="ms_hire",
            condition="hire_ready <= ms_hire",
            show="hire_ready",
            options=["hire_5"],
            depends=["hire_start"],
        ),
        TimelineItem(
            name="gpu_ready",
            label="GPU capacity ready",
            kind="milestone",
            emoji="🖥️",
            month="ms_gpu",
            condition="gpu_ready <= ms_gpu",
            show="gpu_ready",
            options=["reserve_gpu"],
            depends=["gpu_start"],
        ),
        TimelineItem(
            name="cash_goal",
            label="Cash at year end at least the goal",
            kind="goal",
            emoji="💰",
            month=12,
            condition="cash >= g_cash",
        ),
        TimelineItem(
            name="churn_goal",
            label="Churn at most the goal",
            kind="goal",
            emoji="🤝",
            month=12,
            condition="churn <= g_churn",
        ),
    ],
    # each lever: start by month X at the latest (prefer the later start), only for its option
    levers=[
        Lever(
            variable="m_price",
            label="Announce the new prices by month",
            low=0,
            high=6,
            step=0.5,
            unit="month",
            prefer="high",
            options=["raise_prices_8"],
        ),
        Lever(
            variable="m_debt",
            label="Start the debt work by month",
            low=0,
            high=6,
            step=0.5,
            unit="month",
            prefer="high",
            options=["pay_tech_debt"],
        ),
        Lever(
            variable="m_hire",
            label="Start hiring by month",
            low=0,
            high=6,
            step=0.5,
            unit="month",
            prefer="high",
            options=["hire_5"],
        ),
        Lever(
            variable="m_gpu",
            label="Order the GPU reservation by month",
            low=0,
            high=6,
            step=0.5,
            unit="month",
            prefer="high",
            options=["reserve_gpu"],
        ),
    ],
)

TIMELINES = {"delivery_model": DELIVERY_TIMELINE, "company_model": COMPANY_TIMELINE}


def progress_milestone(name: str, label: str, share: float, month: float) -> TimelineItem:
    """A milestone of your own: ``share`` of the work done by ``month`` (the page adds the same kind)."""
    done = done_at(f"{share} * work")
    return TimelineItem(
        name=name,
        label=label,
        kind="milestone",
        emoji="🧩",
        month=month,
        condition=f"{done} <= {month}",
        show=done,
    )
