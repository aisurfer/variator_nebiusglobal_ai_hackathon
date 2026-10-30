import csv
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from variator.synthetic import DemoTable, generate_demo_case, list_demo_cases, load_demo_case, write_demo_case

DATA_DEMO = Path(__file__).resolve().parents[2] / "data" / "demo"


@pytest.mark.parametrize("name", ["warehouse", "price_increase", "new_market"])
def test_csv_round_trip_is_exact(tmp_path, name):
    case = generate_demo_case(name)
    paths = write_demo_case(case, tmp_path / name)
    assert paths[-1].name == "case.json"
    assert sorted(p.stem for p in paths[:-1]) == sorted(case.table_names)
    loaded = load_demo_case(tmp_path / name)
    assert loaded == case


def test_csv_files_are_plain_and_readable(tmp_path):
    case = generate_demo_case("warehouse")
    write_demo_case(case, tmp_path)
    with (tmp_path / "tpl_quotes.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == case.table("tpl_quotes").columns
    assert rows[1]["region_coverage"] == "south, east"  # comma inside a field survives quoting
    manifest = json.loads((tmp_path / "case.json").read_text())
    assert manifest["question"] == case.question
    assert {t["name"]: t["n_rows"] for t in manifest["tables"]}["monthly_orders"] == 576


def test_all_cases_fit_well_under_one_megabyte(tmp_path):
    total = 0
    for name in list_demo_cases():
        total += sum(p.stat().st_size for p in write_demo_case(generate_demo_case(name), tmp_path / name))
    assert total < 500_000


@pytest.mark.parametrize("name", ["warehouse", "price_increase", "new_market"])
def test_committed_demo_data_is_up_to_date(name):
    """data/demo is regenerated with ``python -m variator.synthetic``; fail if it drifts."""
    case_dir = DATA_DEMO / name
    if not (case_dir / "case.json").exists():
        pytest.fail(f"{case_dir} missing; run python -m variator.synthetic")
    committed = load_demo_case(case_dir)
    fresh = generate_demo_case(name)
    for table in fresh.tables:  # text tables generated live may be present in addition
        assert committed.table(table.name) == table
    assert committed.model_dump(exclude={"tables"}) == fresh.model_dump(exclude={"tables"})


def test_table_validation():
    with pytest.raises(ValidationError, match="mixes types"):
        DemoTable(name="t", description="", columns=["a"], rows=[{"a": 1}, {"a": 1.5}])
    with pytest.raises(ValidationError, match="expected"):
        DemoTable(name="t", description="", columns=["a", "b"], rows=[{"a": 1}])
    with pytest.raises(ValidationError, match="snake_case"):
        DemoTable(name="Bad Name", description="", columns=["a"], rows=[])
    table = DemoTable(name="t", description="", columns=["a"], rows=[{"a": "x"}])
    with pytest.raises(KeyError):
        table.column("missing")


def test_cli_writes_selected_case(tmp_path, capsys):
    from variator.synthetic.__main__ import main

    assert main(["--out", str(tmp_path), "--case", "price_increase", "--seed", "42"]) == 0
    assert load_demo_case(tmp_path / "price_increase") == generate_demo_case("price_increase")
    assert not (tmp_path / "warehouse").exists()
    assert "price_increase" in capsys.readouterr().out
