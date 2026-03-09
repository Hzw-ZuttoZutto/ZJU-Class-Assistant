from __future__ import annotations

import os
import threading
from pathlib import Path


class RotatingLineWriter:
    def __init__(
        self,
        *,
        path: Path,
        max_bytes: int,
        backup_count: int,
        encoding: str = "utf-8",
    ) -> None:
        self.path = path
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(1, int(backup_count))
        self.encoding = encoding
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        payload = str(text)
        with self._lock:
            self._rotate_if_needed()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding=self.encoding) as handle:
                handle.write(payload)

    def _rotate_if_needed(self) -> None:
        current = self.path
        try:
            if not current.exists() or current.stat().st_size < self.max_bytes:
                return
        except OSError:
            return

        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink(missing_ok=True)

        for index in range(self.backup_count - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{index}")
            if not src.exists():
                continue
            dst = self.path.with_name(f"{self.path.name}.{index + 1}")
            os.replace(src, dst)

        if current.exists():
            os.replace(current, self.path.with_name(f"{self.path.name}.1"))
