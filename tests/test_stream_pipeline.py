from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src.common.billing import reset_billing_alert_cooldown_for_tests
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig
from src.live.insight.openai_client import InsightModelResult
from src.live.insight.stream_asr import RealtimeAsrEvent
from src.live.insight.stream_pipeline import StreamRealtimeInsightPipeline, load_hotwords


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


class _AlwaysFalseAsrClient(_FakeAsrClient):
    def send_audio_frame(self, data: bytes) -> bool:
        del data
        return False


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

    def notify_event(self, event, **kwargs) -> bool:
        self.events.append((event, dict(kwargs)))
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
            reasons = [str(getattr(event, "reason", "")) for event, _meta in notifier.events]
            self.assertIn("stream_queue_drop_oldest", reasons)

    def test_final_event_carries_pre_send_relative_timestamps(self) -> None:
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
            pipeline.mark_server_frame_received(now_ms=1000)
            pipeline._on_asr_event(
                RealtimeAsrEvent(
                    global_seq=1,
                    provider_sentence_id="s-1",
                    ts_local="20260308_120000",
                    text="test final",
                    event_type="final",
                    is_final=True,
                    start_ms=20,
                    end_ms=120,
                    model="paraformer-realtime-v2",
                    scene="zh",
                )
            )

            deadline = time.time() + 2.0
            while time.time() < deadline and not notifier.events:
                time.sleep(0.05)
            pipeline.stop()

            self.assertTrue(notifier.events)
            event, meta = notifier.events[0]
            self.assertEqual(getattr(event, "asr_end_ms", None), 120)
            self.assertEqual(meta.get("stream_t0_ms"), 1000)
            self.assertIsInstance(meta.get("pre_send_ts_ms"), int)
            self.assertIsInstance(meta.get("pre_send_rel_ms"), int)
            self.assertEqual(int(meta["pre_send_rel_ms"]), int(meta["pre_send_ts_ms"]) - int(meta["stream_t0_ms"]))

    def test_submit_audio_frame_false_triggers_asr_error_callback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hotwords = base / "hotwords.json"
            hotwords.write_text('["签到"]', encoding="utf-8")
            config = self._build_config(hotwords)
            pipeline = StreamRealtimeInsightPipeline(
                session_dir=base,
                config=config,
                keywords=KeywordConfig(),
                llm_client=_FakeLlmClient(),
                dashscope_api_key="k",
                notifier=_FakeNotifier(),  # type: ignore[arg-type]
                asr_client=_AlwaysFalseAsrClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            pipeline.start()
            try:
                with mock.patch.object(pipeline, "_on_asr_error") as on_asr_error:
                    ok = pipeline.submit_audio_frame(b"\x00\x01")
                self.assertFalse(ok)
                on_asr_error.assert_called_once()
                self.assertIn("returned False", str(on_asr_error.call_args[0][0]))
            finally:
                pipeline.stop()

    def test_runtime_metrics_include_stream_and_stage_counters(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hotwords = base / "hotwords.json"
            hotwords.write_text('["签到"]', encoding="utf-8")
            config = self._build_config(hotwords)
            pipeline = StreamRealtimeInsightPipeline(
                session_dir=base,
                config=config,
                keywords=KeywordConfig(),
                llm_client=_FakeLlmClient(),
                dashscope_api_key="k",
                notifier=_FakeNotifier(),  # type: ignore[arg-type]
                asr_client=_FakeAsrClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            pipeline.start()
            pipeline.submit_audio_frame(b"\x00\x01")
            pipeline._on_asr_event(
                RealtimeAsrEvent(
                    global_seq=1,
                    provider_sentence_id="1",
                    ts_local="20260308_120000",
                    text="测试",
                    event_type="final",
                    is_final=True,
                    start_ms=0,
                    end_ms=100,
                    model="paraformer-realtime-v2",
                    scene="zh",
                )
            )
            time.sleep(0.2)
            metrics = pipeline.get_runtime_metrics()
            pipeline.stop()

            self.assertGreaterEqual(int(metrics.get("audio_frames_in_total", 0) or 0), 1)
            self.assertGreaterEqual(int(metrics.get("asr_final_total", 0) or 0), 1)
            stage_metrics = metrics.get("analysis_metrics")
            self.assertIsInstance(stage_metrics, dict)
            assert isinstance(stage_metrics, dict)
            self.assertIn("analysis_ok_total", stage_metrics)
            self.assertIn("analysis_drop_timeout_total", stage_metrics)
            self.assertIn("analysis_drop_error_total", stage_metrics)

    def test_dashscope_billing_alert_emitted_and_cooled_down(self) -> None:
        reset_billing_alert_cooldown_for_tests()
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
            try:
                with mock.patch.object(pipeline, "_schedule_reconnect", return_value=False):
                    pipeline._on_asr_error("Arrearage: account is not in good standing")
                    pipeline._on_asr_error("Arrearage: account is not in good standing")
            finally:
                pipeline.stop()

            self.assertEqual(len(notifier.events), 1)
            event, _meta = notifier.events[0]
            self.assertEqual(getattr(event, "reason", ""), "billing_arrears_dashscope")
            self.assertEqual(getattr(event, "status", ""), "billing_alert")

    def test_load_hotwords_raises_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "missing_hotwords.json"
            with self.assertRaises(ValueError):
                _ = load_hotwords(path, log_fn=lambda _msg: None)

    def test_empty_asr_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hotwords = base / "hotwords.json"
            hotwords.write_text('["签到"]', encoding="utf-8")
            config = self._build_config(hotwords)
            config.asr_model = ""
            with self.assertRaises(ValueError):
                _ = StreamRealtimeInsightPipeline(
                    session_dir=base,
                    config=config,
                    keywords=KeywordConfig(),
                    llm_client=_FakeLlmClient(),
                    dashscope_api_key="k",
                    notifier=_FakeNotifier(),  # type: ignore[arg-type]
                    asr_client=_FakeAsrClient(),  # type: ignore[arg-type]
                    log_fn=lambda _msg: None,
                )

    def test_asr_event_log_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            hotwords = base / "hotwords.json"
            hotwords.write_text('["签到"]', encoding="utf-8")
            config = self._build_config(hotwords)
            config.log_rotate_max_bytes = 128
            config.log_rotate_backup_count = 2
            pipeline = StreamRealtimeInsightPipeline(
                session_dir=base,
                config=config,
                keywords=KeywordConfig(),
                llm_client=_FakeLlmClient(),
                dashscope_api_key="k",
                notifier=_FakeNotifier(),  # type: ignore[arg-type]
                asr_client=_FakeAsrClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            pipeline.start()
            for seq in range(1, 30):
                pipeline._on_asr_event(
                    RealtimeAsrEvent(
                        global_seq=seq,
                        provider_sentence_id=str(seq),
                        ts_local="20260308_120000",
                        text=f"句子{seq}" + ("x" * 20),
                        event_type="partial",
                        is_final=False,
                        start_ms=seq * 100,
                        end_ms=seq * 100 + 80,
                        model="paraformer-realtime-v2",
                        scene="zh",
                    )
                )
            pipeline.stop()
            self.assertTrue((base / "realtime_asr_events.jsonl.1").exists())


if __name__ == "__main__":
    unittest.main()
