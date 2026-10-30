"""As-of: no row at or after T is used (differential test) and the output is deterministic.

The differential test copies the history, corrupts every row known only at or after T0 and
scrambles the snapshot columns of the flat tables, then learns at T0 on both copies. The
documents must be equal.
"""

from __future__ import annotations

import csv
import gzip
import json
import shutil
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from variator.actors.artifacts import dumps, write_set
from variator.actors.history import load_history
from variator.actors.learn import learn

T0 = date(2026, 6, 15)
TZ = ZoneInfo("Europe/London")
T0_DT = datetime.combine(T0, time(0, 0), tzinfo=TZ)

# CSV file -> the column that says when the row is known (date columns: known at the end of the day)
TIME_COLUMNS = {
    "issues.csv": "created",
    "transitions.csv": "ts",
    "merge_requests.csv": "created",
    "emails.csv": "sent",
    "bank.csv": "ts",
    "incident_impact.csv": "date",
    "arr_daily.csv": "date",
    "cloud_cost_daily.csv": "date",
    "metrics_weekly.csv": "published",
    "nps_responses.csv": "date",
    "monitoring_daily.csv": "date",
}
SNAPSHOT = {
    "clients.csv": ("start", "end", "status_at_t", "acv_at_t"),
    "people.csv": ("last_day", "salary_at_t"),
    "issues.csv": ("status", "done_at", "assignee", "sprints", "blocked_times", "comments"),
    "merge_requests.csv": ("notes",),
}


def _after(value: str) -> bool:
    if not value:
        return False
    if len(value) <= 10:
        return date.fromisoformat(value) >= T0 - __import__("datetime").timedelta(days=1)
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t >= T0_DT


def _corrupt(v):
    if isinstance(v, bool):
        return not v
    if isinstance(v, (int, float)):
        return v * 7 + 3
    if isinstance(v, str) and v.startswith("CL-"):
        return "CL-001"
    return v


def _corrupt_copy(src: Path, dst: Path) -> None:
    shutil.copytree(src / "datasets", dst / "datasets")
    shutil.copytree(src / "delivery" / "config", dst / "delivery" / "config")
    (dst / "delivery" / "fixtures").mkdir(parents=True)
    ds = dst / "datasets"
    # sim records: corrupt every version at or after T0, add a future churn event
    for p in ds.glob("sim_*.jsonl.gz"):
        out = []
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                o = json.loads(line)
                if _after(o["ts"]):
                    o = {k: (v if k in ("id", "ts", "kind") else _corrupt(v)) for k, v in o.items()}
                out.append(json.dumps(o))
        if p.name == "sim_life_event.jsonl.gz":
            out.append(
                json.dumps(
                    {
                        "id": "LE-9999",
                        "ts": "2026-08-01T08:00:00Z",
                        "kind": "life_event_happened",
                        "client": "CL-001",
                        "event_type": "churn",
                        "effective_date": "2026-08-01",
                    }
                )
            )
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    # flat tables: corrupt rows at or after T0; scramble snapshot columns everywhere
    closed_sprints = {n for n, _, end in load_history(src, T0, timeline=False).sprints if end <= T0}
    for p in ds.glob("*.csv"):
        with p.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        col = TIME_COLUMNS.get(p.name)
        snap = SNAPSHOT.get(p.name, ())
        for r in rows:
            for k in snap:
                if k in r:
                    r[k] = {"status_at_t": "churned", "status": "Done", "assignee": "LW-E01"}.get(
                        k, "1999-01-01"
                    )
                    if k in ("acv_at_t", "salary_at_t", "sprints", "blocked_times", "comments", "notes"):
                        r[k] = "777"
            if col and _after(r.get(col, "")):
                for k, v in list(r.items()):
                    if k == col or k in ("key", "id", "iid"):
                        continue
                    try:
                        r[k] = str(float(v) * 7 + 3) if v not in ("", None) else v
                    except ValueError:
                        pass
            if p.name == "merge_requests.csv" and r.get("merged_at") and _after(r["merged_at"]):
                r["merged_at"] = "2026-12-31T10:00:00+00:00"
            if p.name == "sprint_load.csv" and int(r["sprint"]) not in closed_sprints:
                r["load"] = "9.9"  # the load of a sprint is known at its end
            if p.name == "payroll_lines.csv" and str(r["period"]) >= "2026-06":
                r["gross"] = "999999"
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    # the timeline: corrupt the summaries of Jira issues created at or after T0
    tl = Path(str(Path(src) / "delivery" / "fixtures" / "timeline.jsonl.gz"))
    if tl.is_file():
        with (
            gzip.open(tl, "rt", encoding="utf-8") as f,
            gzip.open(dst / "delivery" / "fixtures" / "timeline.jsonl.gz", "wt", encoding="utf-8") as g,
        ):
            for line in f:
                if '"jira-company"' in line[:400]:
                    o = json.loads(line)
                    if _after(o.get("world_at") or o["at"]):
                        o["payload"]["fields"]["summary"] = "Board pack for June 2026"
                        line = json.dumps(o) + "\n"
                g.write(line)


def test_loader_drops_rows_at_or_after_t(history):
    h = load_history(history, T0)
    assert h.as_of == T0
    for rows in h.records.values():
        assert all(r["ts"] < h.t_end for r in rows)
    assert all(r["ts"] < h.t_end for r in h.table("transitions"))
    assert all(r["created"] < h.t_end for r in h.table("issues"))
    assert all(r["sent"] < h.t_end for r in h.table("emails"))
    assert all(r["merged_at"] is None or r["merged_at"] < h.t_end for r in h.table("merge_requests"))
    assert sum(h.dropped.values()) > 0
    for name, cols in SNAPSHOT.items():
        t = h.table(name.removesuffix(".csv"))
        assert all(c not in r for r in t for c in cols)
    assert all(p.start < T0 for p in h.people.values())


@pytest.fixture(scope="module")
def at_t0(history):
    return learn(history, T0)


def test_no_future_rows_are_used(history, at_t0, tmp_path):
    _corrupt_copy(history, tmp_path / "h")
    other = learn(tmp_path / "h", T0)
    a, b = at_t0.documents(), other.documents()
    assert sorted(a) == sorted(b)
    for name in a:
        assert dumps(a[name]) == dumps(b[name]), f"{name} depends on rows after T"


def test_deterministic(history, at_t0, tmp_path):
    again = learn(history, T0)
    m1 = write_set(at_t0, tmp_path / "a")
    m2 = write_set(again, tmp_path / "b")
    assert [f["sha256"] for f in m1["files"]] == [f["sha256"] for f in m2["files"]]
    m1.pop("id")
    m2.pop("id")
    assert dumps(m1) == dumps(m2)
