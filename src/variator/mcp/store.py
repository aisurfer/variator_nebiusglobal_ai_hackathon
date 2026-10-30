"""Server-side state behind short handles, with optional SQLite persistence.

``Store()`` keeps everything in memory (tests). ``Store(path)`` also writes each change
to SQLite at once and loads missing handles from it, so a server restart keeps all
datasets, cases, runs, RAG reports and tunnel maps. Arrays go into compressed ``.npz``
blobs; text columns go into JSON (no pickle, so a store file cannot run code).

Ownership (see ``variator.mcp.sessions``): when ``OWNER`` is set, each new handle gets
that owner, and a handle of another owner is "unknown". ``case_lock`` serializes the
read-change-write steps on one case.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from variator.adapters import Dataset
from variator.domain import Criterion, DecisionModel
from variator.mcp.case import Case
from variator.mcp.sessions import OWNER
from variator.rag import RagReport, TunnelMap
from variator.research.models import ResearchBundle
from variator.rules import RulePack
from variator.simulation import TensorRun

MAX_RUNS_IN_MEMORY = 32
MAX_RUNS_STORED = 500
ENV_STORE = "VARIATOR_STORE"
DEFAULT_PATH = Path(".variator") / "store.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS datasets (id TEXT PRIMARY KEY, meta TEXT NOT NULL, arrays BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, case_id TEXT NOT NULL, meta TEXT NOT NULL,
                                 arrays BLOB NOT NULL, created REAL DEFAULT (julianday('now')));
CREATE TABLE IF NOT EXISTS extras (key TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owners (id TEXT PRIMARY KEY, owner TEXT NOT NULL);
"""


def _npz(arrays: dict[str, np.ndarray]) -> bytes:
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **arrays)
    return buffer.getvalue()


def _unnpz(blob: bytes) -> dict[str, np.ndarray]:
    with np.load(io.BytesIO(blob), allow_pickle=False) as data:
        return {k: data[k] for k in data.files}


class _Sqlite:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Tools run in worker threads; one connection behind a lock is enough here.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(_SCHEMA)
        self.lock = threading.Lock()

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
            self.db.commit()
            return rows


class Store:
    def __init__(self, path: str | Path | None = None):
        self.datasets: dict[str, Dataset] = {}
        self.cases: dict[str, Case] = {}
        self.runs: OrderedDict[str, tuple[str, TensorRun]] = OrderedDict()
        self.extras: dict[str, Any] = {}
        self._sql = _Sqlite(Path(path)) if path is not None else None
        self.owners: dict[str, str] = {}
        self._next = 1
        self._id_lock = threading.Lock()
        self._case_locks: dict[str, threading.Lock] = {}
        if self._sql is not None:
            rows = self._sql.execute("SELECT value FROM meta WHERE key = 'next_id'")
            self._next = int(rows[0][0]) if rows else 1

    @property
    def persistent(self) -> bool:
        return self._sql is not None

    def new_id(self, prefix: str) -> str:
        with self._id_lock:
            value, self._next = self._next, self._next + 1
        key = f"{prefix}{value}"
        owner = OWNER.get()
        if owner is not None:
            self.owners[key] = owner
        if self._sql is not None:
            self._sql.execute("INSERT OR REPLACE INTO meta VALUES ('next_id', ?)", (str(self._next),))
            if owner is not None:
                self._sql.execute("INSERT OR REPLACE INTO owners VALUES (?, ?)", (key, owner))
        return key

    def owner(self, key: str) -> str | None:
        if key not in self.owners and self._sql is not None:
            rows = self._sql.execute("SELECT owner FROM owners WHERE id = ?", (key,))
            if rows:
                self.owners[key] = rows[0][0]
        return self.owners.get(key)

    def visible(self, key: str) -> bool:
        """True without isolation, else only for handles of the current owner."""
        current = OWNER.get()
        return current is None or self.owner(key) == current

    def case_lock(self, key: str) -> threading.Lock:
        with self._id_lock:
            return self._case_locks.setdefault(key, threading.Lock())

    # --- datasets ----------------------------------------------------------------------
    def add_dataset(self, dataset: Dataset) -> str:
        key = self.new_id("ds")
        self.datasets[key] = dataset
        if self._sql is not None:
            numeric = {k: v for k, v in dataset.columns.items() if v.dtype == np.float64}
            text = {k: [str(x) for x in v.tolist()] for k, v in dataset.columns.items() if k not in numeric}
            meta = {
                "name": dataset.name,
                "source": dataset.source,
                "kind": dataset.kind,
                "origin": dataset.origin,
                "notes": dataset.notes,
                "text": dataset.text,
                "scanned_pages": dataset.scanned_pages,
                "order": list(dataset.columns),
                "text_columns": text,
            }
            arrays = {f"c{i}": numeric[k] for i, k in enumerate(meta["order"]) if k in numeric}
            self._sql.execute(
                "INSERT OR REPLACE INTO datasets VALUES (?, ?, ?)", (key, json.dumps(meta), _npz(arrays))
            )
        return key

    def dataset(self, key: str) -> Dataset:
        if key not in self.datasets and self._sql is not None:
            rows = self._sql.execute("SELECT meta, arrays FROM datasets WHERE id = ?", (key,))
            if rows:
                meta, arrays = json.loads(rows[0][0]), _unnpz(rows[0][1])
                columns = {}
                for i, name in enumerate(meta["order"]):
                    if name in meta["text_columns"]:
                        columns[name] = np.asarray(meta["text_columns"][name], dtype=object)
                    else:
                        columns[name] = arrays[f"c{i}"]
                self.datasets[key] = Dataset(
                    meta["name"],
                    meta["source"],
                    meta["kind"],
                    columns,
                    meta["origin"],
                    meta["notes"],
                    meta["text"],
                    meta["scanned_pages"],
                )
        return _get(self, self.datasets, key, "dataset")

    # --- cases -------------------------------------------------------------------------
    def save_case(self, case: Case) -> None:
        self.cases[case.id] = case
        if self._sql is not None:
            body = {
                "model": case.model.model_dump(mode="json"),
                "packs": [p.model_dump(mode="json", by_alias=True) for p in case.packs],
                "plugins": case.plugins,
                "datasets": case.datasets,
                "quality": case.quality,
                "notes": case.notes,
                "version": case.version,
            }
            self._sql.execute("INSERT OR REPLACE INTO cases VALUES (?, ?)", (case.id, json.dumps(body)))

    def case(self, key: str) -> Case:
        if key not in self.cases and self._sql is not None:
            rows = self._sql.execute("SELECT body FROM cases WHERE id = ?", (key,))
            if rows:
                body = json.loads(rows[0][0])
                self.cases[key] = Case(
                    key,
                    DecisionModel.model_validate(body["model"]),
                    packs=[RulePack.model_validate(p) for p in body["packs"]],
                    plugins=body["plugins"],
                    datasets=body["datasets"],
                    quality=body["quality"],
                    notes=body["notes"],
                    version=body["version"],
                )
        return _get(self, self.cases, key, "case")

    # --- runs --------------------------------------------------------------------------
    def add_run(self, case_id: str, run: TensorRun) -> str:
        key = self.new_id("run")
        self._remember(key, case_id, run)
        if self._sql is not None:
            meta = {
                "criteria": [c.model_dump(mode="json") for c in run.criteria],
                "options": run.option_names,
                "rule_hits": run.rule_hits,
                "check_failures": run.check_failures,
                "seed": run.seed,
                "backend": run.backend,
                "elapsed_s": run.elapsed_s,
                "notes": run.notes,
            }
            blob = _npz({"values": run.values, "flags": run.flags, "infeasible": run.infeasible_mask()})
            self._sql.execute(
                "INSERT OR REPLACE INTO runs (id, case_id, meta, arrays) VALUES (?, ?, ?, ?)",
                (key, case_id, json.dumps(meta), blob),
            )
            self._sql.execute(
                "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY created DESC LIMIT ?)",
                (MAX_RUNS_STORED,),
            )
        return key

    def run(self, key: str) -> tuple[Case, TensorRun]:
        if key not in self.runs and self._sql is not None:
            rows = self._sql.execute("SELECT case_id, meta, arrays FROM runs WHERE id = ?", (key,))
            if rows:
                case_id, meta, arrays = rows[0][0], json.loads(rows[0][1]), _unnpz(rows[0][2])
                run = TensorRun(
                    criteria=[Criterion.model_validate(c) for c in meta["criteria"]],
                    option_names=meta["options"],
                    values=arrays["values"],
                    flags=arrays["flags"],
                    rule_hits=meta["rule_hits"],
                    check_failures=meta["check_failures"],
                    seed=meta["seed"],
                    backend=meta["backend"],
                    elapsed_s=meta["elapsed_s"],
                    notes=meta["notes"]
                    + (
                        []
                        if "infeasible" in arrays
                        else ["stored before check masks: all scenarios count as feasible"]
                    ),
                    infeasible=arrays.get("infeasible"),
                )
                self._remember(key, case_id, run)
        case_id, run = _get(self, self.runs, key, "run")
        self.runs.move_to_end(key)
        return self.case(case_id), run

    def _remember(self, key: str, case_id: str, run: TensorRun) -> None:
        self.runs[key] = (case_id, run)
        while len(self.runs) > MAX_RUNS_IN_MEMORY:
            self.runs.popitem(last=False)

    # --- extras: RAG reports and tunnel maps per run -------------------------------------
    def set_rag(self, run: str, report: RagReport) -> None:
        self._put_extra(f"{run}:rag", "rag", report)

    def rag(self, run: str) -> RagReport | None:
        return self._get_extra(f"{run}:rag", "rag")

    def add_tunnel(self, run: str, tunnel: TunnelMap) -> None:
        tunnels = [*self.tunnels(run), tunnel]
        self._put_extra(f"{run}:tunnels", "tunnels", tunnels)

    def tunnels(self, run: str) -> list[TunnelMap]:
        return self._get_extra(f"{run}:tunnels", "tunnels") or []

    def set_research(self, research: str, bundle: ResearchBundle) -> None:
        self._put_extra(research, "research", bundle)

    def research(self, research: str) -> ResearchBundle:
        bundle = self._get_extra(research, "research")
        if bundle is None or not self.visible(research):
            raise KeyError(f"unknown research {research!r}")
        return bundle

    def _put_extra(self, key: str, kind: str, value: Any) -> None:
        self.extras[key] = value
        if self._sql is not None:
            body = (
                [v.model_dump(mode="json") for v in value]
                if isinstance(value, list)
                else value.model_dump(mode="json")
            )
            self._sql.execute("INSERT OR REPLACE INTO extras VALUES (?, ?, ?)", (key, kind, json.dumps(body)))

    def _get_extra(self, key: str, kind: str) -> Any:
        if key not in self.extras and self._sql is not None:
            rows = self._sql.execute("SELECT body FROM extras WHERE key = ? AND kind = ?", (key, kind))
            if rows:
                body = json.loads(rows[0][0])
                self.extras[key] = (
                    [TunnelMap.model_validate(t) for t in body]
                    if kind == "tunnels"
                    else ResearchBundle.model_validate(body)
                    if kind == "research"
                    else RagReport.model_validate(body)
                )
        return self.extras.get(key)


def _get(store: Store, table: dict[str, Any], key: str, kind: str) -> Any:
    if key not in table or not store.visible(key):
        known = ", ".join([k for k in table if store.visible(k)][-10:]) or "none"
        raise KeyError(f"unknown {kind} {key!r} (known: {known})")
    return table[key]
