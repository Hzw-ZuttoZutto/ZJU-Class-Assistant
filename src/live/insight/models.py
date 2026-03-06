from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def format_local_ts(value: datetime) -> str:
    return value.astimezone().strftime("%Y%m%d_%H%M%S")


@dataclass
class KeywordConfig:
    version: int = 1
    important_terms: list[str] = field(default_factory=list)
    important_phrases: list[str] = field(default_factory=list)
    negative_terms: list[str] = field(default_factory=list)

    @classmethod
    def from_json_dict(cls, payload: dict) -> "KeywordConfig":
        return cls(
            version=int(payload.get("version", 1)),
            important_terms=_coerce_str_list(payload.get("important_terms")),
            important_phrases=_coerce_str_list(payload.get("important_phrases")),
            negative_terms=_coerce_str_list(payload.get("negative_terms")),
        )

    def prompt_text(self) -> str:
        return (
            f"important_terms={self.important_terms}\n"
            f"important_phrases={self.important_phrases}\n"
            f"negative_terms={self.negative_terms}"
        )


def _coerce_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


@dataclass
class RealtimeInsightConfig:
    enabled: bool = False
    chunk_seconds: int = 10
    context_window_seconds: int = 180  # legacy option; default maps to 18 chunks with 10s chunk
    model: str = "gpt-5-mini"
    stt_model: str = "gpt-4o-mini-transcribe"
    keywords_file: Path = field(default_factory=lambda: Path("config/realtime_keywords.json"))
    request_timeout_sec: float = 12.0
    retry_count: int = 2
    alert_threshold: int = 90
    poll_interval_sec: float = 1.0
    api_key_env: str = "OPENAI_API_KEY"
    base_url_env: str = "OPENAI_BASE_URL"
    api_base_url: str = ""
    max_concurrency: int = 5
    stage_timeout_sec: float = 60.0
    context_min_ready: int = 15
    context_recent_required: int = 4
    context_wait_timeout_sec: float = 15.0
    context_wait_timeout_sec_1: float = 1.0
    context_wait_timeout_sec_2: float = 5.0
    context_check_interval_sec: float = 0.2
    use_dual_context_wait: bool = False
    context_target_chunks: int = 18


@dataclass
class TranscriptChunk:
    chunk_seq: int
    chunk_file: str
    ts_local: str
    text: str
    status: str = "ok"
    error: str = ""

    @classmethod
    def from_json_dict(cls, payload: dict) -> "TranscriptChunk":
        return cls(
            chunk_seq=int(payload.get("chunk_seq", 0)),
            chunk_file=str(payload.get("chunk_file", "")).strip(),
            ts_local=str(payload.get("ts_local", "")).strip(),
            text=str(payload.get("text", "")).strip(),
            status=str(payload.get("status", "ok")).strip() or "ok",
            error=str(payload.get("error", "")).strip(),
        )

    def to_json_dict(self) -> dict:
        return {
            "chunk_seq": self.chunk_seq,
            "chunk_file": self.chunk_file,
            "ts_local": self.ts_local,
            "text": self.text,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class InsightEvent:
    ts: datetime
    chunk_seq: int
    chunk_file: str
    model: str
    important: bool
    summary: str
    context_summary: str
    matched_terms: list[str]
    reason: str
    attempt_count: int
    context_chunk_count: int
    is_recovery: bool = False
    status: str = "ok"
    error: str = ""

    @property
    def urgency_percent(self) -> int:
        return 95 if self.important else 10

    def to_json_dict(self) -> dict:
        return {
            "ts_local": format_local_ts(self.ts),
            "chunk_seq": self.chunk_seq,
            "chunk_file": self.chunk_file,
            "model": self.model,
            "important": self.important,
            "urgency_percent": self.urgency_percent,
            "summary": self.summary,
            "context_summary": self.context_summary,
            "matched_terms": self.matched_terms,
            "reason": self.reason,
            "attempt_count": self.attempt_count,
            "context_chunk_count": self.context_chunk_count,
            "is_recovery": self.is_recovery,
            "status": self.status,
            "error": self.error,
        }
