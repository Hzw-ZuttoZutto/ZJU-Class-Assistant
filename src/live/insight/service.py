from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.common.account import resolve_openai_api_key
from src.live.insight.audio_chunker import RealtimeAudioChunker
from src.live.insight.models import (
    InsightEvent,
    KeywordConfig,
    RealtimeInsightConfig,
    TranscriptChunk,
    format_local_ts,
)
from src.live.insight.openai_client import InsightModelResult, OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor


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
    ) -> None:
        self.poller = poller
        self.session_dir = session_dir
        self.config = config
        self._log_fn = log_fn or print
        self._log_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None

        self._keywords = KeywordConfig()
        self._chunker = chunker or RealtimeAudioChunker(
            chunk_dir=self.session_dir / "_rt_chunks",
            chunk_seconds=max(2, int(config.chunk_seconds)),
        )
        self._client = client

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

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self._thread is not None:
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="realtime-insight")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        self._thread = None

    def _run(self) -> None:
        if not self._prepare_runtime():
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

    def _prepare_runtime(self) -> bool:
        self._keywords = self._load_keywords(self.config.keywords_file)
        if not self._chunker.ensure_available():
            self._log("[rt-insight] ffmpeg not found; disabling realtime insight")
            return False
        if self._client is None:
            api_key, key_error = resolve_openai_api_key(env_name=self.config.api_key_env)
            if not api_key:
                self._log(
                    f"[rt-insight] {key_error}; realtime insight disabled"
                )
                return False
            try:
                self._client = OpenAIInsightClient(
                    api_key=api_key,
                    timeout_sec=self.config.request_timeout_sec,
                )
            except Exception as exc:
                self._log(f"[rt-insight] failed to initialize OpenAI client: {exc}")
                return False
        self._log(
            "[rt-insight] started with "
            f"stt_model={self.config.stt_model}, analysis_model={self.config.model}, "
            f"chunk={self.config.chunk_seconds}s, max_concurrency={self.config.max_concurrency}"
        )
        self._stage_processor = InsightStageProcessor(
            session_dir=self.session_dir,
            config=self.config,
            keywords=self._keywords,
            client=self._client,
            log_fn=self._log,
            stop_event=self._stop_event,
        )
        return True

    def _sync_stream_source(self) -> None:
        teacher_url = self._teacher_stream_url()
        if not teacher_url:
            if self._active_url:
                self._log("[rt-insight] teacher stream unavailable; pausing audio chunker")
            self._active_url = ""
            self._chunker.stop()
            return
        if teacher_url == self._active_url:
            return
        self._active_url = teacher_url
        try:
            self._chunker.start(teacher_url)
            self._log("[rt-insight] audio chunker attached to teacher stream")
        except Exception as exc:
            self._log(f"[rt-insight] failed to start audio chunker: {exc}")

    def _teacher_stream_url(self) -> str:
        snap = self.poller.get_snapshot()
        stream = snap.streams.get("teacher")
        if not stream:
            return ""
        return str(stream.stream_m3u8 or "").strip()

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
            current_text=transcript_text,
            context_text=context_text,
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

        total_attempts = max(1, 1 + int(self.config.retry_count))
        deadline = time.monotonic() + max(1.0, float(self.config.stage_timeout_sec))
        last_error = ""
        for attempt in range(1, total_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "", "transcript_drop_timeout", attempt - 1, last_error or "stage timeout"
            per_call_timeout = min(max(1.0, float(self.config.request_timeout_sec)), remaining)
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
                    time.sleep(0.2)
                    continue
        timed_out = time.monotonic() >= deadline or ("timeout" in last_error.lower())
        status = "transcript_drop_timeout" if timed_out else "transcript_drop_error"
        return "", status, total_attempts, last_error

    def _analyze_with_retry(
        self,
        *,
        current_text: str,
        context_text: str,
    ) -> tuple[InsightModelResult | None, str, int, str]:
        if self._client is None:
            return None, "analysis_drop_error", 0, "OpenAI client unavailable"

        total_attempts = max(1, 1 + int(self.config.retry_count))
        deadline = time.monotonic() + max(1.0, float(self.config.stage_timeout_sec))
        last_error = ""
        for attempt in range(1, total_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "analysis_drop_timeout", attempt - 1, last_error or "stage timeout"
            per_call_timeout = min(max(1.0, float(self.config.request_timeout_sec)), remaining)
            try:
                result = self._client.analyze_text(
                    analysis_model=self.config.model,
                    keywords=self._keywords,
                    current_text=current_text,
                    context_text=context_text,
                    timeout_sec=per_call_timeout,
                )
                return result, "ok", attempt, ""
            except Exception as exc:
                last_error = str(exc)
                if attempt < total_attempts:
                    time.sleep(0.2)
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
            return "无历史文本块"
        lines: list[str] = []
        for item in history:
            lines.append(f"[seq={item.chunk_seq}][{item.ts_local}] {item.text}")
        return "\n".join(lines)

    def _append_transcript(self, transcript: TranscriptChunk) -> None:
        payload = transcript.to_json_dict()
        with self._io_lock:
            with self._transcript_jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")

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
            with self._insight_jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")

            with self._text_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"紧急程度：{event.urgency_percent}%\n")
                handle.write(f"具体内容：{event.summary}\n")
                handle.write(f"具体上下文：{event.context_summary}\n")
                handle.write("\n")

        level = "[ALERT]" if event.urgency_percent >= int(self.config.alert_threshold) else "[INFO]"
        self._log(
            f"{level} [rt-insight] seq={event.chunk_seq} chunk={event.chunk_file} "
            f"urgency={event.urgency_percent}% status={event.status} summary={event.summary}"
        )

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
