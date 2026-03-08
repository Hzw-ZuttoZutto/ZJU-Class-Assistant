from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import requests

from src.live.insight.models import KeywordConfig, RealtimeInsightConfig
from src.live.insight.openai_client import InsightModelResult
from src.live.insight.stage_processor import InsightStageProcessor
from src.live.mic import (
    MicChunkProcessor,
    MicPublisher,
    _resolve_mic_publish_work_dir,
    build_mic_http_handler,
    run_mic_publish,
)


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


class _FakeClient:
    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        return "hello"

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        timeout_sec: float,
    ) -> InsightModelResult:
        return InsightModelResult(
            important=False,
            summary="ok",
            context_summary="ok",
            matched_terms=[],
            reason="ok",
        )


class _SttTimeoutClient(_FakeClient):
    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        raise TimeoutError("stt timeout")


class MicPipelineTests(unittest.TestCase):
    def test_mic_publisher_ffmpeg_command_contains_dshow(self) -> None:
        cmd = MicPublisher.build_ffmpeg_command(
            ffmpeg_bin="ffmpeg",
            device="Microphone (USB)",
            chunk_seconds=10,
            work_dir=Path("/tmp/work"),
        )
        self.assertIn("-f", cmd)
        self.assertIn("dshow", cmd)
        self.assertIn("audio=Microphone (USB)", cmd)
        self.assertIn("-segment_time", cmd)
        self.assertIn("10", cmd)

    def test_resolve_mic_publish_work_dir_auto_timestamp(self) -> None:
        work_dir, auto_generated = _resolve_mic_publish_work_dir("", now=datetime(2026, 3, 8, 12, 34, 56))
        self.assertTrue(auto_generated)
        self.assertEqual(work_dir.name, ".mic_publish_chunks_20260308_123456")

    def test_run_mic_publish_warns_history_pollution_for_existing_work_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            existing_dir = Path(td) / ".mic_publish_chunks_run_01"
            existing_dir.mkdir(parents=True, exist_ok=True)
            (existing_dir / "mic_20260308_123456.mp3").write_bytes(b"old")

            args = argparse.Namespace(
                target_url="http://127.0.0.1:18765",
                mic_upload_token="token",
                device="Microphone (USB)",
                chunk_seconds=10.0,
                work_dir=str(existing_dir),
                ffmpeg_bin="ffmpeg",
                request_timeout_sec=10.0,
                ready_age_sec=1.2,
                retry_base_sec=0.5,
                retry_max_sec=8.0,
                scan_interval_sec=0.2,
            )

            publisher_instance = mock.Mock()
            publisher_instance.run.return_value = 0

            with mock.patch("src.live.mic.MicPublisher", return_value=publisher_instance) as publisher_cls:
                with mock.patch("builtins.print") as print_mock:
                    rc = run_mic_publish(args)

            self.assertEqual(rc, 0)
            self.assertTrue(publisher_cls.called)
            self.assertEqual(publisher_cls.call_args.kwargs["work_dir"], existing_dir.resolve())
            printed = "\n".join(" ".join(str(part) for part in call.args) for call in print_mock.call_args_list)
            self.assertIn("HISTORY-POLLUTION", printed)

    def test_mic_http_auth_size_dedupe_and_processing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = RealtimeInsightConfig(
                enabled=True,
                chunk_seconds=10,
                stt_request_timeout_sec=8.0,
                stt_stage_timeout_sec=32.0,
                stt_retry_count=2,
                stt_retry_interval_sec=0.01,
                analysis_request_timeout_sec=15.0,
                analysis_stage_timeout_sec=60.0,
                analysis_retry_count=2,
                analysis_retry_interval_sec=0.01,
                context_recent_required=0,
                context_wait_timeout_sec_1=0.0,
                context_wait_timeout_sec_2=0.0,
                context_wait_timeout_sec=0.0,
                context_target_chunks=18,
                use_dual_context_wait=True,
                mic_chunk_max_bytes=32,
            )
            stage_processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_FakeClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            processor = MicChunkProcessor(
                stage_processor=stage_processor,
                chunk_dir=base / "_rt_chunks_mic",
                max_chunk_bytes=cfg.mic_chunk_max_bytes,
                log_fn=lambda _msg: None,
            )
            processor.start()

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                build_mic_http_handler(processor=processor, upload_token="token-1"),
            )
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"

                bad = requests.post(
                    f"{base_url}/api/mic/chunk",
                    data=b"1234",
                    headers={"X-Mic-Token": "bad", "X-Chunk-Name": "a.mp3"},
                    timeout=3,
                )
                self.assertEqual(bad.status_code, 401)

                too_large = requests.post(
                    f"{base_url}/api/mic/chunk",
                    data=b"x" * 64,
                    headers={"X-Mic-Token": "token-1", "X-Chunk-Name": "big.mp3"},
                    timeout=3,
                )
                self.assertEqual(too_large.status_code, 413)

                ok = requests.post(
                    f"{base_url}/api/mic/chunk",
                    data=b"abcd",
                    headers={"X-Mic-Token": "token-1", "X-Chunk-Name": "ok.mp3"},
                    timeout=3,
                )
                self.assertEqual(ok.status_code, 202)
                self.assertTrue(ok.json().get("accepted"))

                dup = requests.post(
                    f"{base_url}/api/mic/chunk",
                    data=b"abcd",
                    headers={"X-Mic-Token": "token-1", "X-Chunk-Name": "dup.mp3"},
                    timeout=3,
                )
                self.assertEqual(dup.status_code, 200)
                self.assertTrue(dup.json().get("duplicate"))

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    metrics = requests.get(f"{base_url}/api/mic/metrics", timeout=3).json()
                    if int(metrics.get("processed_total", 0)) >= 1:
                        break
                    time.sleep(0.05)

                metrics = requests.get(f"{base_url}/api/mic/metrics", timeout=3).json()
                self.assertGreaterEqual(int(metrics.get("processed_total", 0)), 1)
                self.assertEqual(int(metrics.get("duplicate_total", 0)), 1)
                self.assertEqual(int(metrics.get("auth_failures", 0)), 1)
                self.assertEqual(int(metrics.get("too_large_total", 0)), 1)

                transcript_rows = _read_jsonl(base / "realtime_transcripts.jsonl")
                insight_rows = _read_jsonl(base / "realtime_insights.jsonl")
                self.assertEqual(len(transcript_rows), 1)
                self.assertEqual(len(insight_rows), 1)
                self.assertEqual(transcript_rows[0]["status"], "ok")
                self.assertEqual(insight_rows[0]["status"], "ok")
            finally:
                server.shutdown()
                server.server_close()
                processor.stop()

    def test_mic_profile_jsonl_contains_timestamps_and_states(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = RealtimeInsightConfig(
                enabled=True,
                chunk_seconds=10,
                stt_request_timeout_sec=8.0,
                stt_stage_timeout_sec=32.0,
                stt_retry_count=2,
                stt_retry_interval_sec=0.01,
                analysis_request_timeout_sec=15.0,
                analysis_stage_timeout_sec=60.0,
                analysis_retry_count=2,
                analysis_retry_interval_sec=0.01,
                context_recent_required=0,
                context_wait_timeout_sec_1=0.0,
                context_wait_timeout_sec_2=0.0,
                context_wait_timeout_sec=0.0,
                context_target_chunks=18,
                use_dual_context_wait=True,
                mic_chunk_max_bytes=1024,
                profile_enabled=True,
            )
            stage_processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_FakeClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            processor = MicChunkProcessor(
                stage_processor=stage_processor,
                chunk_dir=base / "_rt_chunks_mic",
                max_chunk_bytes=cfg.mic_chunk_max_bytes,
                profile_enabled=cfg.profile_enabled,
                log_fn=lambda _msg: None,
            )
            processor.start()

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                build_mic_http_handler(processor=processor, upload_token="token-1"),
            )
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                sent_at_ms = int(time.time() * 1000)
                ok = requests.post(
                    f"{base_url}/api/mic/chunk",
                    data=b"abcd",
                    headers={
                        "X-Mic-Token": "token-1",
                        "X-Chunk-Name": "ok.mp3",
                        "X-Chunk-Sent-At-Ms": str(sent_at_ms),
                    },
                    timeout=3,
                )
                self.assertEqual(ok.status_code, 202)

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    metrics = requests.get(f"{base_url}/api/mic/metrics", timeout=3).json()
                    if int(metrics.get("processed_total", 0)) >= 1:
                        break
                    time.sleep(0.05)

                profile_rows = _read_jsonl(base / "realtime_profile.jsonl")
                self.assertEqual(len(profile_rows), 1)
                row = profile_rows[0]
                self.assertEqual(int(row["local_send_ts_ms"]), sent_at_ms)
                self.assertEqual(str(row.get("final_status", "")), "ok")
                self.assertEqual(str(row.get("stt_status", "")), "ok")
                self.assertEqual(str(row.get("analysis_status", "")), "ok")
                self.assertEqual(int(row.get("chunk_seconds", 0)), 10)
                self.assertIn("remote_dispatch_ts_ms", row)
                self.assertIn("stt_request_ts_ms", row)
                self.assertIn("stt_response_ts_ms", row)
                self.assertIn("analysis_request_ts_ms", row)
                self.assertIn("analysis_response_ts_ms", row)
                self.assertIn("insight_console_log_ts_ms", row)
                self.assertIsNotNone(row.get("network_send_to_remote_receive_ms"))
                self.assertIsNotNone(row.get("queue_wait_ms"))
                self.assertIsNotNone(row.get("stt_ms_per_audio_sec"))
                self.assertIsNotNone(row.get("analysis_ms_per_audio_sec"))
                self.assertIsNotNone(row.get("remote_ms_per_audio_sec"))
                self.assertIsNotNone(row.get("stt_rtf"))
                self.assertIsNotNone(row.get("analysis_rtf"))
                self.assertIsNotNone(row.get("remote_rtf"))
            finally:
                server.shutdown()
                server.server_close()
                processor.stop()

    def test_mic_profile_written_for_transcript_drop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = RealtimeInsightConfig(
                enabled=True,
                chunk_seconds=10,
                stt_request_timeout_sec=1.0,
                stt_stage_timeout_sec=1.0,
                stt_retry_count=1,
                stt_retry_interval_sec=0.01,
                analysis_request_timeout_sec=15.0,
                analysis_stage_timeout_sec=60.0,
                analysis_retry_count=2,
                analysis_retry_interval_sec=0.01,
                context_recent_required=0,
                context_wait_timeout_sec_1=0.0,
                context_wait_timeout_sec_2=0.0,
                context_wait_timeout_sec=0.0,
                context_target_chunks=18,
                use_dual_context_wait=True,
                mic_chunk_max_bytes=1024,
                profile_enabled=True,
            )
            stage_processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_SttTimeoutClient(),  # type: ignore[arg-type]
                log_fn=lambda _msg: None,
            )
            processor = MicChunkProcessor(
                stage_processor=stage_processor,
                chunk_dir=base / "_rt_chunks_mic",
                max_chunk_bytes=cfg.mic_chunk_max_bytes,
                profile_enabled=cfg.profile_enabled,
                log_fn=lambda _msg: None,
            )
            processor.start()

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                build_mic_http_handler(processor=processor, upload_token="token-1"),
            )
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                ok = requests.post(
                    f"{base_url}/api/mic/chunk",
                    data=b"drop",
                    headers={"X-Mic-Token": "token-1", "X-Chunk-Name": "drop.mp3"},
                    timeout=3,
                )
                self.assertEqual(ok.status_code, 202)

                deadline = time.time() + 3.0
                while time.time() < deadline:
                    metrics = requests.get(f"{base_url}/api/mic/metrics", timeout=3).json()
                    if int(metrics.get("processed_total", 0)) >= 1:
                        break
                    time.sleep(0.05)

                profile_rows = _read_jsonl(base / "realtime_profile.jsonl")
                self.assertEqual(len(profile_rows), 1)
                row = profile_rows[0]
                self.assertEqual(str(row.get("final_status", "")), "transcript_drop_timeout")
                self.assertEqual(str(row.get("stt_status", "")), "transcript_drop_timeout")
                self.assertNotIn("analysis_request_ts_ms", row)
                self.assertNotIn("analysis_response_ts_ms", row)
            finally:
                server.shutdown()
                server.server_close()
                processor.stop()


if __name__ == "__main__":
    unittest.main()
