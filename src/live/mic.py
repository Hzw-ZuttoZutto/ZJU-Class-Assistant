from __future__ import annotations

import argparse
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
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from shutil import which
from typing import Any, Callable

import requests

from src.common.account import resolve_openai_client_settings
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig, format_local_ts
from src.live.insight.openai_client import OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor


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


@dataclass
class _RetryState:
    attempts: int = 0
    next_retry_at: float = 0.0


@dataclass
class _QueuedChunkItem:
    path: Path
    profile: dict[str, Any] | None = None


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
            with self._profile_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")

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


def build_mic_http_handler(*, processor: MicChunkProcessor, upload_token: str):
    class _MicHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/mic/health":
                return self._write_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "running": processor.is_running(),
                        "chunk_dir": processor.chunk_dir.as_posix(),
                    },
                )
            if self.path == "/api/mic/metrics":
                return self._write_json(HTTPStatus.OK, processor.metrics())
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

    chunk_seconds = max(2.0, float(args.rt_chunk_seconds))
    context_window_seconds = max(30.0, float(args.rt_context_window_seconds))
    context_target_chunks = max(1, int(context_window_seconds / max(0.1, chunk_seconds)))
    chunk_dir_cfg = Path(args.mic_chunk_dir).expanduser()
    chunk_dir = chunk_dir_cfg if chunk_dir_cfg.is_absolute() else (session_dir / chunk_dir_cfg)

    config = RealtimeInsightConfig(
        enabled=True,
        chunk_seconds=chunk_seconds,
        context_window_seconds=int(context_window_seconds),
        model=(args.rt_model or "").strip() or "gpt-5-mini",
        stt_model=(args.rt_stt_model or "").strip() or "whisper-large-v3",
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
        context_target_chunks=max(1, context_target_chunks),
        audio_source_mode="mic_upload",
        mic_upload_token=upload_token,
        mic_chunk_max_bytes=max(1, int(args.mic_chunk_max_bytes)),
        mic_chunk_dir=chunk_dir,
        profile_enabled=bool(getattr(args, "rt_profile_enabled", False)),
    )

    client = _build_openai_client(config)
    if client is None:
        return 1

    keywords = _load_keywords(config.keywords_file, log_fn=print)
    stage_processor = InsightStageProcessor(
        session_dir=session_dir,
        config=config,
        keywords=keywords,
        client=client,
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

    handler_cls = build_mic_http_handler(processor=processor, upload_token=upload_token)
    server = ThreadingHTTPServer((args.host, int(args.port)), handler_cls)
    try:
        print(f"[mic-listen] started at: http://{args.host}:{int(args.port)}")
        print(f"[mic-listen] upload endpoint: http://{args.host}:{int(args.port)}/api/mic/chunk")
        print(f"[mic-listen] health endpoint: http://{args.host}:{int(args.port)}/api/mic/health")
        print(f"[mic-listen] metrics endpoint: http://{args.host}:{int(args.port)}/api/mic/metrics")
        print(f"[mic-listen] session_dir={session_dir}")
        print(f"[mic-listen] chunk_dir={chunk_dir}")
        if config.profile_enabled:
            print(f"[mic-listen] profile_log={session_dir / 'realtime_profile.jsonl'}")
        print("[mic-listen] Press Ctrl+C to stop.")
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
    finally:
        server.server_close()
        processor.stop()
    return 0


def run_mic_publish(args: argparse.Namespace) -> int:
    publisher = MicPublisher(
        target_url=(args.target_url or "").strip(),
        upload_token=(args.mic_upload_token or "").strip(),
        device=(args.device or "").strip(),
        chunk_seconds=max(2.0, float(args.chunk_seconds)),
        work_dir=Path(args.work_dir).expanduser().resolve(),
        ffmpeg_bin=(args.ffmpeg_bin or "").strip(),
        request_timeout_sec=max(1.0, float(args.request_timeout_sec)),
        ready_age_sec=max(0.2, float(args.ready_age_sec)),
        retry_base_sec=max(0.1, float(args.retry_base_sec)),
        retry_max_sec=max(0.1, float(args.retry_max_sec)),
        scan_interval_sec=max(0.05, float(args.scan_interval_sec)),
        log_fn=print,
    )
    return publisher.run()


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
