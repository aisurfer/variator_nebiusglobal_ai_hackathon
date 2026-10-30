"""P14 client NPS: quarterly surveys, the responses and their effect on churn.

- Survey: on the second Tuesday (or the next working day) of each month in
  ``nps_survey_months`` every active client gets the survey. The main contact answers with
  ``nps_response_main``, the billing contact with ``nps_response_billing`` (both x the client
  habit ``nps_response``). A response comes 1 to 15 working days later
  (mean ``nps_reply_bdays``). Responses after T are not delivered.
- Level: the expected NPS of the client (company_model ``nps``) from its experience in the
  ``nps_window_days`` before the survey::

      level = nps_base + habit
              - k_nps_sla x sum of weekly SLA gaps x recency x incident sensitivity
              - k_nps_overdue x overdue
              + k_nps_support x (support quality seen - support_quality)

  The weekly SLA of the client = 1 - its incident downtime / minutes of the week - the
  latency term of company_model (``monitoring``). Overdue = working days in the window with
  a late implementation / ``nps_overdue_full_bdays`` (at most 1). Support quality seen =
  ``support_quality`` + (mean ticket experience - ``nps_csat_ref``) / 2, shrunk with few
  tickets; a ticket's experience is its CSAT, or the CSAT that its first-response and
  resolution times give (``support``).
- Score: 0..10 from a normal around a mean score; the mean score is the one whose expected
  NPS (promoters - detractors) is the level. A billing contact adds ``nps_billing_offset``.
- Record ``nps_response``: client, contact, survey, score, category, sent_at, responded_at,
  comment_slot. Causes: PROC-NPS, and for a passive or a detractor the incidents, the late
  implementation or the support tickets that lowered the level (only when the term is large).
- Couplings: a detractor gets a follow-up BIZ ticket of the account manager (caused by the
  response). In the ``nps_renewal_window_days`` before the term end, the client NPS signal
  of its latest survey adds churn (company_model ``k_churn_nps``):
  p = k_churn_nps x max(nps_churn_floor - signal, 0) over the window, where the signal is
  the client's NPS shrunk to ``nps_prior`` with the weight ``nps_shrink``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from variator.orgsim.company.model import ClientState, Issue, Record, TextSlot, World

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

MAX_LAG_BDAYS = 15


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expected_nps(mu: float, sd: float) -> float:
    """Expected promoters - detractors (-1..1) of scores round(mu + sd x N(0, 1)) clipped to 0..10."""
    return (1.0 - _phi((8.5 - mu) / sd)) - _phi((6.5 - mu) / sd)


def mean_score(nps: float, sd: float) -> float:
    """The mean score whose expected NPS is ``nps`` (-1..1)."""
    lo, hi = -6.0, 17.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if expected_nps(mid, sd) < nps:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def category(score: int) -> str:
    return "promoter" if score >= 9 else "detractor" if score <= 6 else "passive"


def nps_of(scores: list[int]) -> float | None:
    if not scores:
        return None
    p = sum(1 for s in scores if s >= 9)
    d = sum(1 for s in scores if s <= 6)
    return 100.0 * (p - d) / len(scores)


class Nps:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        self.overdue: dict[str, list[tuple[date, Issue]]] = defaultdict(list)
        self.tickets: dict[str, list[tuple[datetime, float, Record]]] = defaultdict(list)
        self.pending: list[tuple[datetime, int, dict[str, Any]]] = []
        self.latest: dict[str, tuple[str, list[Record]]] = {}
        self.surveys: list[dict[str, Any]] = []
        self.levels: dict[str, dict[str, dict[str, Any]]] = {}
        self.followups = 0
        self._seq = 0
        self._weeks: dict[date, list[tuple[date, date, float]]] = {}
        self.days = self._survey_days()

    def _survey_days(self) -> dict[date, str]:
        w, t = self.w, self.w.spec.truth
        out: dict[date, str] = {}
        for y in range(w.start.year, w.anchor.year + 1):
            for m in t.nps_survey_months:
                first = date(y, m, 1)
                second_tuesday = first + timedelta(days=(1 - first.weekday()) % 7 + 7)
                d = w.cal.roll_forward(second_tuesday)
                if w.start <= d < w.anchor:
                    out[d] = f"{y}-Q{(m - 1) // 3 + 1}"
        return out

    # ------------------------------------------------------------------ inputs
    def ticket_resolved(
        self, c: ClientState, ts: datetime, csat: int | None, first_h: float, total_d: float, rec: Record
    ) -> None:
        """The experience of a resolved ticket: its CSAT, or the CSAT that its times give."""
        t = self.w.spec.truth
        if csat is not None:
            x = float(csat)
        else:
            x = 4.7 - 0.4 * (first_h > 4) - 0.5 * (total_d > 3) + 2.0 * (t.support_quality - 0.8)
        self.tickets[c.id].append((ts, x, rec))

    # ------------------------------------------------------------------ the day
    def step(self, i: int, d: date) -> None:
        w = self.w
        for cid in sorted(w.clients):
            c = w.clients[cid]
            if c.overdue and c.implementation is not None and c.active_on(d):
                self.overdue[cid].append((d, c.implementation))
        if d in self.days:
            self._send(d, self.days[d])
        self._respond(i, d)

    # ------------------------------------------------------------------ the level of a client
    def _week_spans(self, d: date) -> list[tuple[date, date, float]]:
        """Weeks (Monday to Sunday) of the window before ``d`` with the latency term of each."""
        if d in self._weeks:
            return self._weeks[d]
        w, t = self.w, self.w.spec.truth
        mon = self.eng.monitoring
        a0 = max(d - timedelta(days=t.nps_window_days), w.start)
        out = []
        m = a0 - timedelta(days=a0.weekday())
        while m < d:
            a, b = max(m, a0), min(m + timedelta(days=7), d)
            days = [a + timedelta(days=k) for k in range((b - a).days)]
            if days:
                lat = float(np.mean([mon.platform_p95(x) for x in days]))
                out.append((a, b, 0.002 * max(lat - t.latency_slo, 0.0) / 50))
            m += timedelta(days=7)
        self._weeks[d] = out
        return out

    def level(self, c: ClientState, d: date) -> dict[str, Any]:
        """The expected NPS of client ``c`` at a survey on day ``d`` and the drivers of it."""
        w, t = self.w, self.w.spec.truth
        a0 = max(d - timedelta(days=t.nps_window_days), w.start)
        # SLA gaps the client saw: its incidents per week
        incs: dict[int, list[Record]] = defaultdict(list)
        spans = self._week_spans(d)
        for _, _, inc in c.bumps:
            day = datetime.fromisoformat(inc.state["started_at"]).date()
            for n, (a, b, _) in enumerate(spans):
                if a <= day < b:
                    incs[n].append(inc)
        weeks = []
        sla_pts = 0.0
        for n, (a, b, lat_pen) in enumerate(spans):
            down = sum(float(x.state.get("downtime_minutes", 0)) for x in incs.get(n, []))
            sla = 1.0 - down / (1440.0 * (b - a).days) - lat_pen
            gap = max(t.sla_target - sla, 0.0)
            if gap <= 0:
                continue
            mid = a + (b - a) / 2
            pts = (
                t.k_nps_sla
                * gap
                * math.exp(-(d - mid).days / t.nps_recency_days)
                * c.habits.incident_sensitivity
            )
            sla_pts += pts
            weeks.append({"from": a, "sla": sla, "points": pts, "incidents": incs.get(n, [])})
        sla_pts = min(sla_pts, 100.0)
        # late implementation
        late = [(x, e) for x, e in self.overdue.get(c.id, []) if a0 <= x < d]
        overdue = min(len(late) / t.nps_overdue_full_bdays, 1.0)
        overdue_pts = t.k_nps_overdue * overdue
        # support quality seen
        tix = [(x, rec) for ts, x, rec in self.tickets.get(c.id, []) if a0 <= ts.date() < d]
        sq_delta = 0.0
        if tix:
            mean_x = float(np.mean([x for x, _ in tix]))
            shrink = len(tix) / (len(tix) + 2.0)
            sq_delta = float(np.clip(shrink * (mean_x - t.nps_csat_ref) / 2.0, -0.2, 0.2))
        support_pts = t.k_nps_support * sq_delta
        lvl = t.nps_base + c.habits.nps_level - sla_pts - overdue_pts + support_pts
        return {
            "level": float(np.clip(lvl, -95.0, 95.0)),
            "habit": c.habits.nps_level,
            "sla_points": sla_pts,
            "overdue": overdue,
            "overdue_points": overdue_pts,
            "support_quality_seen": t.support_quality + sq_delta,
            "support_points": support_pts,
            "tickets": len(tix),
            "weeks": weeks,
            "epic": late[-1][1] if late else None,
            "bad_tickets": [rec for x, rec in sorted(tix, key=lambda y: y[0]) if x <= 3.5][:2],
        }

    def _causes(self, lv: dict[str, Any], score: int) -> tuple:
        causes: tuple = (self.eng.proc("PROC-NPS"),)
        if score >= 9:
            return causes
        hit = [x for x in sorted(lv["weeks"], key=lambda x: -x["points"]) if x["points"] >= 3.0]
        seen: list[Record] = []
        for x in hit:
            for inc in x["incidents"]:
                if inc not in seen:
                    seen.append(inc)
        causes += tuple(("process", inc) for inc in seen[:3])
        if lv["overdue_points"] >= 8.0 and lv["epic"] is not None:
            causes += (("process", lv["epic"]),)
        if lv["support_points"] <= -5.0:
            causes += tuple(("process", rec) for rec in lv["bad_tickets"])
        return causes

    # ------------------------------------------------------------------ the survey
    def _send(self, d: date, survey: str) -> None:
        w, t = self.w, self.w.spec.truth
        sd = t.nps_score_sd
        levels: dict[str, dict[str, Any]] = {}
        sent = 0
        for cid in sorted(w.clients):
            c = w.clients[cid]
            if not c.active_on(d) or c.status not in ("active", "notice"):
                continue
            lv = self.level(c, d)
            levels[cid] = lv
            mu = mean_score(lv["level"] / 100.0, sd)
            r = w.rng("nps", cid, f"survey-{survey}")
            for k, ct in enumerate(c.profile.contacts):
                sent += 1
                base = t.nps_response_main if ct.kind == "main" else t.nps_response_billing
                p = min(base * c.habits.nps_response, 0.95)
                u, z = float(r.random()), float(r.normal())
                lag = 1 + min(int(r.exponential(max(t.nps_reply_bdays - 1.0, 0.1))), MAX_LAG_BDAYS - 1)
                if u >= p:
                    continue
                day = w.bday(d, lag)
                if day >= w.anchor:
                    continue
                ts = w.at(day, "08:00", 600, ("nps", cid))
                offset = t.nps_billing_offset if ct.kind == "billing" else 0.0
                score = int(min(max(round(mu + offset + sd * z), 0), 10))
                self._seq += 1
                self.pending.append(
                    (
                        ts,
                        self._seq,
                        {"client": c, "k": k, "survey": survey, "sent": d, "score": score, "level": lv},
                    )
                )
        self.levels[survey] = levels
        self.surveys.append(
            {"survey": survey, "sent_at": d.isoformat(), "contacts": sent, "clients": len(levels)}
        )

    def _respond(self, i: int, d: date) -> None:
        w = self.w
        end = w.cal.at(d, "23:59")
        due = sorted((x for x in self.pending if x[0] <= end), key=lambda x: (x[0], x[1]))
        self.pending = [x for x in self.pending if x[0] > end]
        for ts, _, ctx in due:
            c: ClientState = ctx["client"]
            score, survey = ctx["score"], ctx["survey"]
            rec = w.record(
                "nps_response",
                "feedback",
                ts,
                "nps",
                "nps_responded",
                self._causes(ctx["level"], score),
                prefix="NPS",
                client=c.id,
                contact=f"CNT-{c.id}-{ctx['k'] + 1}",
                survey=survey,
                score=score,
                category=category(score),
                sent_at=ctx["sent"].isoformat(),
                responded_at=ts.isoformat(),
                comment_slot=TextSlot("nps_comment"),
            )
            prev = self.latest.get(c.id)
            if prev is None or prev[0] != survey:
                self.latest[c.id] = (survey, [rec])
            else:
                prev[1].append(rec)
            if score <= 6:
                self._follow_up(c, rec, score, i, d, ts)

    def _follow_up(self, c: ClientState, rec: Record, score: int, i: int, d: date, ts: datetime) -> None:
        w, eng = self.w, self.eng
        am = c.profile.account_manager
        if not w.people[am].employed(d):
            am = (eng.role_ids("sales", d) or [eng.head("ceo")])[0]
        issue = eng.work.biz_ticket(
            am,
            d,
            i,
            f"NPS follow-up with {c.profile.name}: score {score}",
            causes=(("process", rec),),
            labels=("nps", "detractor"),
            client=c.id,
            days=(1, 5),
            after=ts,
        )
        if issue is not None:
            self.followups += 1

    # ------------------------------------------------------------------ churn
    def churn_term(self, c: ClientState, d: date) -> tuple[float, float | None, list[Record]]:
        """Hazard per working day that the NPS adds in the renewal window, the signal, low responses."""
        t = self.w.spec.truth
        if c.term_end is None:
            return 0.0, None, []
        left = (c.term_end - d).days
        latest = self.latest.get(c.id)
        if not (0 < left <= t.nps_renewal_window_days) or latest is None:
            return 0.0, None, []
        scores = [int(r.state["score"]) for r in latest[1]]
        n = len(scores)
        obs = nps_of(scores) or 0.0
        signal = (n * obs + t.nps_shrink * t.nps_prior) / (n + t.nps_shrink)
        p = min(t.k_churn_nps * max(t.nps_churn_floor - signal, 0.0), 0.9)
        if p <= 0:
            return 0.0, signal, []
        window = t.nps_renewal_window_days * 252 / 365
        low = sorted(
            (r for r in latest[1] if int(r.state["score"]) <= 8), key=lambda r: int(r.state["score"])
        )
        return -math.log1p(-p) / window, signal, low

    # ------------------------------------------------------------------ results
    def compare(self, sla: float) -> dict[str, Any]:
        """company_model ``nps`` at its defaults and with the realised SLA, overdue share and support."""
        w, t = self.w, self.w.spec.truth
        impl = [(e, p) for _, e, p in self.eng.clients.impl_log if p < w.anchor]
        late = sum(1 for e, p in impl if e.done_at is None or e.done_at.date() > p)
        overdue = late / len(impl) if impl else 0.0
        xs = [x for v in self.tickets.values() for _, x, _ in v]
        sq = t.support_quality + ((float(np.mean(xs)) - t.nps_csat_ref) / 2.0 if xs else 0.0)

        def nps(sla_: float, overdue_: float, sq_: float) -> float:
            return (
                t.nps_base
                - t.k_nps_sla * max(t.sla_target - sla_, 0.0)
                - t.k_nps_overdue * overdue_
                + t.k_nps_support * (sq_ - 0.8)
            )

        capacity = (1 - 0.5 * t.attrition) * (1 - t.blockers) * t.skills / t.team_load
        overdue0 = min(t.overdue_base * (1 + t.tech_debt) / max(capacity, 0.3), 1.0)
        inc0 = t.incidents_base * (1 + t.k_inc_debt * t.tech_debt) * t.team_load
        lat0 = t.latency_base * (1 + t.k_lat_debt * t.tech_debt)
        sla0 = 1 - inc0 * t.mttr_h * 0.1 / 720 - 0.002 * max(lat0 - t.latency_slo, 0.0) / 50
        return {
            "realised_inputs": {
                "sla": round(sla, 6),
                "overdue_share": round(overdue, 3),
                "implementations": len(impl),
                "support_quality": round(sq, 3),
                "ticket_experience_mean": round(float(np.mean(xs)), 3) if xs else None,
            },
            "nps_formula_realised": round(nps(sla, overdue, sq), 1),
            "defaults": {
                "sla": round(sla0, 6),
                "overdue_share": round(overdue0, 3),
                "support_quality": t.support_quality,
            },
            "nps_formula_defaults": round(nps(sla0, overdue0, t.support_quality), 1),
        }

    def summary(self, records: list[Record]) -> dict[str, Any]:
        """NPS per survey of the delivered responses and the mean client level."""
        by: dict[str, list[int]] = defaultdict(list)
        for r in records:
            by[r.versions[-1].snapshot["survey"]].append(int(r.versions[-1].snapshot["score"]))
        out = []
        for s in self.surveys:
            scores = by.get(s["survey"], [])
            lv = self.levels.get(s["survey"], {})
            out.append(
                {
                    **s,
                    "responses": len(scores),
                    "response_rate": round(len(scores) / s["contacts"], 3) if s["contacts"] else 0.0,
                    "promoters": sum(1 for x in scores if x >= 9),
                    "passives": sum(1 for x in scores if 7 <= x <= 8),
                    "detractors": sum(1 for x in scores if x <= 6),
                    "nps": round(nps_of(scores), 1) if scores else None,
                    "mean_score": round(float(np.mean(scores)), 2) if scores else None,
                    "level_mean": round(float(np.mean([x["level"] for x in lv.values()])), 1) if lv else None,
                }
            )
        every = [x for v in by.values() for x in v]
        return {
            "surveys": out,
            "responses": len(every),
            "nps_year": round(nps_of(every), 1) if every else None,
            "followups": self.followups,
        }
