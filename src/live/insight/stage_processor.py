from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.live.insight.models import (
    InsightEvent,
    KeywordConfig,
    RealtimeInsightConfig,
    TranscriptChunk,
    format_local_ts,
)
from src.live.insight.openai_client import InsightModelResult, OpenAIInsightClient


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


class InsightStageProcessor:
    def __init__(
        self,
        *,
        session_dir: Path,
        config: RealtimeInsightConfig,
        keywords: KeywordConfig,
        client: OpenAIInsightClient | None,
        log_fn: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.session_dir = session_dir
        self.config = config
        self.keywords = keywords
        self.client = client
        self._log_fn = log_fn or print
        self._stop_event = stop_event

        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._max_written_chunk_seq = 0
        self._last_context_reason = ""

        self._insight_jsonl_path = self.session_dir / "realtime_insights.jsonl"
        self._text_log_path = self.session_dir / "realtime_insights.log"
        self._transcript_jsonl_path = self.session_dir / "realtime_transcripts.jsonl"

    def process_chunk(self, chunk_seq: int, chunk_path: Path, profile: dict[str, Any] | None = None) -> None:
        if profile is not None:
            profile.setdefault("chunk_seq", int(chunk_seq))
            profile.setdefault("chunk_file", chunk_path.name)
            profile["stage_processor_started_ts_ms"] = _now_epoch_ms()

        now = datetime.now().astimezone()
        transcript_text, stt_status, stt_attempt, stt_error, stt_elapsed_sec = self.transcribe_with_retry(
            chunk_path,
            profile=profile,
        )
        transcript_chunk = TranscriptChunk(
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            ts_local=format_local_ts(now),
            text=transcript_text,
            status=stt_status,
            error=stt_error,
            attempt_count=stt_attempt,
            elapsed_sec=stt_elapsed_sec,
        )
        self.append_transcript(transcript_chunk)
        if profile is not None:
            profile["transcript_written_ts_ms"] = _now_epoch_ms()

        if stt_status != "ok" or not transcript_text:
            if profile is not None:
                profile["final_status"] = stt_status
                profile["final_error"] = stt_error
                profile["stage_processor_finished_ts_ms"] = _now_epoch_ms()
            self._log(
                f"[WARNING] [rt-insight] drop chunk seq={chunk_seq} file={chunk_path.name} "
                f"reason={stt_status} error={stt_error}"
            )
            return

        context_wait_started_ms = _now_epoch_ms()
        context_chunks = self.wait_and_collect_history(chunk_seq)
        context_wait_finished_ms = _now_epoch_ms()
        context_text = self.render_history_context(
            context_chunks,
            chunk_seq=chunk_seq,
            target_chunks=max(1, int(self.config.context_target_chunks)),
            mark_missing=bool(getattr(self.config, "use_dual_context_wait", False)),
        )
        context_chunk_count = len(context_chunks)
        context_reason = str(self._last_context_reason or "").strip()
        context_missing_ranges = self._missing_seq_ranges(
            history=context_chunks,
            chunk_seq=chunk_seq,
            target_chunks=max(1, int(self.config.context_target_chunks)),
        )
        if profile is not None:
            profile["context_wait_started_ts_ms"] = context_wait_started_ms
            profile["context_wait_finished_ts_ms"] = context_wait_finished_ms
            profile["context_wait_elapsed_ms"] = max(0, context_wait_finished_ms - context_wait_started_ms)
            profile["context_reason"] = context_reason
            profile["context_chunk_count"] = int(context_chunk_count)
            profile["context_missing_ranges"] = list(context_missing_ranges)

        result, analysis_status, analysis_attempt, analysis_error, analysis_elapsed_sec = self.analyze_with_retry(
            current_text=transcript_text,
            context_text=context_text,
            profile=profile,
        )
        if result is None:
            self.write_drop_insight(
                ts=now,
                chunk_seq=chunk_seq,
                chunk_file=chunk_path.name,
                status=analysis_status,
                attempt_count=analysis_attempt,
                error=analysis_error,
                context_chunk_count=context_chunk_count,
                analysis_elapsed_sec=analysis_elapsed_sec,
                context_reason=context_reason,
                context_missing_ranges=context_missing_ranges,
                profile=profile,
            )
            if profile is not None:
                profile["final_status"] = analysis_status
                profile["final_error"] = analysis_error
                profile["stage_processor_finished_ts_ms"] = _now_epoch_ms()
            self._log(
                f"[WARNING] [rt-insight] analysis dropped seq={chunk_seq} file={chunk_path.name} "
                f"reason={analysis_status} error={analysis_error}"
            )
            return

        self.write_success_insight(
            ts=now,
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            result=result,
            attempt_count=analysis_attempt,
            context_chunk_count=context_chunk_count,
            analysis_elapsed_sec=analysis_elapsed_sec,
            context_reason=context_reason,
            context_missing_ranges=context_missing_ranges,
            profile=profile,
        )
        if profile is not None:
            profile["final_status"] = "ok"
            profile["final_error"] = ""
            profile["stage_processor_finished_ts_ms"] = _now_epoch_ms()

    def process_simulated_chunk(
        self,
        *,
        chunk_seq: int,
        chunk_path: Path,
        transcript_text: str,
        transcript_status: str,
        transcript_error: str,
        transcript_attempt: int,
        analysis_result: InsightModelResult | dict | None,
        analysis_status: str,
        analysis_error: str,
        analysis_attempt: int,
        history_visibility_mask: str | None = None,
    ) -> None:
        now = datetime.now().astimezone()
        normalized_t_status = (transcript_status or "ok").strip()
        normalized_a_status = (analysis_status or "ok").strip()
        text = (transcript_text or "").strip()

        transcript_chunk = TranscriptChunk(
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            ts_local=format_local_ts(now),
            text=text,
            status=normalized_t_status,
            error=(transcript_error or "").strip(),
            attempt_count=max(0, int(transcript_attempt)),
            elapsed_sec=0.0,
        )
        self.append_transcript(transcript_chunk)

        if normalized_t_status != "ok" or not text:
            self._log(
                f"[WARNING] [rt-insight] drop chunk seq={chunk_seq} file={chunk_path.name} "
                f"reason={normalized_t_status} error={transcript_error}"
            )
            return

        history = self.load_history_chunks(chunk_seq)
        if history_visibility_mask:
            history = self.apply_visibility_mask(
                history=history,
                chunk_seq=chunk_seq,
                visibility_mask=history_visibility_mask,
            )
        history = self.trim_history(history)

        if isinstance(analysis_result, dict):
            analysis_result = InsightModelResult(
                important=bool(analysis_result.get("important", False)),
                summary=str(analysis_result.get("summary", "")).strip(),
                context_summary=str(analysis_result.get("context_summary", "")).strip(),
                matched_terms=[str(x).strip() for x in analysis_result.get("matched_terms", []) if str(x).strip()],
                reason=str(analysis_result.get("reason", "")).strip(),
            )

        if normalized_a_status != "ok" or analysis_result is None:
            if normalized_a_status == "ok":
                normalized_a_status = "analysis_drop_error"
            self.write_drop_insight(
                ts=now,
                chunk_seq=chunk_seq,
                chunk_file=chunk_path.name,
                status=normalized_a_status,
                attempt_count=max(1, int(analysis_attempt)),
                error=(analysis_error or "").strip(),
                context_chunk_count=len(history),
                analysis_elapsed_sec=0.0,
                context_reason="simulated",
                context_missing_ranges=self._missing_seq_ranges(
                    history=history,
                    chunk_seq=chunk_seq,
                    target_chunks=max(1, int(self.config.context_target_chunks)),
                ),
            )
            self._log(
                f"[WARNING] [rt-insight] analysis dropped seq={chunk_seq} file={chunk_path.name} "
                f"reason={normalized_a_status} error={analysis_error}"
            )
            return

        self.write_success_insight(
            ts=now,
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            result=analysis_result,
            attempt_count=max(1, int(analysis_attempt)),
            context_chunk_count=len(history),
            analysis_elapsed_sec=0.0,
            context_reason="simulated",
            context_missing_ranges=self._missing_seq_ranges(
                history=history,
                chunk_seq=chunk_seq,
                target_chunks=max(1, int(self.config.context_target_chunks)),
            ),
        )

    def transcribe_with_retry(
        self,
        chunk_path: Path,
        profile: dict[str, Any] | None = None,
    ) -> tuple[str, str, int, str, float]:
        if self.client is None:
            if profile is not None:
                now_ms = _now_epoch_ms()
                profile["stt_request_ts_ms"] = now_ms
                profile["stt_response_ts_ms"] = now_ms
                profile["stt_status"] = "transcript_drop_error"
                profile["stt_attempt_count"] = 0
                profile["stt_error"] = "OpenAI client unavailable"
                profile["stt_elapsed_sec"] = 0.0
            return "", "transcript_drop_error", 0, "OpenAI client unavailable", 0.0

        started = time.monotonic()
        total_attempts = max(1, int(self.config.stt_retry_count))
        deadline = started + max(1.0, float(self.config.stt_stage_timeout_sec))
        last_error = ""
        retry_interval_sec = max(0.0, float(getattr(self.config, "stt_retry_interval_sec", 0.2)))
        first_request_marked = False
        for attempt in range(1, total_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                elapsed = max(0.0, time.monotonic() - started)
                status = "transcript_drop_timeout"
                if profile is not None:
                    now_ms = _now_epoch_ms()
                    if not first_request_marked:
                        profile["stt_request_ts_ms"] = now_ms
                        first_request_marked = True
                    profile["stt_response_ts_ms"] = now_ms
                    profile["stt_status"] = status
                    profile["stt_attempt_count"] = max(0, attempt - 1)
                    profile["stt_error"] = last_error or "stage timeout"
                    profile["stt_elapsed_sec"] = elapsed
                return (
                    "",
                    status,
                    attempt - 1,
                    last_error or "stage timeout",
                    elapsed,
                )
            per_call_timeout = min(max(1.0, float(self.config.stt_request_timeout_sec)), remaining)
            try:
                if profile is not None and not first_request_marked:
                    profile["stt_request_ts_ms"] = _now_epoch_ms()
                    first_request_marked = True
                text = self.client.transcribe_chunk(
                    chunk_path=chunk_path,
                    stt_model=self.config.stt_model,
                    timeout_sec=per_call_timeout,
                )
                text = text.strip()
                if not text:
                    raise ValueError("transcript is empty")
                elapsed = max(0.0, time.monotonic() - started)
                if profile is not None:
                    profile["stt_response_ts_ms"] = _now_epoch_ms()
                    profile["stt_status"] = "ok"
                    profile["stt_attempt_count"] = attempt
                    profile["stt_error"] = ""
                    profile["stt_elapsed_sec"] = elapsed
                return text, "ok", attempt, "", elapsed
            except Exception as exc:
                last_error = str(exc)
                if attempt < total_attempts:
                    time.sleep(retry_interval_sec or 0.2)
                    continue
        timed_out = time.monotonic() >= deadline or ("timeout" in last_error.lower())
        status = "transcript_drop_timeout" if timed_out else "transcript_drop_error"
        elapsed = max(0.0, time.monotonic() - started)
        if profile is not None:
            now_ms = _now_epoch_ms()
            if not first_request_marked:
                profile["stt_request_ts_ms"] = now_ms
            profile["stt_response_ts_ms"] = now_ms
            profile["stt_status"] = status
            profile["stt_attempt_count"] = total_attempts
            profile["stt_error"] = last_error
            profile["stt_elapsed_sec"] = elapsed
        return "", status, total_attempts, last_error, elapsed

    def analyze_with_retry(
        self,
        *,
        current_text: str,
        context_text: str,
        profile: dict[str, Any] | None = None,
    ) -> tuple[InsightModelResult | None, str, int, str, float]:
        if self.client is None:
            if profile is not None:
                now_ms = _now_epoch_ms()
                profile["analysis_request_ts_ms"] = now_ms
                profile["analysis_response_ts_ms"] = now_ms
                profile["analysis_status"] = "analysis_drop_error"
                profile["analysis_attempt_count"] = 0
                profile["analysis_error"] = "OpenAI client unavailable"
                profile["analysis_elapsed_sec"] = 0.0
            return None, "analysis_drop_error", 0, "OpenAI client unavailable", 0.0

        started = time.monotonic()
        total_attempts = max(1, int(self.config.analysis_retry_count))
        deadline = started + max(1.0, float(self.config.analysis_stage_timeout_sec))
        last_error = ""
        retry_interval_sec = max(0.0, float(getattr(self.config, "analysis_retry_interval_sec", 0.2)))
        first_request_marked = False
        for attempt in range(1, total_attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                elapsed = max(0.0, time.monotonic() - started)
                status = "analysis_drop_timeout"
                if profile is not None:
                    now_ms = _now_epoch_ms()
                    if not first_request_marked:
                        profile["analysis_request_ts_ms"] = now_ms
                        first_request_marked = True
                    profile["analysis_response_ts_ms"] = now_ms
                    profile["analysis_status"] = status
                    profile["analysis_attempt_count"] = max(0, attempt - 1)
                    profile["analysis_error"] = last_error or "stage timeout"
                    profile["analysis_elapsed_sec"] = elapsed
                return (
                    None,
                    status,
                    attempt - 1,
                    last_error or "stage timeout",
                    elapsed,
                )
            per_call_timeout = min(max(1.0, float(self.config.analysis_request_timeout_sec)), remaining)
            try:
                if profile is not None and not first_request_marked:
                    profile["analysis_request_ts_ms"] = _now_epoch_ms()
                    first_request_marked = True
                result = self.client.analyze_text(
                    analysis_model=self.config.model,
                    keywords=self.keywords,
                    current_text=current_text,
                    context_text=context_text,
                    timeout_sec=per_call_timeout,
                )
                elapsed = max(0.0, time.monotonic() - started)
                if profile is not None:
                    profile["analysis_response_ts_ms"] = _now_epoch_ms()
                    profile["analysis_status"] = "ok"
                    profile["analysis_attempt_count"] = attempt
                    profile["analysis_error"] = ""
                    profile["analysis_elapsed_sec"] = elapsed
                return result, "ok", attempt, "", elapsed
            except Exception as exc:
                last_error = str(exc)
                if attempt < total_attempts:
                    time.sleep(retry_interval_sec or 0.2)
                    continue
        timed_out = time.monotonic() >= deadline or ("timeout" in last_error.lower())
        status = "analysis_drop_timeout" if timed_out else "analysis_drop_error"
        elapsed = max(0.0, time.monotonic() - started)
        if profile is not None:
            now_ms = _now_epoch_ms()
            if not first_request_marked:
                profile["analysis_request_ts_ms"] = now_ms
            profile["analysis_response_ts_ms"] = now_ms
            profile["analysis_status"] = status
            profile["analysis_attempt_count"] = total_attempts
            profile["analysis_error"] = last_error
            profile["analysis_elapsed_sec"] = elapsed
        return None, status, total_attempts, last_error, elapsed

    def wait_and_collect_history(self, chunk_seq: int) -> list[TranscriptChunk]:
        poll_interval_sec = max(0.01, float(getattr(self.config, "context_check_interval_sec", 0.2)))
        use_dual = bool(getattr(self.config, "use_dual_context_wait", False))
        if not use_dual:
            deadline = time.monotonic() + max(0.1, float(self.config.context_wait_timeout_sec))
            while True:
                history = self.load_history_chunks(chunk_seq)
                if self.history_ready(history=history, chunk_seq=chunk_seq):
                    self._last_context_reason = "legacy_ready"
                    return self.trim_history(history)
                if time.monotonic() >= deadline or self._is_stopping():
                    self._last_context_reason = "legacy_timeout"
                    return self.trim_history(history)
                time.sleep(poll_interval_sec)

        timeout_recent_sec = max(0.0, float(getattr(self.config, "context_wait_timeout_sec_2", 5.0)))
        timeout_full_sec = max(0.0, float(getattr(self.config, "context_wait_timeout_sec_1", 1.0)))
        recent_deadline = time.monotonic() + timeout_recent_sec
        full_deadline: float | None = None
        while True:
            history = self.load_history_chunks(chunk_seq)
            recent_ready = self._history_recent_ready(history=history, chunk_seq=chunk_seq)
            full_ready = self._history_window_full_ready(history=history, chunk_seq=chunk_seq)
            if recent_ready and full_ready:
                self._last_context_reason = "full18_ready"
                return self.trim_history(history)

            now = time.monotonic()
            if recent_ready:
                if full_deadline is None:
                    full_deadline = now + timeout_full_sec
                if now >= full_deadline or self._is_stopping():
                    self._last_context_reason = "timeout_wait_full18"
                    return self.trim_history(history)
            else:
                if now >= recent_deadline or self._is_stopping():
                    self._last_context_reason = "timeout_wait_recent4"
                    return self.trim_history(history)
            time.sleep(poll_interval_sec)

    def _history_recent_ready(self, *, history: list[TranscriptChunk], chunk_seq: int) -> bool:
        recent_required = self._effective_recent_required(chunk_seq=chunk_seq)
        if recent_required <= 0:
            return True
        available = {item.chunk_seq for item in history}
        start = max(1, chunk_seq - recent_required)
        required = range(start, chunk_seq)
        for seq in required:
            if seq not in available:
                return False
        return True

    def _history_window_full_ready(self, *, history: list[TranscriptChunk], chunk_seq: int) -> bool:
        target = self._effective_target_chunks(chunk_seq=chunk_seq)
        if target <= 0:
            return True
        start = max(1, chunk_seq - target)
        available = {item.chunk_seq for item in history}
        for seq in range(start, chunk_seq):
            if seq not in available:
                return False
        return True

    def _effective_recent_required(self, *, chunk_seq: int) -> int:
        configured = max(0, int(self.config.context_recent_required))
        available = max(0, int(chunk_seq) - 1)
        return min(configured, available)

    def _effective_target_chunks(self, *, chunk_seq: int) -> int:
        configured = max(1, int(self.config.context_target_chunks))
        available = max(0, int(chunk_seq) - 1)
        return min(configured, available)

    @staticmethod
    def _missing_seq_ranges(
        *,
        history: list[TranscriptChunk],
        chunk_seq: int,
        target_chunks: int,
    ) -> list[str]:
        if chunk_seq <= 1:
            return []
        start = max(1, chunk_seq - max(1, int(target_chunks)))
        available = {item.chunk_seq for item in history}
        ranges: list[str] = []
        missing_start: int | None = None
        for seq in range(start, chunk_seq):
            if seq in available:
                if missing_start is not None:
                    end = seq - 1
                    ranges.append(str(missing_start) if missing_start == end else f"{missing_start}-{end}")
                    missing_start = None
                continue
            if missing_start is None:
                missing_start = seq
        if missing_start is not None:
            end = chunk_seq - 1
            ranges.append(str(missing_start) if missing_start == end else f"{missing_start}-{end}")
        return ranges

    def history_ready(self, *, history: list[TranscriptChunk], chunk_seq: int) -> bool:
        if len(history) < max(0, int(self.config.context_min_ready)):
            return False
        return self._history_recent_ready(history=history, chunk_seq=chunk_seq)

    def load_history_chunks(self, chunk_seq: int) -> list[TranscriptChunk]:
        all_chunks = self.load_transcript_chunks()
        history = [chunk for chunk in all_chunks if chunk.status == "ok" and chunk.chunk_seq < chunk_seq]
        history.sort(key=lambda item: item.chunk_seq)
        return history

    def trim_history(self, history: list[TranscriptChunk]) -> list[TranscriptChunk]:
        target = max(1, int(self.config.context_target_chunks))
        if len(history) <= target:
            return history
        return history[-target:]

    @staticmethod
    def render_history_context(
        history: list[TranscriptChunk],
        *,
        chunk_seq: int | None = None,
        target_chunks: int | None = None,
        mark_missing: bool = False,
    ) -> str:
        if not history:
            if not mark_missing:
                return "无历史文本块"
            if chunk_seq is None or chunk_seq <= 1:
                return "无历史文本块"
            target = max(1, int(target_chunks or 18))
            start = max(1, chunk_seq - target)
            missing = f"{start}" if start == (chunk_seq - 1) else f"{start}-{chunk_seq - 1}"
            return f"[missing seq={missing}] 历史文本缺失"

        if not mark_missing or chunk_seq is None:
            lines: list[str] = []
            for item in history:
                lines.append(f"[seq={item.chunk_seq}][{item.ts_local}] {item.text}")
            return "\n".join(lines)

        target = max(1, int(target_chunks or 18))
        start = max(1, chunk_seq - target)
        by_seq = {item.chunk_seq: item for item in history}
        lines: list[str] = []
        missing_start: int | None = None
        for seq in range(start, chunk_seq):
            item = by_seq.get(seq)
            if item is None:
                if missing_start is None:
                    missing_start = seq
                continue
            if missing_start is not None:
                end = seq - 1
                missing = f"{missing_start}" if missing_start == end else f"{missing_start}-{end}"
                lines.append(f"[missing seq={missing}] 历史文本缺失")
                missing_start = None
            lines.append(f"[seq={item.chunk_seq}][{item.ts_local}] {item.text}")
        if missing_start is not None:
            end = chunk_seq - 1
            missing = f"{missing_start}" if missing_start == end else f"{missing_start}-{end}"
            lines.append(f"[missing seq={missing}] 历史文本缺失")
        if not lines:
            return "无历史文本块"
        return "\n".join(lines)

    @staticmethod
    def apply_visibility_mask(
        *,
        history: list[TranscriptChunk],
        chunk_seq: int,
        visibility_mask: str,
    ) -> list[TranscriptChunk]:
        # 18-bit mask: left bit=seq-18, right bit=seq-1.
        kept: list[TranscriptChunk] = []
        allowed: set[int] = set()
        start = max(1, chunk_seq - 18)
        expected = list(range(start, chunk_seq))
        expected = expected[-18:]

        pad_size = 18 - len(expected)
        mask = visibility_mask
        if pad_size > 0:
            mask = mask[pad_size:]

        for idx, seq in enumerate(expected):
            bit_index = 18 - len(expected) + idx
            if bit_index < 0 or bit_index >= len(visibility_mask):
                continue
            if visibility_mask[bit_index] == "1":
                allowed.add(seq)

        for item in history:
            if item.chunk_seq in allowed:
                kept.append(item)
        return kept

    def append_transcript(self, transcript: TranscriptChunk) -> None:
        payload = transcript.to_json_dict()
        with self._io_lock:
            with self._transcript_jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")

    def load_transcript_chunks(self) -> list[TranscriptChunk]:
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

    def write_drop_insight(
        self,
        *,
        ts: datetime,
        chunk_seq: int,
        chunk_file: str,
        status: str,
        attempt_count: int,
        error: str,
        context_chunk_count: int,
        analysis_elapsed_sec: float = 0.0,
        context_reason: str = "",
        context_missing_ranges: list[str] | None = None,
        profile: dict[str, Any] | None = None,
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
            is_recovery=self.mark_and_check_recovery(chunk_seq),
            status=status,
            error=error,
            analysis_elapsed_sec=max(0.0, float(analysis_elapsed_sec)),
            context_reason=str(context_reason or "").strip(),
            context_missing_ranges=list(context_missing_ranges or []),
        )
        self.append_insight_event(event, profile=profile)

    def write_success_insight(
        self,
        *,
        ts: datetime,
        chunk_seq: int,
        chunk_file: str,
        result: InsightModelResult,
        attempt_count: int,
        context_chunk_count: int,
        analysis_elapsed_sec: float = 0.0,
        context_reason: str = "",
        context_missing_ranges: list[str] | None = None,
        profile: dict[str, Any] | None = None,
    ) -> None:
        summary = result.summary or "当前没有什么重要内容"
        context_summary = result.context_summary or "无重要内容"
        event = InsightEvent(
            ts=ts,
            chunk_seq=chunk_seq,
            chunk_file=chunk_file,
            model=self.config.model,
            important=bool(result.important),
            summary=summary,
            context_summary=context_summary,
            matched_terms=result.matched_terms,
            reason=result.reason,
            attempt_count=attempt_count,
            context_chunk_count=context_chunk_count,
            is_recovery=self.mark_and_check_recovery(chunk_seq),
            analysis_elapsed_sec=max(0.0, float(analysis_elapsed_sec)),
            context_reason=str(context_reason or "").strip(),
            context_missing_ranges=list(context_missing_ranges or []),
        )
        self.append_insight_event(event, profile=profile)

    def append_insight_event(self, event: InsightEvent, profile: dict[str, Any] | None = None) -> None:
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

        if profile is not None:
            profile["insight_logged_ts_ms"] = _now_epoch_ms()

        level = "[ALERT]" if event.urgency_percent >= int(self.config.alert_threshold) else "[INFO]"
        self._log(
            f"{level} [rt-insight] seq={event.chunk_seq} chunk={event.chunk_file} "
            f"urgency={event.urgency_percent}% status={event.status} summary={event.summary}"
        )
        if profile is not None:
            profile["insight_console_log_ts_ms"] = _now_epoch_ms()

    def mark_and_check_recovery(self, chunk_seq: int) -> bool:
        with self._state_lock:
            is_recovery = chunk_seq <= self._max_written_chunk_seq
            if chunk_seq > self._max_written_chunk_seq:
                self._max_written_chunk_seq = chunk_seq
            return is_recovery

    def _is_stopping(self) -> bool:
        return bool(self._stop_event is not None and self._stop_event.is_set())

    def _log(self, msg: str) -> None:
        self._log_fn(msg)
