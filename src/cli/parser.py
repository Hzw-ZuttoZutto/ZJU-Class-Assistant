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
    watch.add_argument(
        "--rt-insight-enabled",
        action="store_true",
        help="Enable realtime key information extraction from teacher audio",
    )
    watch.add_argument(
        "--rt-chunk-seconds",
        type=int,
        default=10,
        help="Audio chunk duration in seconds for realtime insight",
    )
    watch.add_argument(
        "--rt-context-window-seconds",
        type=int,
        default=180,
        help="Historical summary context window in seconds for realtime insight",
    )
    watch.add_argument(
        "--rt-model",
        default="gpt-5-mini",
        help="OpenAI text model for realtime insight analysis",
    )
    watch.add_argument(
        "--rt-stt-model",
        default="gpt-4o-mini-transcribe",
        help="OpenAI speech-to-text model for realtime insight",
    )
    watch.add_argument(
        "--rt-keywords-file",
        default="config/realtime_keywords.json",
        help="Keyword configuration file path for realtime insight",
    )
    watch.add_argument(
        "--rt-request-timeout-sec",
        type=float,
        default=12.0,
        help="Per-request timeout seconds for realtime insight model call",
    )
    watch.add_argument(
        "--rt-retry-count",
        type=int,
        default=2,
        help="Retry count when realtime insight model call fails",
    )
    watch.add_argument(
        "--rt-alert-threshold",
        type=int,
        default=90,
        help="Urgency threshold for [ALERT] console output",
    )
    watch.add_argument(
        "--rt-max-concurrency",
        type=int,
        default=5,
        help="Maximum concurrent async workers for realtime insight pipeline",
    )
    watch.add_argument(
        "--rt-stage-timeout-sec",
        type=float,
        default=60.0,
        help="Max seconds for each stage (STT or analysis) before dropping chunk",
    )
    watch.add_argument(
        "--rt-context-min-ready",
        type=int,
        default=15,
        help="Minimum ready transcript chunks before strict context gate is considered met",
    )
    watch.add_argument(
        "--rt-context-recent-required",
        type=int,
        default=4,
        help="Required most-recent transcript chunks that must be present for context gate",
    )
    watch.add_argument(
        "--rt-context-wait-timeout-sec",
        type=float,
        default=15.0,
        help="Max seconds waiting for context gate; timeout falls back to partial context",
    )

    simulate = subparsers.add_parser(
        "simulate",
        help="Run offline simulator for realtime insight pipeline with configurable scenarios",
    )
    simulate.add_argument("--mode", type=int, choices=[1, 2, 3, 4, 5], required=True, help="Simulator mode")
    simulate.add_argument(
        "--scenario-file",
        required=True,
        help="YAML scenario file path under tests/simulator/scenarios/modeX/",
    )
    simulate.add_argument(
        "--sim-root",
        default="tests/simulator",
        help="Simulator root directory (contains mp3_inputs/scenarios/cache/runs)",
    )
    simulate.add_argument(
        "--mp3-dir",
        default="tests/simulator/mp3_inputs",
        help="Directory containing prerecorded mp3 files for simulation",
    )
    simulate.add_argument(
        "--run-dir",
        default="tests/simulator/runs",
        help="Directory where simulation run outputs are written",
    )
    simulate.add_argument(
        "--chunk-seconds",
        type=int,
        default=10,
        help="Target chunk duration seconds for preprocessing and feed scheduling",
    )
    simulate.add_argument(
        "--precompute-workers",
        type=int,
        default=4,
        help="Workers used by precompute stage for mode2/mode3",
    )
    simulate.add_argument(
        "--rt-model",
        default="gpt-5-mini",
        help="OpenAI text model for analysis stage",
    )
    simulate.add_argument(
        "--rt-stt-model",
        default="gpt-4o-mini-transcribe",
        help="OpenAI speech-to-text model for translation stage",
    )
    simulate.add_argument(
        "--rt-keywords-file",
        default="config/realtime_keywords.json",
        help="Keyword config file path reused by analysis stage",
    )
    simulate.add_argument(
        "--rt-request-timeout-sec",
        type=float,
        default=12.0,
        help="Per-request timeout seconds for OpenAI stage calls",
    )
    simulate.add_argument(
        "--rt-stage-timeout-sec",
        type=float,
        default=60.0,
        help="Stage timeout seconds for transcript/analysis retries in simulated pipeline",
    )
    simulate.add_argument(
        "--rt-retry-count",
        type=int,
        default=2,
        help="Retry count for transcript/analysis stage calls",
    )
    simulate.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional seed overriding scenario seed for deterministic feed behavior",
    )

    return parser
