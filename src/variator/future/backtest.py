"""Backtest of the base fork: learn the habits on the history before T - k months, simulate to T, compare.

The habits come from ``variator.actors.learn.learn(history, cut)``: the learning reads only the rows
before the cut. The engine starts from the state at the cut and runs "keep the plan" for k months.
The actual values come from the full history: the bank balance and the ARR at each month end, and
the ARR of the clients that gave notice since the cut (over the ARR at the cut).
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from variator.actors.history import load_history
from variator.future.company import CompanyModel
from variator.future.forks import Fork, case_config, compile_knobs
from variator.future.params import HISTORY_DIR, Habits
from variator.future.state import build_world, month_start

QS = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)
METRICS = ("cash", "arr", "churn")


def months_before(d: date, k: int) -> date:
    y, m = d.year, d.month - k
    while m <= 0:
        m, y = m + 12, y - 1
    return date(y, m, min(d.day, 28))


def actuals(root: Path, cut: date, months: int) -> dict[str, list[Any]]:
    """Actual cash, ARR and churn to date at each month end after the cut (the last month ends at T)."""
    ds = root / "datasets"
    with (ds / "bank.csv").open(encoding="utf-8") as f:
        bank = sorted((r["date"], float(r["balance_after"])) for r in csv.DictReader(f))
    with (ds / "arr_daily.csv").open(encoding="utf-8") as f:
        arr = sorted((r["date"], float(r["arr"])) for r in csv.DictReader(f))
    h = load_history(root, timeline=False)
    versions = h.versions("contract")
    notices = []
    for c in h.clients.values():
        if c.notice_at and cut <= c.notice_at < h.as_of:
            acv = 0.0
            for v in versions.get(c.contract or "", []):
                if v["ts"].date() <= c.notice_at and float(v.get("amount") or 0) > 0:
                    acv = float(v["amount"])
            notices.append((c.notice_at.isoformat(), acv))
    arr_cut = next((a for d, a in reversed(arr) if d < cut.isoformat()), arr[0][1])
    out: dict[str, list[Any]] = {k: [] for k in (*METRICS, "month_end")}
    for m in range(1, months + 1):
        end = min(month_start(cut, m + 1), h.as_of).isoformat()  # the first day after month m
        out["month_end"].append(end)
        out["cash"].append(next((b for d, b in reversed(bank) if d < end), None))
        out["arr"].append(next((a for d, a in reversed(arr) if d < end), None))
        out["churn"].append(sum(a for d, a in notices if d < end) / arr_cut)
    return out


def run_backtest(root: Path, cut: date, months: int, n: int, seed: int) -> dict[str, Any]:
    from variator.actors.learn import learn

    learned = learn(root, cut)
    habits = Habits.from_docs(learned.documents(), path=f"learn({root}, {cut})")
    cfg = case_config("company_best_year")
    world = build_world(habits, learned.h, horizon=months)
    model = CompanyModel(habits, world, cfg, horizon=months)
    res = model.run(compile_knobs([Fork("status_quo", "Keep the plan")]), n, seed, horizon=months)
    act = actuals(root, cut, months)
    rows = []
    for metric in METRICS:
        sim = res.metrics[metric][0][:, 1:]
        qs = np.quantile(sim, QS, axis=0)
        for m in range(months):
            a = act[metric][m]
            if a is None:
                continue
            rows.append(
                {
                    "metric": metric,
                    "month": m + 1,
                    "month_end": act["month_end"][m],
                    "actual": round(float(a), 6),
                    "p10": round(float(qs[1, m]), 6),
                    "p50": round(float(qs[3, m]), 6),
                    "p90": round(float(qs[5, m]), 6),
                    "inside_p10_p90": bool(qs[1, m] <= a <= qs[5, m]),
                    "pit": round(float((sim[:, m] < a).mean()), 4),
                }
            )
    hb = habits.habits
    inside = [r["inside_p10_p90"] for r in rows]
    return {
        "schema": "variator.future.backtest/1",
        "cut": cut.isoformat(),
        "months": months,
        "n": n,
        "seed": seed,
        "coverage_p10_p90": round(float(np.mean(inside)), 4),
        "nominal": 0.8,
        "by_metric": {
            k: round(float(np.mean([r["inside_p10_p90"] for r in rows if r["metric"] == k])), 4)
            for k in METRICS
        },
        "rows": rows,
        "learned_at_cut": {
            "churn_p": hb["client_base"]["churn_hazard_base"]["point"],
            "resignation_p": hb["staff"]["resignation_hazard"]["point"],
            "cash": hb["company"]["cash_at_t"]["point"],
            "arr": hb["company"]["arr_at_t"]["point"],
        },
        "notes": [
            "The habits are learned only on the rows before the cut (variator.actors.learn with as_of).",
            f"Month {months} ends at the end of the history (T), not at the end of the calendar month.",
            "The case shocks (assumptions) stay on, as in the forecast.",
        ],
    }


def run_cli(args: Any) -> int:
    root = Path(args.history or HISTORY_DIR)
    full = load_history(root, timeline=False)
    cut = months_before(full.as_of, args.months)
    doc = run_backtest(root, cut, args.months, args.n, args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "backtest.json").write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(
        json.dumps({k: doc[k] for k in ("cut", "coverage_p10_p90", "by_metric", "learned_at_cut")}, indent=1)
    )
    for r in doc["rows"]:
        print(
            r["metric"],
            r["month"],
            r["month_end"],
            r["actual"],
            [r["p10"], r["p50"], r["p90"]],
            r["inside_p10_p90"],
        )
    return 0
