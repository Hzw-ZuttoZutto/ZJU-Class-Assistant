from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.live.insight.models import KeywordConfig
from src.live.insight.openai_client import InsightModelResult
from src.simulator.cache_store import SimulationCacheStore, file_sha256, keywords_hash
from src.simulator.precompute import run_precompute


class _FakeClient:
    def __init__(self) -> None:
        self.stt_calls = 0
        self.analysis_calls = 0

    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        self.stt_calls += 1
        return f"text-{chunk_path.name}"

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        timeout_sec: float,
        debug_hook=None,
    ):
        self.analysis_calls += 1
        return InsightModelResult(
            important=True,
            summary=f"summary-{current_text}",
            context_summary="ctx",
            matched_terms=["x"],
            reason="ok",
        )


class PrecomputeTests(unittest.TestCase):
    def test_hit_first_then_compute_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunks = []
            for idx in range(2):
                path = base / f"chunk_{idx+1:06d}.mp3"
                path.write_bytes(f"audio-{idx}".encode("utf-8"))
                chunks.append(path)

            cache = SimulationCacheStore(base / "cache")
            keywords = KeywordConfig(important_terms=["a"])
            k_hash = keywords_hash(keywords)

            first_sha = file_sha256(chunks[0])
            stt_key = cache.stt_key(
                chunk_sha256=first_sha,
                stt_model="stt",
                analysis_model="ana",
                keywords_hash_value=k_hash,
                chunk_seconds=10,
            )
            analysis_key = cache.analysis_key(
                chunk_sha256=first_sha,
                stt_model="stt",
                analysis_model="ana",
                keywords_hash_value=k_hash,
                chunk_seconds=10,
            )
            cache.store_stt(stt_key, text="cached-text")
            cache.store_analysis(analysis_key, {"important": False, "summary": "cached"})

            client = _FakeClient()
            manifest = run_precompute(
                chunk_paths=chunks,
                cache_store=cache,
                client=client,
                keywords=keywords,
                stt_model="stt",
                analysis_model="ana",
                chunk_seconds=10,
                stt_request_timeout_sec=3.0,
                analysis_request_timeout_sec=3.0,
                workers=2,
                log_fn=lambda _: None,
            )

            self.assertEqual(manifest["stt"]["hits"], 1)
            self.assertEqual(manifest["stt"]["misses"], 1)
            self.assertEqual(manifest["analysis"]["hits"], 1)
            self.assertEqual(manifest["analysis"]["misses"], 1)
            self.assertEqual(client.stt_calls, 1)
            self.assertEqual(client.analysis_calls, 1)


if __name__ == "__main__":
    unittest.main()
