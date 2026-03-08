from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.live.insight.models import KeywordConfig

PROMPT_VERSION = "v3"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def keywords_hash(keywords: KeywordConfig) -> str:
    payload = keywords.to_json_dict()
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SimulationCacheStore:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.stt_dir = self.root_dir / "stt"
        self.analysis_dir = self.root_dir / "analysis"
        self.stt_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def build_cache_key(
        *,
        chunk_sha256: str,
        stage: str,
        stt_model: str,
        analysis_model: str,
        keywords_hash_value: str,
        chunk_seconds: int,
        prompt_version: str = PROMPT_VERSION,
    ) -> str:
        payload = {
            "chunk_sha256": chunk_sha256,
            "stage": stage,
            "stt_model": stt_model,
            "analysis_model": analysis_model,
            "keywords_hash": keywords_hash_value,
            "prompt_version": prompt_version,
            "chunk_seconds": int(chunk_seconds),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def stt_key(
        self,
        *,
        chunk_sha256: str,
        stt_model: str,
        analysis_model: str,
        keywords_hash_value: str,
        chunk_seconds: int,
    ) -> str:
        return self.build_cache_key(
            chunk_sha256=chunk_sha256,
            stage="stt",
            stt_model=stt_model,
            analysis_model=analysis_model,
            keywords_hash_value=keywords_hash_value,
            chunk_seconds=chunk_seconds,
        )

    def analysis_key(
        self,
        *,
        chunk_sha256: str,
        stt_model: str,
        analysis_model: str,
        keywords_hash_value: str,
        chunk_seconds: int,
    ) -> str:
        return self.build_cache_key(
            chunk_sha256=chunk_sha256,
            stage="analysis",
            stt_model=stt_model,
            analysis_model=analysis_model,
            keywords_hash_value=keywords_hash_value,
            chunk_seconds=chunk_seconds,
        )

    def load_stt(self, key: str) -> str | None:
        payload = self._load_json(self.stt_dir / f"{key}.json")
        if not isinstance(payload, dict):
            return None
        text = str(payload.get("text", "")).strip()
        return text or None

    def store_stt(self, key: str, *, text: str, meta: dict[str, Any] | None = None) -> None:
        payload = {
            "text": text,
            "meta": meta or {},
        }
        self._write_json(self.stt_dir / f"{key}.json", payload)

    def load_analysis(self, key: str) -> dict | None:
        payload = self._load_json(self.analysis_dir / f"{key}.json")
        if isinstance(payload, dict):
            return payload
        return None

    def store_analysis(self, key: str, payload: dict[str, Any]) -> None:
        self._write_json(self.analysis_dir / f"{key}.json", payload)

    @staticmethod
    def _load_json(path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict):
            return data
        return None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
