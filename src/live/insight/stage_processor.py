from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.live.insight.models import (
    InsightEvent,
    KeywordConfig,
    RealtimeInsightConfig,
    TranscriptChunk,
    format_local_ts,
)
from src.live.insight.openai_client import InsightModelResult, OpenAIInsightClient


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

        self._insight_jsonl_path = self.session_dir / "realtime_insights.jsonl"
        self._text_log_path = self.session_dir / "realtime_insights.log"
        self._transcript_jsonl_path = self.session_dir / "realtime_transcripts.jsonl"

    def process_chunk(self, chunk_seq: int, chunk_path: Path) -> None:
        now = datetime.now().astimezone()
        transcript_text, stt_status, stt_attempt, stt_error = self.transcribe_with_retry(chunk_path)
        transcript_chunk = TranscriptChunk(
            chunk_seq=chunk_seq,
            chunk_file=chunk_path.name,
            ts_local=format_local_ts(now),
            text=transcript_text,
            status=stt_status,
            error=stt_error,
        )
        self.append_transcript(transcript_chunk)

        if stt_status != "ok" or not transcript_text:
            self._log(
                f"[WARNING] [rt-insight] drop chunk seq={chunk_seq} file={chunk_path.name} "
                f"reason={stt_status} error={stt_error}"
            )
            return

        context_chunks = self.wait_and_collect_history(chunk_seq)
        context_text = self.render_history_context(context_chunks)
        context_chunk_count = len(context_chunks)

        result, analysis_status, analysis_attempt, analysis_error = self.analyze_with_retry(
            current_text=transcript_text,
            context_text=context_text,
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
            )
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
        )

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
        )

    def transcribe_with_retry(self, chunk_path: Path) -> tuple[str, str, int, str]:
        if self.client is None:
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
                text = self.client.transcribe_chunk(
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

    def analyze_with_retry(
        self,
        *,
        current_text: str,
        context_text: str,
    ) -> tuple[InsightModelResult | None, str, int, str]:
        if self.client is None:
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
                result = self.client.analyze_text(
                    analysis_model=self.config.model,
                    keywords=self.keywords,
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

    def wait_and_collect_history(self, chunk_seq: int) -> list[TranscriptChunk]:
        deadline = time.monotonic() + max(0.1, float(self.config.context_wait_timeout_sec))
        while True:
            history = self.load_history_chunks(chunk_seq)
            if self.history_ready(history=history, chunk_seq=chunk_seq):
                return self.trim_history(history)
            if time.monotonic() >= deadline or self._is_stopping():
                return self.trim_history(history)
            time.sleep(0.2)

    def load_history_chunks(self, chunk_seq: int) -> list[TranscriptChunk]:
        all_chunks = self.load_transcript_chunks()
        history = [chunk for chunk in all_chunks if chunk.status == "ok" and chunk.chunk_seq < chunk_seq]
        history.sort(key=lambda item: item.chunk_seq)
        return history

    def history_ready(self, *, history: list[TranscriptChunk], chunk_seq: int) -> bool:
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

    def trim_history(self, history: list[TranscriptChunk]) -> list[TranscriptChunk]:
        target = max(1, int(self.config.context_target_chunks))
        if len(history) <= target:
            return history
        return history[-target:]

    @staticmethod
    def render_history_context(history: list[TranscriptChunk]) -> str:
        if not history:
            return "无历史文本块"
        lines: list[str] = []
        for item in history:
            lines.append(f"[seq={item.chunk_seq}][{item.ts_local}] {item.text}")
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
        )
        self.append_insight_event(event)

    def write_success_insight(
        self,
        *,
        ts: datetime,
        chunk_seq: int,
        chunk_file: str,
        result: InsightModelResult,
        attempt_count: int,
        context_chunk_count: int,
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
        )
        self.append_insight_event(event)

    def append_insight_event(self, event: InsightEvent) -> None:
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
