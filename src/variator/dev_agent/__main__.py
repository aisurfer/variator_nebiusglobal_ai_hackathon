"""Run the workspace-bound Nemotron developer agent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from variator.dev_agent.agent import run_dev_agent
from variator.llm import TokenFactoryClient


def main() -> None:
    parser = argparse.ArgumentParser(prog="variator-dev-agent")
    parser.add_argument("request")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--model", default="super")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--allow-git-write", action="store_true")
    args = parser.parse_args()
    result = run_dev_agent(
        args.request,
        workspace=args.workspace,
        llm=TokenFactoryClient(),
        model=args.model,
        max_steps=args.max_steps,
        allow_git_write=args.allow_git_write,
    )
    print(
        json.dumps(
            {
                "answer": result.answer,
                "steps": [step.__dict__ for step in result.tool_loop.steps],
                "model": result.tool_loop.final.model,
            },
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
