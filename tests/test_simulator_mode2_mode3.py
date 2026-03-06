from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.live.insight.models import KeywordConfig
from src.simulator.cache_store import SimulationCacheStore, file_sha256, keywords_hash
from src.simulator.mode_runner import run_mode
from src.simulator.models import (
    DatasetConfig,
    FeedConfig,
    HistoryRule,
    Scenario,
    SimulatorMode,
    StageControlRule,
)


class _FakeProcessor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def process_simulated_chunk(self, **kwargs) -> None:
        self.calls.append(kwargs)

    def process_chunk(self, chunk_seq: int, chunk_path: Path) -> None:
        self.calls.append({"chunk_seq": chunk_seq, "chunk_path": chunk_path})


class Mode2Mode3Tests(unittest.TestCase):
    def _prepare_chunks_and_cache(self):
        td = tempfile.TemporaryDirectory()
        base = Path(td.name)
        chunks = []
        for idx in range(3):
            path = base / f"chunk_{idx+1:06d}.mp3"
            path.write_bytes(f"audio-{idx}".encode("utf-8"))
            chunks.append(path)

        cache = SimulationCacheStore(base / "cache")
        keywords = KeywordConfig(important_terms=["微积分"])
        k_hash = keywords_hash(keywords)

        for idx, chunk in enumerate(chunks, start=1):
            chunk_sha = file_sha256(chunk)
            stt_key = cache.stt_key(
                chunk_sha256=chunk_sha,
                stt_model="stt",
                analysis_model="ana",
                keywords_hash_value=k_hash,
                chunk_seconds=10,
            )
            analysis_key = cache.analysis_key(
                chunk_sha256=chunk_sha,
                stt_model="stt",
                analysis_model="ana",
                keywords_hash_value=k_hash,
                chunk_seconds=10,
            )
            cache.store_stt(stt_key, text=f"text-{idx}")
            cache.store_analysis(
                analysis_key,
                {
                    "important": True,
                    "summary": f"summary-{idx}",
                    "context_summary": "ctx",
                    "matched_terms": ["微积分"],
                    "reason": "hit",
                },
            )

        return td, chunks, cache, keywords

    def test_mode2_translation_timeout_rule(self) -> None:
        td, chunks, cache, keywords = self._prepare_chunks_and_cache()
        try:
            processor = _FakeProcessor()
            scenario = Scenario(
                mode=SimulatorMode.MODE2,
                name="m2",
                dataset=DatasetConfig(),
                feed=FeedConfig(mode="burst"),
                translation_rules=[StageControlRule(seq=2, status="timeout")],
            )
            result = run_mode(
                mode=SimulatorMode.MODE2,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=processor,  # type: ignore[arg-type]
                cache_store=cache,
                client=None,
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=Path(td.name),
                log_fn=lambda _: None,
                seed_override=1,
            )
            self.assertEqual(len(processor.calls), 3)
            self.assertEqual(processor.calls[1]["transcript_status"], "transcript_drop_timeout")
            trace_file = Path(result.summary["trace_file"])
            self.assertTrue(trace_file.exists())
            self.assertEqual(len(trace_file.read_text(encoding="utf-8").splitlines()), 3)
        finally:
            td.cleanup()

    def test_mode3_controlled_history_mask(self) -> None:
        td, chunks, cache, keywords = self._prepare_chunks_and_cache()
        try:
            processor = _FakeProcessor()
            scenario = Scenario(
                mode=SimulatorMode.MODE3,
                name="m3",
                dataset=DatasetConfig(),
                feed=FeedConfig(mode="burst"),
                history_rules=[HistoryRule(seq=2, visibility="111111110011001100", hold_sec=30.0)],
                mode3_variant="controlled_history",
            )
            result = run_mode(
                mode=SimulatorMode.MODE3,
                scenario=scenario,
                chunk_paths=chunks,
                chunk_seconds=10,
                processor=processor,  # type: ignore[arg-type]
                cache_store=cache,
                client=None,
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=5.0,
                precompute_workers=4,
                output_dir=Path(td.name),
                log_fn=lambda _: None,
                seed_override=1,
            )
            self.assertEqual(processor.calls[1]["history_visibility_mask"], "111111110011001100")
            # hold_sec keeps mask active for following chunks
            self.assertEqual(processor.calls[2]["history_visibility_mask"], "111111110011001100")
            trace_file = Path(result.summary["trace_file"])
            self.assertTrue(trace_file.exists())
            self.assertEqual(len(trace_file.read_text(encoding="utf-8").splitlines()), 3)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
