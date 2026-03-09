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
        description="ZJU classroom tool: scan courses or run live stream analysis"
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

    analysis = subparsers.add_parser(
        "analysis",
        help="Continuously discover live streams and run stream realtime analysis",
    )
    add_common_auth_args(analysis)
    analysis.add_argument("--course-id", type=int, required=True, help="Course ID")
    analysis.add_argument("--sub-id", type=int, required=True, help="Sub ID")
    analysis.add_argument("--poll-interval", type=float, default=10.0, help="Backend poll interval seconds")
    analysis.add_argument(
        "--output-dir",
        default="",
        help="Parent directory used for analysis session output",
    )
    analysis.add_argument(
        "--rt-model",
        default="gpt-4.1-mini",
        help="OpenAI text model for realtime insight analysis",
    )
    analysis.add_argument(
        "--rt-asr-scene",
        choices=["zh", "multi"],
        default="zh",
        help="Streaming ASR scene profile used by analysis mode",
    )
    analysis.add_argument(
        "--rt-asr-model",
        default=None,
        help="Streaming ASR model (required in analysis mode)",
    )
    analysis.add_argument(
        "--rt-hotwords-file",
        default="config/realtime_hotwords.json",
        help="Hotwords JSON array file path for stream mode ASR",
    )
    analysis.add_argument(
        "--rt-window-sentences",
        type=int,
        default=8,
        help="Sliding window sentence count for stream mode",
    )
    analysis.add_argument(
        "--rt-stream-analysis-workers",
        type=int,
        default=32,
        help="Parallel analysis workers for stream mode",
    )
    analysis.add_argument(
        "--rt-stream-queue-size",
        type=int,
        default=100,
        help="Pending analysis queue size for stream mode",
    )
    analysis.add_argument(
        "--rt-asr-endpoint",
        default="wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        help="DashScope websocket endpoint for stream mode ASR",
    )
    analysis.add_argument(
        "--rt-translation-target-languages",
        default="zh",
        help="Comma-separated translation targets used by multi-scene stream ASR",
    )
    analysis.add_argument(
        "--rt-keywords-file",
        default="config/realtime_keywords.json",
        help="Keyword configuration file path for realtime insight",
    )
    analysis.add_argument(
        "--rt-api-base-url",
        default="",
        help="Optional OpenAI-compatible API base URL (e.g. https://aihubmix.com/v1)",
    )
    analysis.add_argument(
        "--rt-analysis-request-timeout-sec",
        type=float,
        default=15.0,
        help="Per-request timeout seconds for realtime analysis stage",
    )
    analysis.add_argument(
        "--rt-analysis-stage-timeout-sec",
        type=float,
        default=60.0,
        help="Stage timeout seconds for realtime analysis retries",
    )
    analysis.add_argument(
        "--rt-analysis-retry-count",
        type=int,
        default=4,
        help="Total attempts allowed for realtime analysis stage",
    )
    analysis.add_argument(
        "--rt-analysis-retry-interval-sec",
        type=float,
        default=0.2,
        help="Wait seconds before analysis retry after each failed attempt",
    )
    analysis.add_argument(
        "--rt-alert-threshold",
        type=int,
        default=90,
        help="Urgency threshold for [ALERT] console output",
    )
    analysis.add_argument(
        "--rt-dingtalk-enabled",
        action="store_true",
        help="Enable DingTalk bot alerts for important realtime insights",
    )
    analysis.add_argument(
        "--rt-dingtalk-cooldown-sec",
        type=float,
        default=30.0,
        help="Cooldown seconds after an accepted DingTalk alert",
    )
    analysis.add_argument(
        "--rt-context-recent-required",
        type=int,
        default=4,
        help="Required most-recent transcript chunks that must be present for context gate",
    )
    analysis.add_argument(
        "--rt-context-wait-timeout-sec-1",
        type=float,
        default=1.0,
        help="After recent context is ready, extra wait seconds for full target context",
    )
    analysis.add_argument(
        "--rt-context-wait-timeout-sec-2",
        type=float,
        default=5.0,
        help="Timeout seconds while waiting recent context to become ready",
    )
    analysis.add_argument(
        "--rt-log-rotate-max-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="Per-file max bytes before rotating realtime logs",
    )
    analysis.add_argument(
        "--rt-log-rotate-backup-count",
        type=int,
        default=20,
        help="Number of rotated realtime log files to retain",
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
        "--rt-pipeline-mode",
        choices=["chunk", "stream"],
        default="chunk",
        help="Realtime insight pipeline mode: chunk(legacy) or stream(new)",
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
        default=None,
        help="OpenAI speech-to-text model for realtime insight",
    )
    mic_listen.add_argument(
        "--rt-asr-scene",
        choices=["zh", "multi"],
        default="zh",
        help="Streaming ASR scene profile used by --rt-pipeline-mode=stream",
    )
    mic_listen.add_argument(
        "--rt-asr-model",
        default=None,
        help="Streaming ASR model (required when --rt-pipeline-mode=stream)",
    )
    mic_listen.add_argument(
        "--rt-hotwords-file",
        default="config/realtime_hotwords.json",
        help="Hotwords JSON array file path for stream mode ASR",
    )
    mic_listen.add_argument(
        "--rt-window-sentences",
        type=int,
        default=8,
        help="Sliding window sentence count for stream mode",
    )
    mic_listen.add_argument(
        "--rt-stream-analysis-workers",
        type=int,
        default=32,
        help="Parallel analysis workers for stream mode",
    )
    mic_listen.add_argument(
        "--rt-stream-queue-size",
        type=int,
        default=100,
        help="Pending analysis queue size for stream mode",
    )
    mic_listen.add_argument(
        "--rt-asr-endpoint",
        default="wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        help="DashScope websocket endpoint for stream mode ASR",
    )
    mic_listen.add_argument(
        "--rt-translation-target-languages",
        default="zh",
        help="Comma-separated translation targets used by multi-scene stream ASR",
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
        "--rt-dingtalk-enabled",
        action="store_true",
        help="Enable DingTalk bot alerts for important realtime insights",
    )
    mic_listen.add_argument(
        "--rt-dingtalk-cooldown-sec",
        type=float,
        default=30.0,
        help="Cooldown seconds after an accepted DingTalk alert",
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
    mic_listen.add_argument(
        "--rt-log-rotate-max-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="Per-file max bytes before rotating realtime logs",
    )
    mic_listen.add_argument(
        "--rt-log-rotate-backup-count",
        type=int,
        default=20,
        help="Number of rotated realtime log files to retain",
    )

    mic_publish = subparsers.add_parser(
        "mic-publish",
        help="Capture local microphone chunks and upload to mic-listen endpoint",
    )
    mic_publish.add_argument("--target-url", required=True, help="mic-listen base URL, e.g. http://127.0.0.1:18765")
    mic_publish.add_argument("--mic-upload-token", required=True, help="Shared upload token")
    mic_publish.add_argument("--device", required=True, help="Windows dshow microphone device name")
    mic_publish.add_argument(
        "--rt-pipeline-mode",
        choices=["chunk", "stream"],
        default="chunk",
        help="Publish mode: chunk upload(legacy) or stream websocket(new)",
    )
    mic_publish.add_argument(
        "--chunk-seconds",
        type=float,
        default=10.0,
        help="Segment duration in seconds (supports decimal)",
    )
    mic_publish.add_argument(
        "--stream-frame-duration-ms",
        type=int,
        default=100,
        help="Frame duration in milliseconds for stream mode",
    )
    mic_publish.add_argument(
        "--work-dir",
        "--worker-dir",
        dest="work_dir",
        default="",
        help="Temporary local chunk directory; default auto-generated with current timestamp",
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

    return parser
