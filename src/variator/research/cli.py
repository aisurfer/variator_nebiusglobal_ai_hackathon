"""Run a business research profile and save the checked bundle for actor cards."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from variator.llm import TokenFactoryClient
from variator.research import TavilyCache, TavilyClient, business_profile_queries, gather_external_facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business", required=True)
    parser.add_argument("--geography", default="")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-queries", type=int, default=24)
    parser.add_argument("--results-per-query", type=int, default=5)
    parser.add_argument(
        "--search-depth", choices=("basic", "advanced", "fast", "ultra-fast"), default="basic"
    )
    parser.add_argument("--extract-top", type=int, default=12)
    parser.add_argument("--max-facts", type=int, default=50)
    args = parser.parse_args(argv)

    queries = business_profile_queries(args.business, geography=args.geography, max_queries=args.max_queries)
    location = f" in {args.geography.strip()}" if args.geography.strip() else ""
    question = f"What external facts define the business of {args.business.strip()}{location}?"
    with TavilyClient(cache=TavilyCache.from_env()) as tavily:
        bundle = gather_external_facts(
            question,
            tf_client=TokenFactoryClient(),
            tavily=tavily,
            queries=queries,
            results_per_query=args.results_per_query,
            search_depth=args.search_depth,
            extract_top=args.extract_top,
            model="lightning",
            max_facts=args.max_facts,
            max_workers=4,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "queries": len(bundle.queries),
                "sources": len(bundle.results),
                "facts": len(bundle.cited_facts),
                "tavily_credits": bundle.tavily_credits,
                "warnings": bundle.warnings,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
