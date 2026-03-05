from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.live.insight.models import KeywordConfig, format_local_ts
from src.live.insight.openai_client import OpenAIInsightClient
from src.simulator.cache_store import SimulationCacheStore, file_sha256, keywords_hash


def run_precompute(
    *,
    chunk_paths: list[Path],
    cache_store: SimulationCacheStore,
    client: OpenAIInsightClient,
    keywords: KeywordConfig,
    stt_model: str,
    analysis_model: str,
    chunk_seconds: int,
    request_timeout_sec: float,
    workers: int,
    log_fn: Callable[[str], None] | None = None,
) -> dict:
    log = log_fn or print
    workers = max(1, int(workers))
    keyword_hash = keywords_hash(keywords)

    manifest = {
        "generated_at_local": format_local_ts(datetime.now().astimezone()),
        "chunk_count": len(chunk_paths),
        "stt": {
            "hits": 0,
            "misses": 0,
            "computed": 0,
            "failures": 0,
        },
        "analysis": {
            "hits": 0,
            "misses": 0,
            "computed": 0,
            "failures": 0,
        },
        "failures": [],
    }

    sha_by_chunk = {path: file_sha256(path) for path in chunk_paths}
    stt_text_by_chunk: dict[Path, str] = {}

    stt_futures: dict[Future[tuple[Path, str]], Path] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sim-precompute-stt") as executor:
        for chunk in chunk_paths:
            chunk_sha = sha_by_chunk[chunk]
            key = cache_store.stt_key(
                chunk_sha256=chunk_sha,
                stt_model=stt_model,
                analysis_model=analysis_model,
                keywords_hash_value=keyword_hash,
                chunk_seconds=chunk_seconds,
            )
            cached = cache_store.load_stt(key)
            if cached:
                manifest["stt"]["hits"] += 1
                stt_text_by_chunk[chunk] = cached
                continue

            manifest["stt"]["misses"] += 1
            stt_futures[
                executor.submit(_compute_stt, client, chunk, stt_model, request_timeout_sec)
            ] = chunk

        for future in as_completed(stt_futures):
            chunk = stt_futures[future]
            try:
                path, text = future.result()
                chunk_sha = sha_by_chunk[path]
                key = cache_store.stt_key(
                    chunk_sha256=chunk_sha,
                    stt_model=stt_model,
                    analysis_model=analysis_model,
                    keywords_hash_value=keyword_hash,
                    chunk_seconds=chunk_seconds,
                )
                cache_store.store_stt(key, text=text)
                stt_text_by_chunk[path] = text
                manifest["stt"]["computed"] += 1
            except Exception as exc:
                manifest["stt"]["failures"] += 1
                manifest["failures"].append(
                    {
                        "stage": "stt",
                        "chunk_file": chunk.name,
                        "error": str(exc),
                    }
                )

    history_lines: list[str] = []
    for seq, chunk in enumerate(chunk_paths, start=1):
        chunk_sha = sha_by_chunk[chunk]
        key = cache_store.analysis_key(
            chunk_sha256=chunk_sha,
            stt_model=stt_model,
            analysis_model=analysis_model,
            keywords_hash_value=keyword_hash,
            chunk_seconds=chunk_seconds,
        )
        cached = cache_store.load_analysis(key)
        if isinstance(cached, dict):
            manifest["analysis"]["hits"] += 1
            text = stt_text_by_chunk.get(chunk, "").strip()
            if text:
                history_lines.append(f"[seq={seq}] {text}")
            continue

        manifest["analysis"]["misses"] += 1
        text = stt_text_by_chunk.get(chunk, "").strip()
        if not text:
            manifest["analysis"]["failures"] += 1
            manifest["failures"].append(
                {
                    "stage": "analysis",
                    "chunk_file": chunk.name,
                    "error": "missing transcript text",
                }
            )
            continue

        context_text = "\n".join(history_lines) if history_lines else "无历史文本块"
        try:
            result = client.analyze_text(
                analysis_model=analysis_model,
                keywords=keywords,
                current_text=text,
                context_text=context_text,
                timeout_sec=max(1.0, float(request_timeout_sec)),
            )
            cache_store.store_analysis(
                key,
                {
                    "important": bool(result.important),
                    "summary": result.summary,
                    "context_summary": result.context_summary,
                    "matched_terms": result.matched_terms,
                    "reason": result.reason,
                },
            )
            manifest["analysis"]["computed"] += 1
        except Exception as exc:
            manifest["analysis"]["failures"] += 1
            manifest["failures"].append(
                {
                    "stage": "analysis",
                    "chunk_file": chunk.name,
                    "error": str(exc),
                }
            )

        history_lines.append(f"[seq={seq}] {text}")

    log(
        "[simulate] precompute summary "
        f"stt(hit={manifest['stt']['hits']}, miss={manifest['stt']['misses']}, fail={manifest['stt']['failures']}) "
        f"analysis(hit={manifest['analysis']['hits']}, miss={manifest['analysis']['misses']}, fail={manifest['analysis']['failures']})"
    )
    return manifest


def write_precompute_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _compute_stt(
    client: OpenAIInsightClient,
    chunk_path: Path,
    stt_model: str,
    request_timeout_sec: float,
) -> tuple[Path, str]:
    started = time.monotonic()
    text = client.transcribe_chunk(
        chunk_path=chunk_path,
        stt_model=stt_model,
        timeout_sec=max(1.0, float(request_timeout_sec)),
    )
    _ = time.monotonic() - started
    return chunk_path, text.strip()
