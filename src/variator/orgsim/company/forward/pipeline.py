"""Day level of case B: paths -> 3 examples per fork -> texts -> deliveries -> intake -> KB.

One command: ``python scripts/mvp/branch_day.py`` (it passes the text step of ``narrative.branch``).
Steps (``branches``):

1. Paths: the leaf examples of the week tree (WP18 ``week-tree.json``: the two best forks, leaves at
   the 10%, 50% and 90% points of the leaf cash). Their key events (incidents with severity and restore
   time, client notices, upsells, scope cuts, late payers, deals won, resignations and joins, shocks) are
   forced into their week; the day run lays out the days and adds the daily detail.
2. Without a week tree: ``n`` forward runs of each fork (common random numbers across the forks) and the
   runs at p10, p50 and p90 of cash at the end of the window.
3. Texts: ``writer`` (a callback) writes the notable events of all examples (``<out>/texts``).
4. Deliveries: ``<out>/branches/<tenant>/delivery`` (corpus contract), ``intake validate`` into
   ``<out>/branches/<tenant>/intake``.
5. Day data: ``<day_out>/day_examples.json`` and ``manifest.json`` for the case page.
6. Optional load: ``make -C deploy/mvp seed`` into the tenant of each branch (``<out>/load.json``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from variator.orgsim.company.forward import day as dayfile
from variator.orgsim.company.forward.deliver import write_branch
from variator.orgsim.company.forward.fan import Cube, WeekFan
from variator.orgsim.company.forward.forks import Case
from variator.orgsim.company.forward.params import build_params
from variator.orgsim.company.forward.sim import ForwardRun, run_forward
from variator.orgsim.company.forward.state import StateAtT
from variator.orgsim.streams import Streams, scenario_part

ROOT = Path(__file__).resolve().parents[5]
KINDS = (("pessimistic", 10), ("median", 50), ("optimistic", 90))
MIN_FREE_GB = 8.0
# runs by tenant, work directory, log -> (texts by tenant and text slot, planned texts by tenant, summary).
# The text step lives in variator.narrative.branch: orgsim does not import narrative (no package cycle).
Writer = Callable[
    [dict[str, ForwardRun], Path, Callable[[str], None]],
    tuple[dict[str, dict[str, dict[str, str]]], dict[str, int], dict[str, Any]],
]
# a run -> (metric definitions {id, label, unit, higher, description}, {date: {metric id: value}})
DayMetrics = Callable[[ForwardRun], tuple[list[dict[str, Any]], dict[str, dict[str, float]]]]


def tenant_of(fork: str, pct: int) -> str:
    return f"company-b-{fork}-p{pct}"


def _ordinal(n: int) -> str:
    tail = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{tail}"


WEEK_TREE = Path("/tmp/variator-1000/mvp-artifacts/future/company_best_year/week-tree.json")
FAN_CUBE = WEEK_TREE.with_name("cube")  # WP18's month Monte Carlo: 1,000 scenarios per fork
WEEK_FAN = WEEK_TREE.with_name("month.json")  # its options[fork].weekFan: the week ensemble on day 26 Apr


def one_run(
    st: StateAtT,
    case: Case,
    fork: str,
    k: int,
    seed: int,
    months: int,
    tenant: str = "company",
    path: list[dict[str, Any]] | None = None,
) -> ForwardRun:
    streams = Streams(seed, (scenario_part(k),))
    P = build_params(st, streams.rng("params", "company", "draw"))
    fk = case.fork(fork, st.anchor, streams)
    return run_forward(st, P, fk, k, seed, months, tenant=tenant, path=path)


def _example(
    run: ForwardRun,
    kind: str,
    tenant: str,
    q: int,
    n: int,
    leaf: dict[str, Any] | None,
    tree_kind: str | None = None,
) -> dict[str, Any]:
    ex = dayfile.example(run, kind, tenant, q, n, leaf=leaf["leaf"] if leaf else None, tree_kind=tree_kind)
    if leaf:  # the week level of the same path, to compare with the days (the money is bottom-up here)
        weeks = (run.end - run.start).days // 7 + 1
        ex["weekCash"] = [
            {"week": w, "cash": round(v["cash"], 2)} for w, v in enumerate(leaf["weekly"]) if v
        ][:weeks]
    return ex


def leaves(tree_path: Path) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]]]:
    """Forks and leaf examples of the week tree (WP18 ``variator.future.tree/1``).

    The scenario of an example is its leaf node index + 1: the genealogy of the tree is the same in
    every fork, so the same leaf in two forks gets the same day streams (common random numbers).
    """
    t = json.loads(tree_path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for f in t["forks"]:
        wt = t["weekTrees"][f]
        values = {n["id"]: n["values"] for lvl in wt["levels"] for n in lvl}
        for ex in wt["examples"]:
            k = int(ex["leaf"].split("n", 1)[1]) + 1
            weekly = [values.get(n) for n in ex.get("nodes") or []]
            out.setdefault(f, {})[ex["kind"]] = {
                "scenario": k,
                "leaf": ex["leaf"],
                "events": ex["events"],
                "keys": ex.get("keys"),
                "weekly": weekly,
                "seed": ex.get("seed"),
            }
    return list(t["forks"]), out


def path_check(run: ForwardRun, leaf: dict[str, Any]) -> dict[str, Any]:
    """The day run against its week path: key events applied, incidents per week, cash at week ends."""
    weeks = (run.end - run.start).days // 7
    inc_day = [0] * (weeks + 1)
    for r in run.incidents:
        w = (r.first_ts.date() - run.start).days // 7 + 1
        if w <= weeks:
            inc_day[w] += 1
    inc_path = [0] * (weeks + 1)
    for e in leaf["events"]:
        if e["type"] == "incident" and e["week"] <= weeks:
            inc_path[e["week"]] += 1
    cash = {x["date"]: x["values"]["cash"] for x in run.series}
    gaps = []
    for w in range(1, weeks + 1):
        v = leaf["weekly"][w] if w < len(leaf["weekly"]) else None
        days = run.week_days(w)
        if v and days:
            gaps.append(cash[days[-1].isoformat()] - v["cash"])
    applied = [x for x in run.path_log if x["applied"]]
    return {
        "leaf": leaf["leaf"],
        "path_events_in_window": len(run.path_log),
        "applied": len(applied),
        "not_applied": [x for x in run.path_log if not x["applied"]][:10],
        "incidents_per_week_equal": inc_day == inc_path,
        "incidents": [sum(inc_day), sum(inc_path)],
        "cash_gap_week_end_mean_abs": round(sum(abs(g) for g in gaps) / max(len(gaps), 1), 0),
        "cash_gap_last_week": round(gaps[-1], 0) if gaps else None,
    }


def ensemble(
    st: StateAtT,
    case: Case,
    forks: list[str],
    n: int,
    seed: int,
    months: int,
    log: Callable[[str], None] = print,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for f in forks:
        t0 = time.perf_counter()
        rows = []
        for k in range(1, n + 1):
            s = one_run(st, case, f, k, seed, months).summary()
            rows.append({"scenario": k, **s})
        out[f] = rows
        log(f"ensemble {f}: {n} runs in {time.perf_counter() - t0:.1f} s")
    return out


def select(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """kind -> (scenario, percentile): nearest rank of cash at the end of the window."""
    order = sorted(rows, key=lambda r: (r["cash_end"], r["scenario"]))
    out = {}
    for kind, q in KINDS:
        i = round(q / 100 * (len(order) - 1))
        out[kind] = (order[i]["scenario"], q)
    return out


def free_gb(path: str = "/") -> float:
    s = os.statvfs(path)
    return s.f_bavail * s.f_frsize / 1e9


def validate(delivery: Path, out: Path) -> dict[str, Any]:
    kb = ROOT / "kb"
    env = {**os.environ, "KB_ENV": "test", "PYTHONPATH": f"{kb / 'sim'}{os.pathsep}{kb / 'app'}"}
    cp = subprocess.run(
        [sys.executable, "-m", "kbsim.intake", "validate", str(delivery), "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    rep = json.loads((out / "validate.json").read_text(encoding="utf-8"))
    return {
        "errors": rep["errors"],
        "warnings": rep["warnings"],
        "failed": rep["failed_checks"],
        "warned": rep["warned_checks"],
        "returncode": cp.returncode,
    }


COMPOSE = ["docker", "compose", "-p", "variator-mvp", "-f", str(ROOT / "deploy" / "mvp" / "compose.yaml")]
API_PROBE = "http://127.0.0.1:8088/workspace/api/kb/company/dates"  # the app under /workspace/ (WP27)
ANALYZE = "ANALYZE causal_link; ANALYZE entity; ANALYZE entity_version; ANALYZE statement; ANALYZE chunk;"


def load(
    delivery: Path, tenant: str, parts: int, log_path: Path, *, threads: int = 3, seed: bool = True
) -> dict[str, Any]:
    """Replay a branch delivery into ``tenant`` and drain its projections in a one-off kb container.

    The one-off container (``compose run``, not ``exec`` into ``kb-api``) runs under ``nice`` with
    ``parts`` drain processes of ``threads`` ONNX threads each, so the KB API keeps its CPU. ``kb-worker``
    stays stopped while the journal loads (as ``make seed``); the caller starts it again. ``seed=False``
    only drains (a load that stopped after its replay). The tenant ``company`` is refused.
    """
    if tenant == "company" or not tenant.startswith("company-b-"):
        raise ValueError(f"refusing to load a branch into the tenant {tenant}")
    if free_gb() < MIN_FREE_GB:
        raise RuntimeError(f"disk guard: {free_gb():.1f} GB free, {MIN_FREE_GB} GB needed")

    def one_off(step: str) -> list[str]:
        return [
            *COMPOSE, "run", "--rm", "--no-deps", "-T", "-e", f"KB_EMBED_THREADS={threads}",
            "-v", f"{delivery / 'fixtures'}:/srv/fixtures/{tenant}:ro",
            "-v", f"{delivery / 'config'}:/srv/config/tenants/{tenant}:ro",
            "kb-api", "nice", "-n", "15", "sh", "-c", step,
        ]  # fmt: skip

    steps = []
    if seed:
        steps.append(one_off(f"python -m kb.cli seed --tenant {tenant} --reset --no-background"))
    # statistics of the new tenant before the drain: a plan made before autoanalyze saturates Postgres
    steps.append([*COMPOSE, "exec", "-T", "postgres", "psql", "-U", "kb", "-d", "kb", "-c", ANALYZE])
    steps.append(
        one_off(
            f"python -m kb.cli drain --tenant {tenant} --until-idle --consumer descriptors "
            f"--consumer search --consumer graph --parts {parts}"
        )
    )
    t0 = time.perf_counter()
    probes: list[float] = []
    rc = 0
    with log_path.open("a", encoding="utf-8") as fh:
        subprocess.run([*COMPOSE, "stop", "kb-worker"], stdout=fh, stderr=subprocess.STDOUT, check=False)
        for cmd in steps:
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)
            while proc.poll() is None:
                probes.append(_probe())
                fh.write(f"api probe: {probes[-1]:.3f} s\n")
                fh.flush()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            rc = proc.returncode
            if rc != 0:
                break
    ok = [p for p in probes if p >= 0]
    return {
        "tenant": tenant,
        "seconds": round(time.perf_counter() - t0, 1),
        "returncode": rc,
        "free_gb_after": round(free_gb(), 2),
        "parts": parts,
        "threads": threads,
        "api_probe_max_s": round(max(ok), 3) if ok else None,
        "api_probe_failed": sum(1 for p in probes if p < 0),
    }


def _probe() -> float:
    """Seconds of one KB API request through caddy; -1 when it fails or is not a 200."""
    import urllib.request

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(API_PROBE, timeout=10) as r:
            return time.perf_counter() - t0 if r.status == 200 else -1.0
    except Exception:  # noqa: BLE001 - any failure is a failed probe
        return -1.0


def branches(
    *,
    history: Path,
    actors: Path,
    out: Path,
    day_out: Path | None,
    forks: list[str],
    case_path: Path | None = None,
    n: int = 30,
    seed: int = 7,
    months: int = 6,
    writer: Writer | None = None,
    week_tree: Path | None = WEEK_TREE,
    only: list[str] | None = None,
    other_forks: list[str] | None = None,
    day_metrics: DayMetrics | None = None,
    fan_cube: Path | None = FAN_CUBE,
    week_fan: Path | None = WEEK_FAN,
    do_load: bool = False,
    parts: int = 3,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """The whole day level; ``writer`` writes the texts of the notable events (``narrative.branch``)."""
    t_start = time.perf_counter()
    out.mkdir(parents=True, exist_ok=True)
    st = StateAtT(history, actors)
    case = Case(case_path)
    report: dict[str, Any] = {
        "case": case.id,
        "forks": forks,
        "seed": seed,
        "months": months,
        "ensemble": n,
        "anchor": st.anchor.isoformat(),
        "history_run_id": st.manifest.get("run_id"),
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # 1-2. the week-tree leaves (WP18), else an ensemble and its p10/p50/p90
    chosen: dict[str, dict[str, tuple[int, int]]] = {}
    paths: dict[str, dict[str, dict[str, Any]]] = {}
    if week_tree is not None and week_tree.is_file():
        tree_forks, paths = leaves(week_tree)
        forks = forks or tree_forks
        report["forks"] = forks
        report["week_tree_sha256"] = hashlib.sha256(week_tree.read_bytes()).hexdigest()
        for f in forks:
            chosen[f] = {kind: (paths[f][kind]["scenario"], q) for kind, q in KINDS}
        report["selection"] = (
            f"the three leaf examples of each fork in {week_tree}; their key events are forced into their "
            "weeks; pessimistic, median and optimistic = their order of cash at the end of the day window"
        )
    else:
        forks = forks or ["pay_tech_debt", "hire_5"]
        report["forks"] = forks
        ens = ensemble(st, case, forks, n, seed, months, log)
        (out / "ensemble.json").write_text(json.dumps(ens, indent=1) + "\n", encoding="utf-8")
        for f in forks:
            chosen[f] = select(ens[f])
        report["selection"] = f"p10/p50/p90 of cash at the end of the window over {n} runs per fork"
    report["chosen"] = {
        f: {k: {"scenario": s, "percentile": q} for k, (s, q) in v.items()} for f, v in chosen.items()
    }
    # 3. the chosen runs and their texts. With a week tree, the three leaves of a fork are labelled
    # pessimistic, median and optimistic by their cash at the end of the day window (the tree ranks them
    # after 52 weeks; a 52-week rank can disagree with the window); ``tree_kind`` keeps the tree's label.
    runs: dict[str, tuple[ForwardRun, str, int, str]] = {}
    checks: dict[str, Any] = {}
    tree_kind: dict[str, str] = {}
    leaf_of: dict[str, dict[str, Any] | None] = {}
    for f in forks:
        made = []
        for kind, (k, q) in chosen[f].items():
            leaf = paths.get(f, {}).get(kind)
            run = one_run(
                st, case, f, k, seed, months, tenant_of(f, q), path=leaf["events"] if leaf else None
            )
            made.append((run, kind, q, leaf))
        if paths:
            made.sort(key=lambda x: (x[0].series[-1]["values"]["cash"], x[0].k))
            made = [(run, kind, q, leaf) for (run, _, _, leaf), (kind, q) in zip(made, KINDS, strict=True)]
        for run, kind, q, leaf in made:
            tenant = tenant_of(f, q)
            run.tenant = tenant
            leaf_of[tenant] = leaf
            if leaf:
                checks[tenant] = path_check(run, leaf)
                tree_kind[tenant] = next(kk for kk, v in paths[f].items() if v["leaf"] == leaf["leaf"])
            runs[tenant] = (run, kind, q, f)
    report["tree_kind"] = tree_kind
    texts_by_tenant: dict[str, dict[str, dict[str, str]]] = {t: {} for t in runs}
    planned: dict[str, int] = {t: 0 for t in runs}
    wsum: dict[str, Any] = {"mode": "none"}
    if writer is not None:
        texts_by_tenant, planned, wsum = writer({t: v[0] for t, v in runs.items()}, out / "texts", log)
    report["texts_planned"] = sum(planned.values())
    report["writers"] = wsum
    # 4. deliveries and intake (``only``: these tenants; the rows of the others stay from the last report)
    old_rows = {}
    if only and (out / "report.json").is_file():
        old_rows = {b["tenant"]: b for b in json.loads((out / "report.json").read_text())["branches"]}
    report["branches"] = []
    examples: dict[str, list[dict[str, Any]]] = {f: [] for f in forks}
    for tenant, (run, kind, q, f) in runs.items():
        leaf = leaf_of.get(tenant)
        if only and tenant not in only and tenant in old_rows:
            report["branches"].append(old_rows[tenant])
            examples[f].append(_example(run, kind, tenant, q, n, leaf, tree_kind.get(tenant)))
            continue
        bdir = out / "branches" / tenant
        if bdir.exists():
            shutil.rmtree(bdir)
        t0 = time.perf_counter()
        label = f"{run.fork.label}, {kind}"
        extra = {"kind": kind, "percentile": q}
        extra.update(
            {"week_tree": str(week_tree), "leaf": leaf["leaf"], "leaf_keys": leaf["keys"]}
            if leaf
            else {"ensemble": n}
        )
        man = write_branch(run, bdir, tenant=tenant, label=label, texts=texts_by_tenant[tenant], extra=extra)
        gen_s = round(time.perf_counter() - t0, 1)
        val = validate(bdir / "delivery", bdir / "intake")
        s = run.summary()
        row = {
            "tenant": tenant,
            "fork": f,
            "kind": kind,
            "percentile": q,
            "scenario": run.k,
            "window": [run.start.isoformat(), run.end.isoformat()],
            "lines": man["counts"]["events"],
            "by_object_type": man["counts"]["by_object_type"],
            "texts": len(texts_by_tenant[tenant]),
            "texts_planned": planned.get(tenant, 0),
            "write_seconds": gen_s,
            "intake": val,
            "summary": s,
            "path_check": checks.get(tenant),
            "leaf": leaf["leaf"] if leaf else None,
            "tree_kind": tree_kind.get(tenant),
            "delivery_sha256": hashlib.sha256(
                (bdir / "delivery" / "fixtures" / "timeline.jsonl.gz").read_bytes()
            ).hexdigest(),
        }
        report["branches"].append(row)
        examples[f].append(_example(run, kind, tenant, q, n, leaf, tree_kind.get(tenant)))
        log(
            f"{tenant}: {row['lines']} lines, {row['texts']}/{row['texts_planned']} texts, "
            f"intake errors {val['errors']}"
        )
    pairs = [(ex, runs[ex["tenant"]][0]) for exs in examples.values() for ex in exs]
    for ex, _ in pairs:
        ex["kb"] = True
    # 4b. day examples of the other forks of the case: an ensemble each, records only (no KB branch)
    others = [f for f in (other_forks if other_forks is not None else list(case.forks)) if f not in forks]
    if others:
        t0 = time.perf_counter()
        ens2 = ensemble(st, case, others, n, seed, months, log)
        for f in others:
            examples[f] = []
            for kind, (k, q) in select(ens2[f]).items():
                run = one_run(st, case, f, k, seed, months)
                ex = _example(run, kind, None, q, n, None)
                ex["kb"] = False
                examples[f].append(ex)
                pairs.append((ex, run))
        report["other_forks"] = {
            "forks": others,
            "ensemble": n,
            "seconds": round(time.perf_counter() - t0, 1),
        }
    # each example is a run of its own: the three daily series of a fork differ
    for f, exs in examples.items():
        series = [tuple(p["values"]["cash"] for p in ex["days"]) for ex in exs]
        if len(set(series)) != len(series):
            raise ValueError(f"{f}: two day examples have the same daily series")
    # daily values of the catalogue metrics, computed on each example's records
    extra_metrics: list[dict[str, Any]] = []
    if day_metrics is not None:
        t0 = time.perf_counter()
        for ex, run in pairs:
            meta, by_day = day_metrics(run)
            extra_metrics = extra_metrics or meta
            for p in ex["days"]:
                p["values"].update(by_day.get(p["date"], {}))
        report["day_metrics"] = {"metrics": [m["id"] for m in extra_metrics], "examples": len(pairs),
                                 "seconds": round(time.perf_counter() - t0, 1)}  # fmt: skip
    # where each example falls among WP18's 1,000 scenarios of its fork at the end of the window: the week
    # ensemble's fan on the same day first, else the month cube (cash at the end of that month)
    fans = [f for f in (WeekFan.at(week_fan), Cube.at(fan_cube)) if f is not None]
    if fans:
        report["fan"] = {"week_fan": str(week_fan), "cube": str(fan_cube), "examples": {}}
        for ex, run in pairs:
            cash = run.series[-1]["values"]["cash"]
            pos = next((p for p in (f.position(run.fork.id, run.days[-1], cash) for f in fans) if p), None)
            if pos is None:
                continue
            ex["percentileAtWindowEnd"] = round(pos["percentile"])
            ex["windowEndFan"] = pos["fan"]
            fan = pos["fan"]
            when = f" (cash at the end of {fan['period']})." if "period" in fan else " that day."
            ex["summary"] += (
                f" Among the {pos['fan']['n']:,} scenarios of the fork in WP18's model, this cash is at "
                f"about the {_ordinal(round(pos['percentile']))} percentile{when}"
            )
            report["fan"]["examples"][f"{run.fork.id}/{ex['kind']}"] = pos["percentile"]
    # 5. day data
    if day_out:
        day_out.mkdir(parents=True, exist_ok=True)
        any_run = next(iter(runs.values()))[0]
        doc = dayfile.day_file(
            case.id,
            st.anchor.isoformat(),
            months,
            seed,
            examples,
            note=(
                "Day examples of the forward run on the learned habits (variator.orgsim.company.forward). "
                f"Each example of {', '.join(forks)} is a KB branch (tenant, kb: true) with the state at T "
                f"and its {months} months; the other forks have records only (kb: false)."
            ),
            params_sources=any_run.P.sources,
            extra_metrics=extra_metrics,
        )
        text = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
        (day_out / "day_examples.json").write_text(text, encoding="utf-8")
        files = [
            {
                "path": "day_examples.json",
                "bytes": len(text.encode()),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
        ]
        (day_out / "manifest.json").write_text(
            json.dumps(
                {
                    "set": "day",
                    "case": case.id,
                    "source": "actors",
                    "schema": "variator.day/1",
                    "made_by": "python scripts/mvp/branch_day.py",
                    "files": files,
                    "tenants": sorted(runs),
                },
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        report["day_out"] = str(day_out)
    # 6. load
    if do_load:
        report["load"] = []
        for tenant in runs:
            bdir = out / "branches" / tenant
            r = load(bdir / "delivery", tenant, parts, bdir / "load.log")
            report["load"].append(r)
            log(f"load {tenant}: {r['seconds']} s, rc {r['returncode']}, free {r['free_gb_after']} GB")
            (out / "load.json").write_text(json.dumps(report["load"], indent=1) + "\n", encoding="utf-8")
            if r["returncode"] != 0:
                break
    report["seconds"] = round(time.perf_counter() - t_start, 1)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return report
