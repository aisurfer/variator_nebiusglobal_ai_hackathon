"""Regenerate the demo cases: ``python -m variator.synthetic [--out data/demo] [--with-text]``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from variator.synthetic.numeric import generate_demo_case, list_demo_cases
from variator.synthetic.storage import write_demo_case


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(prog="python -m variator.synthetic", description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/demo"), help="root directory for the cases")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--case", action="append", choices=list_demo_cases(), help="repeatable; default all")
    parser.add_argument(
        "--with-text",
        action="store_true",
        help="also write Nemotron-generated text tables via Token Factory (needs NEBIUS_API_KEY)",
    )
    parser.add_argument("--records", type=int, default=None, help="text records per kind (default: per kind)")
    args = parser.parse_args(argv)

    for name in args.case or list_demo_cases():
        case = generate_demo_case(name, seed=args.seed)
        if args.with_text:
            from variator.synthetic.designer import add_text_tables

            case = add_text_tables(case, num_records=args.records)
        paths = write_demo_case(case, args.out / name)
        size = sum(path.stat().st_size for path in paths)
        print(f"{name}: {len(paths)} files, {size / 1024:.1f} KiB -> {args.out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
