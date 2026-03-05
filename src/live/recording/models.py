from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


def sanitize_filename(text: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", text.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "unknown"


def format_local_ts(value: datetime) -> str:
    return value.astimezone().strftime("%Y%m%d_%H%M%S")


def build_session_folder_name(course_title: str, teacher_name: str, started_at: datetime) -> str:
    return (
        f"{sanitize_filename(course_title)}_"
        f"{sanitize_filename(teacher_name)}_"
        f"{format_local_ts(started_at)}"
    )


@dataclass
class RecordingConfig:
    root_dir: Path
    segment_minutes: int
    startup_av_timeout: float
    recovery_window_sec: float
    max_lag_sec: float = 10.0
    poll_interval_sec: float = 1.0


@dataclass
class SessionMeta:
    course_title: str
    teacher_name: str
    watch_started_at: datetime
    session_dir: Path


@dataclass
class GapEvent:
    started_at: datetime
    ended_at: datetime
    reason: str
    rendered: bool = False

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.ended_at - self.started_at).total_seconds())

    def to_json_dict(self) -> dict:
        return {
            "started_at_local": format_local_ts(self.started_at),
            "ended_at_local": format_local_ts(self.ended_at),
            "duration_sec": round(self.duration_sec, 3),
            "reason": self.reason,
            "rendered": self.rendered,
        }


@dataclass
class SegmentPart:
    part_type: Literal["clip", "gap"]
    started_at: datetime
    ended_at: datetime
    source_path: Path | None = None
    reason: str = ""
    rendered_path: Path | None = None

    @property
    def duration_sec(self) -> float:
        return max(0.0, (self.ended_at - self.started_at).total_seconds())

    def to_json_dict(self) -> dict:
        return {
            "part_type": self.part_type,
            "started_at_local": format_local_ts(self.started_at),
            "ended_at_local": format_local_ts(self.ended_at),
            "duration_sec": round(self.duration_sec, 3),
            "source_path": str(self.source_path) if self.source_path else "",
            "rendered_path": str(self.rendered_path) if self.rendered_path else "",
            "reason": self.reason,
        }


@dataclass
class SegmentManifest:
    index: int
    started_at: datetime
    tmp_dir: Path
    parts: list[SegmentPart] = field(default_factory=list)
    gaps: list[GapEvent] = field(default_factory=list)
    ended_at: datetime | None = None

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)

    def to_json_dict(self) -> dict:
        return {
            "index": self.index,
            "started_at_local": format_local_ts(self.started_at),
            "ended_at_local": format_local_ts(self.ended_at) if self.ended_at else "",
            "parts": [p.to_json_dict() for p in self.parts],
            "gaps": [g.to_json_dict() for g in self.gaps],
        }
