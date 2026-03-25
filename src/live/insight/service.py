from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.common.account import (
    resolve_dashscope_api_key,
    resolve_effective_llm_base_url,
    resolve_openai_client_settings,
)
from src.common.rotating_log import RotatingLineWriter
from src.live.audio_sources import first_teacher_hls_source, list_teacher_audio_sources
from src.live.insight.audio_streamer import RealtimeAudioFrameReader
from src.live.insight.audio_chunker import RealtimeAudioChunker
from src.live.insight.dingtalk import DingTalkNotifier
from src.live.insight.models import (
    InsightEvent,
    KeywordConfig,
    RealtimeInsightConfig,
    TranscriptChunk,
    format_local_ts,
)
from src.live.insight.openai_client import InsightModelResult, OpenAIInsightClient, invoke_analyze_text
from src.live.insight.prompting import build_history_context_block
from src.live.insight.stage_processor import InsightStageProcessor
from src.live.insight.stream_pipeline import StreamRealtimeInsightPipeline


class RealtimeInsightService:
    def __init__(
        self,
        *,
        poller,
        session_dir: Path,
        config: RealtimeInsightConfig,
        log_fn: Callable[[str], None] | None = None,
        chunker: RealtimeAudioChunker | None = None,
        client: OpenAIInsightClient | None = None,
        notifier: DingTalkNotifier | None = None,
    ) -> None:
        self.poller = poller
        self.session_dir = session_dir
        self.config = config
        self._log_fn = log_fn or print
        self._log_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None

        self._keywords = KeywordConfig()
        self._chunker = chunker or RealtimeAudioChunker(
            chunk_dir=self.session_dir / "_rt_chunks",
            chunk_seconds=max(2, int(config.chunk_seconds)),
        )
        self._stream_reader = RealtimeAudioFrameReader(frame_duration_ms=100, log_fn=self._log)
        self._client = client
        self._notifier = notifier
        self._pipeline_mode = str(getattr(config, "pipeline_mode", "chunk") or "chunk").strip().lower() or "chunk"
        self._stream_pipeline: StreamRealtimeInsightPipeline | None = None

        self._active_url = ""
        self._ready_age_sec = 1.2
        self._chunk_seq_counter = 0
        self._chunk_seq_by_name: dict[str, int] = {}
        self._scheduled_chunks: set[str] = set()
        self._futures: dict[str, Future[None]] = {}
        self._max_written_chunk_seq = 0
        self._stage_processor: InsightStageProcessor | None = None

        self._insight_jsonl_path = self.session_dir / "realtime_insights.jsonl"
        self._text_log_path = self.session_dir / "realtime_insights.log"
        self._transcript_jsonl_path = self.session_dir / "realtime_transcripts.jsonl"
        self._analysis_prompt_trace_path = self.session_dir / "analysis_prompt_trace.jsonl"
        rotate_max_bytes = max(1, int(getattr(self.config, "log_rotate_max_bytes", 64 * 1024 * 1024)))
        rotate_backup_count = max(1, int(getattr(self.config, "log_rotate_backup_count", 20)))
        self._transcript_writer = RotatingLineWriter(
            path=self._transcript_jsonl_path,
            max_bytes=rotate_max_bytes,
            backup_count=rotate_backup_count,
        )
        self._insight_writer = RotatingLineWriter(
            path=self._insight_jsonl_path,
            max_bytes=rotate_max_bytes,
            backup_count=rotate_backup_count,
        )
        self._text_log_writer = RotatingLineWriter(
            path=self._text_log_path,
            max_bytes=rotate_max_bytes,
            backup_count=rotate_backup_count,
        )
        self._analysis_trace_writer = RotatingLineWriter(
            path=self._analysis_prompt_trace_path,
            max_bytes=rotate_max_bytes,
            backup_count=rotate_backup_count,
        )

    def start(self) -> None:
        if not self.config.enabled:
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self._lifecycle_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            self._stop_event.clear()
            thread = threading.Thread(target=self._run, name="realtime-insight")
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None:
            thread.join()
        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None
        if self._stream_pipeline is not None:
            self._stream_pipeline.stop()
            self._stream_pipeline = None
        if self._stage_processor is not None:
            self._stage_processor.close()
        elif self._notifier is not None:
            self._notifier.stop()

    def is_running(self) -> bool:
        with self._lifecycle_lock:
            thread = self._thread
            return bool(thread is not None and thread.is_alive())

    def get_runtime_snapshot(self) -> dict[str, object]:
        stage_metrics: dict[str, int] = {}
        stream_metrics: dict[str, object] = {}

        stage = self._stage_processor
        if stage is not None:
            getter = getattr(stage, "get_runtime_metrics", None)
            if callable(getter):
                try:
                    raw = getter()
                    if isinstance(raw, dict):
                        stage_metrics = {
                            str(k): int(v) for k, v in raw.items() if isinstance(v, int)
                        }
                except Exception:
                    stage_metrics = {}

        pipeline = self._stream_pipeline
        if pipeline is not None:
            getter = getattr(pipeline, "get_runtime_metrics", None)
            if callable(getter):
                try:
                    raw = getter()
                    if isinstance(raw, dict):
                        stream_metrics = dict(raw)
                        embedded = stream_metrics.get("analysis_metrics")
                        if isinstance(embedded, dict):
                            stage_metrics = {
                                str(k): int(v)
                                for k, v in embedded.items()
                                if isinstance(v, int)
                            }
                except Exception:
                    stream_metrics = {}

        return {
            "service_running": self.is_running(),
            "pipeline_mode": self._pipeline_mode,
            "stream_metrics": stream_metrics,
            "stage_metrics": stage_metrics,
        }

    def _run(self) -> None:
        try:
            if not self._prepare_runtime():
                return
            if self._pipeline_mode == "stream":
                self._run_stream_mode()
                return

            self._executor = ThreadPoolExecutor(
                max_workers=max(1, int(self.config.max_concurrency)),
                thread_name_prefix="rt-insight-worker",
            )
            try:
                while not self._stop_event.is_set():
                    self._sync_stream_source()
                    self._dispatch_ready_chunks()
                    self._reap_completed_tasks()
                    self._stop_event.wait(max(0.2, float(self.config.poll_interval_sec)))
            finally:
                self._chunker.stop()
                self._dispatch_ready_chunks(force=True)
                self._wait_for_running_tasks()
                if self._executor is not None:
                    self._executor.shutdown(wait=True, cancel_futures=False)
                self._executor = None
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"[rt-insight] service loop crashed: {exc}")
        finally:
            with self._lifecycle_lock:
                current = threading.current_thread()
                if self._thread is current:
                    self._thread = None

    def _prepare_runtime(self) -> bool:
        if self._pipeline_mode == "stream":
            return self._prepare_stream_runtime()
        return self._prepare_chunk_runtime()

    def _prepare_chunk_runtime(self) -> bool:
        if not str(self.config.stt_model or "").strip():
            self._log("[rt-insight] chunk mode requires explicit stt_model; realtime insight disabled")
            return False
        self._keywords = self._load_keywords(self.config.keywords_file)
        if not self._chunker.ensure_available():
            self._log("[rt-insight] ffmpeg not found; disabling realtime insight")
            return False
        if self._client is None:
            api_key, resolved_base_url, key_error = resolve_openai_client_settings(
                api_key_env_name=self.config.api_key_env,
                base_url_env_name=self.config.base_url_env,
                model_name=self.config.model,
            )
            if not api_key:
                self._log(
                    f"[rt-insight] {key_error}; realtime insight disabled"
                )
                return False
            base_url = resolve_effective_llm_base_url(
                model_name=self.config.model,
                explicit_base_url=self.config.api_base_url,
                resolved_base_url=resolved_base_url,
            )
            self.config.api_base_url = base_url
            try:
                self._client = OpenAIInsightClient(
                    api_key=api_key,
                    timeout_sec=max(
                        float(self.config.stt_request_timeout_sec),
                        float(self.config.analysis_request_timeout_sec),
                    ),
                    base_url=base_url,
                )
            except Exception as exc:
                self._log(f"[rt-insight] failed to initialize OpenAI client: {exc}")
                return False
        self._log(
            "[rt-insight] started with "
            f"stt_model={self.config.stt_model}, analysis_model={self.config.model}, "
            f"chunk={self.config.chunk_seconds}s, max_concurrency={self.config.max_concurrency}, "
            f"api_base_url={(self.config.api_base_url or 'default_openai')}"
        )
        self._stage_processor = InsightStageProcessor(
            session_dir=self.session_dir,
            config=self.config,
            keywords=self._keywords,
            client=self._client,
            notifier=self._notifier,
            log_fn=self._log,
            stop_event=self._stop_event,
        )
        return True

    def _prepare_stream_runtime(self) -> bool:
        if not str(self.config.asr_model or "").strip():
            self._log("[rt-stream] stream mode requires explicit asr_model; stream insight disabled")
            return False
        if not self._stream_reader.ensure_available():
            self._log("[rt-stream] ffmpeg not found; disabling stream insight")
            return False
        if self._notifier is None:
            self._log("[rt-stream] DingTalk notifier is required for stream mode")
            return False
        self._keywords = self._load_keywords(self.config.keywords_file)
        if self._client is None:
            api_key, resolved_base_url, key_error = resolve_openai_client_settings(
                api_key_env_name=self.config.api_key_env,
                base_url_env_name=self.config.base_url_env,
                model_name=self.config.model,
            )
            if not api_key:
                self._log(f"[rt-stream] {key_error}; stream insight disabled")
                return False
            base_url = resolve_effective_llm_base_url(
                model_name=self.config.model,
                explicit_base_url=self.config.api_base_url,
                resolved_base_url=resolved_base_url,
            )
            self.config.api_base_url = base_url
            try:
                self._client = OpenAIInsightClient(
                    api_key=api_key,
                    timeout_sec=max(
                        float(self.config.stt_request_timeout_sec),
                        float(self.config.analysis_request_timeout_sec),
                    ),
                    base_url=base_url,
                )
            except Exception as exc:
                self._log(f"[rt-stream] failed to initialize OpenAI client: {exc}")
                return False
        dashscope_key, dashscope_err = resolve_dashscope_api_key(env_name=self.config.asr_api_key_env)
        if not dashscope_key:
            self._log(f"[rt-stream] {dashscope_err}; stream insight disabled")
            return False
        if self._client is None:
            self._log("[rt-stream] OpenAI client unavailable")
            return False
        try:
            self._stream_pipeline = StreamRealtimeInsightPipeline(
                session_dir=self.session_dir,
                config=self.config,
                keywords=self._keywords,
                llm_client=self._client,
                dashscope_api_key=dashscope_key,
                notifier=self._notifier,
                log_fn=self._log,
                stop_event=self._stop_event,
            )
            self._stream_pipeline.start()
        except Exception as exc:
            self._log(f"[rt-stream] failed to start stream pipeline: {exc}")
            self._stream_pipeline = None
            return False
        self._log(
            "[rt-stream] started with "
            f"asr_scene={self.config.asr_scene}, asr_model={self.config.asr_model}, "
            f"analysis_model={self.config.model}, workers={self.config.stream_analysis_workers}, "
            f"queue_size={self.config.stream_queue_size}"
        )
        return True

    def _run_stream_mode(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._sync_stream_reader_source()
                self._stop_event.wait(max(0.2, float(self.config.poll_interval_sec)))
        finally:
            self._stream_reader.stop()
            if self._stream_pipeline is not None:
                self._stream_pipeline.stop()
                self._stream_pipeline = None

    def _on_stream_audio_frame(self, data: bytes) -> None:
        pipeline = self._stream_pipeline
        if pipeline is None:
            return
        _ = pipeline.submit_audio_frame(data)

    def _sync_stream_source(self) -> None:
        teacher_url = self._teacher_hls_stream_url()
        if not teacher_url:
            if self._active_url:
                self._log("[rt-insight] teacher stream unavailable; pausing audio chunker")
            self._active_url = ""
            self._chunker.stop()
            return
        url_changed = teacher_url != self._active_url
        if not url_changed and self._chunker.is_running():
            return
        self._active_url = teacher_url
        try:
            self._chunker.start(teacher_url)
            if url_changed:
                self._log("[rt-insight] audio chunker switched to new teacher stream")
            else:
                self._log("[rt-insight] audio chunker recovered on unchanged teacher stream")
        except Exception as exc:
            self._log(f"[rt-insight] failed to start audio chunker: {exc}")

    def _sync_stream_reader_source(self) -> None:
        candidates = self._teacher_audio_sources()
        if not candidates:
            if self._active_url:
                self._log("[rt-stream] teacher stream unavailable; pausing frame reader")
            self._active_url = ""
            self._stream_reader.stop()
            return

        if self._active_url and self._active_url in candidates and self._stream_reader.is_running():
            return

        last_error = ""
        for index, candidate in enumerate(candidates):
            url_changed = candidate != self._active_url
            try:
                self._stream_reader.start_stream_source(candidate, on_frame=self._on_stream_audio_frame)
                self._active_url = candidate
                if url_changed:
                    self._log("[rt-stream] audio reader switched to new teacher stream")
                else:
                    self._log("[rt-stream] audio reader recovered on unchanged teacher stream")
                return
            except Exception as exc:
                last_error = str(exc)
                if index + 1 < len(candidates):
                    self._log(f"[rt-stream] audio reader source failed, trying fallback: {exc}")
                continue

        self._active_url = ""
        self._stream_reader.stop()
        if last_error:
            self._log(f"[rt-stream] failed to start audio reader: {last_error}")

    def _teacher_hls_stream_url(self) -> str:
        return first_teacher_hls_source(self.poller.get_snapshot())

    def _teacher_audio_sources(self) -> list[str]:
        return list_teacher_audio_sources(self.poller.get_snapshot())

    def _dispatch_ready_chunks(self, *, force: bool = False) -> None:
        if self._executor is None:
            return
        chunk_dir = self.session_dir / "_rt_chunks"
        if not chunk_dir.exists():
            return
        now = time.time()
        candidates = sorted(chunk_dir.glob("chunk_*.mp3"))
        for chunk_path in candidates:
            chunk_name = chunk_path.name
            if chunk_name in self._scheduled_chunks:
                continue
            if not chunk_path.exists() or chunk_path.stat().st_size <= 0:
                continue
            if not force and (now - chunk_path.stat().st_mtime) < self._ready_age_sec:
                continue
            seq = self._get_or_assign_chunk_seq(chunk_name)
            future = self._executor.submit(self._process_chunk_task, seq, chunk_path)
            self._futures[chunk_name] = future
            self._scheduled_chunks.add(chunk_name)

    def _reap_completed_tasks(self, *, block: bool = False, timeout_sec: float = 0.2) -> None:
        if not self._futures:
            return
        finished: set[Future[None]] = set()
        if block:
            done, _ = wait(
                set(self._futures.values()),
                timeout=max(0.0, timeout_sec),
                return_when=FIRST_COMPLETED,
            )
            finished |= done
        else:
            for future in self._futures.values():
                if future.done():
                    finished.add(future)
        if not finished:
            return

        completed_names = [name for name, future in self._futures.items() if future in finished]
        for name in completed_names:
            future = self._futures.pop(name)
            try:
                future.result()
            except Exception as exc:
                seq = self._chunk_seq_by_name.get(name, 0)
                self._log(f"[rt-insight] worker crashed chunk={name} seq={seq}: {exc}")

    def _wait_for_running_tasks(self) -> None:
        while self._futures:
            self._reap_completed_tasks(block=True, timeout_sec=0.5)

    def _process_chunk_task(self, chunk_seq: int, chunk_path: Path) -> None:
        if self._stage_processor is not None:
            self._stage_processor.process_chunk(chunk_seq, chunk_path)
            return

        now = datetime.now().astimezone()
        transcript_text, stt_status, stt_attempt, stt_error = self._transcribe_with_retry(chunk_path)
        transcript_chunk = TranscriptChunk(
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            ts_local=format_local_ts(now),
            text=transcript_text,
            status=stt_status,
            error=stt_error,
        )
        self._append_transcript(transcript_chunk)

        if stt_status != "ok" or not transcript_text:
            self._log(
                f"[WARNING] [rt-insight] drop chunk seq={chunk_seq} file={chunk_path.name} "
                f"reason={stt_status} error={stt_error}"
            )
            return

        context_chunks = self._wait_and_collect_history(chunk_seq)
        context_text = self._render_history_context(context_chunks)
        context_chunk_count = len(context_chunks)

        result, analysis_status, analysis_attempt, analysis_error = self._analyze_with_retry(
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            current_text=transcript_text,
            context_text=context_text,
            context_chunk_count=context_chunk_count,
        )
        if result is None:
            self._write_drop_insight(
                ts=now,
                chunk_seq=chunk_seq,
                chunk_file=chunk_path.name,
                status=analysis_status,
                attempt_count=analysis_attempt,
                error=analysis_error,
                context_chunk_count=context_chunk_count,
            )
            self._log(
                f"[WARNING] [rt-insight] analysis dropped seq={chunk_seq} file={chunk_path.name} "
                f"reason={analysis_status} error={analysis_error}"
            )
            return

        summary = result.summary or "当前没有什么重要内容"
        context_summary = result.context_summary or "无重要内容"
        event = InsightEvent(
            ts=now,
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            model=self.config.model,
            important=bool(result.important),
            summary=summary,
            context_summary=context_summary,
            matched_terms=result.matched_terms,
            reason=result.reason,
            attempt_count=analysis_attempt,
            context_chunk_count=context_chunk_count,
            is_recovery=self._mark_and_check_recovery(chunk_seq),
        )
        self._append_insight_event(event)

    def _transcribe_with_retry(self, chunk_path: Path) -> tuple[str, str, int, str]:
        if self._client is None:
            return "", "transcript_drop_error", 0, "OpenAI client unavailable"

        total_attempts = max(1, int(self.config.stt_retry_count))
        deadline = time.monotonic() + max(1.0, float(self.config.stt_stage_timeout_sec))
        last_error = ""
        for attempt in range(1, total_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "", "transcript_drop_timeout", attempt - 1, last_error or "stage timeout"
            per_call_timeout = min(max(1.0, float(self.config.stt_request_timeout_sec)), remaining)
            try:
                text = self._client.transcribe_chunk(
                    chunk_path=chunk_path,
                    stt_model=self.config.stt_model,
                    timeout_sec=per_call_timeout,
                )
                text = text.strip()
                if not text:
                    raise ValueError("transcript is empty")
                return text, "ok", attempt, ""
            except Exception as exc:
                last_error = str(exc)
                if attempt < total_attempts:
                    time.sleep(max(0.0, float(self.config.stt_retry_interval_sec)) or 0.2)
                    continue
        timed_out = time.monotonic() >= deadline or ("timeout" in last_error.lower())
        status = "transcript_drop_timeout" if timed_out else "transcript_drop_error"
        return "", status, total_attempts, last_error

    def _analyze_with_retry(
        self,
        *,
        chunk_seq: int,
        chunk_file: str,
        current_text: str,
        context_text: str,
        context_chunk_count: int,
    ) -> tuple[InsightModelResult | None, str, int, str]:
        if self._client is None:
            return None, "analysis_drop_error", 0, "OpenAI client unavailable"

        total_attempts = max(1, int(self.config.analysis_retry_count))
        deadline = time.monotonic() + max(1.0, float(self.config.analysis_stage_timeout_sec))
        last_error = ""
        for attempt in range(1, total_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "analysis_drop_timeout", attempt - 1, last_error or "stage timeout"
            per_call_timeout = min(max(1.0, float(self.config.analysis_request_timeout_sec)), remaining)
            try:
                debug_index = 0

                def trace_hook(trace_payload: dict[str, object]) -> None:
                    nonlocal debug_index
                    debug_index += 1
                    self._append_analysis_prompt_trace(
                        {
                            "chunk_seq": int(chunk_seq),
                            "chunk_file": str(chunk_file),
                            "attempt": int(attempt),
                            "trace_index": int(debug_index),
                            "context_chunk_count": int(context_chunk_count),
                            "chunk_seconds": float(self.config.chunk_seconds),
                            "current_text": current_text,
                            "context_text": context_text,
                            "system_prompt": str(trace_payload.get("system_prompt", "")),
                            "user_prompt": str(trace_payload.get("user_prompt", "")),
                            "request_payload_snapshot": trace_payload.get("request_payload_snapshot", {}),
                            "raw_response_text": str(trace_payload.get("raw_response_text", "")),
                            "parsed_ok": bool(trace_payload.get("parsed_ok", False)),
                            "parsed_payload": trace_payload.get("parsed_payload", {}),
                            "error": str(trace_payload.get("error", "")),
                            "duration_sec": float(trace_payload.get("duration_sec", 0.0)),
                        }
                    )

                result = invoke_analyze_text(
                    self._client,
                    analysis_model=self.config.model,
                    keywords=self._keywords,
                    current_text=current_text,
                    context_text=context_text,
                    chunk_seconds=float(self.config.chunk_seconds),
                    timeout_sec=per_call_timeout,
                    debug_hook=trace_hook,
                )
                return result, "ok", attempt, ""
            except Exception as exc:
                last_error = str(exc)
                if attempt < total_attempts:
                    time.sleep(max(0.0, float(self.config.analysis_retry_interval_sec)) or 0.2)
                    continue
        timed_out = time.monotonic() >= deadline or ("timeout" in last_error.lower())
        status = "analysis_drop_timeout" if timed_out else "analysis_drop_error"
        return None, status, total_attempts, last_error

    def _wait_and_collect_history(self, chunk_seq: int) -> list[TranscriptChunk]:
        deadline = time.monotonic() + max(0.1, float(self.config.context_wait_timeout_sec))
        while True:
            history = self._load_history_chunks(chunk_seq)
            if self._history_ready(history=history, chunk_seq=chunk_seq):
                return self._trim_history(history)
            if time.monotonic() >= deadline or self._stop_event.is_set():
                return self._trim_history(history)
            time.sleep(0.2)

    def _load_history_chunks(self, chunk_seq: int) -> list[TranscriptChunk]:
        all_chunks = self._load_transcript_chunks()
        history = [chunk for chunk in all_chunks if chunk.status == "ok" and chunk.chunk_seq < chunk_seq]
        history.sort(key=lambda item: item.chunk_seq)
        return history

    def _history_ready(self, *, history: list[TranscriptChunk], chunk_seq: int) -> bool:
        if len(history) < max(0, int(self.config.context_min_ready)):
            return False
        recent_required = max(0, int(self.config.context_recent_required))
        if recent_required <= 0:
            return True
        if chunk_seq <= 1:
            return False

        available = {item.chunk_seq for item in history}
        start = max(1, chunk_seq - recent_required)
        required = range(start, chunk_seq)
        for seq in required:
            if seq not in available:
                return False
        return True

    def _trim_history(self, history: list[TranscriptChunk]) -> list[TranscriptChunk]:
        target = max(1, int(self.config.context_target_chunks))
        if len(history) <= target:
            return history
        return history[-target:]

    @staticmethod
    def _render_history_context(history: list[TranscriptChunk]) -> str:
        if not history:
            return build_history_context_block("")
        lines: list[str] = []
        for item in history:
            lines.append(f"[seq={item.chunk_seq}][{item.ts_local}] {item.text}")
        return build_history_context_block("\n".join(lines))

    def _append_transcript(self, transcript: TranscriptChunk) -> None:
        payload = transcript.to_json_dict()
        with self._io_lock:
            self._transcript_writer.append(json.dumps(payload, ensure_ascii=False) + "\n")

    def _load_transcript_chunks(self) -> list[TranscriptChunk]:
        if not self._transcript_jsonl_path.exists():
            return []
        out: list[TranscriptChunk] = []
        with self._io_lock:
            lines = self._transcript_jsonl_path.read_text(encoding="utf-8").splitlines()
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            out.append(TranscriptChunk.from_json_dict(payload))
        return out

    def _write_drop_insight(
        self,
        *,
        ts: datetime,
        chunk_seq: int,
        chunk_file: str,
        status: str,
        attempt_count: int,
        error: str,
        context_chunk_count: int,
    ) -> None:
        summary = "分析超时已丢弃" if status == "analysis_drop_timeout" else "分析失败已丢弃"
        event = InsightEvent(
            ts=ts,
            chunk_seq=chunk_seq,
            chunk_file=chunk_file,
            model=self.config.model,
            important=False,
            summary=summary,
            context_summary="无重要内容",
            matched_terms=[],
            reason=status,
            attempt_count=attempt_count,
            context_chunk_count=context_chunk_count,
            is_recovery=self._mark_and_check_recovery(chunk_seq),
            status=status,
            error=error,
        )
        self._append_insight_event(event)

    def _append_insight_event(self, event: InsightEvent) -> None:
        payload = event.to_json_dict()
        with self._io_lock:
            self._insight_writer.append(json.dumps(payload, ensure_ascii=False) + "\n")
            self._text_log_writer.append(
                f"{event.text_log_level}\n"
                f"具体内容：{event.summary}\n"
                f"具体上下文：{event.context_summary}\n\n"
            )

        level = "[ALERT]" if event.urgency_percent >= int(self.config.alert_threshold) else "[INFO]"
        self._log(
            f"{level} [rt-insight] seq={event.chunk_seq} chunk={event.chunk_file} "
            f"urgency={event.urgency_percent}% status={event.status} summary={event.summary}"
        )
        if self._notifier is not None and bool(getattr(self.config, "dingtalk_enabled", False)):
            try:
                self._notifier.notify_event(event)
            except Exception as exc:
                self._log(
                    f"[rt-dingtalk] enqueue failed seq={event.chunk_seq} chunk={event.chunk_file} error={exc}"
                )

    def _append_analysis_prompt_trace(self, payload: dict[str, object]) -> None:
        with self._io_lock:
            self._analysis_trace_writer.append(json.dumps(payload, ensure_ascii=False) + "\n")

    def _get_or_assign_chunk_seq(self, chunk_name: str) -> int:
        with self._state_lock:
            seq = self._chunk_seq_by_name.get(chunk_name)
            if seq is not None:
                return seq
            self._chunk_seq_counter += 1
            seq = self._chunk_seq_counter
            self._chunk_seq_by_name[chunk_name] = seq
            return seq

    def _mark_and_check_recovery(self, chunk_seq: int) -> bool:
        with self._state_lock:
            is_recovery = chunk_seq <= self._max_written_chunk_seq
            if chunk_seq > self._max_written_chunk_seq:
                self._max_written_chunk_seq = chunk_seq
            return is_recovery

    def _load_keywords(self, path: Path) -> KeywordConfig:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            self._log(f"[rt-insight] keyword file not found/readable: {path}; using empty rules")
            return KeywordConfig()
        except json.JSONDecodeError:
            self._log(f"[rt-insight] keyword file is invalid JSON: {path}; using empty rules")
            return KeywordConfig()
        if not isinstance(payload, dict):
            self._log(f"[rt-insight] keyword file root is not JSON object: {path}; using empty rules")
            return KeywordConfig()
        return KeywordConfig.from_json_dict(payload)

    def _log(self, msg: str) -> None:
        with self._log_lock:
            self._log_fn(msg)
