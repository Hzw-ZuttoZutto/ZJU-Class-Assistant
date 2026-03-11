from __future__ import annotations

import json
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.common.billing import BILLING_ALERT_COOLDOWN_SEC, consume_billing_alert_cooldown, detect_billing_issue
from src.common.rotating_log import RotatingLineWriter
from src.live.insight.dingtalk import DingTalkNotifier
from src.live.insight.models import InsightEvent, KeywordConfig, RealtimeInsightConfig
from src.live.insight.openai_client import OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor
from src.live.insight.stream_asr import DashScopeRealtimeAsrClient, RealtimeAsrEvent


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


def _compact_text(text: str, *, max_len: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_len:
        return compact
    return compact[: max(1, max_len - 1)].rstrip() + "…"


def load_hotwords(path: Path, *, log_fn: Callable[[str], None]) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"hotwords file not found/readable: {path} ({exc})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"hotwords file is invalid JSON array: {path}") from exc

    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            text = str(item).strip()
            if text:
                out.append(text)
        log_fn(f"[rt-stream-asr] loaded hotwords file: {path} items={len(out)}")
        return out
    raise ValueError(f"hotwords file root is not JSON array: {path}")


class StreamRealtimeInsightPipeline:
    def __init__(
        self,
        *,
        session_dir: Path,
        config: RealtimeInsightConfig,
        keywords: KeywordConfig,
        llm_client: OpenAIInsightClient,
        dashscope_api_key: str,
        notifier: DingTalkNotifier,
        log_fn: Callable[[str], None] | None = None,
        asr_client: DashScopeRealtimeAsrClient | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.session_dir = session_dir
        self.config = config
        self.keywords = keywords
        self.llm_client = llm_client
        self.notifier = notifier
        self._log_fn = log_fn or print
        self._stop_event = stop_event or threading.Event()
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False
        self._reconnect_delay_sec = 1.0
        self._reconnect_started_at_mono = 0.0
        self._started = False
        self._stream_t0_ms: int | None = None

        self._final_seq = 0
        self._audio_frames_in_total = 0
        self._asr_final_total = 0
        self._queue_drop_total = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(self.config, "stream_analysis_workers", 32))),
            thread_name_prefix="rt-stream-analysis",
        )
        self._active_futures: set[Future[None]] = set()
        self._pending: deque[tuple[int, RealtimeAsrEvent]] = deque()
        self._stage_processor = InsightStageProcessor(
            session_dir=session_dir,
            config=config,
            keywords=keywords,
            client=llm_client,
            notifier=notifier,
            log_fn=self._log,
            stop_event=self._stop_event,
            stream_t0_provider=self.get_stream_t0_ms,
        )
        self._asr_events_path = self.session_dir / "realtime_asr_events.jsonl"
        self._asr_events_writer = RotatingLineWriter(
            path=self._asr_events_path,
            max_bytes=max(1, int(getattr(self.config, "log_rotate_max_bytes", 64 * 1024 * 1024))),
            backup_count=max(1, int(getattr(self.config, "log_rotate_backup_count", 20))),
        )

        model = (self.config.asr_model or "").strip()
        if not model:
            raise ValueError("stream ASR model is empty; pass --rt-asr-model")
        self.config.asr_model = model
        hotwords = load_hotwords(self.config.hotwords_file, log_fn=self._log)
        self._asr_client = asr_client or DashScopeRealtimeAsrClient(
            scene=self.config.asr_scene,
            model=model,
            api_key=dashscope_api_key,
            endpoint=self.config.asr_endpoint,
            hotwords=hotwords,
            translation_target_languages=list(self.config.translation_target_languages or ["zh"]),
            on_event=self._on_asr_event,
            on_error=self._on_asr_error,
            log_fn=self._log,
        )

    def start(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._start_asr()
        self._started = True

    def stop(self) -> None:
        self._stop_event.set()
        self._asr_client.stop()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._stage_processor.close()
        self._started = False

    def submit_audio_frame(self, data: bytes) -> bool:
        if not self._started:
            return False
        try:
            if data:
                self.mark_server_frame_received()
                with self._state_lock:
                    self._audio_frames_in_total += 1
            ok = bool(self._asr_client.send_audio_frame(data))
            if not ok:
                self._on_asr_error("send frame returned False")
            return ok
        except Exception as exc:
            self._on_asr_error(f"send frame failed: {exc}")
            return False

    def mark_server_frame_received(self, *, now_ms: int | None = None) -> int | None:
        current = _now_epoch_ms() if now_ms is None else int(now_ms)
        if current < 0:
            return None
        with self._state_lock:
            if self._stream_t0_ms is None:
                self._stream_t0_ms = current
            return self._stream_t0_ms

    def get_stream_t0_ms(self) -> int | None:
        with self._state_lock:
            return self._stream_t0_ms

    def _on_asr_event(self, event: RealtimeAsrEvent) -> None:
        payload = event.to_json_dict()
        with self._io_lock:
            self._asr_events_writer.append(json.dumps(payload, ensure_ascii=False) + "\n")
        if not event.is_final:
            return

        with self._state_lock:
            self._final_seq += 1
            self._asr_final_total += 1
            chunk_seq = int(self._final_seq)
        self._enqueue_final(chunk_seq=chunk_seq, event=event)

    def _enqueue_final(self, *, chunk_seq: int, event: RealtimeAsrEvent) -> None:
        with self._state_lock:
            max_workers = max(1, int(getattr(self.config, "stream_analysis_workers", 32)))
            queue_size = max(1, int(getattr(self.config, "stream_queue_size", 100)))
            if len(self._active_futures) < max_workers:
                self._submit_locked(chunk_seq=chunk_seq, event=event)
            else:
                self._pending.append((chunk_seq, event))
                if len(self._pending) > queue_size:
                    dropped_seq, dropped_event = self._pending.popleft()
                    self._queue_drop_total += 1
                    self._notify_drop_alert(chunk_seq=dropped_seq, event=dropped_event)

    def _submit_locked(self, *, chunk_seq: int, event: RealtimeAsrEvent) -> None:
        future = self._executor.submit(self._process_final_task, chunk_seq, event)
        self._active_futures.add(future)
        future.add_done_callback(self._on_future_done)

    def _on_future_done(self, future: Future[None]) -> None:
        with self._state_lock:
            self._active_futures.discard(future)
            if self._pending:
                seq, event = self._pending.popleft()
                self._submit_locked(chunk_seq=seq, event=event)
        try:
            future.result()
        except Exception as exc:
            self._log(f"[rt-stream-analysis] worker crashed: {exc}")

    def _process_final_task(self, chunk_seq: int, event: RealtimeAsrEvent) -> None:
        chunk_file = f"asr_sentence_{chunk_seq:06d}.txt"
        self._stage_processor.process_transcript_event(
            chunk_seq=chunk_seq,
            chunk_file=chunk_file,
            transcript_text=event.text,
            ts=datetime.now().astimezone(),
            asr_global_seq=event.global_seq,
            asr_sentence_id=event.provider_sentence_id,
            asr_start_ms=event.start_ms,
            asr_end_ms=event.end_ms,
            translation_text=event.translation_text,
            event_type=event.event_type,
        )

    def _notify_drop_alert(self, *, chunk_seq: int, event: RealtimeAsrEvent) -> None:
        alert_event = InsightEvent(
            ts=datetime.now().astimezone(),
            chunk_seq=int(chunk_seq),
            chunk_file=f"asr_sentence_{int(chunk_seq):06d}.txt",
            model=self.config.model,
            important=True,
            summary=f"实时分析队列发生丢弃：seq={chunk_seq}",
            context_summary="stream 分析任务队列已满，最旧未执行句子被丢弃",
            matched_terms=[],
            reason="stream_queue_drop_oldest",
            attempt_count=1,
            context_chunk_count=0,
            event_type="system_alert",
            headline="实时分析队列丢弃",
            immediate_action="请检查模型吞吐、并发和输入速率配置",
            key_details=[
                f"asr_global_seq={event.global_seq}",
                f"provider_sentence_id={event.provider_sentence_id or 'unknown'}",
                f"queue_size={max(1, int(getattr(self.config, 'stream_queue_size', 100)))}",
            ],
            asr_global_seq=event.global_seq,
            asr_sentence_id=event.provider_sentence_id,
            asr_start_ms=event.start_ms,
            asr_end_ms=event.end_ms,
            target_text=event.text,
            context_text="",
        )
        try:
            self.notifier.notify_event(alert_event)
        except Exception:
            return

    def _on_asr_error(self, message: str) -> None:
        if self._stop_event.is_set():
            return
        self._maybe_emit_billing_alert(phase="asr_callback", error_text=message)
        scheduled = self._schedule_reconnect()
        if scheduled:
            self._log(f"[rt-stream-asr] error: {message}; reconnecting")

    def _schedule_reconnect(self) -> bool:
        with self._reconnect_lock:
            if self._reconnecting:
                return False
            self._reconnecting = True
            self._reconnect_started_at_mono = time.monotonic()
        thread = threading.Thread(target=self._reconnect_loop, name="rt-stream-asr-reconnect", daemon=True)
        thread.start()
        return True

    def _reconnect_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                delay = max(0.5, float(self._reconnect_delay_sec))
                if self._stop_event.wait(delay):
                    return
                try:
                    self._asr_client.stop()
                except Exception:
                    pass
                try:
                    self._start_asr()
                    self._reconnect_delay_sec = 1.0
                    return
                except Exception as exc:
                    self._maybe_emit_billing_alert(phase="asr_reconnect", error_text=str(exc))
                    self._log(f"[rt-stream-asr] reconnect failed: {exc}")
                    self._reconnect_delay_sec = min(30.0, float(self._reconnect_delay_sec) * 2.0)
        finally:
            with self._reconnect_lock:
                self._reconnecting = False
                self._reconnect_started_at_mono = 0.0

    def _start_asr(self) -> None:
        self._asr_client.start()
        self._reconnect_delay_sec = 1.0

    def get_runtime_metrics(self) -> dict[str, Any]:
        with self._state_lock:
            audio_frames_in_total = int(self._audio_frames_in_total)
            asr_final_total = int(self._asr_final_total)
            queue_drop_total = int(self._queue_drop_total)
            pending_count = int(len(self._pending))
            active_workers = int(len(self._active_futures))

        with self._reconnect_lock:
            reconnect_active = bool(self._reconnecting)
            started_at = float(self._reconnect_started_at_mono)
        reconnect_elapsed_sec = max(0.0, time.monotonic() - started_at) if reconnect_active and started_at > 0 else 0.0

        stage_metrics: dict[str, int] = {}
        get_metrics = getattr(self._stage_processor, "get_runtime_metrics", None)
        if callable(get_metrics):
            try:
                raw = get_metrics()
                if isinstance(raw, dict):
                    stage_metrics = {str(k): int(v) for k, v in raw.items() if isinstance(v, int)}
            except Exception:
                stage_metrics = {}

        return {
            "audio_frames_in_total": audio_frames_in_total,
            "asr_final_total": asr_final_total,
            "queue_drop_total": queue_drop_total,
            "pending_count": pending_count,
            "active_workers": active_workers,
            "reconnect_active": reconnect_active,
            "reconnect_elapsed_sec": reconnect_elapsed_sec,
            "analysis_metrics": stage_metrics,
        }

    def _log(self, message: str) -> None:
        self._log_fn(message)

    def _maybe_emit_billing_alert(self, *, phase: str, error_text: str) -> None:
        issue = detect_billing_issue(service_hint="dashscope", error_text=error_text)
        if issue is None:
            return

        allowed, remain_sec = consume_billing_alert_cooldown(issue.service_key)
        if not allowed:
            self._log(
                f"[rt-billing] skip service={issue.service_key} cooldown_remain={remain_sec:.1f}s "
                f"signal={issue.matched_signal}"
            )
            return

        event = InsightEvent(
            ts=datetime.now().astimezone(),
            chunk_seq=0,
            chunk_file=f"billing_{issue.service_key}.json",
            model=self.config.asr_model or self.config.model,
            important=True,
            summary=f"{issue.display_name} 疑似欠费/停服，ASR 重连异常",
            context_summary="stream ASR 链路不可用或退化，请检查计费状态",
            matched_terms=[],
            reason=issue.reason_code,
            attempt_count=1,
            context_chunk_count=0,
            event_type="system_alert",
            headline=f"{issue.display_name} 欠费告警",
            immediate_action=f"请尽快充值并恢复服务：{issue.payment_url}",
            key_details=[
                f"phase={str(phase or 'unknown').strip() or 'unknown'}",
                f"matched_signal={issue.matched_signal}",
                f"payment_url={issue.payment_url}",
                f"cooldown_sec={int(BILLING_ALERT_COOLDOWN_SEC)}",
                f"error={_compact_text(error_text, max_len=260)}",
            ],
            status="billing_alert",
            error=str(error_text or "").strip(),
        )
        try:
            self._stage_processor.append_insight_event(event)
        except Exception as exc:
            self._log(f"[rt-billing] notify failed service={issue.service_key} error={exc}")
