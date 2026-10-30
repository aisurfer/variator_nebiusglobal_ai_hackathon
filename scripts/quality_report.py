"""Aggregate the quality runs (``quality_runs``) for the analysis and the quality slide.

Writes ``reports/quality/summary.json`` (the latest value of each test and metric, per case, with the trend of the
last runs) and ``reports/quality/slide10.json`` ({X, Y, Z, stability, ...} with their source run). Usage:
``uv run python scripts/quality_report.py [--out reports/quality]``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from variator.telemetry.store import DEFAULT_STATS_PATH


def latest(db: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, Any]]:
    """The newest non-error row of each (test, metric, case)."""
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in db.execute("SELECT * FROM quality_runs WHERE metric != 'error' ORDER BY id"):
        out[(r["test"], r["metric"], r["case_id"] or "")] = dict(r)
    return out


def trend(db: sqlite3.Connection, test: str, metric: str, case: str, k: int = 10) -> list[dict[str, Any]]:
    rows = db.execute("SELECT ts, git_sha, value FROM quality_runs WHERE test = ? AND metric = ? AND IFNULL(case_id, '') = ? "
                      "AND value IS NOT NULL ORDER BY id DESC LIMIT ?", (test, metric, case, k)).fetchall()
    return [dict(r) for r in reversed(rows)]


def pct(x: float | None) -> str | None:
    return None if x is None else f"{round(100 * x)}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_STATS_PATH))
    ap.add_argument("--out", default="reports/quality")
    a = ap.parse_args(argv)
    db = sqlite3.connect(a.db)
    db.row_factory = sqlite3.Row
    last = latest(db)
    summary = [{**r, "trend": trend(db, r["test"], r["metric"], r["case_id"] or "")} for r in last.values()]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    def val(test: str, metric: str, case: str = "") -> dict[str, Any] | None:
        return last.get((test, metric, case))

    stab = [r for (t, m, _), r in last.items() if t == "stability" and m == "winner_agreement"]

    def pooled(metric: str) -> float | None:
        # the latest back-test of each company: a company that ran twice counts once
        rows = db.execute("SELECT value, n FROM quality_runs WHERE id IN (SELECT MAX(id) FROM quality_runs WHERE test = 'calibration' "
                          "AND metric = ? AND value IS NOT NULL AND n > 0 GROUP BY IFNULL(case_id, run_id))", (metric,)).fetchall()
        total = sum(r["n"] for r in rows)
        return sum(r["value"] * r["n"] for r in rows) / total if total else None
    # the size of the evidence: it grows with every run and every new test
    one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    futures = 0
    for r in db.execute("SELECT test, metric, n, note FROM quality_runs WHERE value IS NOT NULL"):
        note = json.loads(r["note"]) if (r["note"] or "").startswith("{") else {}
        if r["test"] == "stability" and r["metric"] == "winner_agreement":
            futures += r["n"] * note.get("seeds", 0)
        elif r["test"] == "determinism":
            futures += 2 * r["n"]
        elif r["test"] == "inputs":
            futures += r["n"] * (note.get("variants", 0) + 1)
        elif r["test"] in ("iia", "weights") and r["metric"] in ("winner_kept", "monotone_criteria"):
            futures += r["n"] * (int((r["note"] or "0").split()[0]) + 1 if r["test"] == "iia" else 16)
        elif r["test"] == "convergence":
            futures += r["n"] * sum(v for v in note.values() if isinstance(v, int))
        elif r["test"] == "calibration" and r["metric"] == "inside_p10_p90_cut":
            futures += 1000
    tally = {
        "runs": one("SELECT COUNT(DISTINCT run_id) FROM quality_runs"),
        "measurements": one("SELECT COUNT(*) FROM quality_runs WHERE metric != 'error' AND value IS NOT NULL"),
        "tests": one("SELECT COUNT(DISTINCT test) FROM quality_runs WHERE metric != 'error' AND value IS NOT NULL"),
        "cases": one("SELECT COUNT(DISTINCT case_id) FROM quality_runs WHERE case_id IS NOT NULL"),
        "futures_simulated": futures,
        "held_out_checks": one("SELECT COALESCE(SUM(n), 0) FROM quality_runs WHERE id IN (SELECT MAX(id) FROM quality_runs "
                               "WHERE test = 'calibration' AND metric = 'inside_p10_p90' AND n > 0 GROUP BY IFNULL(case_id, run_id))"),
        "first_run": one("SELECT MIN(ts) FROM quality_runs"),
    }
    slide = {
        # pooled over every company back-tested (one inside_p10_p90 row per company run), weighted by its checks
        "X_calibrated": pct(pooled("inside_p10_p90")),
        "X_p25_p75": pct(pooled("inside_p25_p75")),
        "X_companies": one("SELECT COUNT(DISTINCT IFNULL(case_id, run_id)) FROM quality_runs WHERE test = 'calibration' AND metric = 'inside_p10_p90' AND value IS NOT NULL"),
        "Y_identifiable": pct((val("recovery", "coverage") or {}).get("value")),
        "Z_grounded": pct((val("grounded", "numbers_strict") or val("grounded", "numbers_in_cited_sources") or {}).get("value")),
        "stability": {r["case_id"]: {"winner_agreement": pct(r["value"]), "n": r["n"], "note": json.loads(r["note"] or "{}")} for r in stab},
        "determinism": all((r["value"] == 1) for (t, _, _), r in last.items() if t == "determinism") or None,
        # the logic of the choice and the system: one value per case (or per metric for the sharpness)
        "logic": {f"{t}.{m}" + (f".{c}" if c else ""): pct(r["value"]) for (t, m, c), r in sorted(last.items())
                  if t in ("iia", "weights", "inputs", "regression")},
        "sharpness": {m: round(r["value"], 3) for (t, m, _), r in sorted(last.items()) if t == "sharpness"},
        "tally": tally,
        "source": {"runs": sorted({r["run_id"] for r in last.values()}), "git": sorted({r["git_sha"] or "" for r in last.values()})},
    }
    (out / "slide10.json").write_text(json.dumps(slide, indent=1), encoding="utf-8")
    print(json.dumps(slide, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
