"""History of the MVP company: spec, determinism, roster, money calibration, causes, delivery."""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from variator.orgsim.calendar import add_months
from variator.orgsim.company import default_spec, generate_history, write_history
from variator.orgsim.company.model import ArrivalClock, FirstPassage
from variator.orgsim.company.processes.clients import boundary_on_or_after
from variator.orgsim.company.spec import CompanySpec
from variator.orgsim.company.ukcal import company_calendar, england_bank_holidays
from variator.orgsim.delivery import TIMELINE, verify_delivery
from variator.orgsim.streams import Streams, arrival_counts, cumulative_hazard, first_passage

ANCHOR = date(2026, 10, 27)
TZ = ZoneInfo("Europe/London")
# required fields of a sim record per object type (kb/sim/kbsim/contract.py SIM_REQUIRED)
SIM_REQUIRED = {
    "contract": ("number", "client", "status"),
    "invoice": ("number", "client", "amount", "currency", "status"),
    "payment": ("amount", "currency", "direction", "client|vendor|counterparty|authority"),
    "bank_entry": ("amount", "currency", "direction", "booked_at"),
    "deal": ("client", "owner", "status"),
    "employee_event": ("employee", "type", "effective_date"),
    "requisition": ("role", "department", "status"),
    "payroll": ("period", "amount", "currency"),
    "incident": ("title", "severity", "status"),
    "deploy": ("component", "version", "environment", "status"),
    "support_ticket": ("title", "client", "status"),
    "cloud_bill": ("vendor", "period", "amount", "currency"),
    "tax_payment": ("tax", "period", "amount", "currency"),
    "decision": ("statement", "status"),
    "nps_response": ("client", "contact", "score", "survey"),
    "life_event": ("statement",),
    "rule": ("rules",),
    "process": ("statement",),
}
RESERVED = {"event", "event_at", "process", "simulation", "causes"}
KB_PREFIXES = ("jira.", "gitlab.", "mail.", "confluence.", "person.", "org.", "sim.")


@pytest.fixture(scope="module")
def year():
    return generate_history(None, ANCHOR, 7)


@pytest.fixture(scope="module")
def written(year, tmp_path_factory):
    out = tmp_path_factory.mktemp("history")
    write_history(year, out)
    return out


def files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def sim_lines(h, otype: str | None = None):
    return [ln for ln in h.lines if ln["source"] == "sim" and (otype is None or ln["object_type"] == otype)]


def last_by_entity(lines):
    out = {}
    for ln in lines:
        out[ln["external_id"]] = ln
    return out


# ---------------------------------------------------------------------------------------------- spec
def test_spec_has_the_mvp_roster_and_round_trips(tmp_path):
    spec = default_spec()
    roles = Counter(p.role for p in spec.people)
    assert len(spec.people) == 20
    assert roles["ceo"] == roles["finance"] == roles["delivery"] == roles["pm"] == 1
    assert sum(roles[r] for r in ("backend", "frontend", "devops", "qa")) == 10
    assert roles["support"] == roles["sales"] == 2 and roles["marketing"] == roles["analyst_designer"] == 1
    names = {p.role_id: p.name for p in spec.people if p.role_id}
    assert names == {"ceo": "Avery Collins", "finance": "Morgan Reyes", "delivery": "Jordan Patel"}
    assert spec.company.name == "Larchwood Systems" and spec.company.domain == "larchwood.io"
    assert spec.truth.arr_target == 12_000_000 and spec.truth.cash_target == 2_000_000
    path = tmp_path / "spec.json"
    spec.dump(path)
    assert CompanySpec.load(path) == spec


def test_england_bank_holidays_2026():
    assert england_bank_holidays(2026) == [
        date(2026, 1, 1),
        date(2026, 4, 3),
        date(2026, 4, 6),
        date(2026, 5, 4),
        date(2026, 5, 25),
        date(2026, 8, 31),
        date(2026, 12, 25),
        date(2026, 12, 28),
    ]


def test_clocks_match_the_stream_functions():
    hazard = np.full(200, 0.07)
    e = Streams(3).rng("t", "clock").standard_exponential(60)
    clock = ArrivalClock(np.random.default_rng(0))
    clock.next, clock.rng = float(e[0]), _Seq(e[1:])
    counts = [clock.tick(h) for h in hazard]
    want, _ = arrival_counts(cumulative_hazard(hazard), e)
    assert counts == want.tolist()
    fp = FirstPassage(np.random.default_rng(0))
    fp.e = float(e[0])
    hit = next(k for k, h in enumerate(hazard) if fp.tick(h))
    assert hit == int(first_passage(cumulative_hazard(hazard), e[0]))


class _Seq:
    def __init__(self, xs):
        self.xs = list(xs)

    def standard_exponential(self):
        return self.xs.pop(0)


# ---------------------------------------------------------------------------------------------- determinism
def test_same_seed_and_anchor_give_the_same_bytes(tmp_path):
    for name in ("a", "b"):
        write_history(generate_history(None, ANCHOR, 11, months=3), tmp_path / name)
    assert files(tmp_path / "a") == files(tmp_path / "b")
    for name in ("nps_responses.csv", "monitoring_daily.csv", "sim_nps_response.jsonl.gz"):
        assert (tmp_path / "a" / "datasets" / name).stat().st_size > 100, name
    other = generate_history(None, ANCHOR, 12, months=3)
    a = gzip.decompress((tmp_path / "a" / "delivery" / TIMELINE).read_bytes())
    assert (
        a.count(b"\n") != len(other.lines)
        or other.lines[-1]["payload"] != json.loads(a.splitlines()[-1])["payload"]
    )


# ---------------------------------------------------------------------------------------------- people
def test_every_seat_is_filled_or_has_an_open_requisition(year):
    people = year.people["people"]
    t = ANCHOR.isoformat()
    active = [p for p in people if p["start_date"] <= t and (p["end_date"] is None or p["end_date"] >= t)]
    reqs = last_by_entity(sim_lines(year, "requisition"))
    open_reqs = [r for r in reqs.values() if r["payload"]["fields"]["status"] not in ("filled",)]
    assert len(active) + len(open_reqs) == 20
    assert 17 <= len(active) <= 20
    assert {p["id"] for p in people} >= {p.id for p in default_spec().people}


def test_nobody_acts_on_a_day_off_or_outside_employment(year):
    absent = defaultdict(list)
    for a in year.people["availability"]:
        absent[a["person"]].append((a["from"], a["to"]))
    span = {p["id"]: (p["start_date"], p["end_date"] or "9999") for p in year.people["people"]}
    for ln in sim_lines(year):
        who = ln["payload"]["fields"].get("actor")
        if not who:
            continue
        day = ln["at"][:10]
        assert span[who][0] <= day <= span[who][1], ln["external_id"]
        assert not any(a <= day <= b for a, b in absent[who]), (ln["external_id"], who, day)


# ---------------------------------------------------------------------------------------------- money
def test_arr_and_cash_at_t_match_the_truth(year):
    s = year.stats
    assert abs(s["arr_at_t"] - 12_000_000) / 12_000_000 < 0.01
    assert abs(s["cash_at_t"] - 2_000_000) < 1.0
    assert 80 <= s["clients_at_t"] <= 120
    assert 0.8 < s["arr_scale"] < 1.25
    bank = sim_lines(year, "bank_entry")
    assert bank[-1]["payload"]["fields"]["balance_after"] == pytest.approx(2_000_000, abs=0.01)
    assert 45 <= s["dso_days_at_t"] <= 80


def test_invoices_sum_to_the_contract_values_over_the_period(year):
    cal = company_calendar("Europe/London", ANCHOR.year - 2, ANCHOR.year + 1)
    start = add_months(ANCHOR, -12)
    contracts = last_by_entity(
        ln for ln in sim_lines(year, "contract") if ln["payload"]["fields"].get("contract_type") != "vendor"
    )
    billed = defaultdict(float)
    for ln in last_by_entity(sim_lines(year, "invoice")).values():
        f = ln["payload"]["fields"]
        if f["direction"] == "receivable" and f["issued_at"] >= start.isoformat():
            billed[f["contract"]] += f["amount"]
    checked = 0
    for cid, ln in contracts.items():
        f = ln["payload"]["fields"]
        sched = [(date.fromisoformat(x["from"]), x["acv"]) for x in f["acv_schedule"]]
        months = 3 if f["billing_frequency"] == "quarterly" else 1
        end = date.fromisoformat(f["end_date"]) if f.get("end_date") else date.max
        ps = boundary_on_or_after(date.fromisoformat(f["start_date"]), f["billing_frequency"])
        want = 0.0
        while ps < ANCHOR and ps < end:
            issue = cal.roll_forward(ps)
            acv = ([a for s, a in sorted(sched) if s <= ps] or [0.0])[-1]  # the ACV in force
            if start <= issue < ANCHOR:
                want += round(acv * months / 12, 2)
            ps = add_months(ps, months)
        assert billed[cid] == pytest.approx(want, abs=0.05), cid
        checked += 1
    assert checked >= 100


def test_every_payment_follows_its_invoice(year):
    first = {}
    inv = {}
    for ln in sim_lines(year, "invoice"):
        first.setdefault(ln["external_id"], ln["at"])
        inv[ln["external_id"]] = ln["payload"]["fields"]
    payments = sim_lines(year, "payment")
    assert len(payments) > 1000
    for ln in payments:
        f = ln["payload"]["fields"]
        assert f["settles"] in inv
        assert datetime.fromisoformat(first[f["settles"]]) < datetime.fromisoformat(ln["at"])
        assert f["paid_at"] >= inv[f["settles"]]["issued_at"]
        assert f["amount"] == pytest.approx(inv[f["settles"]]["amount"])


# ---------------------------------------------------------------------------------------------- the timeline
def test_no_event_after_t(year):
    t = datetime.combine(ANCHOR, time(0, 0), tzinfo=TZ)
    for ln in year.lines:
        assert datetime.fromisoformat(ln["at"]) < t
        assert datetime.fromisoformat(ln["world_at"]) < t
    for e in year.events:
        assert e.ts < t


def test_sim_records_meet_the_kb_contract(year):
    for ln in sim_lines(year):
        p, f = ln["payload"], ln["payload"]["fields"]
        assert ln["external_id"] == p["entity_id"] and ln["container"]
        assert not RESERVED & set(f), ln["external_id"]
        for req in SIM_REQUIRED[ln["object_type"]]:
            assert any(f.get(k) not in (None, "", [], {}) for k in req.split("|")), (ln["external_id"], req)
    types = defaultdict(set)
    for ln in sim_lines(year):
        types[ln["external_id"]].add(ln["object_type"])
    assert all(len(v) == 1 for v in types.values())


def test_causes_and_references_point_at_existing_entities(year):
    first: dict[str, str] = {}
    for ln in year.lines:
        p = ln["payload"]
        if ln["source"] == "sim":
            eid = f"sim.{ln['external_id']}"
        elif ln["object_type"] == "issue":
            eid = f"jira.{ln['external_id']}"
        elif ln["object_type"] == "merge_request":
            eid = f"gitlab.larchwood-platform.mr-{p['iid']}"
        elif ln["object_type"] == "message":
            eid = f"mail.{p['id']}"
        else:
            eid = f"confluence.page-{ln['external_id']}"
        first.setdefault(eid, ln["at"])
    people = {p["id"] for p in year.people["people"]} | {c["id"] for c in year.people["contacts"]}
    orgs = {o["id"] for o in year.people["organizations"]}
    n = 0
    for ln in sim_lines(year):
        p, f = ln["payload"], ln["payload"]["fields"]
        for c in p["causes"]:
            target = c["id"] if c["id"].startswith(KB_PREFIXES) else f"sim.{c['id']}"
            assert target in first, (ln["external_id"], c)
            assert datetime.fromisoformat(first[target]) <= datetime.fromisoformat(ln["at"]), (
                ln["external_id"],
                c,
            )
            n += 1
        for k in ("contract", "settles", "records", "deal", "incident", "related"):
            for x in f.get(k) if isinstance(f.get(k), list) else [f.get(k)] if f.get(k) else []:
                assert (x if x.startswith(KB_PREFIXES) else f"sim.{x}") in first, (ln["external_id"], k, x)
        for k in ("owner", "assignee", "author", "reporter", "employee", "actor", "contact"):
            for x in f.get(k) if isinstance(f.get(k), list) else [f.get(k)] if f.get(k) else []:
                assert x in people, (ln["external_id"], k, x)
        for k in ("client", "vendor", "counterparty", "authority"):
            if f.get(k):
                assert f[k] in orgs, (ln["external_id"], k, f[k])
    assert n > 5000
    kinds = Counter((ln["object_type"], c["type"]) for ln in sim_lines(year) for c in ln["payload"]["causes"])
    assert (
        kinds[("invoice", "process")]
        and kinds[("incident", "process")]
        and kinds[("requisition", "decision")]
    )
    assert kinds[("contract", "life_event")] and kinds[("employee_event", "life_event")]


def test_the_brief_couplings_are_declared(year):
    churn = [ln for ln in sim_lines(year, "contract") if ln["payload"]["kind"] == "notice_given"]
    assert churn and all(any(c["type"] == "life_event" for c in ln["payload"]["causes"]) for ln in churn)
    incidents = sim_lines(year, "incident")
    assert any(c["id"].startswith("DEP-") for ln in incidents for c in ln["payload"]["causes"])
    assert any(
        c["id"].startswith("INC-") for ln in sim_lines(year, "life_event") for c in ln["payload"]["causes"]
    )
    upsell_tickets = [
        ln
        for ln in sim_lines(year, "support_ticket")
        if any(c["id"].startswith("DEAL-") for c in ln["payload"]["causes"])
    ]
    assert upsell_tickets
    impl = [ln for ln in year.lines if ln["object_type"] == "issue" and ln["external_id"].startswith("IMPL-")]
    assert impl


def test_volumes_are_close_to_the_truth(year):
    s = year.stats
    assert 30_000 <= s["lines"] <= 45_000
    assert 2.5 <= s["incidents_per_month"] <= 6
    assert 7 <= s["deploys_per_week"] <= 13
    assert 0.85 <= s["team_load_mean"] <= 1.1
    assert 1.0 <= s["mttr_h_mean"] <= 3.5
    assert 0.08 <= s["realised_churn_notice_rate"] <= 0.3


# ---------------------------------------------------------------------------------------------- the files
def test_the_delivery_passes_verify_delivery(written):
    assert verify_delivery(written / "delivery") == []
    assert verify_delivery(written / "delivery" / "sample") == []
    manifest = json.loads((written / "delivery" / "manifest.json").read_text())
    assert manifest["contract"] == "plancy-kb-corpus/1" and manifest["counts"]["cases"]["total"] >= 100
    assert all(v["status"] == "ok" for v in manifest["checks"].values())
    truth = json.loads((written / "truth.json").read_text())
    assert truth["spec"]["truth"]["ar_days"] == 60 and truth["calibration"]["cash_at_t"] == pytest.approx(2e6)
    assert not (written / "delivery" / "truth.json").exists()
    for name in ("arr_daily.csv", "metrics_monthly.csv", "issues.csv", "bank.csv", "sim_invoice.jsonl.gz"):
        assert (written / "datasets" / name).is_file()
    head = {
        "nps_responses.csv": "date,response_id,client,contact,survey,score",
        "monitoring_daily.csv": "date,service,p95_ms,availability,downtime_minutes,error_rate",
    }
    for name, cols in head.items():
        assert (written / "datasets" / name).read_text().splitlines()[0] == cols


def test_the_delivery_passes_the_kb_intake_with_no_errors(written, tmp_path):
    kb = Path(__file__).resolve().parents[3] / "kb"
    if not (kb / "sim" / "kbsim" / "intake.py").is_file():
        pytest.skip("no kb/ copy next to the tests")
    env = {**os.environ, "KB_ENV": "test", "PYTHONPATH": f"{kb / 'sim'}{os.pathsep}{kb / 'app'}"}
    out = tmp_path / "intake"
    run = subprocess.run(
        [sys.executable, "-m", "kbsim.intake", "validate", str(written / "delivery"), "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    report = json.loads((out / "validate.json").read_text())
    assert report["errors"] == 0, (report["failed_checks"], run.stdout[-2000:], run.stderr[-2000:])
    assert run.returncode == 0


# ---------------------------------------------------------------------------------------------- NPS
SERVICES = {
    "api": 0.30,
    "web-app": 0.22,
    "auth": 0.10,
    "integrations": 0.12,
    "billing-engine": 0.08,
    "data-pipeline": 0.08,
    "ml-inference": 0.10,
}


def test_nps_is_plausible_and_clients_hit_by_incidents_score_lower(year):
    s = year.stats["nps"]
    # company_model: nps = 40 - 2000 x SLA gap - 40 x overdue + 50 x (support quality - 0.8), about 33
    assert 20 <= s["nps_year"] <= 45
    assert [x["survey"] for x in s["surveys"]] == ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    assert all(-40 <= x["nps"] <= 70 for x in s["surveys"])
    assert all(0.3 <= x["response_rate"] <= 0.65 for x in s["surveys"][:3])
    rows = year.datasets["nps_responses"]
    assert len(rows) == s["responses"] > 250
    assert all(0 <= r["score"] <= 10 for r in rows) and len({r["response_id"] for r in rows}) == len(rows)
    # the clients hit by an incident in the window before the survey score lower
    hit_on = defaultdict(list)
    for x in year.datasets["incident_impact"]:
        hit_on[x["client"]].append(date.fromisoformat(x["date"]))
    sent = {x["survey"]: date.fromisoformat(x["sent_at"]) for x in s["surveys"]}
    hit, calm = [], []
    for r in rows:
        d = sent[r["survey"]]
        near = any(d - timedelta(days=91) <= x < d for x in hit_on[r["client"]])
        (hit if near else calm).append(r["score"])
    assert len(hit) > 30 and np.mean(hit) < np.mean(calm) - 0.2
    # declared causes: the survey process always; incidents only for passives and detractors
    resp = sim_lines(year, "nps_response")
    assert all(any(c["id"] == "PROC-NPS" for c in ln["payload"]["causes"]) for ln in resp)
    by_inc = [ln for ln in resp if any(c["id"].startswith("INC-") for c in ln["payload"]["causes"])]
    others = [ln["payload"]["fields"]["score"] for ln in resp if ln not in by_inc]
    assert len(by_inc) > 20 and max(ln["payload"]["fields"]["score"] for ln in by_inc) <= 8
    assert np.mean([ln["payload"]["fields"]["score"] for ln in by_inc]) < np.mean(others) - 0.5
    f = resp[0]["payload"]["fields"]
    assert f["comment_slot"] == f"sim:{resp[0]['external_id']}:nps_comment" and f["contact_name"]
    assert f["category"] in ("promoter", "passive", "detractor") and resp[0]["container"] == "feedback"
    # couplings: a follow-up ticket per detractor (when the account manager is at work); NPS -> churn
    detractors = sum(1 for r in rows if r["score"] <= 6)
    follow = {
        ln["external_id"]
        for ln in year.lines
        if ln["object_type"] == "issue"
        and ln["payload"]["fields"]["summary"].startswith("NPS follow-up with")
    }
    assert 0.6 * detractors <= len(follow) <= detractors
    assert any("k_churn_nps" in x for x in year.truth["couplings"])
    for ln in sim_lines(year, "life_event"):
        cited = [c["id"] for c in ln["payload"]["causes"] if c["id"].startswith("NPS-")]
        clients = {r["client"] for r in rows if r["response_id"] in cited}
        assert clients <= {ln["payload"]["fields"].get("client")}


def test_late_implementations_exist_and_lower_the_score(year):
    cm = year.stats["nps"]["company_model"]
    assert 0.03 <= cm["realised_inputs"]["overdue_share"] <= 0.4  # company_model overdue_base 0.10
    assert 0.7 <= cm["realised_inputs"]["support_quality"] <= 0.9
    assert 20 <= cm["nps_formula_realised"] <= 45 and cm["nps_formula_defaults"] == pytest.approx(
        33.4, abs=0.1
    )
    levels = [v for x in year.truth["nps_levels"].values() for v in x.values()]
    assert any(v["overdue_points"] > 0 for v in levels) and any(v["sla_points"] > 10 for v in levels)


# ---------------------------------------------------------------------------------------------- monitoring
def _local(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(TZ).replace(tzinfo=None)


def test_availability_matches_the_incident_downtime(year):
    rows = year.datasets["monitoring_daily"]
    start = add_months(ANCHOR, -12)
    assert len(rows) == (ANCHOR - start).days * len(SERVICES)
    assert {r["service"] for r in rows} == set(SERVICES)
    got = {(r["date"], r["service"]): r for r in rows}
    want: dict[tuple[str, str], float] = defaultdict(float)
    open_days: set[str] = set()
    for ln in last_by_entity(sim_lines(year, "incident")).values():
        f = ln["payload"]["fields"]
        assert f["services"] and set(f["services"]) <= set(SERVICES), ln["external_id"]
        a = _local(f["started_at"])
        if "downtime_minutes" not in f:  # resolved after T: its downtime is not delivered
            open_days |= {(a + timedelta(days=k)).date().isoformat() for k in range(2)}
            continue
        b = a + timedelta(minutes=f["downtime_minutes"])
        while a < b:
            midnight = datetime.combine(a.date() + timedelta(days=1), time(0))
            cut = min(b, midnight)
            for s in f["services"]:
                want[(a.date().isoformat(), s)] += (cut - a).total_seconds() / 60
            a = cut
    assert sum(want.values()) > 500
    for key, r in got.items():
        assert r["availability"] == pytest.approx(1 - r["downtime_minutes"] / 1440, abs=1e-6)
        assert 0 <= r["error_rate"] <= 1 and 50 < r["p95_ms"] < 1500
        if key[0] not in open_days:
            assert r["downtime_minutes"] == round(want.get(key, 0.0)), key
    # latency follows the load: weekdays are slower than weekends
    wd = [r["p95_ms"] for r in rows if date.fromisoformat(r["date"]).weekday() < 5]
    we = [r["p95_ms"] for r in rows if date.fromisoformat(r["date"]).weekday() >= 5]
    assert np.mean(wd) > np.mean(we)


def test_yearly_sla_of_the_series_agrees_with_the_company_model_formula(year):
    sla = year.stats["sla"]
    rows = year.datasets["monitoring_daily"]
    days = sorted({r["date"] for r in rows})
    down = defaultdict(float)
    platform = defaultdict(float)
    for r in rows:
        down[r["service"]] += r["downtime_minutes"]
        platform[r["date"]] += SERVICES[r["service"]] * r["p95_ms"]
    avail = sum(w * (1 - down[s] / (1440 * len(days))) for s, w in SERVICES.items())
    penalty = np.mean([0.002 * max(x - 250, 0) / 50 for x in platform.values()])
    assert avail - penalty == pytest.approx(sla["sla_series"], abs=2e-6)
    # company_model: sla = 1 - incidents x mttr x 0.1 / 720 - 0.002 x max(latency - 250, 0) / 50
    inc = last_by_entity(sim_lines(year, "incident")).values()
    restore = [
        ln["payload"]["fields"]["restore_minutes"]
        for ln in inc
        if "restore_minutes" in ln["payload"]["fields"]
    ]
    per_month = len(list(inc)) / (len(days) / (365 / 12))
    formula = 1 - per_month * np.mean(restore) / 60 * 0.1 / 720
    assert formula == pytest.approx(sla["sla_formula_realised"], abs=2e-5)
    # agreement: 0.1 percentage point, a fifth of the 0.5 pp the 99.5% SLA allows
    assert abs(sla["sla_series"] - sla["sla_formula_realised"]) <= 0.001
    assert sla["sla_series"] > sla["sla_target"] and 150 <= sla["p95_platform_mean_ms"] <= 230
