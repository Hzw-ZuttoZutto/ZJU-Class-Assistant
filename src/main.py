from __future__ import annotations

import sys

from src.cli.parser import build_parser
from src.live.server import run_watch
from src.scan.service import run_scan
from src.simulator.service import run_simulate


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "scan":
        return run_scan(args)
    if args.command == "watch":
        return run_watch(args)
    if args.command == "simulate":
        return run_simulate(args)

    print(f"Unsupported command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
