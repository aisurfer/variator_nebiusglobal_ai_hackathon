"""Where a day example falls among the scenarios of the month Monte Carlo (WP18), by its cash.

The day examples are three runs per fork; the page also shows WP18's fan of 1,000 scenarios per fork. An
example's own rank (p10/p50/p90 of three leaves or of a 30-run ensemble) says little about where it is in
that fan, so each example also gets its percentile among the 1,000 at the end of the window.

Inputs, read as files (orgsim does not import ``variator.future``):

- ``WeekFan`` (first choice): ``month.json`` ``options[fork].weekFan``, the cash quantiles (p5..p95) of the
  week ensemble (1,000 paths) on the window's last day; the percentile is read off them linearly.
- ``Cube`` (fallback): ``future/<case>/cube`` (``cube.json`` + ``arrays.npz``, values (S, O, K, P)). It is
  monthly: period ``YYYY-MM-01`` holds the cash at the END of that month, not on the window's last day.
"""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

QUANTILES = (5, 10, 25, 50, 75, 90, 95)


class WeekFan:
    """The cash fan of WP18's week ensemble on a given day (``month.json``: ``options[fork].weekFan``)."""

    def __init__(self, month_json: Path):
        self.path = Path(month_json)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        self.fans = {f: o["weekFan"] for f, o in (doc.get("options") or {}).items() if o.get("weekFan")}

    @classmethod
    def at(cls, month_json: Path | None) -> WeekFan | None:
        if month_json is None or not Path(month_json).is_file():
            return None
        fan = cls(month_json)
        return fan if fan.fans else None

    def position(self, fork: str, day: date, cash: float) -> dict[str, Any] | None:
        """``percentile``: linear between the published quantiles; outside p5..p95 it is held at 5 or 95 and
        ``bound`` says so. None when the fork has no fan on that day."""
        wf = self.fans.get(fork)
        if not wf or wf.get("date") != day.isoformat():
            return None
        pts = sorted((float(k[1:]), float(v)) for k, v in wf["cash"].items())
        qs, vs = [q for q, _ in pts], [v for _, v in pts]
        bound = None
        if cash <= vs[0]:
            pct, bound = qs[0], f"at or below p{qs[0]:.0f}"
        elif cash >= vs[-1]:
            pct, bound = qs[-1], f"at or above p{qs[-1]:.0f}"
        else:
            pct = float(np.interp(cash, vs, qs))
        return {
            "percentile": round(pct, 1),
            "fan": {
                "source": "WP18 week ensemble on the window's last day (month.json weekFan)",
                "file": str(self.path),
                "n": int(wf.get("n", 0)),
                "week": wf.get("week"),
                "date": day.isoformat(),
                "cash": round(float(cash), 2),
                **{f"p{q:.0f}": round(v, 2) for q, v in pts},
                "method": "linear between the published quantiles",
                **({"bound": bound} if bound else {}),
            },
        }


class Cube:
    """The cash of every scenario, fork and month of a WP18 cube."""

    def __init__(self, cube_dir: Path):
        self.dir = Path(cube_dir)
        self.meta = json.loads((self.dir / "cube.json").read_text(encoding="utf-8"))
        ids = [m["id"] for m in self.meta["metrics"]]
        with np.load(self.dir / "arrays.npz") as z:
            self.cash = np.asarray(z["values"][:, :, ids.index("cash"), :], dtype=np.float64)  # (S, O, P)

    @classmethod
    def at(cls, cube_dir: Path | None) -> Cube | None:
        if cube_dir is None or not (Path(cube_dir) / "cube.json").is_file():
            return None
        return cls(cube_dir)

    def scenarios(self, fork: str, day: date) -> tuple[str, np.ndarray] | None:
        """The period label of the month of ``day`` and the cash of the fork's scenarios at its end."""
        label = day.replace(day=1).isoformat()
        if fork not in self.meta["options"] or label not in self.meta["periods"]:
            return None
        x = self.cash[:, self.meta["options"].index(fork), self.meta["periods"].index(label)]
        return label, x[np.isfinite(x)]

    def position(self, fork: str, day: date, cash: float) -> dict[str, Any] | None:
        """``percentile``: the share of the scenarios with less cash (ties count half), in %; ``fan``: the
        quantiles used, the month and its end, the number of scenarios."""
        got = self.scenarios(fork, day)
        if got is None or not len(got[1]):
            return None
        label, x = got
        below, same = float(np.sum(x < cash)), float(np.sum(x == cash))
        end = day.replace(day=monthrange(day.year, day.month)[1])
        qs = np.percentile(x, QUANTILES)
        fan = {f"p{q}": round(float(v), 2) for q, v in zip(QUANTILES, qs, strict=True)}
        return {
            "percentile": round(100.0 * (below + 0.5 * same) / len(x), 1),
            "fan": {
                "source": f"WP18 month Monte Carlo ({self.meta.get('run')}), cash of the fork's scenarios",
                "cube": str(self.dir),
                "n": int(len(x)),
                "period": label[:7],
                "periodEnd": end.isoformat(),
                "date": day.isoformat(),
                "cash": round(float(cash), 2),
                **fan,
                "note": f"The cube holds the cash at the end of each month ({end.isoformat()}); the example "
                f"ends on {day.isoformat()}.",
            },
        }
