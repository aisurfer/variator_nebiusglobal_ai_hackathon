"""A small synthetic history with hand-computed numbers (UTC company; T = 2026-04-02; start 2026-01-01).

Expected values as of 2026-04-01 (see the tests): cash 400; net burn 202.92 a month; runway 2.0; revenue of
2026-02 = 500; costs 185; gross margin 72.2% (the subcontractor bill BILL-1 is cost of revenue); DSO 60.0;
taxes 30; ARR 1800; new ARR 600; churn 33.33% (not annualised); incidents 2; MTTR 1.5 h; deploys 1.0 a week;
open requisitions 1; headcount 2; late implementations 2; throughput 1; team load 110%.
Rows after T exist in several tables: the loader must drop them.
"""

from __future__ import annotations

import csv
import gzip
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

AS_OF = date(2026, 4, 1)
T = date(2026, 4, 2)


def rec(rid: str, ts: str, kind: str, causes: tuple[str, ...] = (), **fields: Any) -> dict[str, Any]:
    return {"id": rid, "ts": ts, "kind": kind, "process": "test", "causes": list(causes), **fields}


def _bank(rid, ts, booked, amount, direction, balance, **f):
    return rec(
        rid,
        ts,
        "bank_entry_booked",
        amount=amount,
        direction=direction,
        booked_at=booked,
        balance_after=balance,
        currency="USD",
        **f,
    )


SIM: dict[str, list[dict[str, Any]]] = {
    "bank_entry": [
        rec(
            "BNK-00000",
            "2026-01-01T06:30:00Z",
            "opening_balance",
            amount=1000.0,
            direction="credit",
            booked_at="2026-01-01",
            balance_after=1000.0,
            reference="Balance brought forward",
        ),
        _bank(
            "BNK-00001",
            "2026-02-10T10:00:00Z",
            "2026-02-10",
            300.0,
            "credit",
            1300.0,
            counterparty="CL-001",
            counterparty_name="Client One",
            records="PAY-00001",
            reference="INV-1",
        ),
        _bank(
            "BNK-00002",
            "2026-03-05T10:00:00Z",
            "2026-03-05",
            900.0,
            "debit",
            400.0,
            records="PR-2026-02",
            reference="Payroll 2026-02",
        ),
        _bank(
            "BNK-00003",
            "2026-04-05T10:00:00Z",
            "2026-04-05",
            5000.0,
            "credit",
            5400.0,
            counterparty="CL-001",
            counterparty_name="Client One",
            reference="after T",
        ),
    ],
    "payroll": [
        rec(
            "PR-2026-02",
            "2026-02-20T12:00:00Z",
            "payroll_approved",
            amount=100.0,
            employer_tax=15.0,
            period="2026-02",
            status="approved",
            title="Payroll February 2026",
            headcount=2,
            lines=[
                {"employee": "E1", "gross": 60.0, "commission": 0},
                {"employee": "E2", "gross": 40.0, "commission": 0},
            ],
        ),
    ],
    "cloud_bill": [
        rec(
            "CB-2026-02",
            "2026-03-04T07:00:00Z",
            "cloud_bill_received",
            amount=20.0,
            period="2026-02",
            vendor="VN-03",
            vendor_name="Nimbus",
            status="issued",
            gpu_share=0.3,
        ),
    ],
    "invoice": [
        rec(
            "BILL-1",
            "2026-03-02T08:00:00Z",
            "bill_received",
            direction="payable",
            vendor="VN-01",
            vendor_name="Vistula",
            client="LW",
            period="2026-02",
            amount=50.0,
            issued_at="2026-03-02",
            due_date="2026-04-01",
            status="received",
            title="Vistula bill for February 2026",
        ),
        rec(
            "INV-1",
            "2026-02-01T08:00:00Z",
            "invoice_issued",
            direction="receivable",
            client="CL-001",
            client_name="Client One",
            period="2026-02",
            amount=300.0,
            issued_at="2026-02-01",
            due_date="2026-03-03",
            status="issued",
            title="Invoice INV-1",
        ),
        rec(
            "INV-1",
            "2026-02-10T10:30:00Z",
            "invoice_paid",
            direction="receivable",
            client="CL-001",
            client_name="Client One",
            period="2026-02",
            amount=300.0,
            issued_at="2026-02-01",
            due_date="2026-03-03",
            status="paid",
            paid_at="2026-02-10",
            title="Invoice INV-1",
        ),
        rec(
            "INV-2",
            "2026-01-02T08:00:00Z",
            "invoice_issued",
            direction="receivable",
            client="CL-002",
            client_name="Client Two",
            period="2026-01/3m",
            amount=600.0,
            issued_at="2026-01-02",
            due_date="2026-02-01",
            status="issued",
            title="Invoice INV-2",
        ),
        rec(
            "INV-9",
            "2026-04-03T08:00:00Z",
            "invoice_issued",
            direction="receivable",
            client="CL-001",
            client_name="Client One",
            period="2026-04",
            amount=99999.0,
            issued_at="2026-04-03",
            due_date="2026-05-03",
            status="issued",
            title="Invoice after T",
        ),
    ],
    "tax_payment": [
        rec(
            "TAX-PAYE-2026-02",
            "2026-03-01T07:00:00Z",
            "tax_assessed",
            amount=30.0,
            tax="PAYE and NIC",
            period="2026-02",
            due_date="2026-03-22",
            status="scheduled",
            title="PAYE February",
        ),
        rec(
            "TAX-PAYE-2026-02",
            "2026-03-22T07:00:00Z",
            "tax_paid",
            amount=30.0,
            tax="PAYE and NIC",
            period="2026-02",
            due_date="2026-03-22",
            status="paid",
            paid_at="2026-03-22",
            title="PAYE February",
        ),
        rec(
            "TAX-PAYE-2026-03",
            "2026-03-30T07:00:00Z",
            "tax_assessed",
            amount=35.0,
            tax="PAYE and NIC",
            period="2026-03",
            due_date="2026-04-22",
            status="scheduled",
            title="PAYE March",
        ),
    ],
    "contract": [
        rec(
            "CTR-1",
            "2026-01-01T06:00:00Z",
            "opening_balance",
            client="CL-001",
            client_name="Client One",
            start_date="2025-01-01",
            acv_schedule=[{"acv": 1200, "from": "2025-01-01"}],
            status="active",
            title="Client One: services agreement",
        ),
        rec(
            "CTR-1",
            "2026-02-15T10:00:00Z",
            "contract_amended_upsell",
            ("process:DEAL-1",),
            client="CL-001",
            client_name="Client One",
            start_date="2025-01-01",
            deal="DEAL-1",
            status="active",
            acv_schedule=[{"acv": 1200, "from": "2025-01-01"}, {"acv": 1500, "from": "2026-03-01"}],
            last_amendment="upsell: analytics from 2026-03-01",
            title="Client One: services agreement",
        ),
        rec(
            "CTR-2",
            "2026-01-01T06:01:00Z",
            "opening_balance",
            client="CL-002",
            client_name="Client Two",
            start_date="2024-06-01",
            acv_schedule=[{"acv": 600, "from": "2024-06-01"}],
            status="active",
            title="Client Two: services agreement",
        ),
        rec(
            "CTR-2",
            "2026-02-01T09:00:00Z",
            "notice_given",
            ("life_event:LE-1", "rule:RULE-CLIENT-NOTICE"),
            client="CL-002",
            client_name="Client Two",
            start_date="2024-06-01",
            end_date="2026-03-01",
            notice_at="2026-02-01",
            acv_schedule=[{"acv": 600, "from": "2024-06-01"}],
            status="notice",
            title="Client Two: services agreement",
        ),
        rec(
            "CTR-2",
            "2026-03-01T07:00:00Z",
            "contract_terminated",
            ("life_event:LE-1", "rule:RULE-CLIENT-NOTICE"),
            client="CL-002",
            client_name="Client Two",
            start_date="2024-06-01",
            end_date="2026-03-01",
            notice_at="2026-02-01",
            acv_schedule=[{"acv": 600, "from": "2024-06-01"}],
            status="terminated",
            title="Client Two: services agreement",
        ),
        rec(
            "CTR-3",
            "2026-02-10T12:00:00Z",
            "contract_signed",
            ("process:DEAL-2",),
            client="CL-003",
            client_name="Client Three",
            start_date="2026-03-01",
            deal="DEAL-2",
            acv_schedule=[{"acv": 300, "from": "2026-03-01"}],
            status="signed",
            title="Client Three: services agreement",
        ),
        rec(
            "CTR-3",
            "2026-03-20T12:00:00Z",
            "implementation_completed",
            ("process:DEAL-2",),
            client="CL-003",
            client_name="Client Three",
            start_date="2026-03-01",
            deal="DEAL-2",
            acv_schedule=[{"acv": 300, "from": "2026-03-01"}],
            status="active",
            go_live="2026-03-20",
            go_live_late_days=2,
            title="Client Three: services agreement",
        ),
        rec(
            "VA-VN-01",
            "2026-01-01T05:50:00Z",
            "opening_balance",
            contract_type="vendor",
            client="LW",
            vendor="VN-01",
            amount=600.0,
            status="active",
            title="Vistula agreement",
        ),
    ],
    "deal": [
        rec(
            "DEAL-1",
            "2026-02-14T10:00:00Z",
            "deal_won",
            amount=300,
            status="won",
            closed_at="2026-02-14",
            title="Upsell: analytics for Client One",
        ),
        rec(
            "DEAL-2",
            "2026-02-09T10:00:00Z",
            "deal_won",
            amount=300,
            status="won",
            closed_at="2026-02-09",
            title="New business: Client Three",
        ),
    ],
    "life_event": [
        rec(
            "LE-1",
            "2026-02-01T08:00:00Z",
            "life_event_happened",
            client="CL-002",
            event_type="churn",
            statement="Client Two decides to leave: budget cuts.",
            title="Client Two decides to leave",
        ),
    ],
    "rule": [rec("RULE-CLIENT-NOTICE", "2026-01-01T05:00:00Z", "rule_adopted", title="Client notice")],
    "decision": [rec("DEC-1", "2026-03-14T10:00:00Z", "decision_made", title="Hire a backend engineer")],
    "incident": [
        rec(
            "INC-1",
            "2026-03-20T10:00:00Z",
            "incident_opened",
            ("process:DEP-1",),
            started_at="2026-03-20T09:50:00+00:00",
            severity="SEV2",
            component="api",
            status="open",
            title="api: errors",
        ),
        rec(
            "INC-1",
            "2026-03-20T11:00:00Z",
            "incident_resolved",
            ("process:DEP-1",),
            started_at="2026-03-20T09:50:00+00:00",
            severity="SEV2",
            component="api",
            status="resolved",
            resolved_at="2026-03-20T10:50:00+00:00",
            restore_minutes=60,
            downtime_minutes=30,
            title="api: errors",
        ),
        rec(
            "INC-2",
            "2026-03-25T12:01:00Z",
            "incident_resolved",
            started_at="2026-03-25T09:00:00+00:00",
            severity="SEV3",
            component="web-app",
            status="resolved",
            resolved_at="2026-03-25T12:00:00+00:00",
            restore_minutes=180,
            title="web-app: slow pages",
        ),
        rec(
            "INC-3",
            "2026-01-10T09:31:00Z",
            "incident_resolved",
            started_at="2026-01-10T09:00:00+00:00",
            severity="SEV3",
            component="api",
            status="resolved",
            resolved_at="2026-01-10T09:30:00+00:00",
            restore_minutes=30,
            title="api: timeouts",
        ),
        rec(
            "INC-4",
            "2026-04-04T09:00:00Z",
            "incident_opened",
            started_at="2026-04-04T08:00:00+00:00",
            severity="SEV1",
            component="api",
            status="open",
            title="after T",
        ),
    ],
    "deploy": [
        rec(
            f"DEP-{n}",
            f"2026-{d}T10:00:00Z",
            "deployed",
            deployed_at=f"2026-{d}T10:00:00+00:00",
            environment="production",
            component="platform",
            status="succeeded",
            title=f"Deploy v1.{n}",
        )
        for n, d in ((0, "02-01"), (1, "03-10"), (2, "03-15"), (3, "03-20"), (4, "03-25"))
    ],
    "requisition": [
        rec(
            "REQ-1",
            "2026-01-10T10:00:00Z",
            "requisition_opened",
            opened_at="2026-01-10",
            status="open",
            role="QA Engineer",
            department="Engineering",
        ),
        rec(
            "REQ-1",
            "2026-03-01T10:00:00Z",
            "requisition_filled",
            opened_at="2026-01-10",
            status="filled",
            filled_at="2026-03-01",
            role="QA Engineer",
            department="Engineering",
        ),
        rec(
            "REQ-2",
            "2026-03-15T10:00:00Z",
            "requisition_opened",
            ("decision:DEC-1",),
            opened_at="2026-03-15",
            status="open",
            role="Backend Engineer",
            department="Engineering",
        ),
    ],
}

ISSUES = [
    {
        "key": "IMPL-1",
        "project": "IMPL",
        "type": "Epic",
        "points": 0,
        "created": "2026-01-05T10:00:00+00:00",
        "client": "CL-003",
    },
    {
        "key": "PLAT-1",
        "project": "PLAT",
        "type": "Story",
        "points": 3,
        "created": "2026-03-20T10:00:00+00:00",
        "client": "",
    },
    {
        "key": "PLAT-2",
        "project": "PLAT",
        "type": "Task",
        "points": 2,
        "created": "2026-03-01T10:00:00+00:00",
        "client": "",
    },
]
TRANSITIONS = [
    ("IMPL-1", "2026-01-06T10:00:00+00:00", "To Do", "In Progress", "E1"),
    ("PLAT-1", "2026-03-25T10:00:00+00:00", "To Do", "In Progress", "E1"),
    ("PLAT-1", "2026-03-30T08:00:00+00:00", "In Progress", "Done", "E1"),
    ("PLAT-2", "2026-03-02T10:00:00+00:00", "To Do", "In Progress", "E3"),
    ("PLAT-2", "2026-03-10T10:00:00+00:00", "In Progress", "Done", "E3"),
]
PEOPLE = [
    ("E1", "Ann Engineer", "Backend Engineer", "backend", "Engineering", "2020-01-01", ""),
    ("E2", "Bob Boss", "Chief Executive Officer", "ceo", "Management", "2026-03-15", ""),
    ("E3", "Cat Leaver", "QA Engineer", "qa", "Engineering", "2021-01-01", "2026-02-28"),
]
SPRINTS = [
    ("PLAT Sprint 40", "2026-03-02T09:00:00+00:00", "2026-03-16T09:00:00+00:00", 0.9),
    ("PLAT Sprint 41", "2026-03-16T09:00:00+00:00", "2026-03-30T09:00:00+00:00", 1.1),
    ("PLAT Sprint 42", "2026-03-30T09:00:00+00:00", "2026-04-13T09:00:00+00:00", 1.2),
]
NPS = [
    ("2026-03-01", "NPS-1", "CL-001", "C1", "q1", 10),
    ("2026-03-02", "NPS-2", "CL-001", "C1", "q1", 9),
    ("2026-03-03", "NPS-3", "CL-002", "C2", "q1", 8),
    ("2026-03-04", "NPS-4", "CL-002", "C2", "q1", 6),
    ("2026-03-05", "NPS-5", "CL-003", "C3", "q1", 3),
    ("2026-04-05", "NPS-9", "CL-003", "C3", "q1", 0),
]


def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def write_history(
    root: Path, *, nps: bool = False, monitoring: bool = False, drop: tuple[str, ...] = (), tz: str = "UTC"
) -> Path:
    """Write the synthetic history; ``drop`` names sim types or flat tables to leave out."""
    ds = root / "datasets"
    cfg = root / "delivery" / "config"
    ds.mkdir(parents=True)
    cfg.mkdir(parents=True)
    (root / "delivery" / "manifest.json").write_text(
        json.dumps(
            {
                "tenant": "test",
                "name": "Test Co",
                "run_id": "test-1",
                "history": {"anchor": T.isoformat(), "start": "2026-01-01"},
            }
        ),
        encoding="utf-8",
    )
    (cfg / "sources.yaml").write_text(
        f"tenant:\n  id: test\n  name: Test Co\n  timezone: {tz}\n"
        "sources:\n- id: gitlab-test\n  provider: gitlab\n"
        "  containers:\n    test/platform:\n      name: Platform\n",
        encoding="utf-8",
    )
    (cfg / "people.yaml").write_text(
        "people:\n"
        "- {id: E1, name: Ann Engineer, department: Engineering}\n"
        "- {id: E2, name: Bob Boss, department: Management}\n"
        "- {id: E3, name: Cat Leaver, department: Engineering}\n"
        "organizations:\n"
        "- {id: CL-001, name: Client One}\n"
        "- {id: CL-002, name: Client Two}\n"
        "- {id: CL-003, name: Client Three}\n"
        "- {id: VN-01, name: Vistula}\n",
        encoding="utf-8",
    )
    for ot, rows in SIM.items():
        if ot in drop:
            continue
        text = "".join(json.dumps(r) + "\n" for r in rows)
        (ds / f"sim_{ot}.jsonl.gz").write_bytes(gzip.compress(text.encode()))
    if "issues" not in drop:
        _csv(
            ds / "issues.csv",
            [
                {
                    "component": "",
                    "priority": "Medium",
                    "reporter": "E2",
                    "parent": "",
                    "unplanned": "False",
                    **r,
                }
                for r in ISSUES
            ],
        )
        _csv(
            ds / "transitions.csv",
            [dict(zip(("key", "ts", "from", "to", "actor"), t, strict=True)) for t in TRANSITIONS],
        )
    if "people" not in drop:
        _csv(
            ds / "people.csv",
            [
                dict(zip(("id", "name", "title", "role", "department", "start", "last_day"), p, strict=True))
                for p in PEOPLE
            ],
        )
    if "sprint_load" not in drop:
        _csv(ds / "sprint_load.csv", [{"sprint": n, "load": s[3]} for n, s in enumerate(SPRINTS)])
        line = {
            "at": "2026-03-20T10:00:00+00:00",
            "source": "jira-test",
            "object_type": "issue",
            "external_id": "PLAT-1",
            "payload": {
                "fields": {
                    "summary": "Build the report",
                    "customfield_10020": [
                        {"name": name, "startDate": s, "endDate": e} for name, s, e, _ in SPRINTS
                    ],
                }
            },
        }
        (root / "delivery" / "fixtures").mkdir(parents=True)
        (root / "delivery" / "fixtures" / "timeline.jsonl.gz").write_bytes(
            gzip.compress((json.dumps(line) + "\n").encode())
        )
    if nps:
        _csv(
            ds / "nps_responses.csv",
            [
                dict(zip(("date", "response_id", "client", "contact", "survey", "score"), r, strict=True))
                for r in NPS
            ],
        )
    if monitoring:
        rows = []
        for day in range(26, 32):
            d = f"2026-03-{day:02d}"
            rows.append(
                {
                    "date": d,
                    "service": "api",
                    "p95_ms": 200,
                    "availability": 0.999,
                    "downtime_minutes": 1.44,
                    "error_rate": 0.001,
                }
            )
            rows.append(
                {
                    "date": d,
                    "service": "web-app",
                    "p95_ms": 300,
                    "availability": 0.998,
                    "downtime_minutes": 2.88,
                    "error_rate": 0.002,
                }
            )
        for svc, p95, a in (("api", 200, 0.999), ("web-app", 300, 0.998)):
            rows.append(
                {
                    "date": "2026-04-01",
                    "service": svc,
                    "p95_ms": p95,
                    "availability": a,
                    "downtime_minutes": 0,
                    "error_rate": 0,
                }
            )
        rows.append(
            {
                "date": "2026-04-05",
                "service": "api",
                "p95_ms": 9999,
                "availability": 0.1,
                "downtime_minutes": 1000,
                "error_rate": 1,
            }
        )
        _csv(ds / "monitoring_daily.csv", rows)
    return root


@pytest.fixture
def history(tmp_path: Path) -> Callable[..., Path]:
    """Factory: ``history(nps=True, monitoring=True, drop=("payroll",))`` -> a history directory."""
    n = {"k": 0}

    def make(**kw: Any) -> Path:
        n["k"] += 1
        return write_history(tmp_path / f"h{n['k']}", **kw)

    return make
