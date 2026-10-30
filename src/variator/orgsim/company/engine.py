"""The day loop of the history and the cause catalog.

``Engine.run`` steps every working day from T - 12 months to T - 1 and calls the processes
in a fixed order: people, clients, sales, operations, support, NPS, work. Couplings go
through the shared ``World`` state (team load, incidents per client, upsells, vacancies)
and the monitoring series (incident downtime and latency per service).

The cause catalog: ``process`` and ``rule`` records that the business records cite as causes
(the KB adapter needs every cause id to be a delivered record).
"""

from __future__ import annotations

from datetime import timedelta

from variator.orgsim.company.model import Record, World

PROCESSES = {
    "PROC-ROADMAP": (
        "LW-E03",
        "Sprint planning",
        "The delivery team plans two-week sprints. Committed client work goes first; roadmap work fills "
        "each engineer close to full capacity.",
    ),
    "PROC-BILLING": (
        "LW-E02",
        "Client billing",
        "Client invoices go out in advance at the start of each billing period, monthly or quarterly.",
    ),
    "PROC-PAYROLL": (
        "LW-E02",
        "Monthly payroll",
        "Salaries are paid on the 25th of each month, or on the working day before it.",
    ),
    "PROC-VENDOR-BILLS": (
        "LW-E02",
        "Vendor bills",
        "Finance checks each vendor bill and pays it on its due date.",
    ),
    "PROC-CLOUD": (
        "LW-E03",
        "Cloud billing",
        "The cloud provider bills the usage of a month early in the next month; a direct debit pays it.",
    ),
    "PROC-DEPLOY": (
        "LW-E12",
        "Continuous delivery",
        "Merged changes go to production through the CI/CD pipeline several times a day.",
    ),
    "PROC-INCIDENT": (
        "LW-E03",
        "Incident response",
        "The on-call engineer handles an incident; SEV1 and SEV2 incidents get a post-incident review.",
    ),
    "PROC-SUPPORT": (
        "LW-E16",
        "Client support",
        "Client requests reach the support team by e-mail or the portal; bugs go to engineering.",
    ),
    "PROC-SALES": (
        "LW-E18",
        "New business",
        "A deal moves through qualification, proposal and negotiation before the contract is signed.",
    ),
    "PROC-RENEWAL": (
        "LW-E18",
        "Contract renewal",
        "A contract renews at the end of its term unless the client gives notice.",
    ),
    "PROC-HIRING": (
        "LW-E02",
        "Hiring",
        "A vacancy opens after the CEO approves the hire; Finance and HR run the requisition.",
    ),
    "PROC-IMPLEMENTATION": (
        "LW-E04",
        "Client implementation",
        "Each new client and each upsell gets an implementation project with a promised go-live date.",
    ),
    "PROC-REPORTING": ("LW-E02", "Weekly metrics", "Finance publishes the company metrics every Monday."),
    "PROC-NPS": (
        "LW-E18",
        "Client NPS survey",
        "Each quarter the main and billing contacts of every active client get a short NPS survey. "
        "The account manager follows up each score of 6 or less.",
    ),
    "PROC-OPENING": (
        "LW-E02",
        "Opening balances",
        "Records that were open at the start of the history: contracts, open invoices and bills, "
        "deals in progress.",
    ),
}
RULES = {
    "RULE-NET30": (
        "LW-E02",
        "Net 30 payment terms",
        "Client invoices are due 30 days after issue. A reminder goes out 7 days after the due date.",
    ),
    "RULE-BURNOUT": (
        "LW-E03",
        "Burnout",
        "A team over capacity loses more people: a load above 1.05 raises attrition.",
    ),
    "RULE-LEAVE": (
        "LW-E02",
        "Annual leave",
        "Each employee has 25 days of annual leave plus bank holidays, booked in advance.",
    ),
    "RULE-SICK": ("LW-E02", "Sick leave", "Sickness is reported on its first day."),
    "RULE-NOTICE": ("LW-E02", "Notice period", "An employee gives one month of notice."),
    "RULE-SALARY-REVIEW": ("LW-E02", "Salary reviews", "Salaries are reviewed each quarter."),
    "RULE-PAYE": (
        "LW-E02",
        "PAYE and National Insurance",
        "Payroll taxes of a month are paid to HM Revenue & Customs by the 22nd of the next month.",
    ),
    "RULE-CORP-TAX": ("LW-E02", "Corporation tax", "Corporation tax is paid in quarterly instalments."),
    "RULE-SLA": (
        "LW-E03",
        "Availability target",
        "The platform availability target in client contracts is 99.5%.",
    ),
    "RULE-CLIENT-NOTICE": (
        "LW-E18",
        "Client notice",
        "A client gives notice before leaving; the service stops at the end of a billing period.",
    ),
}
HEADS = {"ceo": "LW-E01", "finance": "LW-E02", "delivery": "LW-E03"}
DEPT_HEAD = {
    "backend": "LW-E03",
    "frontend": "LW-E03",
    "devops": "LW-E03",
    "qa": "LW-E03",
    "pm": "LW-E03",
    "analyst_designer": "LW-E03",
    "support": "LW-E03",
    "sales": "LW-E01",
    "marketing": "LW-E01",
    "finance": "LW-E01",
    "delivery": "LW-E01",
    "ceo": "LW-E01",
}


class Engine:
    def __init__(self, w: World):
        from variator.orgsim.company.processes.clients import Clients
        from variator.orgsim.company.processes.monitoring import Monitoring
        from variator.orgsim.company.processes.nps import Nps
        from variator.orgsim.company.processes.ops import Ops
        from variator.orgsim.company.processes.people import People
        from variator.orgsim.company.processes.sales import Sales
        from variator.orgsim.company.processes.support import Support
        from variator.orgsim.company.processes.work import Work

        self.w = w
        self.catalog: dict[str, Record] = {}
        self._catalog()
        self.people = People(w, self)
        self.work = Work(w, self)
        self.clients = Clients(w, self)
        self.sales = Sales(w, self)
        self.ops = Ops(w, self)
        self.support = Support(w, self)
        self.monitoring = Monitoring(w, self)
        self.nps = Nps(w, self)

    # ------------------------------------------------------------------ catalog
    def _catalog(self) -> None:
        w = self.w
        t = w.at(w.days[0], "05:00")
        for n, (pid, (owner, title, text)) in enumerate(sorted(PROCESSES.items())):
            self.catalog[pid] = w.record(
                "process",
                "company",
                t + timedelta(minutes=n),
                "governance",
                "process_defined",
                prefix="PROC",
                ext=pid,
                title=title,
                statement=text,
                owner=owner,
                status="active",
            )
        for n, (rid, (owner, title, text)) in enumerate(sorted(RULES.items())):
            self.catalog[rid] = w.record(
                "rule",
                "company",
                t + timedelta(minutes=30 + n),
                "governance",
                "rule_adopted",
                prefix="RULE",
                ext=rid,
                title=title,
                rules=text,
                owner=owner,
                status="active",
            )

    def proc(self, key: str) -> tuple[str, Record]:
        return ("process", self.catalog[key])

    def rule(self, key: str) -> tuple[str, Record]:
        return ("rule", self.catalog[key])

    # ------------------------------------------------------------------ roles
    def head(self, key: str) -> str:
        return HEADS[key]

    def dept_head(self, role: str) -> str:
        return DEPT_HEAD.get(role, "LW-E01")

    def role_ids(self, role: str, d) -> list[str]:
        return sorted(self.w.employed_ids(d, (role,)))

    # ------------------------------------------------------------------ the loop
    def run(self) -> None:
        w = self.w
        self.people.setup()
        self.clients.setup()
        self.sales.setup()
        for i, d in enumerate(w.days):
            w.day, w.day_i = d, i
            self.people.step(i, d)
            self.clients.step(i, d)
            self.sales.step(i, d)
            self.ops.step(i, d)
            self.support.step(i, d)
            self.nps.step(i, d)
            self.people.biz(i, d)
            self.work.step(i, d)
        self.work.finish()
