"""P12 reporting: the weekly company metrics page and the monthly update to each key client.

- Every Monday Finance publishes a Confluence page labelled ``report`` with the page
  properties of the dashboard's North Star: ARR, active clients, new and churned ARR, cash
  (all from the generated records, after the ARR calibration).
- On the first working day of a month the account manager of each key client sends a short
  update that names last month's delivered work by its summary, without the ticket key: an
  implicit reference (gold link ``refers_to``, ``explicit: false``).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from variator.orgsim.company.model import Money, World
from variator.orgsim.company.processes.finance import Finance

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine


class Reports:
    def __init__(self, w: World, eng: Engine, finance: Finance):
        self.w, self.eng, self.fin = w, eng, finance
        self.implicit: list[dict[str, Any]] = []

    def arr(self, d: date) -> tuple[float, int]:
        s, n = 0.0, 0
        for c in self.w.clients.values():
            if c.active_on(d):
                s += Money(c.acv_on(d), 1200).value(self.fin.scale)
                n += 1
        return s, n

    def flows(self, a: date, b: date) -> tuple[float, float]:
        """New and churned ARR with effect in [a, b)."""
        new = lost = 0.0
        f = self.fin.scale
        for c in self.w.clients.values():
            prev = 0.0
            for since, acv in c.schedule:
                v = Money(acv, 1200).value(f)
                if a <= since < b and (c.end is None or since < c.end):
                    if v > prev:
                        new += v - prev
                    else:
                        lost += prev - v
                prev = v
            if c.end is not None and a <= c.end < b:
                lost += Money(c.acv_on(c.end - timedelta(days=1)), 1200).value(f)
        return new, lost

    def cash(self, at: datetime) -> float:
        rows = self.w.datasets.get("bank", [])
        bal = self.fin.opening
        for r in rows:
            if datetime.fromisoformat(r["ts"]) < at:
                bal = r["balance_after"]
        return bal

    def run(self) -> None:
        self._weekly()
        self._client_updates()

    def _weekly(self) -> None:
        w, eng = self.w, self.eng
        seen: set[tuple[int, int]] = set()
        for d in w.days:
            week = d.isocalendar()[:2]
            if week in seen:
                continue
            seen.add(week)
            a, b = d - timedelta(days=7), d
            arr, n = self.arr(d)
            new, lost = self.flows(a, b)
            cash = self.cash(w.cal.at(d, "00:00"))
            author = w.first_present([eng.head("finance"), eng.head("ceo"), eng.head("delivery")], d)
            if author is None:
                continue
            ts = w.stamp(author, d, lo=0.05, hi=0.25, purpose="report")
            period = f"{a:%Y-%m-%d} - {(b - timedelta(days=1)):%Y-%m-%d}"
            props = [
                ("Period", period),
                ("Type", "weekly metrics report"),
                ("ARR, $k", f"{arr / 1000:.0f}"),
                ("Active clients", str(n)),
                ("New ARR, $k", f"{new / 1000:.0f}"),
                ("Churned ARR, $k", f"{lost / 1000:.0f}"),
                ("Cash, $k", f"{cash / 1000:.0f}"),
            ]
            body = (
                f"Weekly company metrics for {a:%d %b} to {(b - timedelta(days=1)):%d %b %Y}. "
                f"ARR is ${arr / 1e6:.2f}M across {n} active clients; cash is ${cash / 1e6:.2f}M."
            )
            w.page(
                ts,
                author,
                "MGMT",
                f"Company metrics: week of {a:%d %b %Y}",
                props,
                body,
                ["report"],
                process="reporting",
                kind="weekly_report",
            )
            w.row(
                "metrics_weekly",
                week_start=a.isoformat(),
                published=ts.isoformat(),
                arr=round(arr, 2),
                active_clients=n,
                new_arr=round(new, 2),
                churned_arr=round(lost, 2),
                cash=round(cash, 2),
            )

    def _client_updates(self) -> None:
        w = self.w
        counts = Counter(i.summary for i in w.issues)
        firsts = [d for k, d in enumerate(w.days) if k > 0 and w.days[k - 1].month != d.month]
        for d in firsts:
            prev = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
            for cid in sorted(w.clients):
                c = w.clients[cid]
                if not c.profile.key or not c.active_on(d):
                    continue
                am = c.profile.account_manager
                if not w.present(am, d):
                    continue
                ts = w.stamp(am, d, lo=0.3, hi=0.8, purpose="updates")
                done = [
                    i
                    for i in w.issues
                    if i.done_at
                    and prev <= i.done_at.date() < d.replace(day=1)
                    and i.status == "Done"
                    and i.itype in ("Story", "Task", "Bug")
                    and counts[i.summary] == 1
                    and i.done_at < ts
                    and i.project in ("PLAT", "IMPL")
                    and (i.client in (None, cid))
                ]
                own = [i for i in done if i.client == cid]
                pick = (own + [i for i in done if i.itype == "Story" and i.client is None])[:3]
                if not pick:
                    continue
                items = "; ".join(i.summary[0].lower() + i.summary[1:] for i in pick)
                main = (c.profile.contacts[0].name, self.eng.clients.contact_email(c, 0))
                mail = w.mail(
                    ts,
                    w.addr(am),
                    [main],
                    f"Monthly update for {c.profile.name}: {prev:%B %Y}",
                    f"Hello {main[0].split()[0]}, here is what we delivered in {prev:%B}: {items}. "
                    f"Let me know if you have questions.",
                    process="reporting",
                    kind="client_update",
                    slot_kind="client_update",
                )
                for i in pick:
                    quote = i.summary[0].lower() + i.summary[1:]
                    self.implicit.append({"mail": mail, "issue": i, "quote": quote})
