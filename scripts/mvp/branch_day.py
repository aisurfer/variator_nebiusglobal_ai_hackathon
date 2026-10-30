"""Day level of case B in one command (WP19): example futures as KB branches and the day data of the page.

    python scripts/mvp/branch_day.py --out /tmp/variator-1000/mvp-branches \
        --day-out /tmp/variator-1000/mvp-artifacts/future/company_best_year/day [--load]

Steps (``variator.orgsim.company.forward.pipeline.branches``): the leaf paths of the WP18 week tree (or an
ensemble), the forward runs, the texts of the notable events (``variator.narrative.branch.write_texts``:
headless Nemotron, lightning by default, with the fact checker), the deliveries and ``intake validate``, day examples of
the other forks of the case (an ensemble each, records only), the daily values of the CEO metric catalogue
on each example's records (``variator.metrics.catalog``, read-only), the day data and, with ``--load``, the
KB load of each branch. Exit code 1 when a delivery has intake errors.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from variator.metrics.catalog import registry
from variator.metrics.catalog.datasets import load_history
from variator.metrics.catalog.engine import fill
from variator.metrics.catalog.engine import run as metric_run
from variator.metrics.catalog.finance import burn_display
from variator.narrative.branch import write_texts
from variator.orgsim.company.forward.datasets import NOT_ON_SIMULATED, write_datasets
from variator.orgsim.company.forward.forks import DEFAULT_CASE
from variator.orgsim.company.forward.pipeline import FAN_CUBE, WEEK_FAN, WEEK_TREE, branches

# shown with the sign flipped, as the brief shows them (the catalogue's display hint, ``negate``): a series
# changes sign from day to day, so the whole series is flipped and labelled by the hint
NEGATED = {"fin.burn": burn_display(Decimal(-1)) or {}}


def _meta(d: Any, params: Any) -> dict[str, Any]:
    """A day metric with the catalogue texts and their parameters filled in ({window_days}, {opt.<name>})."""
    p = params.of(d.id)
    m = {"id": d.id, "label": fill(d.title, p), "unit": d.unit, "higher": d.direction == "higher",
         "description": fill(d.definition, p), "source": "variator.metrics.catalog"}  # fmt: skip
    hint = NEGATED.get(d.id)
    if hint:
        m.update(
            label=hint["label"],
            higher=not m["higher"],
            description=f"Average monthly net cash flow of the bank account over the last {p.window_days} "
            "days: credits minus debits. Positive: more cash came in than went out. The catalogue's net "
            "burn with the sign flipped, as the CEO brief shows it.",
            display={"negate": True, "of": d.id},
        )
    return m


def catalog_day_metrics(run: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    """The catalogue metrics as of every day of the run, on the history plus the run's records."""
    defs = [d for d in registry.CATALOG if d.id not in NOT_ON_SIMULATED]
    params = registry.default_params()
    by_day: dict[str, dict[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix="variator-day-") as tmp:
        write_datasets(run, tmp)
        data = load_history(tmp, as_of_anchor=run.end)  # T = the first day after the window
        for d in run.days:
            vals = {}
            for defn in defs:
                c = metric_run(defn, data, params, d)
                if c.value is not None:
                    vals[defn.id] = -c.value if defn.id in NEGATED and c.value else c.value
            by_day[d.isoformat()] = vals
    return [_meta(d, params) for d in defs], by_day


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="branch_day.py", description=__doc__.split("\n", 1)[0])
    ap.add_argument("--case", default="company_best_year", help="case id (workspace/config/cases/<id>.json)")
    ap.add_argument(
        "--forks", default="", help="comma-separated fork ids (default: the forks of the week tree)"
    )
    ap.add_argument("--history", type=Path, default=Path("/tmp/variator-1000/mvp-history-mgmt"))
    ap.add_argument("--actors", type=Path, default=Path("/tmp/variator-1000/mvp-artifacts/actors"))
    ap.add_argument(
        "--out", type=Path, required=True, help="work directory: texts, branch deliveries, report"
    )
    ap.add_argument("--day-out", type=Path, default=None, help="directory of the day data of the case page")
    ap.add_argument(
        "--week-tree", type=Path, default=WEEK_TREE, help="WP18 week tree (missing file: an ensemble)"
    )
    ap.add_argument("--ensemble", type=int, default=30, help="runs per fork without a week tree")
    ap.add_argument("--months", type=int, default=6, help="day window after T")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--texts", choices=("nemotron", "placeholder", "none"), default="nemotron")
    ap.add_argument("--parallel", type=int, default=6, help="writer calls at the same time")
    ap.add_argument("--load", action="store_true", help="load each branch into its KB tenant")
    ap.add_argument("--parts", type=int, default=3, help="drain processes of the KB load")
    ap.add_argument("--only", default="", help="rewrite only these tenants' deliveries (comma-separated)")
    ap.add_argument(
        "--other-forks", default=None, help="day-only forks (default: every other fork of the case)"
    )
    ap.add_argument("--no-metrics", action="store_true", help="skip the catalogue metrics of the days")
    ap.add_argument("--week-fan", type=Path, default=WEEK_FAN, help="WP18 month.json with weekFan (first)")
    ap.add_argument("--fan-cube", type=Path, default=FAN_CUBE, help="WP18 cube (when no week fan fits)")
    args = ap.parse_args(argv)
    writer = None
    if args.texts != "none":
        writer = functools.partial(write_texts, mode=args.texts, parallel=args.parallel)
    rep = branches(
        history=args.history,
        actors=args.actors,
        out=args.out,
        day_out=args.day_out,
        forks=[f for f in args.forks.split(",") if f],
        case_path=DEFAULT_CASE.with_name(f"{args.case}.json"),
        n=args.ensemble,
        seed=args.seed,
        months=args.months,
        writer=writer,
        week_tree=args.week_tree,
        only=[t for t in args.only.split(",") if t] or None,
        other_forks=None if args.other_forks is None else [f for f in args.other_forks.split(",") if f],
        day_metrics=None if args.no_metrics else catalog_day_metrics,
        fan_cube=args.fan_cube,
        week_fan=args.week_fan,
        do_load=args.load,
        parts=args.parts,
    )
    print(json.dumps({k: rep.get(k) for k in ("forks", "chosen", "writers", "load", "seconds")}, indent=1))
    return 0 if all(b["intake"]["errors"] == 0 for b in rep["branches"]) else 1


if __name__ == "__main__":
    sys.exit(main())
