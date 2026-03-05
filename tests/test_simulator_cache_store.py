from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.live.insight.models import KeywordConfig
from src.simulator.cache_store import PROMPT_VERSION, SimulationCacheStore, file_sha256, keywords_hash


class SimulatorCacheStoreTests(unittest.TestCase):
    def test_store_and_load_stt_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = SimulationCacheStore(Path(td))
            key = "abc"
            cache.store_stt(key, text="hello")
            cache.store_analysis(key, {"important": True, "summary": "s"})
            self.assertEqual(cache.load_stt(key), "hello")
            self.assertEqual(cache.load_analysis(key)["summary"], "s")

    def test_cache_key_changes_with_parameters(self) -> None:
        base = {
            "chunk_sha256": "x",
            "stt_model": "a",
            "analysis_model": "b",
            "keywords_hash_value": "k",
            "chunk_seconds": 10,
        }
        key1 = SimulationCacheStore.build_cache_key(stage="stt", prompt_version=PROMPT_VERSION, **base)
        key2 = SimulationCacheStore.build_cache_key(stage="stt", prompt_version="v2", **base)
        key3 = SimulationCacheStore.build_cache_key(stage="analysis", prompt_version=PROMPT_VERSION, **base)
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)

    def test_hash_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.mp3"
            p.write_bytes(b"audio")
            h1 = file_sha256(p)
            h2 = file_sha256(p)
            self.assertEqual(h1, h2)

        k = KeywordConfig(important_terms=["a"], important_phrases=["b"], negative_terms=["c"])
        self.assertTrue(keywords_hash(k))


if __name__ == "__main__":
    unittest.main()
