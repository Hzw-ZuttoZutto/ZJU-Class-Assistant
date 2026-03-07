from __future__ import annotations

import argparse
import os

from src.simulator.models import DEFAULT_MODE5_PROFILE


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
        default="gpt-4.1-mini",
        help="OpenAI text model for realtime insight analysis",
    )
    watch.add_argument(
        "--rt-stt-model",
        default="whisper-large-v3",
        help="OpenAI speech-to-text model for realtime insight",
    )
    watch.add_argument(
        "--rt-keywords-file",
        default="config/realtime_keywords.json",
        help="Keyword configuration file path for realtime insight",
    )
    watch.add_argument(
        "--rt-api-base-url",
        default="",
        help="Optional OpenAI-compatible API base URL (e.g. https://aihubmix.com/v1)",
    )
    watch.add_argument(
        "--rt-stt-request-timeout-sec",
        type=float,
        default=8.0,
        help="Per-request timeout seconds for realtime STT stage",
    )
    watch.add_argument(
        "--rt-stt-stage-timeout-sec",
        type=float,
        default=32.0,
        help="Stage timeout seconds for realtime STT retries",
    )
    watch.add_argument(
        "--rt-stt-retry-count",
        type=int,
        default=4,
        help="Total attempts allowed for realtime STT stage",
    )
    watch.add_argument(
        "--rt-stt-retry-interval-sec",
        type=float,
        default=0.2,
        help="Wait seconds before STT retry after each failed attempt",
    )
    watch.add_argument(
        "--rt-analysis-request-timeout-sec",
        type=float,
        default=15.0,
        help="Per-request timeout seconds for realtime analysis stage",
    )
    watch.add_argument(
        "--rt-analysis-stage-timeout-sec",
        type=float,
        default=60.0,
        help="Stage timeout seconds for realtime analysis retries",
    )
    watch.add_argument(
        "--rt-analysis-retry-count",
        type=int,
        default=4,
        help="Total attempts allowed for realtime analysis stage",
    )
    watch.add_argument(
        "--rt-analysis-retry-interval-sec",
        type=float,
        default=0.2,
        help="Wait seconds before analysis retry after each failed attempt",
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
        "--rt-context-wait-timeout-sec-1",
        type=float,
        default=1.0,
        help="After recent context is ready, extra wait seconds for full target context",
    )
    watch.add_argument(
        "--rt-context-wait-timeout-sec-2",
        type=float,
        default=5.0,
        help="Timeout seconds while waiting recent context to become ready",
    )

    mic_listen = subparsers.add_parser(
        "mic-listen",
        help="Run standalone microphone upload receiver and realtime insight pipeline on server side",
    )
    mic_listen.add_argument("--host", default="127.0.0.1", help="Listen host")
    mic_listen.add_argument("--port", type=int, default=18765, help="Listen port")
    mic_listen.add_argument(
        "--session-dir",
        default="",
        help="Session directory for realtime output logs; default creates mic session in cwd",
    )
    mic_listen.add_argument(
        "--mic-upload-token",
        default="",
        help="Shared token expected in X-Mic-Token header",
    )
    mic_listen.add_argument(
        "--mic-chunk-max-bytes",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum accepted upload size per chunk",
    )
    mic_listen.add_argument(
        "--mic-chunk-dir",
        default="_rt_chunks_mic",
        help="Chunk directory (relative to session dir if not absolute)",
    )
    mic_listen.add_argument(
        "--rt-chunk-seconds",
        type=float,
        default=10.0,
        help="Expected chunk duration seconds used by downstream analysis config (supports decimal)",
    )
    mic_listen.add_argument(
        "--rt-context-window-seconds",
        type=int,
        default=180,
        help="Historical summary context window in seconds",
    )
    mic_listen.add_argument(
        "--rt-model",
        default="gpt-4.1-mini",
        help="OpenAI text model for realtime insight analysis",
    )
    mic_listen.add_argument(
        "--rt-stt-model",
        default="whisper-large-v3",
        help="OpenAI speech-to-text model for realtime insight",
    )
    mic_listen.add_argument(
        "--rt-keywords-file",
        default="config/realtime_keywords.json",
        help="Keyword configuration file path for realtime insight",
    )
    mic_listen.add_argument(
        "--rt-api-base-url",
        default="",
        help="Optional OpenAI-compatible API base URL",
    )
    mic_listen.add_argument(
        "--rt-stt-request-timeout-sec",
        type=float,
        default=8.0,
        help="Per-request timeout seconds for realtime STT stage",
    )
    mic_listen.add_argument(
        "--rt-stt-stage-timeout-sec",
        type=float,
        default=32.0,
        help="Stage timeout seconds for realtime STT retries",
    )
    mic_listen.add_argument(
        "--rt-stt-retry-count",
        type=int,
        default=4,
        help="Total attempts allowed for realtime STT stage",
    )
    mic_listen.add_argument(
        "--rt-stt-retry-interval-sec",
        type=float,
        default=0.2,
        help="Wait seconds before STT retry after each failed attempt",
    )
    mic_listen.add_argument(
        "--rt-analysis-request-timeout-sec",
        type=float,
        default=15.0,
        help="Per-request timeout seconds for realtime analysis stage",
    )
    mic_listen.add_argument(
        "--rt-analysis-stage-timeout-sec",
        type=float,
        default=60.0,
        help="Stage timeout seconds for realtime analysis retries",
    )
    mic_listen.add_argument(
        "--rt-analysis-retry-count",
        type=int,
        default=4,
        help="Total attempts allowed for realtime analysis stage",
    )
    mic_listen.add_argument(
        "--rt-analysis-retry-interval-sec",
        type=float,
        default=0.2,
        help="Wait seconds before analysis retry after each failed attempt",
    )
    mic_listen.add_argument(
        "--rt-alert-threshold",
        type=int,
        default=90,
        help="Urgency threshold for [ALERT] console output",
    )
    mic_listen.add_argument(
        "--rt-context-min-ready",
        type=int,
        default=15,
        help="Minimum ready transcript chunks before strict context gate is considered met",
    )
    mic_listen.add_argument(
        "--rt-context-recent-required",
        type=int,
        default=4,
        help="Required most-recent transcript chunks that must be present for context gate",
    )
    mic_listen.add_argument(
        "--rt-context-wait-timeout-sec-1",
        type=float,
        default=1.0,
        help="After recent context is ready, extra wait seconds for full target context",
    )
    mic_listen.add_argument(
        "--rt-context-wait-timeout-sec-2",
        type=float,
        default=5.0,
        help="Timeout seconds while waiting recent context to become ready",
    )
    mic_listen.add_argument(
        "--rt-profile-enabled",
        action="store_true",
        help="Enable per-chunk realtime profile logging to separate JSONL output",
    )

    mic_publish = subparsers.add_parser(
        "mic-publish",
        help="Capture local microphone chunks and upload to mic-listen endpoint",
    )
    mic_publish.add_argument("--target-url", required=True, help="mic-listen base URL, e.g. http://127.0.0.1:18765")
    mic_publish.add_argument("--mic-upload-token", required=True, help="Shared upload token")
    mic_publish.add_argument("--device", required=True, help="Windows dshow microphone device name")
    mic_publish.add_argument(
        "--chunk-seconds",
        type=float,
        default=10.0,
        help="Segment duration in seconds (supports decimal)",
    )
    mic_publish.add_argument(
        "--work-dir",
        default=".mic_publish_chunks",
        help="Temporary local chunk directory",
    )
    mic_publish.add_argument("--ffmpeg-bin", default="", help="ffmpeg binary path; default from PATH")
    mic_publish.add_argument(
        "--request-timeout-sec",
        type=float,
        default=10.0,
        help="HTTP request timeout for upload",
    )
    mic_publish.add_argument(
        "--ready-age-sec",
        type=float,
        default=1.2,
        help="Chunk stable age before upload attempt",
    )
    mic_publish.add_argument(
        "--retry-base-sec",
        type=float,
        default=0.5,
        help="Base backoff seconds for retry",
    )
    mic_publish.add_argument(
        "--retry-max-sec",
        type=float,
        default=8.0,
        help="Maximum backoff seconds for retry",
    )
    mic_publish.add_argument(
        "--scan-interval-sec",
        type=float,
        default=0.2,
        help="Polling interval for local chunk scan/upload loop",
    )

    mic_list_devices = subparsers.add_parser(
        "mic-list-devices",
        help="List Windows dshow microphone devices via ffmpeg",
    )
    mic_list_devices.add_argument("--ffmpeg-bin", default="", help="ffmpeg binary path; default from PATH")

    simulate = subparsers.add_parser(
        "simulate",
        help="Run offline simulator for realtime insight pipeline with configurable scenarios",
    )
    simulate.add_argument("--mode", type=int, choices=[1, 2, 3, 4, 5, 6], required=True, help="Simulator mode")
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
        default="gpt-4.1-mini",
        help="OpenAI text model for analysis stage",
    )
    simulate.add_argument(
        "--rt-stt-model",
        default="whisper-large-v3",
        help="OpenAI speech-to-text model for translation stage",
    )
    simulate.add_argument(
        "--rt-keywords-file",
        default="config/realtime_keywords.json",
        help="Keyword config file path reused by analysis stage",
    )
    simulate.add_argument(
        "--rt-api-base-url",
        default="",
        help="Optional OpenAI-compatible API base URL (e.g. https://aihubmix.com/v1)",
    )
    simulate.add_argument(
        "--rt-stt-request-timeout-sec",
        type=float,
        default=8.0,
        help="Per-request timeout seconds for STT stage calls",
    )
    simulate.add_argument(
        "--rt-stt-stage-timeout-sec",
        type=float,
        default=32.0,
        help="Stage timeout seconds for STT retries in simulated pipeline",
    )
    simulate.add_argument(
        "--rt-stt-retry-count",
        type=int,
        default=4,
        help="Total attempts allowed for STT stage in simulated pipeline",
    )
    simulate.add_argument(
        "--rt-stt-retry-interval-sec",
        type=float,
        default=0.2,
        help="Wait seconds before STT retry in simulated pipeline",
    )
    simulate.add_argument(
        "--rt-analysis-request-timeout-sec",
        type=float,
        default=15.0,
        help="Per-request timeout seconds for analysis stage calls",
    )
    simulate.add_argument(
        "--rt-analysis-stage-timeout-sec",
        type=float,
        default=60.0,
        help="Stage timeout seconds for analysis retries in simulated pipeline",
    )
    simulate.add_argument(
        "--rt-analysis-retry-count",
        type=int,
        default=4,
        help="Total attempts allowed for analysis stage in simulated pipeline",
    )
    simulate.add_argument(
        "--rt-analysis-retry-interval-sec",
        type=float,
        default=0.2,
        help="Wait seconds before analysis retry in simulated pipeline",
    )
    simulate.add_argument(
        "--rt-context-recent-required",
        type=int,
        default=4,
        help="Required most-recent transcript chunks for context gate",
    )
    simulate.add_argument(
        "--rt-context-wait-timeout-sec-1",
        type=float,
        default=1.0,
        help="After recent context is ready, extra wait seconds for full target context",
    )
    simulate.add_argument(
        "--rt-context-wait-timeout-sec-2",
        type=float,
        default=5.0,
        help="Timeout seconds while waiting recent context to become ready",
    )
    simulate.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional seed overriding scenario seed for deterministic feed behavior",
    )
    simulate.add_argument(
        "--mode5-profile",
        choices=["all_chunks_dual", "single_chunk_dual", "all_chunks_serial_once"],
        default=DEFAULT_MODE5_PROFILE,
        help="Mode5 execution profile: full dual benchmark, single chunk dual benchmark, or full serial once",
    )
    simulate.add_argument(
        "--mode5-target-seq",
        type=int,
        default=None,
        help="Target 1-based chunk seq used when --mode5-profile=single_chunk_dual",
    )

    return parser
