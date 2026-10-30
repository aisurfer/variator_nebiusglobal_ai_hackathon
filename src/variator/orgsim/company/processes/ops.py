"""P6 operations: deploys and incidents.

- Deploys: a counting process with ``deploy_freq`` per week; each deploy ships the merge
  requests merged since the last one.
- Incidents: a counting process with ``incidents_base`` per month x (team load / target load)
  (company_model: incidents grow with the load). A share ``incident_deploy_share`` is caused
  by the latest deploy (and one of its merge requests); the rest by the cloud provider or
  other outside causes. Time to restore: log-normal around
  ``mttr_h`` x (10 / deploy_freq) ^ k_mttr_deploy x (1 + 0.5 x key person gone) x severity.

Couplings: affected clients get a churn bump; support tickets follow; a regression gets a
Jira bug "is caused by" the merge request; SEV1 and SEV2 get a post-incident review. The
incident takes down the ``services`` of its footprint for ``downtime_minutes``
(``monitoring``); a cloud capacity life event starts a GPU shortage that raises latency.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from variator.orgsim.company import templates as T
from variator.orgsim.company.model import MergeRequest, Record, World

if TYPE_CHECKING:
    from variator.orgsim.company.engine import Engine

SEVERITIES = ("SEV1", "SEV2", "SEV3")
SEV_FACTOR = {"SEV1": 1.6, "SEV2": 1.0, "SEV3": 0.6}
SEV_DOWNTIME = {"SEV1": 1.0, "SEV2": 0.5, "SEV3": 0.2}  # share of the restore time a client cannot work
OUTSIDE = ["cloud provider capacity", "third-party API outage", "traffic spike", "certificate expiry"]


class Ops:
    def __init__(self, w: World, eng: Engine):
        self.w, self.eng = w, eng
        self.deploy_clock = w.clock("ops", "deploys", "arrivals")
        self.incident_clock = w.clock("ops", "incidents", "arrivals")
        self.deploys: list[Record] = []
        self.incidents: list[Record] = []
        self.minor = 12
        self.patch = 0
        self.key_seats = [p.id for p in w.spec.people if p.key]

    def rng(self, entity: str, purpose: str) -> np.random.Generator:
        return self.w.rng("ops", entity, purpose)

    def key_gone(self, d: date) -> bool:
        for seat in self.key_seats:
            if not any(p.seat == seat and p.employed(d) for p in self.w.people.values()):
                return True
        return False

    def oncall(self, d: date) -> str:
        w = self.w
        devops = self.eng.role_ids("devops", d)
        week = d.isocalendar()[1]
        order = devops[week % len(devops) :] + devops[: week % len(devops)] if devops else []
        return w.first_present(
            order + [self.eng.work.lead(d), self.eng.head("delivery")], d
        ) or self.eng.head("delivery")

    # ================================================================================== the day
    def step(self, i: int, d: date) -> None:
        t = self.w.spec.truth
        n = self.deploy_clock.tick(t.deploy_freq / 5.0)
        if n:
            r = self.rng("deploys", "times")
            times = sorted(float(x) for x in r.uniform(0.15, 0.95, n))
            for x in times:
                self._deploy(d, x)
        rate = t.incidents_base * 12 / 252.0 * (self.w.team_load / t.team_load)
        for _ in range(self.incident_clock.tick(rate)):
            self._incident(i, d)

    def _deploy(self, d: date, frac: float) -> None:
        w = self.w
        who = self.oncall(d)
        ts = w.stamp(who, d, lo=frac - 0.05, hi=frac)
        mrs = [m for m in self.eng.work.merged if m.merged_at and m.merged_at < ts]
        self.eng.work.merged = [m for m in self.eng.work.merged if m not in mrs]
        self.patch += 1
        if self.patch > 9:
            self.minor, self.patch = self.minor + 1, 0
        version = f"v4.{self.minor}.{self.patch}"
        comps = [m.issue.component for m in mrs if m.issue.component]
        component = max(set(comps), key=lambda c: (comps.count(c), c)) if comps else "platform"
        rec = w.record(
            "deploy",
            "engineering",
            ts,
            "operations",
            "deployed",
            (self.eng.proc("PROC-DEPLOY"),),
            prefix="DEP",
            title=f"Deploy {version}",
            component=component,
            version=version,
            environment="production",
            status="succeeded",
            author=who,
            deployed_at=ts.isoformat(),
            changes=len(mrs),
            related=list(mrs)[:25],
            actor=who if w.present(who, d) else None,
        )
        r = self.rng("deploys", "notes")
        for m in mrs:
            m.deployed_in = rec
            if w.present(who, d) and r.random() < 0.6 and m.issue.status in ("Done", "In QA"):
                self.eng.work.comment(
                    m.issue,
                    ts + timedelta(minutes=3),
                    who,
                    f"Deployed to production in {version}.",
                    "deploy_note",
                    (("process", rec),),
                )
        self.deploys.append(rec)

    def _incident(self, i: int, d: date) -> None:
        w, t = self.w, self.w.spec.truth
        r = self.rng("incidents", f"n{len(self.incidents) + 1}")
        start = w.cal.at(d, "07:00") + timedelta(minutes=int(r.integers(0, 14 * 60)))
        recent = [x for x in self.deploys if x.first_ts < start and (start - x.first_ts) < timedelta(days=3)]
        deploy, mr = None, None
        if recent and r.random() < t.incident_deploy_share:
            deploy = recent[-1]
            mrs = [m for m in deploy.state.get("related", []) if isinstance(m, MergeRequest)]
            mr = mrs[int(r.integers(len(mrs)))] if mrs else None
        sev = str(r.choice(SEVERITIES, p=list(t.severity_mix)))
        component = mr.issue.component if mr and mr.issue.component else str(T.pick(r, w.spec.components))
        symptom = str(T.pick(r, T.INCIDENT_SYMPTOMS))
        clients = [c for c in w.clients.values() if c.active_on(d)]
        clients.sort(key=lambda c: c.id)
        if sev == "SEV1":
            k = max(1, int(len(clients) * r.uniform(0.15, 0.3)))
        elif sev == "SEV2":
            k = int(r.integers(4, 13))
        else:
            k = int(r.integers(1, 4))
        k = min(k, len(clients))
        wts = np.array([math.sqrt(c.acv) for c in clients])
        idx = r.choice(len(clients), size=k, replace=False, p=wts / wts.sum()) if clients else []
        hit = sorted((clients[int(j)] for j in idx), key=lambda c: -c.acv)
        responder = self.oncall(d)
        mttr = t.mttr_h * (10.0 / t.deploy_freq) ** t.k_mttr_deploy * (1 + 0.5 * self.key_gone(d))
        sigma = 0.6
        mean_factor = sum(p * SEV_FACTOR[s] for p, s in zip(t.severity_mix, SEVERITIES, strict=True))
        minutes = max(
            10.0, 60 * mttr * SEV_FACTOR[sev] / mean_factor * math.exp(r.normal(0, sigma) - sigma**2 / 2)
        )
        detected = start + timedelta(minutes=int(r.integers(3, 16)))
        resolved = start + timedelta(minutes=int(minutes))
        causes: tuple = ()
        cause_kind = "deploy" if deploy else str(T.pick(r, OUTSIDE))
        if deploy is not None:
            causes = (("process", deploy),)
        elif cause_kind == "cloud provider capacity":
            cloud = w.spec.vendor("cloud")
            le = w.record(
                "life_event",
                "engineering",
                start - timedelta(minutes=1),
                "operations",
                "life_event_happened",
                (),
                prefix="LE",
                title=f"{cloud.name} capacity shortage",
                statement=f"{cloud.name} runs short of GPU capacity in the region of the platform.",
                vendor=cloud.id,
                effective_date=d.isoformat(),
                event_type="vendor_capacity",
                polarity="negative",
            )
            causes = (("life_event", le),)
            self.eng.monitoring.gpu_event(le, d)
        services = self.eng.monitoring.footprint(component, sev, r)
        title = f"{component}: {symptom}"
        base = dict(
            title=title,
            severity=sev,
            status="open",
            component=component,
            services=services,
            started_at=start.isoformat(),
            detected_at=detected.isoformat(),
            client=hit[0].id if hit else None,
            affected_clients=[c.id for c in hit],
            affected_count=len(hit),
            cause_kind=cause_kind,
            owner=self.eng.head("delivery"),
            assignee=responder,
            project="platform",
        )
        if deploy is not None:
            base["related"] = [deploy] + ([mr] if mr else [])
        rec = w.record(
            "incident", "engineering", detected, "operations", "incident_opened", causes, prefix="INC", **base
        )
        self.incidents.append(rec)
        downtime = minutes * SEV_DOWNTIME[sev]
        rec.update(
            resolved,
            "operations",
            "incident_resolved",
            status="resolved",
            resolved_at=resolved.isoformat(),
            restore_minutes=int(minutes),
            downtime_minutes=int(downtime),
        )
        self.eng.monitoring.add_incident(rec, start, int(downtime), int(minutes), services, symptom)
        for c in hit:
            c.bumps.append((i, {"SEV1": 1.0, "SEV2": 0.5, "SEV3": 0.15}[sev], rec))
            w.row(
                "incident_impact",
                incident=rec,
                client=c.id,
                date=d.isoformat(),
                severity=sev,
                downtime_minutes=round(downtime, 1),
            )
        if deploy is not None and sev == "SEV1":
            deploy.update(
                start + timedelta(minutes=20),
                "operations",
                "deploy_rolled_back",
                (("process", rec),),
                status="rolled_back",
            )
        self.eng.support.incident_tickets(rec, hit, d, detected)
        self._follow_up(rec, sev, mr, component, symptom, d, resolved, hit)

    def _follow_up(
        self,
        rec: Record,
        sev: str,
        mr: MergeRequest | None,
        component: str,
        symptom: str,
        d: date,
        resolved: datetime,
        hit: list,
    ) -> None:
        w, work = self.w, self.eng.work
        i = w.index.get(d, 0)
        day = d if resolved.date() == d and resolved.hour < 17 else w.bday(d, 1)
        if day >= w.anchor:
            return
        related = list(rec.state.get("related", []))
        if mr is not None:
            author = (
                mr.author
                if w.people[mr.author].employed(day) and not work._leaving(mr.author, day)
                else work.owner(component, day)
            )
            reporter = w.first_present(
                [rec.state["assignee"], work.lead(day), self.eng.head("delivery")], day
            )
            if author and reporter:
                ts = w.stamp(reporter, day, after=resolved if resolved.date() == day else None)
                bug = work.new_issue(
                    "PLAT",
                    "Bug",
                    f"Regression: {symptom} in {component}",
                    ts,
                    reporter,
                    assignee=author,
                    component=component,
                    points=int(self.rng("incidents", "bug").choice([1, 2, 3])),
                    priority="Highest",
                    labels=("incident", "regression"),
                    causes=(("process", rec),),
                    description=f"Regression found in incident {sev} on {d:%d %b}.",
                )
                work.remote_link(bug, ts + timedelta(minutes=1), reporter, mr)
                work.add_unplanned(bug, author, ts)
                related.append(bug)
        if sev in ("SEV1", "SEV2"):
            head = self.eng.head("delivery")
            pir = work.biz_ticket(
                head,
                day,
                w.index.get(day, i),
                f"Post-incident review: {rec.state['title']}",
                causes=(("process", rec),),
                labels=("postmortem",),
                days=(2, 5),
                after=resolved if resolved.date() == day else None,
            )
            if pir is not None:
                related.append(pir)
        if related != rec.state.get("related", []):
            rec.update(
                max(resolved, rec.last_ts) + timedelta(minutes=30),
                "operations",
                "incident_followed_up",
                related=related,
            )
        for c in hit:
            if not c.profile.key:
                continue
            am = c.profile.account_manager
            if not w.present(am, day):
                continue
            main = (c.profile.contacts[0].name, self.eng.clients.contact_email(c, 0))
            ts = w.stamp(am, day, after=resolved if resolved.date() == day else None)
            w.mail(
                ts,
                w.addr(am),
                [main],
                f"Service incident on {d:%d %B}: resolved",
                f"The {symptom} that affected your users on {d:%d %B} is resolved. We will share the review.",
                process="operations",
                kind="incident_notice",
                slot_kind="incident_notice",
            )
