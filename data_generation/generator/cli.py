"""Dispatch de linha de comando: python -m generator <etapa> [flags]."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .paths import DataPaths

STAGE_ORDER = ("ingest", "rank", "contexts", "artifacts", "oracle")


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="generator")
    sub = parser.add_subparsers(dest="stage", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--force", action="store_true")
        p.add_argument("--sample-users", type=int, default=None)
        p.add_argument("--seed", type=int, default=config.DEFAULT_SEED)
        p.add_argument("--data-dir", type=str, default=None)

    for name in (*STAGE_ORDER, "all"):
        p = sub.add_parser(name)
        add_common(p)

    p = sub.add_parser("synthetic")
    add_common(p)
    p.add_argument("--scale", type=int, required=True)
    p.add_argument("--injection-fraction", type=float, default=config.SYNTHETIC_INJECTION_FRACTION)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _base_parser()
    args = parser.parse_args(argv)

    data_dir = DataPaths(Path(args.data_dir)) if args.data_dir else DataPaths.default()

    from . import artifacts, contexts, ingest, oracle, rank, synthetic

    stage_runners = {
        "ingest": lambda: ingest.run(data_dir, args.sample_users, args.seed, args.force),
        "rank": lambda: rank.run(data_dir, args.sample_users, args.seed, args.force),
        "contexts": lambda: contexts.run(data_dir, args.sample_users, args.seed, args.force),
        "artifacts": lambda: artifacts.run(data_dir, args.sample_users, args.seed, args.force),
        "oracle": lambda: oracle.run(data_dir, args.seed, args.force),
    }

    if args.stage == "all":
        for name in STAGE_ORDER:
            stage_runners[name]()
    elif args.stage == "synthetic":
        synthetic.run(
            data_dir,
            scale=args.scale,
            p=args.injection_fraction,
            seed=args.seed,
            force=args.force,
        )
    else:
        stage_runners[args.stage]()

    return 0


if __name__ == "__main__":
    sys.exit(main())
