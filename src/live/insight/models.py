from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def format_local_ts(value: datetime) -> str:
    return value.astimezone().strftime("%Y%m%d_%H%M%S")


@dataclass
class KeywordGroup:
    id: str = ""
    label: str = ""
    aliases: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    detail_cues: list[str] = field(default_factory=list)

    @classmethod
    def from_json_dict(cls, payload: dict) -> "KeywordGroup":
        if not isinstance(payload, dict):
            return cls()
        group_id = str(payload.get("id", "")).strip()
        label = str(payload.get("label", "")).strip() or group_id
        return cls(
            id=group_id,
            label=label,
            aliases=_coerce_str_list(payload.get("aliases")),
            phrases=_coerce_str_list(payload.get("phrases")),
            detail_cues=_coerce_str_list(payload.get("detail_cues")),
        )

    def to_json_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "aliases": self.aliases,
            "phrases": self.phrases,
            "detail_cues": self.detail_cues,
        }


@dataclass
class KeywordConfig:
    version: int = 1
    important_terms: list[str] = field(default_factory=list)
    important_phrases: list[str] = field(default_factory=list)
    negative_terms: list[str] = field(default_factory=list)
    global_negative_terms: list[str] = field(default_factory=list)
    groups: list[KeywordGroup] = field(default_factory=list)

    @classmethod
    def from_json_dict(cls, payload: dict) -> "KeywordConfig":
        groups = _coerce_keyword_groups(payload.get("groups"))
        negative_terms = _coerce_str_list(payload.get("negative_terms"))
        global_negative_terms = _coerce_str_list(payload.get("global_negative_terms"))
        if groups and not global_negative_terms and negative_terms:
            global_negative_terms = list(negative_terms)
        return cls(
            version=int(payload.get("version", 1)),
            important_terms=_coerce_str_list(payload.get("important_terms")),
            important_phrases=_coerce_str_list(payload.get("important_phrases")),
            negative_terms=negative_terms,
            global_negative_terms=global_negative_terms,
            groups=groups,
        )

    def prompt_text(self) -> str:
        return json.dumps(self.prompt_payload(), ensure_ascii=False, indent=2)

    def prompt_payload(self) -> dict:
        if self.has_grouped_rules:
            return {
                "rule_style": "grouped",
                "global_negative_terms": self.effective_negative_terms(),
                "groups": [group.to_json_dict() for group in self.groups],
            }
        return {
            "rule_style": "legacy",
            "important_terms": self.important_terms,
            "important_phrases": self.important_phrases,
            "negative_terms": self.effective_negative_terms(),
        }

    def to_json_dict(self) -> dict:
        payload = {
            "version": self.version,
            "important_terms": self.important_terms,
            "important_phrases": self.important_phrases,
            "negative_terms": self.negative_terms,
        }
        if self.has_grouped_rules or self.global_negative_terms or self.version >= 2:
            payload["global_negative_terms"] = self.effective_negative_terms()
            payload["groups"] = [group.to_json_dict() for group in self.groups]
        return payload

    @property
    def has_grouped_rules(self) -> bool:
        return any(group.id for group in self.groups)

    def effective_negative_terms(self) -> list[str]:
        return list(self.global_negative_terms or self.negative_terms)


def _coerce_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _coerce_keyword_groups(value: object) -> list[KeywordGroup]:
    if not isinstance(value, list):
        return []
    groups: list[KeywordGroup] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        group = KeywordGroup.from_json_dict(item)
        if group.id:
            groups.append(group)
    return groups


@dataclass
class RealtimeInsightConfig:
    enabled: bool = False
    audio_source_mode: str = "teacher_stream"
    pipeline_mode: str = "chunk"
    chunk_seconds: float = 10.0
    context_window_seconds: int = 180  # legacy option; default maps to 18 chunks with 10s chunk
    model: str = "gpt-4.1-mini"
    stt_model: str = "whisper-large-v3"
    asr_scene: str = "zh"
    asr_model: str = ""
    hotwords_file: Path = field(default_factory=lambda: Path("config/realtime_hotwords.json"))
    window_sentences: int = 8
    stream_analysis_workers: int = 32
    stream_queue_size: int = 100
    asr_endpoint: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    asr_api_key_env: str = "DASHSCOPE_API_KEY"
    translation_target_languages: list[str] = field(default_factory=lambda: ["zh"])
    keywords_file: Path = field(default_factory=lambda: Path("config/realtime_keywords.json"))
    stt_request_timeout_sec: float = 8.0
    stt_stage_timeout_sec: float = 32.0
    stt_retry_count: int = 4
    stt_retry_interval_sec: float = 0.2
    analysis_request_timeout_sec: float = 20.0
    analysis_stage_timeout_sec: float = 60.0
    analysis_retry_count: int = 3
    analysis_retry_interval_sec: float = 0.2
    alert_threshold: int = 90
    poll_interval_sec: float = 1.0
    api_key_env: str = "OPENAI_API_KEY"
    base_url_env: str = "OPENAI_BASE_URL"
    api_base_url: str = ""
    max_concurrency: int = 5
    context_min_ready: int = 15
    context_recent_required: int = 4
    context_wait_timeout_sec: float = 5.0
    context_wait_timeout_sec_1: float = 1.0
    context_wait_timeout_sec_2: float = 5.0
    context_check_interval_sec: float = 0.2
    use_dual_context_wait: bool = True
    context_target_chunks: int = 18
    mic_upload_token: str = ""
    mic_chunk_max_bytes: int = 10 * 1024 * 1024
    mic_chunk_dir: Path = field(default_factory=lambda: Path("_rt_chunks_mic"))
    profile_enabled: bool = False
    dingtalk_enabled: bool = False
    dingtalk_cooldown_sec: float = 30.0
    dingtalk_queue_size: int = 500
    dingtalk_send_timeout_sec: float = 5.0
    dingtalk_send_retry_count: int = 5
    log_rotate_max_bytes: int = 64 * 1024 * 1024
    log_rotate_backup_count: int = 20


@dataclass
class TranscriptChunk:
    chunk_seq: int
    chunk_file: str
    ts_local: str
    text: str
    status: str = "ok"
    error: str = ""
    attempt_count: int = 0
    elapsed_sec: float = 0.0
    asr_global_seq: int = 0
    asr_sentence_id: str = ""
    asr_start_ms: int | None = None
    asr_end_ms: int | None = None
    translation_text: str = ""
    event_type: str = ""

    @classmethod
    def from_json_dict(cls, payload: dict) -> "TranscriptChunk":
        start_ms = payload.get("asr_start_ms", None)
        end_ms = payload.get("asr_end_ms", None)
        return cls(
            chunk_seq=int(payload.get("chunk_seq", 0)),
            chunk_file=str(payload.get("chunk_file", "")).strip(),
            ts_local=str(payload.get("ts_local", "")).strip(),
            text=str(payload.get("text", "")).strip(),
            status=str(payload.get("status", "ok")).strip() or "ok",
            error=str(payload.get("error", "")).strip(),
            attempt_count=int(payload.get("attempt_count", 0) or 0),
            elapsed_sec=float(payload.get("elapsed_sec", 0.0) or 0.0),
            asr_global_seq=int(payload.get("asr_global_seq", 0) or 0),
            asr_sentence_id=str(payload.get("asr_sentence_id", "")).strip(),
            asr_start_ms=int(start_ms) if isinstance(start_ms, int) or (isinstance(start_ms, str) and start_ms.isdigit()) else None,
            asr_end_ms=int(end_ms) if isinstance(end_ms, int) or (isinstance(end_ms, str) and end_ms.isdigit()) else None,
            translation_text=str(payload.get("translation_text", "")).strip(),
            event_type=str(payload.get("event_type", "")).strip(),
        )

    def to_json_dict(self) -> dict:
        payload = {
            "chunk_seq": self.chunk_seq,
            "chunk_file": self.chunk_file,
            "ts_local": self.ts_local,
            "text": self.text,
            "status": self.status,
            "error": self.error,
            "attempt_count": self.attempt_count,
            "elapsed_sec": self.elapsed_sec,
        }
        if self.asr_global_seq > 0:
            payload["asr_global_seq"] = int(self.asr_global_seq)
        if self.asr_sentence_id:
            payload["asr_sentence_id"] = self.asr_sentence_id
        if self.asr_start_ms is not None:
            payload["asr_start_ms"] = int(self.asr_start_ms)
        if self.asr_end_ms is not None:
            payload["asr_end_ms"] = int(self.asr_end_ms)
        if self.translation_text:
            payload["translation_text"] = self.translation_text
        if self.event_type:
            payload["event_type"] = self.event_type
        return payload


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
    event_type: str = ""
    headline: str = ""
    immediate_action: str = ""
    key_details: list[str] = field(default_factory=list)
    is_recovery: bool = False
    status: str = "ok"
    error: str = ""
    analysis_elapsed_sec: float = 0.0
    context_reason: str = ""
    context_missing_ranges: list[str] = field(default_factory=list)
    asr_global_seq: int = 0
    asr_sentence_id: str = ""
    asr_start_ms: int | None = None
    asr_end_ms: int | None = None
    target_text: str = ""
    context_text: str = ""

    @property
    def urgency_percent(self) -> int:
        return 95 if self.important else 10

    @property
    def text_log_level(self) -> str:
        return "紧急!" if self.important else "平常"

    def to_json_dict(self) -> dict:
        payload = {
            "ts_local": format_local_ts(self.ts),
            "chunk_seq": self.chunk_seq,
            "chunk_file": self.chunk_file,
            "model": self.model,
            "important": self.important,
            "urgency_percent": self.urgency_percent,
            "summary": self.summary,
            "context_summary": self.context_summary,
            "event_type": self.event_type,
            "headline": self.headline,
            "immediate_action": self.immediate_action,
            "key_details": self.key_details,
            "matched_terms": self.matched_terms,
            "reason": self.reason,
            "attempt_count": self.attempt_count,
            "context_chunk_count": self.context_chunk_count,
            "is_recovery": self.is_recovery,
            "status": self.status,
            "error": self.error,
            "analysis_elapsed_sec": self.analysis_elapsed_sec,
            "context_reason": self.context_reason,
            "context_missing_ranges": self.context_missing_ranges,
        }
        if self.asr_global_seq > 0:
            payload["asr_global_seq"] = int(self.asr_global_seq)
        if self.asr_sentence_id:
            payload["asr_sentence_id"] = self.asr_sentence_id
        if self.asr_start_ms is not None:
            payload["asr_start_ms"] = int(self.asr_start_ms)
        if self.asr_end_ms is not None:
            payload["asr_end_ms"] = int(self.asr_end_ms)
        if self.target_text:
            payload["target_text"] = self.target_text
        if self.context_text:
            payload["context_text"] = self.context_text
        return payload
