from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from src.live.insight.models import KeywordConfig, RealtimeInsightConfig
from src.live.insight.openai_client import InsightModelResult
from src.live.insight.stream_asr import RealtimeAsrEvent
from src.live.insight.stream_pipeline import StreamRealtimeInsightPipeline


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


class _FakeAsrClient:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def send_audio_frame(self, data: bytes) -> bool:
        return bool(data)


class _FakeLlmClient:
    def __init__(self, *, sleep_sec: float = 0.0) -> None:
        self.sleep_sec = max(0.0, float(sleep_sec))

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        timeout_sec: float,
        debug_hook=None,
    ) -> InsightModelResult:
        if self.sleep_sec > 0:
            time.sleep(self.sleep_sec)
        if debug_hook is not None:
            debug_hook(
                {
                    "system_prompt": "sys",
                    "user_prompt": "usr",
                    "request_payload_snapshot": {"model": analysis_model},
                    "raw_response_text": '{"important": false}',
                    "parsed_ok": True,
                    "parsed_payload": {"important": False},
                    "error": "",
                    "duration_sec": 0.01,
                }
            )
        return InsightModelResult(
            important=False,
            summary="none",
            context_summary="none",
            matched_terms=[],
            reason="none",
        )


class _FakeNotifier:
    def __init__(self) -> None:
        self.events = []
        self.stopped = False

    def notify_event(self, event) -> bool:
        self.events.append(event)
        return True

    def stop(self) -> None:
        self.stopped = True


class StreamPipelineTests(unittest.TestCase):
    def _build_config(self, hotwords_file: Path) -> RealtimeInsightConfig:
        return RealtimeInsightConfig(
            enabled=True,
            pipeline_mode="stream",
            asr_scene="zh",
            asr_model="paraformer-realtime-v2",
            hotwords_file=hotwords_file,
            stream_analysis_workers=1,
            stream_queue_size=1,
            window_sentences=8,
            context_target_chunks=8,
            context_recent_required=0,
            context_wait_timeout_sec=0.0,
            context_wait_timeout_sec_1=0.0,
            context_wait_timeout_sec_2=0.0,
            use_dual_context_wait=True,
            dingtalk_enabled=True,
            analysis_retry_count=1,
            analysis_retry_interval_sec=0.0,
        )

    def test_partial_event_only_writes_asr_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hotwords = base / "hotwords.json"
            hotwords.write_text('["签到"]', encoding="utf-8")
            config = self._build_config(hotwords)
            notifier = _FakeNotifier()
            pipeline = StreamRealtimeInsightPipeline(
                session_dir=base,
                config=config,
                keywords=KeywordConfig(),
                llm_client=_FakeLlmClient(),
                dashscope_api_key="k",
                notifier=notifier,  # type: ignore[arg-type]
                asr_client=_FakeAsrClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            pipeline.start()
            pipeline._on_asr_event(
                RealtimeAsrEvent(
                    global_seq=1,
                    provider_sentence_id="1",
                    ts_local="20260308_120000",
                    text="测试",
                    event_type="partial",
                    is_final=False,
                    start_ms=0,
                    end_ms=100,
                    model="paraformer-realtime-v2",
                    scene="zh",
                )
            )
            time.sleep(0.05)
            pipeline.stop()

            asr_rows = _read_jsonl(base / "realtime_asr_events.jsonl")
            transcript_rows = _read_jsonl(base / "realtime_transcripts.jsonl")
            self.assertEqual(len(asr_rows), 1)
            self.assertEqual(asr_rows[0]["event_type"], "partial")
            self.assertEqual(transcript_rows, [])

    def test_queue_overflow_drops_oldest_and_sends_alert(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hotwords = base / "hotwords.json"
            hotwords.write_text('["签到"]', encoding="utf-8")
            config = self._build_config(hotwords)
            notifier = _FakeNotifier()
            pipeline = StreamRealtimeInsightPipeline(
                session_dir=base,
                config=config,
                keywords=KeywordConfig(),
                llm_client=_FakeLlmClient(sleep_sec=0.2),
                dashscope_api_key="k",
                notifier=notifier,  # type: ignore[arg-type]
                asr_client=_FakeAsrClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            pipeline.start()
            for seq in range(1, 5):
                pipeline._on_asr_event(
                    RealtimeAsrEvent(
                        global_seq=seq,
                        provider_sentence_id=str(seq),
                        ts_local="20260308_120000",
                        text=f"句子{seq}",
                        event_type="final",
                        is_final=True,
                        start_ms=seq * 100,
                        end_ms=seq * 100 + 80,
                        model="paraformer-realtime-v2",
                        scene="zh",
                    )
                )
            time.sleep(1.0)
            pipeline.stop()

            transcript_rows = _read_jsonl(base / "realtime_transcripts.jsonl")
            self.assertEqual(len(transcript_rows), 2)
            self.assertGreaterEqual(len(notifier.events), 1)
            reasons = [str(getattr(event, "reason", "")) for event in notifier.events]
            self.assertIn("stream_queue_drop_oldest", reasons)


if __name__ == "__main__":
    unittest.main()
