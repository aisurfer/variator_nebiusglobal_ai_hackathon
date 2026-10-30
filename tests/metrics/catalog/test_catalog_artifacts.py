"""The artifact set and the CLI: files, manifest, validation, the web artifact store, a parameter file."""

from __future__ import annotations

import json
from pathlib import Path

from variator.metrics.catalog.cli import main
from variator.metrics.catalog.contracts import MetricArtifact
from variator.metrics.catalog.core import tile_value
from variator.metrics.catalog.registry import CATALOG
from variator.metrics.results import KbBrief, MetricResult
from variator.web.artifacts import ArtifactStore


def catalog_params(out: Path, metric_id: str) -> dict:
    return json.loads((out / "catalog.json").read_text())["params"]["metrics"][metric_id]


def _compute(history_dir: Path, out: Path, *extra: str) -> int:
    return main(
        [
            "compute",
            "--history",
            str(history_dir),
            "--out",
            str(out),
            "--built-at",
            "2026-04-02T00:00:00+00:00",
            *extra,
        ]
    )


def test_cli_writes_a_valid_set(history, tmp_path, capsys):
    out = tmp_path / "arts" / "history-metrics"
    assert _compute(history(nps=True, monitoring=True), out, "--as-of", "2026-04-01") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["asOf"] == "2026-04-01" and summary["metrics"]["fin.cash"] == [400.0, "bad"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["kind"] == "history-metrics" and manifest["asOf"] == "2026-04-01"
    assert manifest["schema"] == 2 and set(manifest["roles"]) == {"ceo"}
    assert set(manifest["metrics"]) == {m.id for m in CATALOG}
    assert {v["area"] for v in manifest["metrics"].values()} == {"money", "clients", "delivery"}
    paths = {f["path"] for f in manifest["files"]}
    assert {"catalog.json", "sanity.json", "briefs/ceo.json", "metrics/fin.cash.json"} <= paths
    assert not {"briefs/finance.json", "briefs/delivery.json"} & paths
    for f in manifest["files"]:
        assert (out / f["path"]).stat().st_size == f["bytes"] and f["path"].endswith(".json")
    # one brief, the CEO's: every metric once, in the top row and the three areas
    text = (out / "briefs/ceo.json").read_text()
    brief = KbBrief.model_validate_json(text)
    assert brief.to_json_dict() == json.loads(text)
    assert brief.role.id == "ceo" and brief.as_of == "2026-04-01" and len(brief.weeks) == 52
    assert [r["id"] for r in brief.organization["roles"]] in ([], ["ceo"])
    assert [m.id for m in brief.metrics] == manifest["roles"]["ceo"]["metrics"]
    assert sorted(m.id for m in brief.metrics) == sorted(m.id for m in CATALOG)
    assert [m.id for m in brief.metrics] == brief.top + [i for a in brief.areas for i in a.metrics]
    assert [a.title for a in brief.areas] == ["Money", "Clients and growth", "Delivery and service"]
    assert manifest["brief"] == {
        "role": "ceo",
        "file": "briefs/ceo.json",
        "top": brief.top,
        "areas": [a.to_json_dict() for a in brief.areas],
    }
    labels = [h.split(":")[0] for h in brief.headline]
    assert labels[:3] == ["Money", "Clients", "Delivery"], brief.headline
    for mid in manifest["metrics"]:
        doc = json.loads((out / f"metrics/{mid}.json").read_text())
        MetricArtifact.model_validate(doc)
        MetricResult.model_validate(doc["result"])
        assert doc["metric"]["roles"] == ["ceo"] and doc["params"] == catalog_params(out, mid)
    catalog = json.loads((out / "catalog.json").read_text())
    # on track at the target: the good band is the target
    assert catalog["params"]["metrics"]["fin.cash"]["bands"] == {"good": 2000000.0, "warn": 1000000.0}
    assert len(catalog["metrics"]) == len(CATALOG)


def test_the_web_store_serves_the_set(history, tmp_path, capsys):
    root = tmp_path / "arts"
    assert _compute(history(), root / "history-metrics", "--as-of", "2026-04-01") == 0
    capsys.readouterr()
    store = ArtifactStore(root)
    sets = store.list_sets()
    assert [s["id"] for s in sets] == ["history-metrics"] and "error" not in sets[0]
    assert store.file("history-metrics", "metrics/rev.arr.json").is_file()
    assert store.file("history-metrics", "briefs/ceo.json").is_file()


def test_params_file_changes_status_and_stale_files_go(history, tmp_path, capsys):
    path = history()
    out = tmp_path / "set"
    (out / "metrics").mkdir(parents=True)
    (out / "metrics" / "old.metric.json").write_text("{}")
    params = tmp_path / "params.json"
    params.write_text(
        json.dumps({"metrics": {"fin.cash": {"target": 100, "bands": {"good": 100, "warn": 50}}}})
    )
    assert _compute(path, out, "--as-of", "2026-04-01", "--params", str(params)) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["metrics"]["fin.cash"] == [400.0, "good"]
    assert not (out / "metrics" / "old.metric.json").exists()
    params.write_text(json.dumps({"metrics": {"fin.cash": {"options": {"nope": 1}}}}))
    assert _compute(path, out, "--params", str(params)) == 2


def test_params_command_writes_the_defaults(tmp_path):
    out = tmp_path / "p.json"
    assert main(["params", "--out", str(out)]) == 0
    p = json.loads(out.read_text())
    assert (
        p["series_weeks"] == 52
    )  # parameters keep snake_case: they are edited by people, not read by the UI
    assert "ops.mttr" in p["metrics"]


# the numbers of the workspace tiles (src/kb/api.ts formatValue): the headline gives the same numbers
TILE_CASES = [
    (12_001_200, "money", "$12.0M"),
    (2_000_000, "money", "$2.00M"),
    (478_800, "money", "$478,800"),
    (-48_208.33, "money", "−$48,208"),
    (99.991, "percent", "99.991%"),
    (25.64, "percent", "25.6%"),
    (-5.51, "percent", "−5.5%"),
    (12.0, "percent", "12%"),
    (62.0, "days", "62 days"),
    (58.3, "days", "58.3 days"),
    (1.81, "hours", "1.8 h"),
    (181.8, "ms", "182 ms"),
    (30.1, "index", "30"),
    (9.75, "count", "9.75"),
    (24.0, "count", "24"),
    (None, "count", "unknown"),
]


def test_headline_numbers_are_the_tile_numbers():
    for value, unit, want in TILE_CASES:
        assert tile_value(value, unit) == want, (value, unit)
