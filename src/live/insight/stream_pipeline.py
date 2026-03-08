from __future__ import annotations

import json
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.live.insight.dingtalk import DingTalkNotifier
from src.live.insight.models import InsightEvent, KeywordConfig, RealtimeInsightConfig
from src.live.insight.openai_client import OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor
from src.live.insight.stream_asr import DashScopeRealtimeAsrClient, RealtimeAsrEvent, resolve_default_asr_model


def load_hotwords(path: Path, *, log_fn: Callable[[str], None]) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        log_fn(f"[rt-stream-asr] hotwords file not found/readable: {path}; using empty hotwords")
        return []
    except json.JSONDecodeError:
        log_fn(f"[rt-stream-asr] hotwords file is invalid JSON: {path}; using empty hotwords")
        return []

    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    log_fn(f"[rt-stream-asr] hotwords file root is not JSON array: {path}; using empty hotwords")
    return []


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
        self._started = False

        self._final_seq = 0
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
        )
        self._asr_events_path = self.session_dir / "realtime_asr_events.jsonl"

        model = (self.config.asr_model or "").strip() or resolve_default_asr_model(self.config.asr_scene)
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
            return self._asr_client.send_audio_frame(data)
        except Exception as exc:
            self._on_asr_error(f"send frame failed: {exc}")
            return False

    def _on_asr_event(self, event: RealtimeAsrEvent) -> None:
        payload = event.to_json_dict()
        with self._io_lock:
            with self._asr_events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.write("\n")
        if not event.is_final:
            return

        with self._state_lock:
            self._final_seq += 1
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
        self._log(f"[rt-stream-asr] error: {message}; reconnecting")
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        thread = threading.Thread(target=self._reconnect_loop, name="rt-stream-asr-reconnect", daemon=True)
        thread.start()

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
                    self._log(f"[rt-stream-asr] reconnect failed: {exc}")
                    self._reconnect_delay_sec = min(30.0, float(self._reconnect_delay_sec) * 2.0)
        finally:
            with self._reconnect_lock:
                self._reconnecting = False

    def _start_asr(self) -> None:
        self._asr_client.start()
        self._reconnect_delay_sec = 1.0

    def _log(self, message: str) -> None:
        self._log_fn(message)
