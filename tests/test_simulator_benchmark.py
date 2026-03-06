from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.live.insight.models import KeywordConfig
from src.live.insight.openai_client import InsightModelResult
from src.simulator.cache_store import SimulationCacheStore, file_sha256, keywords_hash
from src.simulator.mode_runner import _summarize_samples, run_mode
from src.simulator.models import BenchmarkConfig, DatasetConfig, FeedConfig, Scenario, SimulatorMode


class _FakeProcessor:
    def process_chunk(self, chunk_seq: int, chunk_path: Path) -> None:
        return

    def process_simulated_chunk(self, **kwargs) -> None:
        return


class _FakeClient:
    def __init__(
        self,
        *,
        transcript_map: dict[str, str] | None = None,
        transcribe_failures: set[str] | None = None,
    ) -> None:
        self.transcript_map = transcript_map or {}
        self.transcribe_failures = set(transcribe_failures or set())
        self.transcribe_calls = 0
        self.analyze_calls = 0
        self.analyze_inputs: list[dict[str, str]] = []

    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        self.transcribe_calls += 1
        if chunk_path.name in self.transcribe_failures:
            raise RuntimeError(f"forced transcribe failure for {chunk_path.name}")
        return self.transcript_map.get(chunk_path.name, f"text-{chunk_path.name}")

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords,
        current_text: str,
        context_text: str,
        timeout_sec: float,
        debug_hook=None,
    ):
        self.analyze_calls += 1
        self.analyze_inputs.append({"current_text": current_text, "context_text": context_text})
        if debug_hook is not None:
            debug_hook(
                {
                    "system_prompt": "sys",
                    "user_prompt": "usr",
                    "request_payload_snapshot": {"model": analysis_model, "max_output_tokens": 1200},
                    "raw_response_text": '{"important": false}',
                    "parsed_ok": True,
                    "parsed_payload": {
                        "important": False,
                        "summary": f"s-{current_text}",
                        "context_summary": f"c-{current_text}",
                        "matched_terms": [],
                        "reason": "ok",
                    },
                    "error": "",
                    "duration_sec": 0.01,
                }
            )
        return InsightModelResult(
            important=False,
            summary=f"s-{current_text}",
            context_summary=f"c-{current_text}",
            matched_terms=[],
            reason="ok",
        )


class SimulatorBenchmarkTests(unittest.TestCase):
    @staticmethod
    def _build_chunks(base: Path, count: int) -> list[Path]:
        chunks: list[Path] = []
        for idx in range(count):
            path = base / f"chunk_{idx+1:06d}.mp3"
            path.write_bytes(f"audio-{idx}".encode("utf-8"))
            chunks.append(path)
        return chunks

    @staticmethod
    def _build_scenario(mode: SimulatorMode, *, repeats: int) -> Scenario:
        return Scenario(
            mode=mode,
            name=f"m{int(mode)}",
            dataset=DatasetConfig(),
            feed=FeedConfig(),
            benchmark=BenchmarkConfig(parallel_workers=2, repeats=repeats),
        )

    def test_summarize_samples(self) -> None:
        summary = _summarize_samples([(True, 1.0), (True, 2.0), (False, 0.2)])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["success"], 2)
        self.assertEqual(summary["fail"], 1)
        self.assertGreater(summary["avg_sec"], 0.0)
        self.assertGreaterEqual(summary["max_sec"], summary["min_sec"])

    def test_mode4_mode5_reports_have_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 2)

            cache = SimulationCacheStore(base / "cache")
            scenario4 = self._build_scenario(SimulatorMode.MODE4, repeats=2)
            scenario5 = self._build_scenario(SimulatorMode.MODE5, repeats=2)
            keywords = KeywordConfig()
            client4 = _FakeClient()
            client5 = _FakeClient()

            result4 = run_mode(
                mode=SimulatorMode.MODE4,
                scenario=scenario4,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client4,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            self.assertIn("serial", result4.summary)
            self.assertIn("parallel", result4.summary)
            self.assertIn("transcript_samples", result4.summary)
            self.assertTrue(result4.summary["transcript_samples"])
            self.assertIn("text", result4.summary["transcript_samples"][0])

            result5 = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario5,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client5,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            self.assertIn("serial", result5.summary)
            self.assertIn("parallel", result5.summary)
            self.assertIn("analysis_samples", result5.summary)
            self.assertIn("chunk_results", result5.summary)
            self.assertIn("analysis_trace_file", result5.summary)
            self.assertIn("transcript_prep", result5.summary)
            self.assertTrue(result5.summary["analysis_samples"])
            self.assertIn("summary", result5.summary["analysis_samples"][0])
            self.assertEqual(result5.summary["transcript_prep"]["chunk_count"], 2)
            self.assertEqual(result5.summary["transcript_prep"]["api_calls"], 2)
            self.assertEqual(client5.transcribe_calls, 2)

    def test_mode5_uses_real_transcripts_not_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 2)
            cache = SimulationCacheStore(base / "cache")
            scenario = self._build_scenario(SimulatorMode.MODE5, repeats=1)
            keywords = KeywordConfig()
            client = _FakeClient(
                transcript_map={
                    chunks[0].name: "老师强调傅里叶变换定义",
                    chunks[1].name: "接下来推导频谱性质",
                }
            )

            result = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            samples = result.summary["analysis_samples"]
            self.assertTrue(samples)
            self.assertEqual(samples[0]["current_text"], "老师强调傅里叶变换定义")
            self.assertNotIn("模拟文本", samples[0]["current_text"])
            self.assertIn("[seq=1] 老师强调傅里叶变换定义", samples[1]["context_preview"])

    def test_mode5_context_window_is_limited_to_18_history_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 20)
            cache = SimulationCacheStore(base / "cache")
            scenario = self._build_scenario(SimulatorMode.MODE5, repeats=1)
            keywords = KeywordConfig()
            transcript_map = {chunk.name: f"t{idx}" for idx, chunk in enumerate(chunks, start=1)}
            client = _FakeClient(transcript_map=transcript_map)

            _ = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            serial_inputs = client.analyze_inputs[:20]
            self.assertEqual(len(serial_inputs), 20)
            context_text = serial_inputs[19]["context_text"]
            self.assertEqual(len(context_text.splitlines()), 18)
            self.assertIn("[seq=2] t2", context_text)
            self.assertIn("[seq=19] t19", context_text)
            self.assertNotIn("[seq=1] t1", context_text)
            self.assertNotIn("[seq=20] t20", context_text)

    def test_mode5_transcribe_prepare_runs_once_for_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 3)
            cache = SimulationCacheStore(base / "cache")
            scenario = self._build_scenario(SimulatorMode.MODE5, repeats=3)
            keywords = KeywordConfig()
            client = _FakeClient()

            result = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            prep = result.summary["transcript_prep"]
            self.assertEqual(client.transcribe_calls, 3)
            self.assertEqual(prep["chunk_count"], 3)
            self.assertEqual(prep["cache_hits"], 0)
            self.assertEqual(prep["cache_misses"], 3)
            self.assertEqual(prep["api_calls"], 3)

    def test_mode5_uses_stt_cache_hit_without_api_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 2)
            cache = SimulationCacheStore(base / "cache")
            scenario = self._build_scenario(SimulatorMode.MODE5, repeats=2)
            keywords = KeywordConfig(important_terms=["热力学"])
            k_hash = keywords_hash(keywords)
            stt_model = "stt"
            analysis_model = "ana"
            for chunk in chunks:
                key = cache.stt_key(
                    chunk_sha256=file_sha256(chunk),
                    stt_model=stt_model,
                    analysis_model=analysis_model,
                    keywords_hash_value=k_hash,
                    chunk_seconds=10,
                )
                cache.store_stt(key, text=f"cached-{chunk.name}")
            client = _FakeClient()

            result = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model=stt_model,
                analysis_model=analysis_model,
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            prep = result.summary["transcript_prep"]
            self.assertEqual(client.transcribe_calls, 0)
            self.assertEqual(prep["cache_hits"], 2)
            self.assertEqual(prep["cache_misses"], 0)
            self.assertEqual(prep["api_calls"], 0)
            self.assertIn("cached-chunk_000001.mp3", result.summary["analysis_samples"][0]["current_text"])

    def test_mode5_single_chunk_dual_profile_runs_only_target_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 4)
            cache = SimulationCacheStore(base / "cache")
            scenario = self._build_scenario(SimulatorMode.MODE5, repeats=2)
            keywords = KeywordConfig()
            transcript_map = {chunk.name: f"tx-{idx}" for idx, chunk in enumerate(chunks, start=1)}
            client = _FakeClient(transcript_map=transcript_map)

            result = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
                mode5_profile="single_chunk_dual",
                mode5_target_seq=3,
            )
            self.assertEqual(result.summary["profile"], "single_chunk_dual")
            self.assertEqual(result.summary["selected_chunk_count"], 1)
            self.assertEqual(client.analyze_calls, 4)
            self.assertEqual(len(result.summary["chunk_results"]), 4)
            self.assertTrue(all(item["chunk_seq"] == 3 for item in result.summary["chunk_results"]))
            self.assertIn("[seq=1] tx-1", client.analyze_inputs[0]["context_text"])
            self.assertIn("[seq=2] tx-2", client.analyze_inputs[0]["context_text"])

    def test_mode5_all_chunks_serial_once_disables_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 3)
            cache = SimulationCacheStore(base / "cache")
            scenario = self._build_scenario(SimulatorMode.MODE5, repeats=5)
            keywords = KeywordConfig()
            client = _FakeClient()

            result = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=client,  # type: ignore[arg-type]
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
                mode5_profile="all_chunks_serial_once",
            )
            self.assertEqual(result.summary["profile"], "all_chunks_serial_once")
            self.assertEqual(result.summary["serial_repeats"], 1)
            self.assertEqual(result.summary["parallel_repeats"], 0)
            self.assertEqual(result.summary["parallel"]["count"], 0)
            self.assertEqual(client.analyze_calls, 3)
            self.assertEqual(len(result.summary["chunk_results"]), 3)
            self.assertTrue(all(item["source"] == "serial" for item in result.summary["chunk_results"]))
            trace_file = Path(result.summary["analysis_trace_file"])
            self.assertTrue(trace_file.exists())
            trace_lines = [line for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(trace_lines), 3)

    def test_mode5_transcribe_failure_raises_and_stops_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = self._build_chunks(base, 2)
            cache = SimulationCacheStore(base / "cache")
            scenario = self._build_scenario(SimulatorMode.MODE5, repeats=1)
            keywords = KeywordConfig()
            client = _FakeClient(transcribe_failures={chunks[0].name})

            with self.assertRaises(RuntimeError):
                _ = run_mode(
                    mode=SimulatorMode.MODE5,
                    scenario=scenario,
                    chunk_paths=chunks,
                    chunk_seconds=10,
                    processor=_FakeProcessor(),  # type: ignore[arg-type]
                    cache_store=cache,
                    client=client,  # type: ignore[arg-type]
                    keywords=keywords,
                    stt_model="stt",
                    analysis_model="ana",
                    request_timeout_sec=5.0,
                    precompute_workers=4,
                    output_dir=base,
                    log_fn=lambda _: None,
                    seed_override=1,
                )
            self.assertEqual(client.transcribe_calls, 1)
            self.assertEqual(client.analyze_calls, 0)


if __name__ == "__main__":
    unittest.main()
