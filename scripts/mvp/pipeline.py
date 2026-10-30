"""VARIATOR MVP precompute pipeline (plan task H1): one command from the history to the served artifacts.

    .venv/bin/python scripts/mvp/pipeline.py run --dry-run          # what would run, and why
    .venv/bin/python scripts/mvp/pipeline.py run [--only a,b] [--force a,b] [--llm] [--kb] [--publish]
    .venv/bin/python scripts/mvp/pipeline.py status                 # the record of every stage
    .venv/bin/python scripts/mvp/pipeline.py adopt STAGE --from DIR # record outputs made before the pipeline
    make -C deploy/mvp precompute | precompute-dry                  # the same through make

Stages, in order (owner work package in brackets):
  history    [WP9]   orgsim generator: one year before T, deterministic (anchor, seed)
  narrative  [WP12]  management layer: plan, headless Nemotron writers (resumable), check, apply
  intake     [WP9]   kbsim intake validate of the final delivery: 0 errors
  kb-load    [WP8]   load the delivery into the KB tenant "company" (make seed): hours of CPU embedding
  actors     [WP10]  learn actors, habits, cohorts, risks -> artifacts/actors
  metrics    [WP15]  metric catalogue, CEO brief as of the last day of data -> artifacts/history-metrics-ceo
  cases      [WP16]  case configs and previews rebuilt in a staging copy, compared with workspace/config/cases
  future     [WP18]  month and week futures (python -m variator.future.cli) -> artifacts/future-*
  branches   [WP19]  day examples on the week-tree leaves: branch deliveries, day data (branch_day.py) -> work
  branch-kb  [WP19]  load each branch delivery into its own KB tenant (branch_load.py)
  day        [WP19]  Ask examples of each branch, checked on its KB tenant -> artifacts/day-company_best_year
  branch-metrics [WP19] CEO brief of each branch as of its last day -> artifacts/branch-metrics-<tenant>
  cards      [WP20]  actor and cohort cards (python -m variator.actors.cards.cli) -> artifacts/actor-cards
  cards-kb   [WP20]  append the card pages to the KB tenant company
  artifacts  [WP22]  check every served set (manifest.json, JSON files) and write artifacts.json
  publish    [WP22]  copy the sets into the running stack (make seed-artifacts), check /api/variator/artifacts

A stage is skipped when its record in pipeline.json has the same inputs (source files, parameters,
upstream outputs) and its outputs are unchanged on disk (KB stages: the same KB state). A stage whose CLI
has not landed is skipped with a note. Contract for a stage CLI that lands later: it takes --history DIR
and --out DIR (and --actors DIR where it needs them) and writes an artifact set: a directory with
manifest.json and JSON files.

Costs: "llm" stages call Nemotron through Token Factory only with
--llm; "kb" stages change the KB of the variator-mvp stack only with --kb; "stack" stages touch the running
stack only with --publish. Without the flag a stage that must run is "blocked" and the run exits with 2.

Release root ($MVP_RELEASE_ROOT, default /tmp/variator-<uid>/mvp-release): pipeline.json, logs/<stage>.log,
work/ (history-base, narrative, history, intake, cases, branches, day, branch-examples, branch-metrics,
cards), artifacts/<set>/ (the served root), artifacts.json.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SCHEMA = "variator-mvp-pipeline/1"
DEFAULT_ROOT = Path(os.environ.get("MVP_RELEASE_ROOT") or f"/tmp/variator-{os.getuid()}/mvp-release")
# T of the history loaded into the KB on 2026-10-27; a new anchor regenerates everything, the KB load too.
DEFAULT_ANCHOR = "2026-10-27"
DEFAULT_SEED = 7
TENANT = "company"
COMPOSE = ["docker", "compose", "-p", "variator-mvp", "-f", str(REPO / "deploy/mvp/compose.yaml")]
API_URL = os.environ.get("MVP_URL", "http://127.0.0.1:8088")
# the case scripts import each other (and new helpers appear): the staging copy takes every script of
# scripts/mvp except the release scripts
RELEASE_SCRIPTS = ("pipeline.py", "bundle.py", "release_test.py")


def case_scripts() -> list[Path]:
    return sorted(p for p in (REPO / "scripts/mvp").glob("*.py") if p.name not in RELEASE_SCRIPTS)


# the stages see only the keys the code reads; any other API key or token of the shell is dropped
STAGE_KEYS = frozenset({
    "NEBIUS_API_KEY", "TAVILY_API_KEY", "KB_LLM_API_KEY", "NVD_API_KEY", "TYPESAFE_API_KEY",
})
SECRET_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN")
# served set names, as the owners (WP15, WP20) seeded them into the stack
METRICS_SET = "history-metrics-ceo"
CARDS_SET = "actor-cards"


class StageError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------- hashing


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_files(path: Path) -> list[Path]:
    """Files under a path (the path itself when it is a file), sorted; caches and temporary files left out."""
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    out = []
    for p in path.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith((".pyc", ".part")):
            out.append(p)
    return sorted(out)


def digest_paths(paths: list[Path], base: Path) -> str | None:
    """One digest over the relative names and contents of the files under the paths; None: one is missing."""
    h = hashlib.sha256()
    for path in paths:
        if not path.exists():
            return None
        for f in tree_files(path):
            rel = f.relative_to(base).as_posix() if f.is_relative_to(base) else f.as_posix()
            h.update(f"{rel}\0{sha256_file(f)}\n".encode())
    return h.hexdigest()


def digest_sources(patterns: tuple[str, ...], repo: Path = REPO) -> str:
    """Digest of the repository files that match the glob patterns (relative to the repository root)."""
    files: set[Path] = set()
    for pat in patterns:
        files.update(p for p in repo.glob(pat) if p.is_file() and "__pycache__" not in p.parts)
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f"{f.relative_to(repo).as_posix()}\0{sha256_file(f)}\n".encode())
    return h.hexdigest()


def digest_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------- context


@dataclass
class Ctx:
    root: Path
    anchor: str = DEFAULT_ANCHOR
    seed: int = DEFAULT_SEED
    llm: bool = False
    kb: bool = False
    publish: bool = False
    dry_run: bool = False
    python: str = sys.executable
    log: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def history(self) -> Path:
        return self.work / "history"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in STAGE_KEYS or not k.endswith(SECRET_SUFFIXES)}
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def sh(self, cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
        """Run a command; its output streams to the stage log; raise StageError on a non-zero exit."""
        assert self.log is not None
        lines: list[str] = []
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n$ {' '.join(cmd)}\n")
            fh.flush()
            with subprocess.Popen(
                cmd,
                cwd=cwd or REPO,
                env=env or self.env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    fh.write(line)
                    fh.flush()
                    lines.append(line)
                code = proc.wait()
        if code != 0:
            tail = " | ".join(x.strip() for x in lines[-3:])
            raise StageError(f"exit {code}: {' '.join(cmd[:4])}...: {tail}")
        return "".join(lines)

    def py(self, *args: str, cwd: Path | None = None) -> str:
        return self.sh([self.python, *args], cwd=cwd)


def same_tree(a: Path, b: Path) -> bool:
    """Both directories hold the same files: relative names and contents."""
    return a.is_dir() and b.is_dir() and digest_paths([a], a) == digest_paths([b], b)


def replace_dir(tmp: Path, final: Path) -> bool:
    """Swap a finished directory into place (the old one is removed only after the new one is complete).

    An unchanged result keeps the old directory: a bind mount of it stays valid (variator-api mounts
    work/history/datasets and work/history/delivery/config) and the files keep their times. True: replaced."""
    if same_tree(tmp, final):
        shutil.rmtree(tmp)
        return False
    old = final.with_name(final.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.rename(old)
    tmp.rename(final)
    shutil.rmtree(old, ignore_errors=True)
    return True


def fresh_tmp(final: Path) -> Path:
    tmp = final.with_name(final.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    return tmp


def cli_commands(module: str) -> set[str] | None:
    """Subcommand names a `python -m module --help` lists ({a,b,c}); None when the module does not exist."""
    try:
        if importlib.util.find_spec(module) is None:
            return None
    except (ImportError, ValueError):
        return None
    done = subprocess.run(
        [sys.executable, "-m", module, "--help"], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    text = done.stdout + done.stderr
    names: set[str] = set()
    for line in text.splitlines():
        if "{" in line and "}" in line:
            names.update(x.strip() for x in line[line.index("{") + 1 : line.index("}")].split(","))
    return names


def last_day_of_data(history: Path) -> str:
    """The as-of date of the history metrics: the date of the last event (summary.json period.last_at)."""
    summary = json.loads((history / "summary.json").read_text(encoding="utf-8"))
    return summary["manifest_counts"]["period"]["last_at"][:10]


SEED_FILES = ("fixtures/timeline.jsonl.gz", "config/sources.yaml", "config/people.yaml")


def seed_digest(delivery: Path) -> str | None:
    """Digest of the files `make seed` reads from a delivery (other files never reach the KB)."""
    return digest_paths([delivery / f for f in SEED_FILES], delivery)


def timeline_lines(delivery: Path) -> int:
    import gzip

    with gzip.open(delivery / "fixtures/timeline.jsonl.gz", "rt", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


# ------------------------------------------------------------------------------------------------- KB probes


def kb_probe(tenant: str = TENANT) -> str | None:
    """The KB state of a tenant on the variator-mvp stack (read-only psql): events, journal head, projections.

    "events=38551 journal=38552 projections=caught-up"; None when the stack is not reachable."""
    sql = (
        "SELECT (SELECT count(*) FROM ingest_event WHERE tenant_id = :'t' AND status = 'processed'), "
        "(SELECT last_tx_seq FROM tenant_clock WHERE tenant_id = :'t'), "
        "(SELECT coalesce(min(tx_seq), 0) FROM projection_watermark WHERE tenant_id = :'t'), "
        "(SELECT count(*) FROM projection_watermark WHERE tenant_id = :'t')"
    )
    done = subprocess.run(
        [*COMPOSE, "exec", "-T", "postgres", "psql", "-U", "kb", "-d", "kb", "-At", "-v", f"t={tenant}"],
        input=sql + ";\n",
        capture_output=True,
        text=True,
    )
    if done.returncode != 0 or not done.stdout.strip():
        return None
    events, journal, low, projectors = (done.stdout.strip().split("|") + ["", "", "", ""])[:4]
    caught = journal and projectors not in ("", "0") and low == journal
    return f"events={events} journal={journal or 0} projections={'caught-up' if caught else 'behind'}"


def kb_loaded(tenant: str, lines: int) -> str | None:
    """ "loaded" when the tenant holds at least `lines` processed events; None when unreachable.

    Later appends (card pages) keep it loaded; projections lagging for a moment are no reason to reload."""
    state = kb_probe(tenant)
    if state is None:
        return None
    events = int(state.split()[0].split("=")[1] or 0)
    return "loaded" if lines and events >= lines else f"events={events} of {lines}"


def history_loaded(c: Ctx) -> str | None:
    delivery = c.history / "delivery"
    lines = timeline_lines(delivery) if (delivery / SEED_FILES[0]).is_file() else 0
    return kb_loaded(TENANT, lines)


def api_sets(url: str = API_URL) -> dict[str, str] | None:
    """{set id: builtAt or as_of} that /api/variator/artifacts lists through caddy; None when unreachable."""
    import urllib.request

    try:
        # the app (and its API) lives under /workspace/ on the site (WP27)
        with urllib.request.urlopen(f"{url}/workspace/api/variator/artifacts", timeout=5) as r:
            body = json.loads(r.read())
    except (OSError, ValueError):
        return None
    out = {}
    for s in body.get("sets", []):
        m = s.get("manifest") or {}
        out[s["id"]] = str(m.get("builtAt") or m.get("built_at") or m.get("as_of") or "")
    return out


# ------------------------------------------------------------------------------------------------- stages


@dataclass
class Stage:
    name: str
    owner: str
    what: str
    run: Callable[[Ctx], None]
    outputs: Callable[[Ctx], list[Path]] = lambda c: []
    sources: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    params: Callable[[Ctx], dict[str, Any]] = lambda c: {}
    cost: str = "cheap"  # cheap | llm | kb | stack
    gated: bool = True  # False: the stage checks its own cost (narrative needs --llm only for missing texts)
    available: Callable[[Ctx], str | None] = lambda c: None  # None, or why the stage cannot run yet
    probe: Callable[[Ctx], str | None] | None = None
    sets: tuple[str, ...] = ()  # served artifact sets the stage writes


def run_history(c: Ctx) -> None:
    out = c.work / "history-base"
    tmp = fresh_tmp(out)
    c.py(
        "-m",
        "variator.orgsim.company.cli",
        "generate",
        "--anchor",
        c.anchor,
        "--seed",
        str(c.seed),
        "--out",
        str(tmp),
    )
    replace_dir(tmp, out)


def narrative_pending(work: Path) -> list[str]:
    plan = json.loads((work / "plan.json").read_text(encoding="utf-8"))
    texts = work / "texts.json"
    st = json.loads(texts.read_text(encoding="utf-8")) if texts.is_file() else {}
    return [
        a["id"] for a in plan["artifacts"] if st.get(a["id"], {}).get("status") not in ("accepted", "dropped")
    ]


def run_narrative(c: Ctx) -> None:
    base, work, out = c.work / "history-base", c.work / "narrative", c.history
    work.mkdir(parents=True, exist_ok=True)
    cli = ("-m", "variator.narrative.cli")
    c.py(*cli, "plan", "--history", str(base), "--work", str(work))
    pending = narrative_pending(work)
    if pending:
        if not c.llm:
            raise StageError(
                f"{len(pending)} artifacts have no accepted text: run with --llm (Nemotron writers)"
            )
        c.py(*cli, "write", "--history", str(base), "--work", str(work))
    c.py(*cli, "check", "--history", str(base), "--work", str(work))
    tmp = fresh_tmp(out)  # apply writes a new directory
    c.py(*cli, "apply", "--history", str(base), "--work", str(work), "--out", str(tmp))
    replaced = replace_dir(tmp, out)
    c.notes.append(f"narrative: {len(pending)} writer calls needed")
    if replaced:
        # the bind mounts of a running variator-api still show the old (deleted) directory
        c.notes.append(
            "narrative: work/history changed: restart variator-api (make app-rebuild S_APP=variator-api)"
        )


def run_intake(c: Ctx) -> None:
    out = c.work / "intake"
    tmp = fresh_tmp(out)
    env = c.env() | {"PYTHONPATH": "sim:app"}
    c.sh(
        [c.python, "-m", "kbsim.intake", "validate", str(c.history / "delivery"), "--out", str(tmp)],
        cwd=REPO / "kb",
        env=env,
    )
    report = json.loads((tmp / "validate.json").read_text(encoding="utf-8"))
    replace_dir(tmp, out)
    if report["errors"]:
        raise StageError(f"intake: {report['errors']} errors ({', '.join(report['failed_checks'])})")
    c.notes.append(f"intake: 0 errors, {report['warnings']} warnings ({', '.join(report['warned_checks'])})")


def run_kb_load(c: Ctx) -> None:
    c.sh(
        [
            "make",
            "-C",
            str(REPO / "deploy/mvp"),
            "seed",
            f"DELIVERY={c.history / 'delivery'}",
            f"TENANT={TENANT}",
            "PARTS=3",
        ]
    )


def run_actors(c: Ctx) -> None:
    out = c.artifacts / "actors"
    tmp = fresh_tmp(out)
    c.py("-m", "variator.actors.cli", "learn", "--history", str(c.history), "--out", str(tmp))
    replace_dir(tmp, out)


def run_metrics(c: Ctx) -> None:
    out = c.artifacts / METRICS_SET
    tmp = fresh_tmp(out)
    c.py(
        "-m",
        "variator.metrics.catalog.cli",
        "compute",
        "--history",
        str(c.history),
        "--out",
        str(tmp),
        "--as-of",
        last_day_of_data(c.history),
    )
    replace_dir(tmp, out)


FACTS = Path("workspace/config/company.facts.json")


def run_cases(c: Ctx) -> None:
    """The case scripts write to <their root>/workspace/config/cases: a staging copy keeps the repo as is."""
    out = c.work / "cases"
    tmp = fresh_tmp(out)
    scripts = tmp / "scripts/mvp"
    scripts.mkdir(parents=True)
    for f in case_scripts():
        shutil.copy2(f, scripts / f.name)
    # the company facts first: case_company.py reads them (workspace/config/company.facts.json)
    (tmp / FACTS).parent.mkdir(parents=True)
    c.py(str(scripts / "make_company_facts.py"), cwd=tmp)
    c.py(str(scripts / "export_case.py"), cwd=tmp)
    c.py(str(scripts / "proto_future.py"), cwd=tmp)
    shutil.rmtree(scripts)
    built = tmp / "workspace/config/cases"
    drift = []
    if (tmp / FACTS).read_bytes() != (REPO / FACTS).read_bytes():
        drift.append(FACTS.name)
    for f in sorted(built.glob("*.json")):
        repo_file = REPO / "workspace/config/cases" / f.name
        if not repo_file.is_file() or repo_file.read_bytes() != f.read_bytes():
            drift.append(f.name)
    (tmp / "compare.json").write_text(
        json.dumps({"built": sorted(p.name for p in built.glob("*.json")), "drift": drift}, indent=1) + "\n",
        encoding="utf-8",
    )
    replace_dir(tmp, out)
    c.notes.append(
        "cases: workspace/config/cases is current"
        if not drift
        else "cases: DRIFT, rebuild and commit (scripts/mvp/make_company_facts.py, export_case.py, "
        f"proto_future.py): {drift}"
    )


def _set_cli(module: str, command: str) -> Callable[[Ctx], str | None]:
    def available(c: Ctx) -> str | None:
        names = cli_commands(module)
        if names is None:
            return f"`python -m {module}` has not landed yet"
        if command not in names:
            return f"`python -m {module}` has no `{command}` command yet (it lists {sorted(names) or 'none'})"
        return None

    return available


def check_set(path: Path) -> dict[str, Any]:
    """A served set: manifest.json is a JSON object and every JSON file parses. Returns its summary."""
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise StageError(f"{path.name}/manifest.json is not a JSON object")
    files, size = 0, 0
    for f in tree_files(path):
        if f.suffix == ".json":
            try:
                json.loads(f.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise StageError(f"{f.relative_to(path.parent)}: not JSON ({exc})") from None
        files += 1
        size += f.stat().st_size
    keys = ("kind", "schema", "asOf", "as_of", "builtAt", "built_at", "title")
    return {"files": files, "bytes": size, **{k: manifest[k] for k in keys if k in manifest}}


def run_artifacts(c: Ctx) -> None:
    c.artifacts.mkdir(parents=True, exist_ok=True)
    sets = {}
    for d in sorted(p for p in c.artifacts.iterdir() if p.is_dir()):
        if d.name.endswith((".tmp", ".old")):
            shutil.rmtree(d)
            continue
        if not (d / "manifest.json").is_file():
            raise StageError(f"artifacts/{d.name} has no manifest.json (variator-api would not list it)")
        sets[d.name] = check_set(d)
    index = {
        "schema": "variator-mvp-artifacts/1",
        "built_at": now(),
        "sets": sets,
        "bytes": sum(s["bytes"] for s in sets.values()),
    }
    (c.root / "artifacts.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    c.notes.append(f"artifacts: {len(sets)} sets, {index['bytes'] / 1e6:.1f} MB: {', '.join(sets)}")


def run_publish(c: Ctx) -> None:
    for d in sorted(p for p in c.artifacts.iterdir() if (p / "manifest.json").is_file()):
        c.sh(["make", "-C", str(REPO / "deploy/mvp"), "seed-artifacts", f"DIR={d}", f"SET={d.name}"])
    listed = api_sets()
    want = {p.name for p in c.artifacts.iterdir() if (p / "manifest.json").is_file()}
    if listed is None or not want <= set(listed):
        raise StageError(
            f"{API_URL}/workspace/api/variator/artifacts lists {sorted(listed or [])}, "
            f"expected {sorted(want)}"
        )


def publish_probe(c: Ctx) -> str | None:
    listed = api_sets()
    return None if listed is None else digest_json(listed)


def sets_after(c: Ctx) -> list[Path]:
    return [c.root / "artifacts.json"]


FUTURE_N = 1000
FUTURE_SEED = 7
DAY_CASE = "company_best_year"
CARDS_CLI = ("-m", "variator.actors.cards.cli")


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def future_cases() -> list[str]:
    """Case B always; case A once its engine (variator.future.delivery) lands."""
    return ["company_best_year"] + (["delivery_best_way"] if has_module("variator.future.delivery") else [])


def run_future(c: Ctx) -> None:
    for case in future_cases():
        out = c.artifacts / f"future-{case}"
        tmp = fresh_tmp(out)
        c.py(
            "-m",
            "variator.future.cli",
            "run",
            "--case",
            case,
            "--n",
            str(FUTURE_N),
            "--seed",
            str(FUTURE_SEED),
            "--history",
            str(c.history),
            "--actors",
            str(c.artifacts / "actors"),
            "--out",
            str(tmp),
        )
        if not (tmp / "manifest.json").is_file():
            raise StageError(f"variator.future.cli run --case {case} wrote no manifest.json")
        replace_dir(tmp, out)


def run_branches(c: Ctx) -> None:
    """Day examples on the leaves of the WP18 week tree: KB branch deliveries in work/branches (texts by
    Nemotron, intake validate) and the day data of the case page in work/day
    (scripts/mvp/branch_day.py). The stage day serves the day data with the Ask examples of each branch.

    Without --llm the stage generates deterministic placeholder text."""
    future = c.artifacts / f"future-{DAY_CASE}"
    work, out = c.work / "branches", c.work / "day"
    tmp = fresh_tmp(out)
    cmd = [c.python, str(REPO / "scripts/mvp/branch_day.py"), "--case", DAY_CASE]
    cmd += ["--history", str(c.history), "--actors", str(c.artifacts / "actors")]
    cmd += ["--out", str(work), "--day-out", str(tmp), "--seed", str(c.seed)]
    # the tree, the week fan and the cube of the pipeline's own future set (the defaults point elsewhere)
    cmd += ["--week-tree", str(future / "week-tree.json"), "--week-fan", str(future / "month.json")]
    cmd += ["--fan-cube", str(future / "cube")]
    if c.llm:
        c.sh(cmd)
        replace_dir(tmp, out)
        return
    c.sh([*cmd, "--texts", "placeholder"])
    replace_dir(tmp, out)
    c.notes.append("branches: deterministic placeholder text (no writer call)")


def branch_deliveries(c: Ctx) -> dict[str, Path]:
    """The branch deliveries of the last branch_day.py run (its report; directories of older runs stay)."""
    root = c.work / "branches/branches"
    if not root.is_dir():
        return {}
    listed = set(branch_tenants(c))
    return {
        d.name: d / "delivery"
        for d in sorted(root.iterdir())
        if (not listed or d.name in listed) and (d / "delivery" / SEED_FILES[0]).is_file()
    }


def run_branch_kb(c: Ctx) -> None:
    """scripts/mvp/branch_load.py: one tenant per branch, one at a time, with its own disk guard."""
    todo = [t for t, d in branch_deliveries(c).items() if kb_loaded(t, timeline_lines(d)) != "loaded"]
    if TENANT in todo:
        raise StageError("a branch never loads into the tenant company")
    if todo:
        c.py(
            str(REPO / "scripts/mvp/branch_load.py"),
            "--out",
            str(c.work / "branches"),
            "--tenants",
            ",".join(todo),
        )


def branch_kb_probe(c: Ctx) -> str | None:
    states = [f"{t}:{kb_loaded(t, timeline_lines(d))}" for t, d in branch_deliveries(c).items()]
    return " ".join(states) or None


def run_day(c: Ctx) -> None:
    """The served day set: the day data of work/day plus the Ask examples of each branch
    (scripts/mvp/branch_examples.py): each candidate question goes to the Ask of the branch's KB tenant on the
    stack (read-only; the KB logs the run) and is kept when its answer names declared causes."""
    if not (c.work / "day/day_examples.json").is_file():
        raise StageError("no day data in work/day: run the stage branches")
    state = branch_kb_probe(c) or ""
    if any(not x.endswith(":loaded") for x in state.split()) or not state:
        raise StageError(f"the branches are not all in the KB ({state or 'none'}): run the stage branch-kb")
    ask = c.work / "branch-examples"  # the timelines the questions come from; examples.json of each branch
    shutil.rmtree(ask, ignore_errors=True)
    for tenant, delivery in branch_deliveries(c).items():
        dst = ask / "branches" / tenant / "delivery" / "fixtures"
        dst.mkdir(parents=True)
        shutil.copy2(delivery / SEED_FILES[0], dst)
    out = c.artifacts / f"day-{DAY_CASE}"
    tmp = fresh_tmp(out)
    shutil.copytree(c.work / "day", tmp)
    c.py(str(REPO / "scripts/mvp/branch_examples.py"), "--out", str(ask), "--day-out", str(tmp))
    replace_dir(tmp, out)


BRANCH_METRICS = "branch-metrics-"


def branch_tenants(c: Ctx) -> list[str]:
    report = c.work / "branches/report.json"
    if not report.is_file():
        return []
    return sorted(b["tenant"] for b in json.loads(report.read_text(encoding="utf-8"))["branches"])


def run_branch_metrics(c: Ctx) -> None:
    """The CEO brief of the metric catalogue for each branch (scripts/mvp/branch_metrics.py): the forward run
    again on the pinned week tree of work/branches, its datasets (work/branch-metrics/datasets) and the set
    branch-metrics-<tenant> as of the branch's last day. Sets of branches no longer in the report go."""
    if not (c.work / "branches/report.json").is_file():
        raise StageError("no branch report in work/branches: run the stage branches")
    work = c.work / "branch-metrics"
    shutil.rmtree(work, ignore_errors=True)
    sets = work / "sets"
    sets.mkdir(parents=True)
    for name in ("report.json", "week-tree.used.json"):
        shutil.copy2(c.work / "branches" / name, work / name)
    c.py(
        str(REPO / "scripts/mvp/branch_metrics.py"),
        "--out",
        str(work),
        "--sets",
        str(sets),
        "--history",
        str(c.history),
        "--actors",
        str(c.artifacts / "actors"),
    )
    made = sorted(p.name for p in sets.iterdir() if (p / "manifest.json").is_file())
    want = [BRANCH_METRICS + t for t in branch_tenants(c)]
    if made != want:
        raise StageError(f"branch_metrics.py wrote {made}, expected {want}")
    for name in made:
        replace_dir(sets / name, c.artifacts / name)
    for old in c.artifacts.glob(BRANCH_METRICS + "*"):
        if old.is_dir() and old.name not in made:
            shutil.rmtree(old)
    sets.rmdir()
    c.notes.append(f"branch-metrics: {len(made)} sets")


def run_cards(c: Ctx) -> None:
    work = c.work / "cards"
    work.mkdir(parents=True, exist_ok=True)
    w = ("--work", str(work))
    c.py(
        *CARDS_CLI,
        "build",
        *w,
        "--actors",
        str(c.artifacts / "actors"),
        "--history",
        str(c.history),
        "--cases",
        str(REPO / "workspace/config/cases"),
    )
    c.py(*CARDS_CLI, "ids", *w)  # the KB ids a citation may use (read-only KB API)
    c.py(*CARDS_CLI, "write", *w)  # Nemotron; resumable
    out = c.artifacts / CARDS_SET
    tmp = fresh_tmp(out)
    c.py(*CARDS_CLI, "artifacts", *w, "--out", str(tmp))
    replace_dir(tmp, out)
    c.py(*CARDS_CLI, "export", *w, "--base-delivery", str(c.history / "delivery"))


def cards_loaded(c: Ctx) -> str | None:
    cards = c.work / "cards/delivery"
    history = c.history / "delivery"
    if not (cards / SEED_FILES[0]).is_file() or not (history / SEED_FILES[0]).is_file():
        return None
    return kb_loaded(TENANT, timeline_lines(history) + timeline_lines(cards))


def run_cards_kb(c: Ctx) -> None:
    c.py(*CARDS_CLI, "append", "--delivery", str(c.work / "cards/delivery"), "--tenant", TENANT, "--yes")


def _kb_available(c: Ctx) -> str | None:
    return (
        None if kb_probe() is not None else "the variator-mvp stack is not reachable (make -C deploy/mvp up)"
    )


ORGSIM = ("src/variator/orgsim/**/*.py",)
STAGES: list[Stage] = [
    Stage(
        "history",
        "WP9",
        "generate one year of company history before T",
        run_history,
        outputs=lambda c: [c.work / "history-base"],
        sources=ORGSIM,
        params=lambda c: {"anchor": c.anchor, "seed": c.seed},
    ),
    Stage(
        "narrative",
        "WP12",
        "management layer: plan, writers, check, apply",
        run_narrative,
        outputs=lambda c: [c.history],
        sources=("src/variator/narrative/**/*.py",),
        after=("history",),
        cost="llm",
        gated=False,
    ),
    Stage(
        "intake",
        "WP9",
        "kbsim intake validate of the final delivery",
        run_intake,
        outputs=lambda c: [c.work / "intake/validate.json"],
        sources=("kb/sim/kbsim/**/*.py",),
        after=("narrative",),
    ),
    Stage(
        "kb-load",
        "WP8",
        "load the delivery into the KB tenant (journal, search, graph)",
        run_kb_load,
        sources=("kb/app/**/*.py", "kb/config/**/*"),
        # not after=narrative: only the seed files of the delivery reach the KB (summary paths do not)
        params=lambda c: {"tenant": TENANT, "delivery": seed_digest(c.history / "delivery")},
        cost="kb",
        available=_kb_available,
        probe=history_loaded,
    ),
    Stage(
        "actors",
        "WP10",
        "learn actors, habits, cohorts, risks",
        run_actors,
        outputs=lambda c: [c.artifacts / "actors"],
        sources=("src/variator/actors/**/*.py",),
        after=("narrative",),
        sets=("actors",),
    ),
    Stage(
        "metrics",
        "WP15",
        "metric catalogue and CEO brief",
        run_metrics,
        outputs=lambda c: [c.artifacts / METRICS_SET],
        after=("narrative",),
        sources=("src/variator/metrics/**/*.py",),
        sets=(METRICS_SET,),
    ),
    Stage(
        "cases",
        "WP16",
        "case configs and previews, compared with workspace/config/cases",
        run_cases,
        outputs=lambda c: [c.work / "cases"],
        sources=("scripts/mvp/*.py",)  # the release scripts too (a cheap rerun)
        + (
            "workspace/config/cases/*.json",
            str(FACTS),
            "plugins/variator_it_services/src/**/*.py",
            "src/variator/{domain,expr,rules,simulation,mcda,timeline,report,synthetic,orgsim}/**/*.py",
        ),
    ),
    Stage(
        "future",
        "WP18",
        "month Monte Carlo, ranking and week tree per case",
        run_future,
        outputs=lambda c: [c.artifacts / f"future-{case}" for case in future_cases()],
        sources=("src/variator/future/**/*.py",),
        after=("narrative", "actors"),
        params=lambda c: {"n": FUTURE_N, "seed": FUTURE_SEED, "cases": future_cases()},
        available=_set_cli("variator.future.cli", "run"),
        sets=("future-company_best_year", "future-delivery_best_way"),
    ),
    Stage(
        "branches",
        "WP19",
        "day examples on the week-tree leaves: branch deliveries and the day data of the case page",
        run_branches,
        outputs=lambda c: [c.work / "day", c.work / "branches/branches"],
        sources=ORGSIM + ("src/variator/narrative/**/*.py", "scripts/mvp/branch_day.py"),
        after=("narrative", "actors", "future"),
        params=lambda c: {"case": DAY_CASE, "seed": c.seed},
        cost="llm",
        gated=False,  # without --llm it runs on the accepted texts only (run_branches)
        available=lambda c: (
            None
            if (REPO / "scripts/mvp/branch_day.py").is_file()
            else "scripts/mvp/branch_day.py has not landed yet"
        ),
    ),
    Stage(
        "branch-kb",
        "WP19",
        "load each branch delivery into its own KB tenant",
        run_branch_kb,
        sources=("kb/app/**/*.py",),
        params=lambda c: {t: seed_digest(d) for t, d in branch_deliveries(c).items()},
        cost="kb",
        available=lambda c: (
            _kb_available(c) or (None if branch_deliveries(c) else "no branch deliveries yet")
        ),
        probe=branch_kb_probe,
    ),
    Stage(
        "day",
        "WP19",
        "Ask examples of each branch, checked on its KB tenant; the served day set",
        run_day,
        outputs=lambda c: [c.artifacts / f"day-{DAY_CASE}"],
        sources=("scripts/mvp/branch_examples.py", "kb/app/kb/reasoning/**/*.py"),
        after=("branches",),
        available=_kb_available,
        probe=branch_kb_probe,
        sets=(f"day-{DAY_CASE}",),
    ),
    Stage(
        "branch-metrics",
        "WP19",
        "CEO brief of the metric catalogue for each branch, as of its last day",
        run_branch_metrics,
        outputs=lambda c: [c.artifacts / (BRANCH_METRICS + t) for t in branch_tenants(c)],
        sources=ORGSIM + ("src/variator/metrics/**/*.py", "scripts/mvp/branch_metrics.py"),
        after=("narrative", "actors", "branches"),
    ),
    Stage(
        "cards",
        "WP20",
        "actor and cohort cards: plan, KB ids, writers, artifact set, KB delivery",
        run_cards,
        outputs=lambda c: [c.artifacts / CARDS_SET, c.work / "cards/delivery"],
        sources=("src/variator/actors/**/*.py",),
        after=("narrative", "actors"),
        cost="llm",
        available=lambda c: _set_cli("variator.actors.cards.cli", "write")(c) or _kb_available(c),
        sets=(CARDS_SET,),
    ),
    Stage(
        "cards-kb",
        "WP20",
        "append the card pages to the KB tenant company (never a reset)",
        run_cards_kb,
        params=lambda c: {"delivery": seed_digest(c.work / "cards/delivery")},
        cost="kb",
        available=lambda c: _kb_available(c) or (None if cards_loaded(c) else "no card delivery yet"),
        probe=cards_loaded,
    ),
    Stage(
        "artifacts",
        "WP22",
        "check the served sets, write artifacts.json",
        run_artifacts,
        outputs=sets_after,
        after=("actors", "metrics", "future", "day", "branch-metrics", "cards"),
    ),
    Stage(
        "publish",
        "WP22",
        "copy the sets into the running stack and check the API",
        run_publish,
        after=("artifacts",),
        cost="stack",
        probe=publish_probe,
        available=lambda c: None if shutil.which("docker") else "no docker",
    ),
]
BY_NAME = {s.name: s for s in STAGES}
FLAG = {"llm": "--llm", "kb": "--kb", "stack": "--publish"}


def expand(pattern: str) -> list[str]:
    """Brace expansion for the source globs: "a/{b,c}/*.py" -> ["a/b/*.py", "a/c/*.py"]."""
    if "{" not in pattern:
        return [pattern]
    head, rest = pattern.split("{", 1)
    alts, tail = rest.split("}", 1)
    return [x for alt in alts.split(",") for x in expand(head + alt + tail)]


# ------------------------------------------------------------------------------------------------- manifest


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / "pipeline.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema": SCHEMA, "stages": {}}


def save_manifest(root: Path, manifest: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "pipeline.json.part"
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(root / "pipeline.json")


def inputs_of(stage: Stage, c: Ctx, manifest: dict[str, Any]) -> dict[str, Any]:
    patterns = tuple(x for p in stage.sources for x in expand(p))
    upstream = {u: (manifest["stages"].get(u) or {}).get("outputs") for u in stage.after}
    return {"sources": digest_sources(patterns), "params": stage.params(c), "upstream": upstream}


def outputs_of(stage: Stage, c: Ctx) -> str | None:
    paths = stage.outputs(c)
    return digest_paths(paths, c.root) if paths else None


def assess(stage: Stage, c: Ctx, manifest: dict[str, Any], planned: dict[str, str]) -> tuple[str, str]:
    """(state, reason): fresh | run | blocked | unavailable | waiting."""
    why = stage.available(c)
    if why:
        return "unavailable", why
    # a failed upstream stops its downstream; a blocked one leaves its last outputs (or none) in place
    waiting = [u for u in stage.after if planned.get(u) in ("failed", "waiting")]
    if waiting:
        return "waiting", f"upstream {', '.join(waiting)} cannot run"
    rec = manifest["stages"].get(stage.name)
    reasons = []
    if rec is None or rec.get("status") not in ("done", "adopted"):
        reasons.append("no record" if rec is None else f"last run {rec.get('status')}")
    else:
        inp = inputs_of(stage, c, manifest)
        old = rec.get("inputs", {})
        if old.get("sources") != inp["sources"]:
            reasons.append("source files changed")
        if old.get("params") != inp["params"]:
            reasons.append(f"parameters changed {old.get('params')} -> {inp['params']}")
        for u in stage.after:
            if planned.get(u) == "run":
                reasons.append(f"after {u} (may stay fresh if its output is unchanged)")
            elif (old.get("upstream") or {}).get(u) != inp["upstream"].get(u):
                reasons.append(f"{u} output changed")
        paths = stage.outputs(c)
        if paths:
            if any(not p.exists() for p in paths):
                reasons.append("outputs missing")
            elif digest_paths(paths, c.root) != rec.get("outputs"):
                reasons.append("outputs changed on disk")
        if stage.probe is not None and stage.probe(c) != rec.get("probe"):
            reasons.append("live state differs from the record")
    if not reasons:
        return "fresh", f"recorded {rec.get('finished_at', '?')}"
    allowed = {"cheap": True, "llm": c.llm, "kb": c.kb, "stack": c.publish}[stage.cost]
    if stage.gated and not allowed:
        return "blocked", f"needs {FLAG[stage.cost]}: " + "; ".join(reasons)
    return "run", "; ".join(reasons)


def record(stage: Stage, c: Ctx, manifest: dict[str, Any], status: str, seconds: float, **extra: Any) -> None:
    rec = {
        "status": status,
        "owner": stage.owner,
        "inputs": inputs_of(stage, c, manifest),
        "outputs": outputs_of(stage, c),
        "probe": stage.probe(c) if stage.probe else None,
        "seconds": round(seconds, 1),
        "finished_at": now(),
        "log": f"logs/{stage.name}.log",
        "commit": git_head(),
        **extra,
    }
    manifest["stages"][stage.name] = rec
    save_manifest(c.root, manifest)


def git_head() -> str:
    done = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True)
    return done.stdout.strip()


# ------------------------------------------------------------------------------------------------- commands


def select(only: str | None, start: str | None) -> list[Stage]:
    names = [s.name for s in STAGES]
    for n in (only or "").split(",") + [start or ""]:
        if n and n not in BY_NAME:
            raise SystemExit(f"unknown stage {n!r}; stages: {', '.join(names)}")
    picked = STAGES[names.index(start) :] if start else STAGES
    if only:
        keep = set(only.split(","))
        picked = [s for s in picked if s.name in keep]
    return picked


def cmd_run(args: argparse.Namespace) -> int:
    c = Ctx(
        root=args.root,
        anchor=args.anchor,
        seed=args.seed,
        llm=args.llm,
        kb=args.kb,
        publish=args.publish,
        dry_run=args.dry_run,
    )
    manifest = load_manifest(c.root)
    force = set(filter(None, (args.force or "").split(",")))
    planned: dict[str, str] = {}
    rows = []
    t_all = time.monotonic()
    print(f"pipeline: root {c.root} ({'dry run' if c.dry_run else 'run'}), commit {git_head()[:12]}")
    for stage in select(args.only, args.start):
        state, reason = assess(stage, c, manifest, planned)
        if stage.name in force and state == "fresh":
            state, reason = "run", "forced"
        planned[stage.name] = state
        if c.dry_run or state != "run":
            rows.append((stage, state, None, reason))
            print(f"  {stage.name:<14} {state:<11} {reason}")
            continue
        (c.root / "logs").mkdir(parents=True, exist_ok=True)
        c.log = c.root / "logs" / f"{stage.name}.log"
        c.log.write_text(f"# {stage.name} ({stage.owner}) {now()}: {reason}\n", encoding="utf-8")
        print(f"  {stage.name:<14} running     {reason}", flush=True)
        t0 = time.monotonic()
        notes_before = len(c.notes)
        try:
            stage.run(c)
        except (StageError, OSError, ValueError, KeyError) as exc:
            secs = time.monotonic() - t0
            planned[stage.name] = "failed"
            record(stage, c, manifest, "failed", secs, error=str(exc))
            rows.append((stage, "failed", secs, str(exc)))
            print(f"  {stage.name:<14} FAILED      {secs:7.1f} s  {exc} (log: {c.log})")
            continue
        secs = time.monotonic() - t0
        notes = c.notes[notes_before:]
        record(stage, c, manifest, "done", secs, notes=notes)
        planned[stage.name] = "done"
        rows.append((stage, "done", secs, "; ".join(notes) or reason))
        print(f"  {stage.name:<14} done        {secs:7.1f} s  {'; '.join(notes)}", flush=True)
    total = time.monotonic() - t_all
    states = [r[1] for r in rows]
    print(
        f"pipeline: {len(rows)} stages in {total:.1f} s: "
        + ", ".join(f"{states.count(s)} {s}" for s in dict.fromkeys(states))
    )
    if c.dry_run:
        return 0
    manifest["last_run"] = {
        "finished_at": now(),
        "seconds": round(total, 1),
        "stages": {r[0].name: [r[1], None if r[2] is None else round(r[2], 1)] for r in rows},
    }
    save_manifest(c.root, manifest)
    if "failed" in states:
        return 1
    return 2 if "blocked" in states or "waiting" in states else 0


def cmd_status(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.root)
    print(f"pipeline: root {args.root}")
    for stage in STAGES:
        rec = manifest["stages"].get(stage.name)
        if rec is None:
            print(f"  {stage.name:<14} {stage.owner:<5} (no record)")
            continue
        notes = "; ".join(rec.get("notes") or []) or rec.get("error") or ""
        print(
            f"  {stage.name:<14} {stage.owner:<5} {rec['status']:<8} {rec.get('seconds', 0):8.1f} s  "
            f"{rec.get('finished_at', '')}  {notes}"
        )
    return 0


def _copy_into(src: Path, dst: Path) -> None:
    tmp = fresh_tmp(dst)
    shutil.copytree(src, tmp)
    replace_dir(tmp, dst)


def _record_if_complete(stage: Stage, c: Ctx, manifest: dict[str, Any], t0: float, src: Path) -> None:
    """Record the stage as adopted once every output exists (one adopt call per output directory)."""
    missing = [str(p) for p in stage.outputs(c) if not p.exists()]
    if missing:
        print(f"adopt: {stage.name}: still missing {', '.join(missing)}; adopt them too, or run the stage")
        return
    record(stage, c, manifest, "adopted", time.monotonic() - t0, adopted_from=str(src))


def cmd_adopt(args: argparse.Namespace) -> int:
    """Record outputs made before the pipeline existed, so an expensive stage is not run again.

    A stage with one output directory gets a copy of --from. kb-load copies nothing: --from is the delivery
    that was loaded; it must equal the pipeline's delivery, and the KB must hold its events, all projected."""
    c = Ctx(root=args.root, anchor=args.anchor, seed=args.seed)
    stage = BY_NAME[args.stage]
    manifest = load_manifest(c.root)
    src = Path(args.source).resolve()
    t0 = time.monotonic()
    if stage.name == "kb-load":
        ours = c.history / "delivery"
        if seed_digest(src) is None or seed_digest(src) != seed_digest(ours):
            raise SystemExit(
                f"adopt: the seed files of {src} differ from the pipeline delivery {ours}; "
                "run the narrative stage first"
            )
        probe = kb_probe()
        lines = timeline_lines(ours)
        if kb_loaded(TENANT, lines) != "loaded" or probe is None or not probe.endswith("caught-up"):
            raise SystemExit(f"adopt: the KB does not hold the {lines} events, all projected ({probe})")
        record(stage, c, manifest, "adopted", 0.0, adopted_from=str(src), notes=[f"KB: {probe}"])
    elif stage.name == "narrative" and (src / "plan.json").is_file():
        # a writers' work directory: the next run plans again and calls writers only for missing texts
        work = c.work / "narrative"
        tmp = fresh_tmp(work)
        shutil.copytree(src, tmp)
        replace_dir(tmp, work)
        texts = json.loads((work / "texts.json").read_text(encoding="utf-8"))
        accepted = sum(1 for t in texts.values() if t.get("status") == "accepted")
        print(f"adopt: narrative work <- {src}: {accepted} accepted texts; the next run reuses them")
        return 0
    elif stage.cost == "kb" and stage.probe is not None:
        # a KB load made outside the pipeline (cards-kb, branch-kb): the probe must say "loaded" for each
        state = stage.probe(c)
        if not state or any(not x.endswith("loaded") or "of" in x for x in state.split()):
            raise SystemExit(f"adopt: {stage.name}: the KB does not hold its deliveries ({state})")
        record(stage, c, manifest, "adopted", 0.0, adopted_from=str(src), notes=[f"KB: {state}"])
    elif stage.name == "branches" and (src / "branches").is_dir():
        # branch_day.py's work directory (texts, deliveries): the writers resume from it
        _copy_into(src, c.work / "branches")
        _record_if_complete(stage, c, manifest, t0, src)
    elif stage.name == "branches" and (src / "day_examples.json").is_file():
        # branch_day.py's day data (its --day-out); the stage day adds the Ask examples again
        _copy_into(src, c.work / "day")
        _record_if_complete(stage, c, manifest, t0, src)
    elif stage.name == "cards" and (src / "plan.json").is_file():
        # the cards' work directory (plan, KB ids, writer texts, KB delivery): the writers resume from it
        _copy_into(src, c.work / "cards")
        _record_if_complete(stage, c, manifest, t0, src)
    elif (src / "manifest.json").is_file() and stage.sets:
        # an artifact set made outside the pipeline (by the stage's owner): served as the stage's first set
        _copy_into(src, c.artifacts / stage.sets[0])
        _record_if_complete(stage, c, manifest, t0, src)
    else:
        outs = stage.outputs(c)
        if len(outs) != 1 or not src.is_dir():
            raise SystemExit(
                f"adopt: {stage.name} has no single output directory, or {src} is not a directory"
            )
        _copy_into(src, outs[0])
        record(stage, c, manifest, "adopted", time.monotonic() - t0, adopted_from=str(src))
    print(f"adopt: {stage.name} <- {src}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts/mvp/pipeline.py", description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"release root (default {DEFAULT_ROOT})")
    ap.add_argument("--anchor", default=DEFAULT_ANCHOR, help=f"T of the history (default {DEFAULT_ANCHOR})")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the stale stages in order")
    r.add_argument("--dry-run", action="store_true", help="print each stage's state and reason; run nothing")
    r.add_argument("--only", help="comma-separated stages")
    r.add_argument("--from", dest="start", help="start at this stage")
    r.add_argument("--force", help="comma-separated stages to run even when fresh")
    r.add_argument("--llm", action="store_true", help="allow Nemotron writers through Token Factory")
    r.add_argument("--kb", action="store_true", help="allow loading the KB of the variator-mvp stack (hours)")
    r.add_argument("--publish", action="store_true", help="allow copying the sets into the running stack")
    sub.add_parser("status", help="print the record of every stage")
    a = sub.add_parser("adopt", help="record outputs made before the pipeline")
    a.add_argument("stage", choices=sorted(BY_NAME))
    a.add_argument("--from", dest="source", required=True)
    args = ap.parse_args(argv)
    args.root = args.root.resolve()
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "status":
        return cmd_status(args)
    return cmd_adopt(args)


if __name__ == "__main__":
    sys.exit(main())
