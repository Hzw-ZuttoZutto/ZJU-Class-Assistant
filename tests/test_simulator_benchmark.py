from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.live.insight.models import KeywordConfig
from src.live.insight.openai_client import InsightModelResult
from src.simulator.cache_store import SimulationCacheStore
from src.simulator.mode_runner import _summarize_samples, run_mode
from src.simulator.models import BenchmarkConfig, DatasetConfig, FeedConfig, Scenario, SimulatorMode


class _FakeProcessor:
    def process_chunk(self, chunk_seq: int, chunk_path: Path) -> None:
        return

    def process_simulated_chunk(self, **kwargs) -> None:
        return


class _FakeClient:
    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        return f"text-{chunk_path.name}"

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords,
        current_text: str,
        context_text: str,
        timeout_sec: float,
    ):
        return InsightModelResult(
            important=False,
            summary="s",
            context_summary="c",
            matched_terms=[],
            reason="ok",
        )


class SimulatorBenchmarkTests(unittest.TestCase):
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
            chunks = []
            for idx in range(2):
                path = base / f"chunk_{idx+1:06d}.mp3"
                path.write_bytes(b"audio")
                chunks.append(path)

            cache = SimulationCacheStore(base / "cache")
            scenario4 = Scenario(
                mode=SimulatorMode.MODE4,
                name="m4",
                dataset=DatasetConfig(),
                feed=FeedConfig(),
                benchmark=BenchmarkConfig(parallel_workers=2, repeats=2),
            )
            scenario5 = Scenario(
                mode=SimulatorMode.MODE5,
                name="m5",
                dataset=DatasetConfig(),
                feed=FeedConfig(),
                benchmark=BenchmarkConfig(parallel_workers=2, repeats=2),
            )
            keywords = KeywordConfig()

            result4 = run_mode(
                mode=SimulatorMode.MODE4,
                scenario=scenario4,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=_FakeClient(),  # type: ignore[arg-type]
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

            result5 = run_mode(
                mode=SimulatorMode.MODE5,
                scenario=scenario5,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=_FakeProcessor(),  # type: ignore[arg-type]
                cache_store=cache,
                client=_FakeClient(),  # type: ignore[arg-type]
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


if __name__ == "__main__":
    unittest.main()
