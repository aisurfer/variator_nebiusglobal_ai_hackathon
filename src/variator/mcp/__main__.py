"""Run variator-core: ``python -m variator.mcp`` (stdio) or ``--http PORT``."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path

from variator.containers.nebius import runner_from_env
from variator.mcp.plugins import discover
from variator.mcp.server import Services, build_server
from variator.mcp.sessions import ENV_ISOLATE
from variator.mcp.store import DEFAULT_PATH, ENV_STORE, Store
from variator.mcp.warmup import warm_up
from variator.telemetry import stats_from_env


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(prog="variator-mcp")
    parser.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--store",
        default=os.environ.get(ENV_STORE, str(DEFAULT_PATH)),
        help="SQLite file for datasets, cases and runs; 'memory' keeps nothing",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit a non-loopback --host; tools then read and write any path the server can reach",
    )
    args = parser.parse_args()
    if args.http and not args.allow_remote and not is_loopback(args.host):
        # Paths (ingest, code_scan, plugin out_dir) and read_url are not limited yet (backlog task 7).
        parser.error(f"--host {args.host} is not a loopback address; add --allow-remote to accept the risk")
    warm_up()  # before the transport starts: native loads in worker threads hang on Windows stdio
    plugins = discover()
    store = Store(None if args.store == "memory" else Path(args.store))
    # HTTP can serve many users: each handle belongs to its owner (variator.mcp.sessions).
    isolate = bool(args.http) and os.environ.get(ENV_ISOLATE, "1") != "0"
    services = Services(
        plugins, store=store, runner=runner_from_env(), stats=stats_from_env(), isolate=isolate
    )
    server = build_server(plugins, services=services)
    if args.http:
        server.run(transport="http", host=args.host, port=args.http)
    else:
        server.run()


if __name__ == "__main__":
    main()
