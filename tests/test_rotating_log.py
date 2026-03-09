from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src.common.rotating_log import RotatingLineWriter


class RotatingLogWriterTests(unittest.TestCase):
    def test_rotate_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "realtime_asr_events.jsonl"
            writer = RotatingLineWriter(path=path, max_bytes=64, backup_count=2)
            for idx in range(20):
                writer.append(f'{{"i":{idx},"text":"{"x" * 20}"}}\n')

            self.assertTrue(path.exists())
            self.assertTrue((Path(td) / "realtime_asr_events.jsonl.1").exists())
            self.assertTrue((Path(td) / "realtime_asr_events.jsonl.2").exists())
            self.assertFalse((Path(td) / "realtime_asr_events.jsonl.3").exists())

    def test_concurrent_appends_keep_lines_intact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "analysis_prompt_trace.jsonl"
            writer = RotatingLineWriter(path=path, max_bytes=1024 * 1024, backup_count=2)
            total_threads = 8
            lines_per_thread = 200

            def worker(tid: int) -> None:
                for index in range(lines_per_thread):
                    writer.append(f"{tid}-{index}\n")

            threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(total_threads)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            all_lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(all_lines), total_threads * lines_per_thread)
            self.assertEqual(len(set(all_lines)), total_threads * lines_per_thread)


if __name__ == "__main__":
    unittest.main()
