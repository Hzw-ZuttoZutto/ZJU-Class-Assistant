from __future__ import annotations

import argparse
import os


def add_common_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--username",
        default="",
        help="Unified auth username (optional if provided in workspace .account file)",
    )
    parser.add_argument(
        "--password",
        default="",
        help="Unified auth password (optional if provided in workspace .account file)",
    )
    parser.add_argument("--tenant-code", default="112", help="Tenant code")
    parser.add_argument("--authcode", default="", help="Captcha code if required")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ZJU classroom tool: scan courses or watch live teacher/ppt streams"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Scan course_id range and match teacher/title")
    add_common_auth_args(scan)
    scan.add_argument("--teacher", required=True, help="Exact teacher name to match")
    scan.add_argument("--title", required=True, help="Exact course title to match")
    scan.add_argument("--center", type=int, default=81889, help="Center course_id")
    scan.add_argument("--radius", type=int, default=200, help="Scan +/- radius")
    scan.add_argument("--workers", type=int, default=min(64, max(4, (os.cpu_count() or 8) * 2)))
    scan.add_argument("--retries", type=int, default=1, help="Per-request retries")
    scan.add_argument("--verbose", action="store_true", help="Print each inspected item")
    scan.add_argument(
        "--require-live",
        action="store_true",
        help="Only keep matched courses that are currently in '直播中' state",
    )
    scan.add_argument(
        "--live-check-timeout",
        type=float,
        default=30.0,
        help="Max seconds to retry live-state detection for each matched candidate",
    )
    scan.add_argument(
        "--live-check-interval",
        type=float,
        default=2.0,
        help="Retry interval seconds for live-state detection",
    )

    watch = subparsers.add_parser(
        "watch",
        help="Continuously discover live streams (meta + livingroom architectures) and play teacher/ppt",
    )
    add_common_auth_args(watch)
    watch.add_argument("--course-id", type=int, required=True, help="Course ID")
    watch.add_argument("--sub-id", type=int, required=True, help="Sub ID")
    watch.add_argument("--poll-interval", type=float, default=10.0, help="Backend poll interval seconds")
    watch.add_argument("--host", default="127.0.0.1", help="Local server host")
    watch.add_argument("--port", type=int, default=8765, help="Local server port")
    watch.add_argument(
        "--open-base-url",
        default="",
        help="URL used for auto-open in browser (for SSH port-forward/local mapping)",
    )
    watch.add_argument("--no-browser", action="store_true", help="Do not auto-open browser windows")

    watch.add_argument(
        "--playlist-retries",
        type=int,
        default=3,
        help="Retries for upstream m3u8 fetch failures",
    )
    watch.add_argument(
        "--asset-retries",
        type=int,
        default=3,
        help="Retries for upstream segment/key fetch failures",
    )
    watch.add_argument(
        "--stale-playlist-grace",
        type=float,
        default=15.0,
        help="Serve cached playlist for this many seconds after upstream failure",
    )
    watch.add_argument(
        "--hls-max-buffer",
        type=int,
        default=20,
        help="HLS maxBufferLength value used by browser player",
    )
    watch.add_argument(
        "--record-dir",
        default="",
        help="Parent directory used for recording session output",
    )
    watch.add_argument(
        "--record-segment-minutes",
        type=int,
        default=10,
        help="Segment duration in minutes; 0 means no split until manual stop",
    )
    watch.add_argument(
        "--record-startup-av-timeout",
        type=float,
        default=15.0,
        help="Fail watch if teacher AV stream is unavailable for this many seconds on startup",
    )
    watch.add_argument(
        "--record-recovery-window-sec",
        type=float,
        default=10.0,
        help="Gap recovery window before marking missing interval",
    )

    return parser
