from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.simulator.feed_scheduler import FeedScheduler
from src.simulator.models import (
    FeedBarrierRule,
    FeedConfig,
    FeedDelayBackfillRule,
    FeedDuplicateRule,
)


class FeedSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _chunks(self) -> list[Path]:
        paths = []
        for idx in range(1, 6):
            path = self._base / f"chunk_{idx:06d}.mp3"
            path.write_bytes(b"x")
            paths.append(path)
        return paths

    def test_speed_mode_with_drop_duplicate_reorder(self) -> None:
        paths = self._chunks()
        feed = FeedConfig(
            mode="speed",
            speed=2.0,
            drop=[2],
            duplicate=[FeedDuplicateRule(seq=3, times=1)],
            reorder=[1, 3, 5, 4],
        )
        events = FeedScheduler(chunk_seconds=10, feed=feed, seed=1).build_events(paths)
        seqs = [e.source_seq for e in events]
        self.assertEqual(seqs, [1, 3, 3, 5, 4])
        waits = [round(e.wait_before_sec, 2) for e in events]
        self.assertEqual(waits, [0.0, 5.0, 5.0, 5.0, 5.0])

    def test_jitter_deterministic_with_seed(self) -> None:
        paths = self._chunks()
        feed = FeedConfig(mode="jitter", jitter_max_sec=1.0)
        events_a = FeedScheduler(chunk_seconds=10, feed=feed, seed=7).build_events(paths)
        events_b = FeedScheduler(chunk_seconds=10, feed=feed, seed=7).build_events(paths)
        waits_a = [round(e.wait_before_sec, 3) for e in events_a]
        waits_b = [round(e.wait_before_sec, 3) for e in events_b]
        self.assertEqual(waits_a, waits_b)

    def test_delay_backfill_and_barrier(self) -> None:
        paths = self._chunks()
        feed = FeedConfig(
            mode="burst",
            delay_backfill=[FeedDelayBackfillRule(seq=2, delay_sec=3.0)],
            barriers=[FeedBarrierRule(after_seq=1, pause_sec=2.0)],
        )
        events = FeedScheduler(chunk_seconds=10, feed=feed, seed=1).build_events(paths)
        seqs = [e.source_seq for e in events]
        waits = [round(e.wait_before_sec, 2) for e in events]
        self.assertEqual(seqs, [1, 3, 4, 5, 2])
        self.assertEqual(waits[0], 0.0)
        self.assertEqual(waits[1], 2.0)
        self.assertEqual(waits[-1], 3.0)


if __name__ == "__main__":
    unittest.main()
