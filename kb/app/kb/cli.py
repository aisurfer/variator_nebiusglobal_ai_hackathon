"""Command line: python -m kb.cli <migrate|seed|replay|drain|reindex|rederive|stats|gc> [--tenant T]."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

from kb.bootstrap import drop_tenant, ensure_tenant, tenant_config_dir, tenant_people_at
from kb.db import connect, migrate
from kb.ingest.replay import replay, timeline_path
from kb.settings import get_settings
from kb.worker.framework import drain_parallel, drain_processes, drain_until_idle, reset, watermarks

PEOPLE_AT = datetime.fromisoformat("2026-01-12T09:00:00+03:00")  # the roster predates the simulated history
# (a tenant may set its own in sources.yaml: tenant.people_at)


def gc_projections(conn) -> dict:
    """Projections are derived: anything indexed for a tenant that is gone from the journal is garbage."""
    from kb.settings import get_settings

    live = {r["tenant_id"] for r in conn.execute("SELECT tenant_id FROM tenant").fetchall()}
    seen: set[str] = set()
    try:
        from kb.graph import memgraph

        if memgraph.enabled() and memgraph.available():
            seen |= {r["t"] for r in memgraph.read("MATCH (n) RETURN DISTINCT n.tenant AS t") if r.get("t")}
    except Exception as exc:  # a projection store that is down is reported, not fatal
        logging.warning("graph scan failed: %s", exc)
    try:
        from kb.search.opensearch import client

        res = client().search(index=f"{get_settings().os_index_prefix}-*",
                              body={"size": 0, "aggs": {"t": {"terms": {"field": "tenant_id", "size": 10000}}}})
        seen |= {b["key"] for b in res["aggregations"]["t"]["buckets"]}
    except Exception as exc:
        logging.warning("search scan failed: %s", exc)
    orphans = sorted(seen - live)
    for t in orphans:
        reset(conn, t)
    return {"live_tenants": sorted(live), "removed": orphans}


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _seed_problem(tenant: str, fixtures: str | None) -> str | None:
    """Refuse a seed that would bootstrap a tenant from another tenant's config or replay nothing."""
    from kb.connectors.base import load_sources
    from kb.settings import get_settings

    s = get_settings()
    timeline = timeline_path(s.fixtures_dir / (fixtures or tenant))
    if not timeline.is_file():
        return f"no timeline for {fixtures or tenant}: {timeline} (or timeline.jsonl.gz)"
    cfg = tenant_config_dir(tenant)
    if not fixtures and cfg == s.config_dir and load_sources(cfg / "sources.yaml").tenant_id != tenant:
        own = s.config_dir / "tenants" / tenant / "sources.yaml"
        return f"no {own}: refusing to seed {tenant} from the default config"
    return None


def _drain_progress(p: dict) -> None:
    """drain --until-idle progress on stderr: elapsed time, processed and pending rows per consumer."""
    parts = " ".join(f"{k}={p['done'].get(k, 0)}(+{p['pending'].get(k, 0)})" for k in sorted(p["pending"]))
    print(f"drain: {p['seconds']} s · {parts} · journal {p['watermarks'].get('journal')}", file=sys.stderr, flush=True)


def _progress(n: int, counts: dict[str, int]) -> None:
    """Replay progress on stderr (stdout carries the JSON result)."""
    print(f"replay: {n} events " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())), file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="kb.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("gc", help="remove projections (OpenSearch, Memgraph) of tenants that no longer exist in the journal")
    lm = sub.add_parser("link-mentions", help="declare refers_to candidates where a text names a ticket by its whole title")
    lm.add_argument("--tenant", default="company")
    lm.add_argument("--dry-run", action="store_true", help="count the mentions, write nothing")
    for name in ("seed", "replay", "drain", "reindex", "stats"):
        p = sub.add_parser(name)
        p.add_argument("--tenant", default="company")
        if name in ("seed", "replay"):
            p.add_argument("--until", help="replay events up to this ISO time only")
            p.add_argument("--fixtures", help="fixture directory name (default: tenant id)")
        if name == "seed":
            p.add_argument("--reset", action="store_true", help="drop the tenant's journal and projections first")
            p.add_argument("--no-background", action="store_true",
                           help="keep the shared worker away from the tenant while it loads (drain it yourself)")
        if name in ("seed", "replay", "drain", "reindex"):
            p.add_argument("--no-llm", action="store_true",
                           help="accepted for compatibility; this copy has no LLM extraction, so it changes nothing")
        if name in ("drain", "reindex"):
            p.add_argument("--consumer", action="append", help="limit to these consumers")
        if name == "drain":
            p.add_argument("--until-idle", action="store_true",
                           help="consumers in parallel until nothing is pending, no time limit, progress on stderr")
            p.add_argument("--threads", action="store_true",
                           help="with --until-idle: threads of one process instead of a process per consumer")
            p.add_argument("--parts", type=int, default=get_settings().drain_parts or min(8, max(1, (os.cpu_count() or 2) // 2)),
                           help="with --until-idle: processes for search, split by entity "
                                "(KB_DRAIN_PARTS; default half the cores, up to 8)")
    args = ap.parse_args(argv)

    if args.cmd == "migrate":
        _print({"applied": migrate()})
        return 0
    if args.cmd == "gc":
        migrate()
        with connect() as conn:
            _print(gc_projections(conn))
        return 0
    migrate()
    with connect() as conn:
        if args.cmd == "seed":
            problem = _seed_problem(args.tenant, args.fixtures)
            if problem:
                print(f"seed: {problem}", file=sys.stderr)
                return 2
            if args.reset:
                reset(conn, args.tenant)
                drop_tenant(conn, args.tenant)
            stats = ensure_tenant(conn, args.tenant, people_at=tenant_people_at(args.tenant) or PEOPLE_AT,
                                  background=not args.no_background)
            until = datetime.fromisoformat(args.until) if args.until else None
            _print({"bootstrap": stats, "replay": replay(conn, args.tenant, fixtures=args.fixtures, until=until,
                                                         on_progress=_progress)})
        elif args.cmd == "replay":
            until = datetime.fromisoformat(args.until) if args.until else None
            _print(replay(conn, args.tenant, fixtures=args.fixtures, until=until, on_progress=_progress))
        elif args.cmd == "drain":
            if args.until_idle:
                t0 = time.monotonic()
                if args.threads:
                    done = drain_parallel(args.tenant, consumers=args.consumer, progress=_drain_progress)
                else:
                    done = drain_processes(args.tenant, consumers=args.consumer, progress=_drain_progress,
                                           parts=max(1, args.parts))
                _print({"done": done, "seconds": round(time.monotonic() - t0), "watermarks": watermarks(conn, args.tenant)})
            else:
                _print(drain_until_idle(conn, args.tenant, consumers=args.consumer))
        elif args.cmd == "reindex":
            reset(conn, args.tenant, args.consumer)
            _print(drain_until_idle(conn, args.tenant, consumers=args.consumer))
        elif args.cmd == "link-mentions":
            from kb.causal.mentions import link_mentions

            _print(link_mentions(conn, args.tenant, dry_run=args.dry_run))
        elif args.cmd == "stats":
            _print(watermarks(conn, args.tenant))
    return 0


if __name__ == "__main__":
    sys.exit(main())
