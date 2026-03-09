from __future__ import annotations

import argparse
import base64
import hashlib
import json
import locale
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import which
from typing import Any, Callable

import requests

from src.common.account import resolve_dashscope_api_key, resolve_dingtalk_bot_settings, resolve_openai_client_settings
from src.common.rotating_log import RotatingLineWriter
from src.live.insight.audio_streamer import build_mic_stream_ffmpeg_command
from src.live.insight.dingtalk import DingTalkNotifier
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig, format_local_ts
from src.live.insight.openai_client import OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor
from src.live.insight.stream_pipeline import StreamRealtimeInsightPipeline, load_hotwords


def _load_keywords(path: Path, *, log_fn: Callable[[str], None]) -> KeywordConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        log_fn(f"[mic-listen] keyword file not found/readable: {path}; using empty rules")
        return KeywordConfig()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log_fn(f"[mic-listen] keyword file is invalid JSON: {path}; using empty rules")
        return KeywordConfig()

    if not isinstance(payload, dict):
        log_fn(f"[mic-listen] keyword file root is not JSON object: {path}; using empty rules")
        return KeywordConfig()

    return KeywordConfig.from_json_dict(payload)


def _build_openai_client(config: RealtimeInsightConfig) -> OpenAIInsightClient | None:
    api_key, resolved_base_url, key_error = resolve_openai_client_settings(
        api_key_env_name=config.api_key_env,
        base_url_env_name=config.base_url_env,
    )
    if not api_key:
        print(f"[mic-listen] {key_error}")
        return None

    base_url = (config.api_base_url or "").strip() or resolved_base_url
    try:
        if base_url:
            print(f"[mic-listen] using OpenAI-compatible base URL: {base_url}")
        return OpenAIInsightClient(
            api_key=api_key,
            timeout_sec=max(float(config.stt_request_timeout_sec), float(config.analysis_request_timeout_sec)),
            base_url=base_url,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[mic-listen] failed to initialize OpenAI client: {exc}")
        return None


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


_MIC_PUBLISH_WORK_DIR_PREFIX = ".mic_publish_chunks"


def _build_timestamped_mic_publish_work_dir(*, now: datetime | None = None) -> Path:
    ts = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return Path(f"{_MIC_PUBLISH_WORK_DIR_PREFIX}_{ts}")


def _resolve_mic_publish_work_dir(raw_work_dir: object, *, now: datetime | None = None) -> tuple[Path, bool]:
    text = str(raw_work_dir or "").strip()
    if text:
        return Path(text).expanduser().resolve(), False
    return _build_timestamped_mic_publish_work_dir(now=now).expanduser().resolve(), True


def _count_existing_mic_publish_chunks(work_dir: Path) -> int:
    total = 0
    for pattern in ("mic_*.mp3", "mic_*.wav"):
        total += sum(1 for _ in work_dir.glob(pattern))
    return total


@dataclass
class _RetryState:
    attempts: int = 0
    next_retry_at: float = 0.0


@dataclass
class _QueuedChunkItem:
    path: Path
    profile: dict[str, Any] | None = None


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MicChunkProcessor:
    def __init__(
        self,
        *,
        stage_processor: InsightStageProcessor,
        chunk_dir: Path,
        max_chunk_bytes: int,
        profile_enabled: bool = False,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.stage_processor = stage_processor
        self.chunk_dir = chunk_dir
        self.max_chunk_bytes = max(1, int(max_chunk_bytes))
        self.profile_enabled = bool(profile_enabled)
        self._log_fn = log_fn or print

        self._lock = threading.Lock()
        self._queue: queue.Queue[_QueuedChunkItem | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._seen_hash: set[str] = set()
        self._next_chunk_seq = 0
        self._profile_path = self.stage_processor.session_dir / "realtime_profile.jsonl"
        self._profile_lock = threading.Lock()
        self._profile_writer = RotatingLineWriter(
            path=self._profile_path,
            max_bytes=max(1, int(getattr(self.stage_processor.config, "log_rotate_max_bytes", 64 * 1024 * 1024))),
            backup_count=max(1, int(getattr(self.stage_processor.config, "log_rotate_backup_count", 20))),
        )
        self._chunk_seconds = max(1.0, float(getattr(self.stage_processor.config, "chunk_seconds", 10.0) or 10.0))

        self._metrics = {
            "uploaded_total": 0,
            "accepted_total": 0,
            "duplicate_total": 0,
            "auth_failures": 0,
            "too_large_total": 0,
            "processed_total": 0,
            "process_failures": 0,
            "last_error": "",
        }

    def start(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._run_worker, name="mic-chunk-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put(None)
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5.0)
        self._worker = None

    def is_running(self) -> bool:
        worker = self._worker
        return bool(worker is not None and worker.is_alive())

    def mark_auth_failure(self) -> None:
        with self._lock:
            self._metrics["auth_failures"] += 1

    def build_too_large_payload(self) -> dict:
        with self._lock:
            self._metrics["uploaded_total"] += 1
            self._metrics["too_large_total"] += 1
        return {
            "error": "chunk too large",
            "max_chunk_bytes": self.max_chunk_bytes,
        }

    def ingest_chunk(
        self,
        *,
        body: bytes,
        chunk_name: str,
        local_sent_ts_ms: int | None = None,
        remote_request_started_ts_ms: int | None = None,
        remote_receive_done_ts_ms: int | None = None,
    ) -> tuple[int, dict]:
        size = len(body)
        with self._lock:
            self._metrics["uploaded_total"] += 1

        if size <= 0:
            return int(HTTPStatus.BAD_REQUEST), {"error": "empty body"}

        if size > self.max_chunk_bytes:
            with self._lock:
                self._metrics["too_large_total"] += 1
            return int(HTTPStatus.REQUEST_ENTITY_TOO_LARGE), {
                "error": "chunk too large",
                "max_chunk_bytes": self.max_chunk_bytes,
            }

        chunk_hash = hashlib.sha256(body).hexdigest()
        with self._lock:
            if chunk_hash in self._seen_hash:
                self._metrics["duplicate_total"] += 1
                return int(HTTPStatus.OK), {"accepted": False, "duplicate": True, "sha256": chunk_hash}
            self._seen_hash.add(chunk_hash)

        safe_stem = _sanitize_chunk_stem(chunk_name)
        ts = format_local_ts(datetime.now().astimezone())
        final_path = self.chunk_dir / f"{safe_stem}_{ts}_{chunk_hash[:8]}.mp3"
        part_path = final_path.with_suffix(".part")

        try:
            with part_path.open("wb") as handle:
                handle.write(body)
            part_path.replace(final_path)
        except OSError as exc:
            with self._lock:
                self._metrics["last_error"] = f"write chunk failed: {exc}"
            return int(HTTPStatus.INTERNAL_SERVER_ERROR), {"error": "write chunk failed"}

        with self._lock:
            self._metrics["accepted_total"] += 1
        queued_ts_ms = _now_epoch_ms()
        profile = self._build_profile_seed(
            chunk_file=final_path.name,
            chunk_hash=chunk_hash,
            chunk_name=chunk_name,
            body_size=size,
            local_sent_ts_ms=local_sent_ts_ms,
            remote_request_started_ts_ms=remote_request_started_ts_ms,
            remote_receive_done_ts_ms=remote_receive_done_ts_ms,
            remote_dispatch_ts_ms=queued_ts_ms,
        )
        self._queue.put(_QueuedChunkItem(path=final_path, profile=profile))
        return int(HTTPStatus.ACCEPTED), {
            "accepted": True,
            "duplicate": False,
            "sha256": chunk_hash,
            "chunk_file": final_path.name,
        }

    def metrics(self) -> dict:
        with self._lock:
            out = dict(self._metrics)
        out["queue_size"] = self._queue.qsize()
        out["running"] = self.is_running()
        return out

    def _build_profile_seed(
        self,
        *,
        chunk_file: str,
        chunk_hash: str,
        chunk_name: str,
        body_size: int,
        local_sent_ts_ms: int | None,
        remote_request_started_ts_ms: int | None,
        remote_receive_done_ts_ms: int | None,
        remote_dispatch_ts_ms: int,
    ) -> dict[str, Any] | None:
        if not self.profile_enabled:
            return None
        return {
            "profile_version": 1,
            "audio_source_mode": "mic_upload",
            "chunk_file": chunk_file,
            "chunk_name_header": chunk_name,
            "chunk_sha256": chunk_hash,
            "chunk_size_bytes": int(body_size),
            "chunk_seconds": self._chunk_seconds,
            "local_send_ts_ms": local_sent_ts_ms,
            "remote_request_started_ts_ms": remote_request_started_ts_ms,
            "remote_receive_done_ts_ms": remote_receive_done_ts_ms,
            "remote_dispatch_ts_ms": remote_dispatch_ts_ms,
            "state": "accepted",
        }

    def _write_profile(self, profile: dict[str, Any] | None) -> None:
        if not self.profile_enabled or profile is None:
            return
        payload = dict(profile)
        payload["profile_logged_ts_ms"] = _now_epoch_ms()
        payload["network_send_to_remote_receive_ms"] = _delta_ms(
            payload.get("local_send_ts_ms"),
            payload.get("remote_receive_done_ts_ms"),
        )
        payload["remote_receive_to_dispatch_ms"] = _delta_ms(
            payload.get("remote_receive_done_ts_ms"),
            payload.get("remote_dispatch_ts_ms"),
        )
        payload["queue_wait_ms"] = _delta_ms(
            payload.get("remote_dispatch_ts_ms"),
            payload.get("worker_dequeued_ts_ms"),
        )
        payload["dispatch_to_stt_request_ms"] = _delta_ms(
            payload.get("remote_dispatch_ts_ms"),
            payload.get("stt_request_ts_ms"),
        )
        payload["stt_round_trip_ms"] = _delta_ms(
            payload.get("stt_request_ts_ms"),
            payload.get("stt_response_ts_ms"),
        )
        payload["analysis_round_trip_ms"] = _delta_ms(
            payload.get("analysis_request_ts_ms"),
            payload.get("analysis_response_ts_ms"),
        )
        payload["analysis_to_insight_log_ms"] = _delta_ms(
            payload.get("analysis_response_ts_ms"),
            payload.get("insight_console_log_ts_ms"),
        )
        payload["remote_total_ms"] = _delta_ms(
            payload.get("remote_receive_done_ts_ms"),
            payload.get("profile_logged_ts_ms"),
        )
        chunk_seconds = payload.get("chunk_seconds", self._chunk_seconds)
        payload["stt_ms_per_audio_sec"] = _ms_per_audio_sec(payload.get("stt_round_trip_ms"), chunk_seconds)
        payload["analysis_ms_per_audio_sec"] = _ms_per_audio_sec(
            payload.get("analysis_round_trip_ms"),
            chunk_seconds,
        )
        payload["remote_ms_per_audio_sec"] = _ms_per_audio_sec(payload.get("remote_total_ms"), chunk_seconds)
        payload["queue_wait_ms_per_audio_sec"] = _ms_per_audio_sec(payload.get("queue_wait_ms"), chunk_seconds)
        payload["stt_rtf"] = _rtf(payload.get("stt_round_trip_ms"), chunk_seconds)
        payload["analysis_rtf"] = _rtf(payload.get("analysis_round_trip_ms"), chunk_seconds)
        payload["remote_rtf"] = _rtf(payload.get("remote_total_ms"), chunk_seconds)
        payload["state_summary"] = {
            "stt_status": str(payload.get("stt_status", "") or ""),
            "analysis_status": str(payload.get("analysis_status", "") or ""),
            "final_status": str(payload.get("final_status", "") or ""),
            "context_reason": str(payload.get("context_reason", "") or ""),
        }
        with self._profile_lock:
            self._profile_writer.append(json.dumps(payload, ensure_ascii=False) + "\n")

    def _run_worker(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue

            if item is None:
                self._queue.task_done()
                if self._stop_event.is_set():
                    break
                continue

            chunk_path = item.path
            profile = item.profile
            with self._lock:
                self._next_chunk_seq += 1
                chunk_seq = int(self._next_chunk_seq)
            if profile is not None:
                profile["chunk_seq"] = int(chunk_seq)
                profile["worker_dequeued_ts_ms"] = _now_epoch_ms()
                profile["state"] = "processing"
            try:
                self.stage_processor.process_chunk(chunk_seq, chunk_path, profile=profile)
                with self._lock:
                    self._metrics["processed_total"] += 1
                if profile is not None:
                    profile["state"] = "processed"
            except Exception as exc:  # pragma: no cover - defensive
                with self._lock:
                    self._metrics["process_failures"] += 1
                    self._metrics["last_error"] = f"process failed: {exc}"
                self._log_fn(f"[mic-listen] failed to process chunk={chunk_path.name}: {exc}")
                if profile is not None:
                    profile["state"] = "process_failed"
                    profile["final_status"] = "processor_exception"
                    profile["final_error"] = str(exc)
                    profile["stage_processor_finished_ts_ms"] = _now_epoch_ms()
            finally:
                if profile is not None:
                    profile["worker_finished_ts_ms"] = _now_epoch_ms()
                self._write_profile(profile)
                self._queue.task_done()


class MicStreamProcessor:
    def __init__(
        self,
        *,
        pipeline: StreamRealtimeInsightPipeline,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self._log_fn = log_fn or print
        self._lock = threading.Lock()
        self._metrics = {
            "ws_connections": 0,
            "ws_disconnects": 0,
            "stream_frames_total": 0,
            "stream_bytes_total": 0,
            "stream_failures": 0,
            "auth_failures": 0,
            "last_error": "",
        }

    def start(self) -> None:
        self.pipeline.start()

    def stop(self) -> None:
        self.pipeline.stop()

    def on_connection_open(self) -> None:
        with self._lock:
            self._metrics["ws_connections"] += 1

    def on_connection_close(self) -> None:
        with self._lock:
            self._metrics["ws_disconnects"] += 1

    def mark_auth_failure(self) -> None:
        with self._lock:
            self._metrics["auth_failures"] += 1

    def ingest_frame(self, payload: bytes) -> None:
        data = bytes(payload or b"")
        if not data:
            return
        try:
            ok = bool(self.pipeline.submit_audio_frame(data))
            with self._lock:
                self._metrics["stream_frames_total"] += 1
                self._metrics["stream_bytes_total"] += len(data)
                if not ok:
                    self._metrics["stream_failures"] += 1
        except Exception as exc:
            with self._lock:
                self._metrics["stream_failures"] += 1
                self._metrics["last_error"] = str(exc)
            self._log_fn(f"[mic-listen] stream frame ingest failed: {exc}")

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._metrics)


def build_mic_http_handler(
    *,
    processor: MicChunkProcessor,
    upload_token: str,
    stream_processor: MicStreamProcessor | None = None,
):
    class _MicHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/ws/mic/stream":
                return self._handle_mic_stream_ws(parsed)

            if self.path == "/api/mic/health":
                stream_running = bool(stream_processor is not None)
                return self._write_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "running": processor.is_running(),
                        "stream_running": stream_running,
                        "chunk_dir": processor.chunk_dir.as_posix(),
                    },
                )
            if self.path == "/api/mic/metrics":
                payload = processor.metrics()
                payload["stream"] = stream_processor.metrics() if stream_processor is not None else {}
                return self._write_json(HTTPStatus.OK, payload)
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            request_started_ts_ms = _now_epoch_ms()
            if self.path != "/api/mic/chunk":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            token = str(self.headers.get("X-Mic-Token", "") or "").strip()
            if token != upload_token:
                processor.mark_auth_failure()
                self._write_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return

            raw_len = self.headers.get("Content-Length", "")
            try:
                content_length = int(raw_len)
            except (TypeError, ValueError):
                self._write_json(HTTPStatus.LENGTH_REQUIRED, {"error": "missing content-length"})
                return

            if content_length < 0:
                self._write_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"})
                return

            if content_length > processor.max_chunk_bytes:
                _ = self.rfile.read(content_length)
                payload = processor.build_too_large_payload()
                self._write_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, payload)
                return

            body = self.rfile.read(content_length)
            remote_receive_done_ts_ms = _now_epoch_ms()
            chunk_name = str(self.headers.get("X-Chunk-Name", "") or "").strip()
            local_sent_ts_ms = _parse_optional_epoch_ms(self.headers.get("X-Chunk-Sent-At-Ms"))
            status, payload = processor.ingest_chunk(
                body=body,
                chunk_name=chunk_name,
                local_sent_ts_ms=local_sent_ts_ms,
                remote_request_started_ts_ms=request_started_ts_ms,
                remote_receive_done_ts_ms=remote_receive_done_ts_ms,
            )
            self._write_json(status, payload)

        def _handle_mic_stream_ws(self, parsed: urllib.parse.ParseResult) -> None:
            if stream_processor is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            token = str(self.headers.get("X-Mic-Token", "") or "").strip()
            if not token:
                query = urllib.parse.parse_qs(parsed.query)
                token = str((query.get("token") or [""])[0] or "").strip()
            if token != upload_token:
                stream_processor.mark_auth_failure()
                self.send_error(HTTPStatus.UNAUTHORIZED, "unauthorized")
                return

            upgrade = str(self.headers.get("Upgrade", "") or "").strip().lower()
            connection = str(self.headers.get("Connection", "") or "").strip().lower()
            ws_key = str(self.headers.get("Sec-WebSocket-Key", "") or "").strip()
            if upgrade != "websocket" or "upgrade" not in connection or not ws_key:
                self.send_error(HTTPStatus.BAD_REQUEST, "websocket upgrade required")
                return

            accept = _build_ws_accept(ws_key)
            self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()

            stream_processor.on_connection_open()
            try:
                while True:
                    frame = _read_ws_frame(self.rfile)
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == 0x8:  # close
                        _write_ws_frame(self.wfile, opcode=0x8, payload=b"")
                        break
                    if opcode == 0x9:  # ping
                        _write_ws_frame(self.wfile, opcode=0xA, payload=payload)
                        continue
                    if opcode in {0x2, 0x0}:  # binary / continuation
                        stream_processor.ingest_frame(payload)
                        continue
                    if opcode == 0x1:  # text
                        continue
            except Exception:
                return
            finally:
                stream_processor.on_connection_close()

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401
            return

        def _write_json(self, status: HTTPStatus | int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _MicHandler


class MicPublisher:
    def __init__(
        self,
        *,
        target_url: str,
        upload_token: str,
        device: str,
        chunk_seconds: float,
        work_dir: Path,
        ffmpeg_bin: str,
        request_timeout_sec: float,
        ready_age_sec: float,
        retry_base_sec: float,
        retry_max_sec: float,
        scan_interval_sec: float,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.target_url = target_url.rstrip("/")
        self.upload_token = upload_token
        self.device = device
        self.chunk_seconds = max(2.0, float(chunk_seconds))
        self.work_dir = work_dir
        self.ffmpeg_bin = ffmpeg_bin.strip() or (which("ffmpeg") or "")
        self.request_timeout_sec = max(1.0, float(request_timeout_sec))
        self.ready_age_sec = max(0.2, float(ready_age_sec))
        self.retry_base_sec = max(0.1, float(retry_base_sec))
        self.retry_max_sec = max(self.retry_base_sec, float(retry_max_sec))
        self.scan_interval_sec = max(0.05, float(scan_interval_sec))
        self._log_fn = log_fn or print

        self._stop_event = threading.Event()
        self._pending: dict[Path, _RetryState] = {}
        self._done: set[Path] = set()
        self._proc: subprocess.Popen | None = None

    @staticmethod
    def build_ffmpeg_command(
        *,
        ffmpeg_bin: str,
        device: str,
        chunk_seconds: float,
        work_dir: Path,
        audio_codec: str = "libmp3lame",
        output_ext: str = "mp3",
    ) -> list[str]:
        output_pattern = work_dir / f"mic_%Y%m%d_%H%M%S.{output_ext}"
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-y",
            "-f",
            "dshow",
            "-i",
            f"audio={device}",
            "-ac",
            "1",
            "-ar",
            "16000",
        ]
        if audio_codec:
            cmd.extend(["-c:a", audio_codec])
        if audio_codec == "libmp3lame":
            cmd.extend(["-b:a", "64k"])
        cmd.extend(
            [
                "-f",
                "segment",
                "-segment_time",
                _format_ffmpeg_seconds(max(2.0, float(chunk_seconds))),
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
                str(output_pattern),
            ]
        )
        return cmd

    def _resolve_capture_format(self) -> tuple[str, str]:
        try:
            proc = subprocess.run(  # noqa: S603
                [self.ffmpeg_bin, "-hide_banner", "-encoders"],
                capture_output=True,
                text=False,
            )
            encoders = (
                _decode_subprocess_output(proc.stdout) + "\n" + _decode_subprocess_output(proc.stderr)
            ).lower()
        except OSError:
            encoders = ""

        if "libmp3lame" in encoders:
            return "libmp3lame", "mp3"
        return "pcm_s16le", "wav"

    def run(self) -> int:
        if not self.ffmpeg_bin:
            print("[mic-publish] ffmpeg not found in PATH")
            return 1
        self.work_dir.mkdir(parents=True, exist_ok=True)

        audio_codec, output_ext = self._resolve_capture_format()
        if audio_codec != "libmp3lame":
            self._log_fn(
                f"[mic-publish] ffmpeg encoder libmp3lame unavailable; fallback to codec={audio_codec} ext={output_ext}"
            )
        cmd = self.build_ffmpeg_command(
            ffmpeg_bin=self.ffmpeg_bin,
            device=self.device,
            chunk_seconds=self.chunk_seconds,
            work_dir=self.work_dir,
            audio_codec=audio_codec,
            output_ext=output_ext,
        )
        self._proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._log_fn(
            f"[mic-publish] started capture device={self.device!r} chunk={self.chunk_seconds}s target={self.target_url}"
        )

        try:
            while not self._stop_event.is_set():
                self._scan_ready_chunks()
                if self._try_upload_pending() != 0:
                    return 1
                if self._proc.poll() is not None:
                    self._log_fn("[mic-publish] ffmpeg process exited unexpectedly")
                    return 1
                time.sleep(self.scan_interval_sec)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return 0

    def stop(self) -> None:
        self._stop_event.set()
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=2.0)
        except Exception:
            proc.kill()
            proc.wait(timeout=1.0)

    def _scan_ready_chunks(self) -> None:
        now = time.time()
        patterns = ("mic_*.mp3", "mic_*.wav")
        for pattern in patterns:
            for path in sorted(self.work_dir.glob(pattern)):
                if path in self._done or path in self._pending:
                    continue
                if not path.exists() or path.stat().st_size <= 0:
                    continue
                if (now - path.stat().st_mtime) < self.ready_age_sec:
                    continue
                self._pending[path] = _RetryState()

    def _try_upload_pending(self) -> int:
        now = time.time()
        for path in list(self._pending.keys()):
            if not path.exists():
                self._pending.pop(path, None)
                continue

            state = self._pending[path]
            if now < state.next_retry_at:
                continue

            try:
                self._upload_once(path)
                self._done.add(path)
                self._pending.pop(path, None)
                try:
                    path.unlink()
                except OSError:
                    pass
            except PermissionError as exc:
                self._log_fn(f"[mic-publish] unauthorized: {exc}")
                return 1
            except ValueError as exc:
                self._log_fn(f"[mic-publish] permanent upload failure chunk={path.name}: {exc}")
                self._pending.pop(path, None)
                self._done.add(path)
                try:
                    path.unlink()
                except OSError:
                    pass
            except Exception as exc:
                state.attempts += 1
                delay = min(self.retry_max_sec, self.retry_base_sec * (2 ** max(0, state.attempts - 1)))
                state.next_retry_at = now + delay
                self._log_fn(
                    f"[mic-publish] upload failed chunk={path.name} attempt={state.attempts} "
                    f"retry_in={delay:.2f}s err={exc}"
                )
        return 0

    def _upload_once(self, path: Path) -> None:
        payload = path.read_bytes()
        if not payload:
            raise ValueError("empty chunk")

        url = f"{self.target_url}/api/mic/chunk"
        sent_ts_ms = _now_epoch_ms()
        headers = {
            "X-Mic-Token": self.upload_token,
            "X-Chunk-Name": path.name,
            "X-Chunk-Sha256": hashlib.sha256(payload).hexdigest(),
            "X-Chunk-Sent-At-Ms": str(sent_ts_ms),
            "Content-Type": "application/octet-stream",
        }
        resp = requests.post(url, data=payload, headers=headers, timeout=self.request_timeout_sec)
        if resp.status_code == HTTPStatus.UNAUTHORIZED:
            raise PermissionError("token rejected by mic-listen")
        if resp.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
            raise ValueError("chunk exceeds mic-listen max size")
        if 200 <= resp.status_code < 300:
            return
        raise RuntimeError(f"status={resp.status_code} body={resp.text[:200]}")


class MicStreamPublisher:
    def __init__(
        self,
        *,
        target_url: str,
        upload_token: str,
        device: str,
        ffmpeg_bin: str,
        frame_duration_ms: int,
        request_timeout_sec: float,
        retry_base_sec: float,
        retry_max_sec: float,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.target_url = str(target_url or "").rstrip("/")
        self.upload_token = str(upload_token or "").strip()
        self.device = str(device or "").strip()
        self.ffmpeg_bin = ffmpeg_bin.strip() or (which("ffmpeg") or "")
        self.frame_duration_ms = max(20, int(frame_duration_ms))
        self.request_timeout_sec = max(1.0, float(request_timeout_sec))
        self.retry_base_sec = max(0.1, float(retry_base_sec))
        self.retry_max_sec = max(self.retry_base_sec, float(retry_max_sec))
        self._log_fn = log_fn or print
        self._stop_event = threading.Event()

    def run(self) -> int:
        if not self.ffmpeg_bin:
            print("[mic-publish] ffmpeg not found in PATH")
            return 1
        if not self.upload_token:
            print("[mic-publish] missing --mic-upload-token")
            return 1
        if not self.device:
            print("[mic-publish] missing --device")
            return 1

        delay = self.retry_base_sec
        try:
            while not self._stop_event.is_set():
                try:
                    self._run_once()
                    delay = self.retry_base_sec
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    self._log_fn(f"[mic-publish] stream failed: {exc}; retry_in={delay:.2f}s")
                    if self._stop_event.wait(delay):
                        break
                    delay = min(self.retry_max_sec, delay * 2.0)
        finally:
            self._stop_event.set()
        return 0

    def _run_once(self) -> None:
        try:
            import websocket  # type: ignore
        except Exception as exc:  # pragma: no cover - dependency error path
            raise RuntimeError("websocket-client is unavailable; install dependencies") from exc

        ws_url = _http_to_ws(self.target_url) + f"/ws/mic/stream?token={urllib.parse.quote_plus(self.upload_token)}"
        ws = websocket.create_connection(  # type: ignore[attr-defined]
            ws_url,
            timeout=self.request_timeout_sec,
            header=[f"X-Mic-Token: {self.upload_token}"],
        )
        proc: subprocess.Popen | None = None
        try:
            cmd = build_mic_stream_ffmpeg_command(
                ffmpeg_bin=self.ffmpeg_bin,
                device=self.device,
                sample_rate=16000,
            )
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            frame_bytes = max(320, int(16000 * 2 * self.frame_duration_ms / 1000))
            self._log_fn(
                f"[mic-publish] started stream device={self.device!r} frame={self.frame_duration_ms}ms target={ws_url}"
            )
            stdout = proc.stdout
            if stdout is None:
                raise RuntimeError("ffmpeg stdout unavailable")
            while not self._stop_event.is_set():
                payload = stdout.read(frame_bytes)
                if not payload:
                    if proc.poll() is not None:
                        raise RuntimeError("ffmpeg exited unexpectedly")
                    continue
                ws.send_binary(payload)
        finally:
            try:
                ws.close()
            except Exception:
                pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGINT)
                    proc.wait(timeout=1.5)
                except Exception:
                    proc.kill()
                    proc.wait(timeout=1.0)


def run_mic_listen(args: argparse.Namespace) -> int:
    upload_token = (args.mic_upload_token or "").strip() or os.environ.get("MIC_UPLOAD_TOKEN", "").strip()
    if not upload_token:
        print("[mic-listen] missing upload token: pass --mic-upload-token or export MIC_UPLOAD_TOKEN")
        return 1

    if args.session_dir:
        session_dir = Path(args.session_dir).expanduser().resolve()
    else:
        ts = format_local_ts(datetime.now().astimezone())
        session_dir = (Path.cwd() / f"mic_session_{ts}").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)

    pipeline_mode = str(getattr(args, "rt_pipeline_mode", "chunk") or "chunk").strip().lower() or "chunk"
    validation_error = _validate_mic_listen_realtime_args(args, pipeline_mode=pipeline_mode)
    if validation_error:
        print(f"[mic-listen] {validation_error}")
        return 1

    chunk_seconds = max(2.0, float(args.rt_chunk_seconds))
    context_window_seconds = max(30.0, float(args.rt_context_window_seconds))
    context_target_chunks = max(1, int(context_window_seconds / max(0.1, chunk_seconds)))
    translation_targets = _parse_csv_values(getattr(args, "rt_translation_target_languages", "zh"))
    chunk_dir_cfg = Path(args.mic_chunk_dir).expanduser()
    chunk_dir = chunk_dir_cfg if chunk_dir_cfg.is_absolute() else (session_dir / chunk_dir_cfg)

    config = RealtimeInsightConfig(
        enabled=True,
        pipeline_mode=pipeline_mode,
        chunk_seconds=chunk_seconds,
        context_window_seconds=int(context_window_seconds),
        model=(args.rt_model or "").strip() or "gpt-4.1-mini",
        stt_model=(args.rt_stt_model or "").strip(),
        asr_scene=str(getattr(args, "rt_asr_scene", "zh") or "zh").strip().lower() or "zh",
        asr_model=(getattr(args, "rt_asr_model", None) or "").strip(),
        hotwords_file=Path(getattr(args, "rt_hotwords_file", "config/realtime_hotwords.json"))
        .expanduser()
        .resolve(),
        window_sentences=max(1, int(getattr(args, "rt_window_sentences", 8))),
        stream_analysis_workers=max(1, int(getattr(args, "rt_stream_analysis_workers", 32))),
        stream_queue_size=max(1, int(getattr(args, "rt_stream_queue_size", 100))),
        asr_endpoint=(getattr(args, "rt_asr_endpoint", "") or "").strip()
        or "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        translation_target_languages=translation_targets,
        keywords_file=Path(args.rt_keywords_file).expanduser().resolve(),
        api_base_url=(args.rt_api_base_url or "").strip(),
        stt_request_timeout_sec=max(1.0, float(args.rt_stt_request_timeout_sec)),
        stt_stage_timeout_sec=max(1.0, float(args.rt_stt_stage_timeout_sec)),
        stt_retry_count=max(0, int(args.rt_stt_retry_count)),
        stt_retry_interval_sec=max(0.0, float(args.rt_stt_retry_interval_sec)),
        analysis_request_timeout_sec=max(1.0, float(args.rt_analysis_request_timeout_sec)),
        analysis_stage_timeout_sec=max(1.0, float(args.rt_analysis_stage_timeout_sec)),
        analysis_retry_count=max(0, int(args.rt_analysis_retry_count)),
        analysis_retry_interval_sec=max(0.0, float(args.rt_analysis_retry_interval_sec)),
        alert_threshold=max(0, min(100, int(args.rt_alert_threshold))),
        context_min_ready=max(0, int(args.rt_context_min_ready)),
        context_recent_required=max(0, int(args.rt_context_recent_required)),
        context_wait_timeout_sec_1=max(0.0, float(args.rt_context_wait_timeout_sec_1)),
        context_wait_timeout_sec_2=max(0.0, float(args.rt_context_wait_timeout_sec_2)),
        context_wait_timeout_sec=max(
            max(0.0, float(args.rt_context_wait_timeout_sec_1)),
            max(0.0, float(args.rt_context_wait_timeout_sec_2)),
        ),
        use_dual_context_wait=True,
        context_target_chunks=(
            max(1, int(getattr(args, "rt_window_sentences", 8)))
            if pipeline_mode == "stream"
            else max(1, context_target_chunks)
        ),
        audio_source_mode="mic_upload",
        mic_upload_token=upload_token,
        mic_chunk_max_bytes=max(1, int(args.mic_chunk_max_bytes)),
        mic_chunk_dir=chunk_dir,
        profile_enabled=bool(getattr(args, "rt_profile_enabled", False)),
        dingtalk_enabled=bool(getattr(args, "rt_dingtalk_enabled", False)),
        dingtalk_cooldown_sec=max(0.0, float(getattr(args, "rt_dingtalk_cooldown_sec", 30.0))),
        dingtalk_send_timeout_sec=5.0,
        dingtalk_send_retry_count=5,
        log_rotate_max_bytes=max(1024 * 1024, int(getattr(args, "rt_log_rotate_max_bytes", 64 * 1024 * 1024))),
        log_rotate_backup_count=max(1, int(getattr(args, "rt_log_rotate_backup_count", 20))),
    )
    if pipeline_mode == "stream" and not config.dingtalk_enabled:
        print("[mic-listen] stream mode requires --rt-dingtalk-enabled with valid bot settings")
        return 1

    client = _build_openai_client(config)
    if client is None:
        return 1

    notifier = None
    dingtalk_trace_path = session_dir / "realtime_dingtalk_trace.jsonl"
    if config.dingtalk_enabled:
        webhook, secret, dingtalk_error = resolve_dingtalk_bot_settings()
        if dingtalk_error:
            print(f"[mic-listen] {dingtalk_error}")
            return 1
        notifier = DingTalkNotifier(
            webhook=webhook,
            secret=secret,
            cooldown_sec=config.dingtalk_cooldown_sec,
            trace_path=dingtalk_trace_path,
            log_rotate_max_bytes=config.log_rotate_max_bytes,
            log_rotate_backup_count=config.log_rotate_backup_count,
            log_fn=print,
        )
    if pipeline_mode == "stream" and notifier is None:
        print("[mic-listen] stream mode requires DingTalk notifier")
        return 1

    keywords = _load_keywords(config.keywords_file, log_fn=print)
    stage_processor = InsightStageProcessor(
        session_dir=session_dir,
        config=config,
        keywords=keywords,
        client=client,
        notifier=notifier,
        log_fn=print,
    )

    processor = MicChunkProcessor(
        stage_processor=stage_processor,
        chunk_dir=chunk_dir,
        max_chunk_bytes=config.mic_chunk_max_bytes,
        profile_enabled=config.profile_enabled,
        log_fn=print,
    )
    processor.start()

    stream_processor: MicStreamProcessor | None = None
    if pipeline_mode == "stream":
        dashscope_key, dashscope_error = resolve_dashscope_api_key(env_name=config.asr_api_key_env)
        if not dashscope_key:
            print(f"[mic-listen] {dashscope_error}")
            processor.stop()
            stage_processor.close()
            return 1
        pipeline = StreamRealtimeInsightPipeline(
            session_dir=session_dir,
            config=config,
            keywords=keywords,
            llm_client=client,
            dashscope_api_key=dashscope_key,
            notifier=notifier,
            log_fn=print,
        )
        stream_processor = MicStreamProcessor(pipeline=pipeline, log_fn=print)
        try:
            stream_processor.start()
        except Exception as exc:
            print(f"[mic-listen] failed to start stream pipeline: {exc}")
            processor.stop()
            stage_processor.close()
            return 1

    handler_cls = build_mic_http_handler(
        processor=processor,
        upload_token=upload_token,
        stream_processor=stream_processor,
    )
    server = ThreadingHTTPServer((args.host, int(args.port)), handler_cls)
    try:
        print(f"[mic-listen] started at: http://{args.host}:{int(args.port)}")
        print(f"[mic-listen] upload endpoint: http://{args.host}:{int(args.port)}/api/mic/chunk")
        if pipeline_mode == "stream":
            print(
                f"[mic-listen] stream websocket: ws://{args.host}:{int(args.port)}/ws/mic/stream?token=***"
            )
        print(f"[mic-listen] health endpoint: http://{args.host}:{int(args.port)}/api/mic/health")
        print(f"[mic-listen] metrics endpoint: http://{args.host}:{int(args.port)}/api/mic/metrics")
        print(f"[mic-listen] session_dir={session_dir}")
        print(f"[mic-listen] chunk_dir={chunk_dir}")
        print(f"[mic-listen] pipeline_mode={pipeline_mode}")
        if config.profile_enabled:
            print(f"[mic-listen] profile_log={session_dir / 'realtime_profile.jsonl'}")
        if config.dingtalk_enabled:
            print(f"[mic-listen] DingTalk alert enabled cooldown={config.dingtalk_cooldown_sec:.1f}s")
            print(f"[mic-listen] dingtalk_trace_log={dingtalk_trace_path}")
        print("[mic-listen] Press Ctrl+C to stop.")
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
        if stream_processor is not None:
            stream_processor.stop()
        processor.stop()
        stage_processor.close()
    return 0


def run_mic_publish(args: argparse.Namespace) -> int:
    pipeline_mode = str(getattr(args, "rt_pipeline_mode", "chunk") or "chunk").strip().lower() or "chunk"
    if pipeline_mode == "stream":
        publisher = MicStreamPublisher(
            target_url=(args.target_url or "").strip(),
            upload_token=(args.mic_upload_token or "").strip(),
            device=(args.device or "").strip(),
            ffmpeg_bin=(args.ffmpeg_bin or "").strip(),
            frame_duration_ms=max(20, int(getattr(args, "stream_frame_duration_ms", 100))),
            request_timeout_sec=max(1.0, float(args.request_timeout_sec)),
            retry_base_sec=max(0.1, float(args.retry_base_sec)),
            retry_max_sec=max(0.1, float(args.retry_max_sec)),
            log_fn=print,
        )
        return publisher.run()

    work_dir, auto_generated = _resolve_mic_publish_work_dir(getattr(args, "work_dir", ""))
    if auto_generated:
        print(f"[mic-publish] --work-dir not provided; auto-generated timestamp work_dir={work_dir}")

    if work_dir.exists():
        if not work_dir.is_dir():
            print(f"[mic-publish] invalid work_dir (exists but is not directory): {work_dir}")
            return 1
        existing_chunks = _count_existing_mic_publish_chunks(work_dir)
        print("[mic-publish][WARNING][HISTORY-POLLUTION] !!! target work_dir already exists !!!")
        print(f"[mic-publish][WARNING][HISTORY-POLLUTION] path={work_dir}")
        print(
            f"[mic-publish][WARNING][HISTORY-POLLUTION] detected existing chunk files={existing_chunks}; "
            "previous run files may be re-uploaded."
        )
        print(
            "[mic-publish][WARNING][HISTORY-POLLUTION] use a fresh --work-dir/--worker-dir "
            "if you need strict run isolation."
        )

    publisher = MicPublisher(
        target_url=(args.target_url or "").strip(),
        upload_token=(args.mic_upload_token or "").strip(),
        device=(args.device or "").strip(),
        chunk_seconds=max(2.0, float(args.chunk_seconds)),
        work_dir=work_dir,
        ffmpeg_bin=(args.ffmpeg_bin or "").strip(),
        request_timeout_sec=max(1.0, float(args.request_timeout_sec)),
        ready_age_sec=max(0.2, float(args.ready_age_sec)),
        retry_base_sec=max(0.1, float(args.retry_base_sec)),
        retry_max_sec=max(0.1, float(args.retry_max_sec)),
        scan_interval_sec=max(0.05, float(args.scan_interval_sec)),
        log_fn=print,
    )
    return publisher.run()


def _parse_csv_values(raw: object) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return ["zh"]
    out: list[str] = []
    for item in text.split(","):
        value = str(item or "").strip()
        if value:
            out.append(value)
    return out or ["zh"]


def _http_to_ws(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme == "https":
        ws_scheme = "wss"
    else:
        ws_scheme = "ws"
    netloc = parsed.netloc
    if not netloc:
        netloc = parsed.path
    return f"{ws_scheme}://{netloc.rstrip('/')}"


def _build_ws_accept(ws_key: str) -> str:
    raw = (str(ws_key or "").strip() + _WS_GUID).encode("utf-8")
    digest = hashlib.sha1(raw).digest()
    return base64.b64encode(digest).decode("utf-8")


def _read_ws_frame(handle) -> tuple[int, bytes] | None:
    header = _read_exact(handle, 2)
    if not header:
        return None
    b1 = header[0]
    b2 = header[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        raw = _read_exact(handle, 2)
        if raw is None:
            return None
        length = int.from_bytes(raw, "big")
    elif length == 127:
        raw = _read_exact(handle, 8)
        if raw is None:
            return None
        length = int.from_bytes(raw, "big")

    mask_key = b""
    if masked:
        raw_mask = _read_exact(handle, 4)
        if raw_mask is None:
            return None
        mask_key = raw_mask
    payload = _read_exact(handle, length)
    if payload is None:
        return None
    if masked and mask_key:
        data = bytearray(payload)
        for idx in range(len(data)):
            data[idx] ^= mask_key[idx % 4]
        payload = bytes(data)
    return opcode, payload


def _write_ws_frame(handle, *, opcode: int, payload: bytes) -> None:
    body = bytes(payload or b"")
    first = 0x80 | (opcode & 0x0F)
    length = len(body)
    if length < 126:
        header = bytes([first, length])
    elif length < (1 << 16):
        header = bytes([first, 126]) + int(length).to_bytes(2, "big")
    else:
        header = bytes([first, 127]) + int(length).to_bytes(8, "big")
    handle.write(header + body)
    handle.flush()


def _read_exact(handle, size: int) -> bytes | None:
    if size <= 0:
        return b""
    out = bytearray()
    while len(out) < size:
        block = handle.read(size - len(out))
        if not block:
            return None
        out.extend(block)
    return bytes(out)


def _validate_mic_listen_realtime_args(args: argparse.Namespace, *, pipeline_mode: str) -> str:
    rotate_max_bytes = int(getattr(args, "rt_log_rotate_max_bytes", 64 * 1024 * 1024))
    rotate_backup_count = int(getattr(args, "rt_log_rotate_backup_count", 20))
    if rotate_max_bytes < 1024 * 1024:
        return "--rt-log-rotate-max-bytes must be >= 1048576"
    if rotate_backup_count < 1:
        return "--rt-log-rotate-backup-count must be >= 1"

    if pipeline_mode == "stream":
        asr_model = (getattr(args, "rt_asr_model", None) or "").strip()
        if not asr_model:
            return "stream mode requires explicit --rt-asr-model"
        hotwords_file = Path(getattr(args, "rt_hotwords_file", "config/realtime_hotwords.json")).expanduser().resolve()
        try:
            _ = load_hotwords(hotwords_file, log_fn=lambda _msg: None)
        except ValueError as exc:
            return str(exc)
        return ""

    stt_model = (getattr(args, "rt_stt_model", None) or "").strip()
    if not stt_model:
        return "chunk mode requires explicit --rt-stt-model"
    return ""


def _parse_optional_epoch_ms(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _delta_ms(start_ms: object, end_ms: object) -> int | None:
    try:
        start = int(start_ms)  # type: ignore[arg-type]
        end = int(end_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if start < 0 or end < start:
        return None
    return end - start


def _format_ffmpeg_seconds(value: float) -> str:
    text = f"{max(0.01, float(value)):.3f}"
    text = text.rstrip("0").rstrip(".")
    return text or "0.01"


def _ms_per_audio_sec(duration_ms: object, chunk_seconds: object) -> float | None:
    try:
        duration = float(duration_ms)  # type: ignore[arg-type]
        seconds = float(chunk_seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if duration < 0 or seconds <= 0:
        return None
    return round(duration / seconds, 3)


def _rtf(duration_ms: object, chunk_seconds: object) -> float | None:
    try:
        per_sec = _ms_per_audio_sec(duration_ms, chunk_seconds)
        if per_sec is None:
            return None
        return round(per_sec / 1000.0, 4)
    except Exception:
        return None


def _decode_subprocess_output(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw

    encodings: list[str] = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    encodings.extend(["gbk", "cp936"])

    seen: set[str] = set()
    for enc in encodings:
        key = enc.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue

    return raw.decode("utf-8", errors="replace")


def _parse_dshow_audio_devices(output: str) -> list[str]:
    devices: list[str] = []
    in_audio = False
    for raw in output.splitlines():
        line = raw.strip()
        lower = line.lower()
        if "alternative name" in lower:
            continue

        if "directshow audio devices" in lower:
            in_audio = True
            continue
        if "directshow video devices" in lower:
            in_audio = False
            continue

        match = re.search(r'"([^"]+)"', line)
        if not match:
            continue
        name = match.group(1).strip()
        if not name:
            continue

        # ffmpeg output format varies by build/version:
        # 1) old: "DirectShow audio devices" section + quoted names
        # 2) newer: quoted names annotated with "(audio)" / "(video)"
        has_audio_tag = "(audio)" in lower
        has_video_tag = "(video)" in lower
        if has_video_tag:
            continue
        if has_audio_tag or in_audio:
            if name not in devices:
                devices.append(name)
    return devices


def _safe_console_print(message: str) -> None:
    text = str(message)
    stdout = getattr(sys, "stdout", None)
    encoding = getattr(stdout, "encoding", None) or locale.getpreferredencoding(False) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(text)


def run_mic_list_devices(args: argparse.Namespace) -> int:
    ffmpeg_bin = (args.ffmpeg_bin or "").strip() or (which("ffmpeg") or "")
    if not ffmpeg_bin:
        print("[mic-list-devices] ffmpeg not found in PATH")
        return 1

    proc = subprocess.run(  # noqa: S603
        [ffmpeg_bin, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        text=False,
    )
    stdout_text = _decode_subprocess_output(proc.stdout)
    stderr_text = _decode_subprocess_output(proc.stderr)
    output = f"{stdout_text}\n{stderr_text}"
    devices = _parse_dshow_audio_devices(output)

    if not devices:
        _safe_console_print("[mic-list-devices] no audio device detected; raw ffmpeg output:")
        _safe_console_print(output.strip())
        return 1

    _safe_console_print("[mic-list-devices] available audio devices:")
    for idx, name in enumerate(devices, start=1):
        _safe_console_print(f"{idx}. {name}")
    return 0


def _sanitize_chunk_stem(value: str) -> str:
    text = (value or "").strip()
    if text:
        stem = Path(text).stem
    else:
        stem = "mic"
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    sanitized = sanitized.strip("._-")
    return sanitized or "mic"
