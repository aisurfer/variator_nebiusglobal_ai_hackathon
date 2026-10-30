"""``python -m variator.agent "question" [--tools a,b] [--model super]``: one agent run, printed as JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from variator.agent.loop import CORE_TOOLS, DecisionContract, run_agent
from variator.llm import TokenFactoryClient
from variator.mcp import Services, build_server, discover
from variator.mcp.warmup import warm_up
from variator.telemetry import stats_from_env


def main() -> None:
    parser = argparse.ArgumentParser(prog="variator-agent")
    parser.add_argument("question")
    parser.add_argument("--model", default="super")
    parser.add_argument("--extra-tools", default="", help="comma-separated tool names added to the core set")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--contract", help="JSON fixed decision contract for comparable runs")
    args = parser.parse_args()
    warm_up()
    plugins = discover()
    llm = TokenFactoryClient()
    server = build_server(plugins, services=Services(plugins, llm=llm, stats=stats_from_env()))
    extra = [t for t in args.extra_tools.split(",") if t]
    contract = DecisionContract.from_json(args.contract) if args.contract else None
    result = asyncio.run(
        run_agent(
            args.question,
            server=server,
            llm=llm,
            model=args.model,
            tools=CORE_TOOLS,
            extra_tools=extra,
            max_steps=args.max_steps,
            contract=contract,
        )
    )
    print(json.dumps(asdict(result), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
