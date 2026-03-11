from __future__ import annotations

import sys

from src.cli.parser import build_parser
from src.live.analysis import run_analysis
from src.live.auto_analysis import run_auto_analysis
from src.live.mic import run_mic_list_devices, run_mic_listen, run_mic_publish
from src.live.tingwu import run_tingwu_process
from src.scan.service import run_scan


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "scan":
        return run_scan(args)
    if args.command == "analysis":
        return run_analysis(args)
    if args.command == "auto-analysis":
        return run_auto_analysis(args)
    if args.command == "tingwu-process":
        return run_tingwu_process(args)
    if args.command == "mic-listen":
        return run_mic_listen(args)
    if args.command == "mic-publish":
        return run_mic_publish(args)
    if args.command == "mic-list-devices":
        return run_mic_list_devices(args)

    print(f"Unsupported command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
