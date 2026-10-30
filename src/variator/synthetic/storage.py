"""Write and read demo cases as plain CSV files plus a ``case.json`` manifest."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from variator.synthetic.models import CellValue, ColumnType, DemoCase, DemoTable

CASE_FILE = "case.json"
MANIFEST_VERSION = 1


def _parse(value: str, dtype: ColumnType) -> CellValue:
    if dtype == "int":
        return int(value)
    if dtype == "float":
        return float(value)
    return value


def write_demo_case(case: DemoCase, out_dir: str | Path) -> list[Path]:
    """Write every table to ``out_dir/<table>.csv`` and the story to ``out_dir/case.json``.

    Floats are written with ``str()`` (shortest round-trip repr), so ``load_demo_case``
    reproduces the tables exactly. Returns the written paths, manifest last.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    manifest_tables: list[dict[str, Any]] = []
    for table in case.tables:
        path = out / f"{table.name}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(table.columns)
            for row in table.rows:
                writer.writerow([row[column] for column in table.columns])
        written.append(path)
        manifest_tables.append(
            {
                "name": table.name,
                "file": path.name,
                "description": table.description,
                "columns": table.columns,
                "dtypes": table.dtypes(),
                "units": table.units,
                "n_rows": table.n_rows,
            }
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        **case.model_dump(exclude={"tables"}),
        "tables": manifest_tables,
    }
    manifest_path = out / CASE_FILE
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    written.append(manifest_path)
    return written


def load_demo_case(case_dir: str | Path) -> DemoCase:
    """Read a case written by ``write_demo_case`` back into a ``DemoCase``."""
    directory = Path(case_dir)
    manifest = json.loads((directory / CASE_FILE).read_text(encoding="utf-8"))
    tables: list[DemoTable] = []
    for spec in manifest.pop("tables"):
        dtypes: dict[str, ColumnType] = spec["dtypes"]
        with (directory / spec["file"]).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != spec["columns"]:
                raise ValueError(f"{spec['file']}: header {reader.fieldnames} != manifest {spec['columns']}")
            rows = [{key: _parse(value, dtypes[key]) for key, value in row.items()} for row in reader]
        tables.append(
            DemoTable(
                name=spec["name"],
                description=spec["description"],
                columns=spec["columns"],
                units=spec.get("units", {}),
                rows=rows,
            )
        )
    manifest.pop("manifest_version", None)
    return DemoCase(**manifest, tables=tables)
