"""CLI of the actor cards: ``python -m variator.actors.cards.cli <step> ...`` (state in ``--work``).

Steps:
  build      card skeletons from the WP10 set, the history and the case configs -> <work>/plan.json
  ids        the KB entity ids that a citation may use (KB API, admin) -> <work>/kb_entities.json
  write      the cards extracted by Nemotron Ultra from the facts and the actor's records, checked; retries
             -> <work>/writer/texts.json (the records the requests carried: <work>/writer/records.json)
  artifacts  the artifact set (manifest kind actor-cards) -> --out (default <actors>/cards)
  export     the KB delivery of the cards (+ kbsim intake validate) -> <work>/delivery
  append     load the delivery into a tenant of the MVP stack with kb.cli replay (never --reset)
  all        build, ids, write, artifacts, export (no append)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from variator.actors.cards import artifacts as A
from variator.actors.cards import export as E
from variator.actors.cards import kbids
from variator.actors.cards.build import build_plan
from variator.actors.cards.records import BUDGET_CHARS, records_for
from variator.actors.cards.writer import CardWriters
from variator.llm.models import ROLE_MODEL
from variator.narrative.check import Names

REPO = Path(__file__).resolve().parents[4]
DEFAULTS = {
    "actors": "/tmp/variator-1000/mvp-artifacts/actors",
    "history": "/tmp/variator-1000/mvp-history-v2",
    "cases": str(REPO / "workspace" / "config" / "cases"),
    "kb_api": "http://127.0.0.1:8720",
    "at": "2026-10-26T19:00:00+00:00",
}
COMPOSE = ["docker", "compose", "-p", "variator-mvp", "-f", str(REPO / "deploy" / "mvp" / "compose.yaml")]
MIN_FREE_GB = 8


def _plan(work: Path) -> dict[str, Any]:
    return json.loads((work / "plan.json").read_text(encoding="utf-8"))


def _names(plan: dict[str, Any]) -> Names:
    hist = Path(plan["source"]["history"])
    return Names.from_people_yaml(yaml.safe_load((hist / "delivery" / "config" / "people.yaml").read_text()))


def _records(work: Path, plan: dict[str, Any], make: bool, budget: int = BUDGET_CHARS) -> dict[str, Any]:
    """The records each card's requests carry. Made once from the plan's history (cut at T) and kept in
    <work>/writer/records.json, so a later check (artifacts, a rerun) reads the same records."""
    path = work / "writer" / "records.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    hist = plan["source"].get("history")
    if not make or not hist or not (Path(hist) / "datasets").is_dir():
        return {}
    from variator.actors.history import load_history

    h = load_history(hist, plan["t"], timeline=False)
    recs = records_for(h, plan["cards"], kbids.load(work / "kb_entities.json"), budget)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
    return recs


def _writers(
    work: Path, plan: dict[str, Any], records: bool = False, budget: int = BUDGET_CHARS, **kw: Any
) -> CardWriters:
    known = kbids.load(work / "kb_entities.json")
    recs = _records(work, plan, records, budget)
    return CardWriters(plan, work / "writer", _names(plan), known, records=recs, **kw)


def cmd_build(a: argparse.Namespace) -> int:
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    plan = build_plan(a.actors, a.history, a.cases, a.research)
    (work / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    groups: dict[str, int] = {}
    for c in plan["cards"]:
        groups[c["group"]] = groups.get(c["group"], 0) + 1
    print(f"build: {len(plan['cards'])} cards {groups}, {len(plan['risks'])} risks, stats {plan['stats']}")
    return 1 if plan["stats"]["stale_case_items"] else 0


def cmd_ids(a: argparse.Namespace) -> int:
    ids = kbids.fetch(a.kb_api, a.tenant)
    kbids.save(ids, Path(a.work) / "kb_entities.json")
    print(f"ids: {len(ids)} KB entities of tenant {a.tenant}")
    return 0


def cmd_write(a: argparse.Namespace) -> int:
    work = Path(a.work)
    w = _writers(
        work,
        _plan(work),
        records=not a.no_records,
        budget=a.record_budget,
        parallel=a.parallel,
        max_attempts=a.max_attempts,
        model=a.model,
    )
    s = w.run(a.only or None)
    (work / "writer_summary.json").write_text(json.dumps(s, indent=1), encoding="utf-8")
    print("write:", json.dumps(s))
    return 0


def cmd_artifacts(a: argparse.Namespace) -> int:
    work = Path(a.work)
    plan = _plan(work)
    out = Path(a.out or Path(plan["source"]["actors"]) / "cards")
    w = _writers(work, plan)
    summary = (
        json.loads((work / "writer_summary.json").read_text())
        if (work / "writer_summary.json").is_file()
        else {}
    )
    m = A.write_set(plan, w.accepted(), w.known, out, summary)
    print(f"artifacts: {m['counts']} -> {out}")
    return 0


def cmd_export(a: argparse.Namespace) -> int:
    work = Path(a.work)
    plan = _plan(work)
    w = _writers(work, plan)
    base = Path(a.base_delivery or Path(plan["source"]["history"]) / "delivery")
    out = Path(a.out or work / "delivery")
    m = E.export_delivery(plan, w.accepted(), w.known, base, out, a.at)
    print(f"export: {m['counts']['events']} events -> {out}")
    if a.no_validate:
        return 0
    rep = E.intake_validate(out, work / "intake")
    print(
        "intake:",
        json.dumps({k: rep.get(k) for k in ("errors", "warnings", "failed_checks", "warned_checks")}),
    )
    return 1 if rep.get("errors") else 0


def cmd_append(a: argparse.Namespace) -> int:
    """kb.cli replay of the card delivery into a tenant: adds versions, never drops the tenant."""
    delivery = Path(a.delivery).resolve()
    if not (delivery / "fixtures" / "timeline.jsonl.gz").is_file():
        print(f"append: no fixtures/timeline.jsonl.gz in {delivery}", file=sys.stderr)
        return 2
    free = shutil.disk_usage("/").free / 2**30
    if free < MIN_FREE_GB:
        print(f"append: {free:.1f} GB free, {MIN_FREE_GB} GB needed: refused", file=sys.stderr)
        return 2
    cmd = COMPOSE + [
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "-v",
        f"{delivery / 'fixtures'}:/srv/fixtures/{a.name}:ro",
        "kb-api",
        "python",
        "-m",
        "kb.cli",
        "replay",
        "--tenant",
        a.tenant,
        "--fixtures",
        a.name,
    ]
    assert "--reset" not in cmd
    print("append:", " ".join(cmd))
    if not a.yes:
        print("append: dry run (pass --yes to load)")
        return 0
    return subprocess.run(cmd).returncode


def cmd_all(a: argparse.Namespace) -> int:
    for step in (cmd_build, cmd_ids, cmd_write, cmd_artifacts, cmd_export):
        code = step(a)
        if code:
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m variator.actors.cards.cli", description=__doc__.split("\n")[0]
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--work", required=True, help="work directory (plan, writer state, delivery)")

    specs = {
        "build": cmd_build,
        "ids": cmd_ids,
        "write": cmd_write,
        "artifacts": cmd_artifacts,
        "export": cmd_export,
        "append": cmd_append,
        "all": cmd_all,
    }
    for name, fn in specs.items():
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        if name != "append":
            common(p)
        if name in ("build", "all"):
            p.add_argument(
                "--actors", default=DEFAULTS["actors"], help="WP10 artifact set (actors.json, facts/...)"
            )
            p.add_argument(
                "--history", default=DEFAULTS["history"], help="history directory (datasets/, delivery/)"
            )
            p.add_argument("--cases", default=DEFAULTS["cases"], help="case configs (workspace/config/cases)")
            p.add_argument(
                "--research",
                default=None,
                help="ResearchBundle JSON with checked web facts for actor and risk context",
            )
        if name in ("ids", "all"):
            p.add_argument("--kb-api", default=DEFAULTS["kb_api"])
            p.add_argument("--tenant", default="company")
        if name in ("write", "all"):
            p.add_argument("--parallel", type=int, default=4)
            p.add_argument("--max-attempts", type=int, default=3)
            p.add_argument("--model", default=ROLE_MODEL["actor_cards"])
            p.add_argument("--only", nargs="*", help="card ids (default: all)")
            p.add_argument("--no-records", action="store_true", help="facts only: no records in the requests")
            p.add_argument(
                "--record-budget", type=int, default=BUDGET_CHARS, help="characters of records per card"
            )
        if name in ("artifacts", "all", "export"):
            p.add_argument("--out", default=None, help="output directory")
        if name in ("export", "all"):
            p.add_argument("--base-delivery", default=None, help="base delivery (config, manifest)")
            p.add_argument(
                "--at", default=DEFAULTS["at"], help="KB time of the cards (before T, after the data)"
            )
            p.add_argument("--no-validate", action="store_true")
        if name == "append":
            p.add_argument("--delivery", required=True)
            p.add_argument("--tenant", default="company")
            p.add_argument("--name", default="actor-cards", help="fixtures directory name in the container")
            p.add_argument("--yes", action="store_true", help="really load (default: print the command)")
    a = ap.parse_args(argv)
    if a.cmd == "all":
        a.only, a.out = a.only or None, None
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
