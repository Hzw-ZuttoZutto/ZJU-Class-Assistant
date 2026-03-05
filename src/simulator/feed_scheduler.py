from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from src.simulator.models import FeedConfig


@dataclass
class FeedEvent:
    source_seq: int
    chunk_path: Path
    wait_before_sec: float


class FeedScheduler:
    def __init__(
        self,
        *,
        chunk_seconds: int,
        feed: FeedConfig,
        seed: int | None = None,
    ) -> None:
        self.chunk_seconds = max(1, int(chunk_seconds))
        self.feed = feed
        self._rng = random.Random(seed)

    def build_events(self, chunk_paths: list[Path]) -> list[FeedEvent]:
        records: list[dict] = [
            {"source_seq": idx + 1, "chunk_path": path, "extra_delay": 0.0}
            for idx, path in enumerate(chunk_paths)
        ]

        if self.feed.drop:
            drop_set = set(self.feed.drop)
            records = [item for item in records if item["source_seq"] not in drop_set]

        if self.feed.duplicate:
            expanded: list[dict] = []
            duplicate_by_seq = {rule.seq: max(1, int(rule.times)) for rule in self.feed.duplicate}
            for item in records:
                expanded.append(item)
                times = duplicate_by_seq.get(int(item["source_seq"]), 0)
                for _ in range(times):
                    expanded.append(dict(item))
            records = expanded

        if self.feed.reorder:
            records = self._apply_reorder(records, self.feed.reorder)

        if self.feed.delay_backfill:
            records = self._apply_delay_backfill(records)

        barrier_by_seq: dict[int, float] = {}
        for barrier in self.feed.barriers:
            barrier_by_seq[barrier.after_seq] = barrier_by_seq.get(barrier.after_seq, 0.0) + max(
                0.0, float(barrier.pause_sec)
            )

        events: list[FeedEvent] = []
        for idx, item in enumerate(records):
            base_wait = self._base_wait(idx)
            wait_before = max(0.0, base_wait + float(item["extra_delay"]))
            events.append(
                FeedEvent(
                    source_seq=int(item["source_seq"]),
                    chunk_path=item["chunk_path"],
                    wait_before_sec=wait_before,
                )
            )

        for idx, event in enumerate(events[:-1]):
            pause = barrier_by_seq.get(event.source_seq, 0.0)
            if pause > 0:
                events[idx + 1].wait_before_sec = max(0.0, events[idx + 1].wait_before_sec + pause)

        return events

    def _base_wait(self, index: int) -> float:
        if index == 0:
            return 0.0

        mode = (self.feed.mode or "realtime").strip().lower()
        if mode == "burst":
            return 0.0
        if mode == "speed":
            return self.chunk_seconds / max(0.01, float(self.feed.speed))
        if mode == "jitter":
            max_sec = max(0.0, float(self.feed.jitter_max_sec))
            return max(0.0, self.chunk_seconds + self._rng.uniform(-max_sec, max_sec))
        return float(self.chunk_seconds)

    def _apply_reorder(self, records: list[dict], order: list[int]) -> list[dict]:
        if not records:
            return []

        buckets: dict[int, list[dict]] = {}
        for item in records:
            seq = int(item["source_seq"])
            buckets.setdefault(seq, []).append(item)

        out: list[dict] = []
        for seq in order:
            if seq in buckets:
                out.extend(buckets.pop(seq))

        for item in records:
            seq = int(item["source_seq"])
            remaining = buckets.get(seq)
            if not remaining:
                continue
            out.extend(remaining)
            buckets.pop(seq, None)
        return out

    def _apply_delay_backfill(self, records: list[dict]) -> list[dict]:
        out: list[dict] = list(records)
        for rule in self.feed.delay_backfill:
            moved: list[dict] = []
            kept: list[dict] = []
            for item in out:
                if int(item["source_seq"]) == int(rule.seq):
                    patch = dict(item)
                    patch["extra_delay"] = float(patch.get("extra_delay", 0.0)) + max(0.0, rule.delay_sec)
                    moved.append(patch)
                else:
                    kept.append(item)
            out = kept + moved
        return out
