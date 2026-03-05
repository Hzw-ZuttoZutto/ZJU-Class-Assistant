from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from shutil import which

from src.simulator.models import DatasetConfig


def collect_input_mp3_files(mp3_dir: Path, dataset: DatasetConfig) -> list[Path]:
    files: list[Path] = []
    if dataset.files:
        for item in dataset.files:
            candidate = Path(item).expanduser()
            if not candidate.is_absolute():
                candidate = (mp3_dir / candidate).resolve()
            if candidate.exists() and candidate.suffix.lower() == ".mp3":
                files.append(candidate)
    else:
        files = sorted(mp3_dir.glob(dataset.include_glob))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def preprocess_mp3_to_chunks(
    *,
    input_files: list[Path],
    output_dir: Path,
    chunk_seconds: int,
) -> list[Path]:
    ffmpeg = which("ffmpeg") or ""
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for simulator preprocessing")

    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("chunk_*.mp3"):
        stale.unlink(missing_ok=True)

    merged_chunks: list[Path] = []
    next_idx = 1

    for src in input_files:
        if not src.exists():
            continue
        with tempfile.TemporaryDirectory(prefix="sim-chunk-") as td:
            tmp_dir = Path(td)
            pattern = tmp_dir / "part_%06d.mp3"
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(src),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-f",
                "segment",
                "-segment_time",
                str(max(1, int(chunk_seconds))),
                "-reset_timestamps",
                "1",
                str(pattern),
            ]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg preprocess failed for {src}: {proc.stderr.strip()}")

            parts = sorted(tmp_dir.glob("part_*.mp3"))
            for part in parts:
                if not part.exists() or part.stat().st_size <= 0:
                    continue
                chunk_path = output_dir / f"chunk_{next_idx:06d}.mp3"
                shutil.copy2(part, chunk_path)
                merged_chunks.append(chunk_path)
                next_idx += 1

    if not merged_chunks:
        raise RuntimeError("no chunk generated from provided mp3 files")
    return merged_chunks
