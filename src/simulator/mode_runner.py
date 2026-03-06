from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from src.live.insight.models import KeywordConfig
from src.live.insight.openai_client import InsightModelResult, OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor
from src.simulator.cache_store import SimulationCacheStore, file_sha256, keywords_hash
from src.simulator.feed_scheduler import FeedScheduler
from src.simulator.models import (
    ALLOWED_MODE5_PROFILES,
    DEFAULT_MODE5_PROFILE,
    Scenario,
    SimulatorMode,
)


@dataclass
class ModeRunResult:
    mode: int
    output_dir: Path
    summary: dict


@dataclass(frozen=True)
class Mode5ChunkSample:
    chunk_seq: int
    chunk_file: str
    current_text: str
    context_text: str
    context_chunk_count: int


def run_mode(
    *,
    mode: SimulatorMode,
    scenario: Scenario,
    chunk_paths: list[Path],
    chunk_seconds: int,
    processor: InsightStageProcessor,
    cache_store: SimulationCacheStore,
    client: OpenAIInsightClient | None,
    keywords: KeywordConfig,
    stt_model: str,
    analysis_model: str,
    request_timeout_sec: float,
    precompute_workers: int,
    output_dir: Path,
    log_fn: Callable[[str], None] | None = None,
    seed_override: int | None = None,
    mode5_profile: str = DEFAULT_MODE5_PROFILE,
    mode5_target_seq: int | None = None,
) -> ModeRunResult:
    log = log_fn or print
    seed = seed_override if seed_override is not None else scenario.seed

    if mode == SimulatorMode.MODE1:
        summary = _run_mode1(
            scenario=scenario,
            chunk_paths=chunk_paths,
            chunk_seconds=chunk_seconds,
            processor=processor,
            seed=seed,
            log_fn=log,
        )
        return ModeRunResult(mode=int(mode), output_dir=output_dir, summary=summary)

    if mode == SimulatorMode.MODE2:
        summary = _run_mode2_or_3(
            mode=mode,
            scenario=scenario,
            chunk_paths=chunk_paths,
            chunk_seconds=chunk_seconds,
            processor=processor,
            cache_store=cache_store,
            keywords=keywords,
            stt_model=stt_model,
            analysis_model=analysis_model,
            seed=seed,
            log_fn=log,
        )
        return ModeRunResult(mode=int(mode), output_dir=output_dir, summary=summary)

    if mode == SimulatorMode.MODE3:
        summary = _run_mode2_or_3(
            mode=mode,
            scenario=scenario,
            chunk_paths=chunk_paths,
            chunk_seconds=chunk_seconds,
            processor=processor,
            cache_store=cache_store,
            keywords=keywords,
            stt_model=stt_model,
            analysis_model=analysis_model,
            seed=seed,
            log_fn=log,
        )
        return ModeRunResult(mode=int(mode), output_dir=output_dir, summary=summary)

    if mode == SimulatorMode.MODE4:
        if client is None:
            raise RuntimeError("mode4 requires OpenAI client")
        report = _run_mode4_benchmark(
            chunk_paths=chunk_paths,
            client=client,
            stt_model=stt_model,
            request_timeout_sec=request_timeout_sec,
            parallel_workers=max(1, int(scenario.benchmark.parallel_workers or precompute_workers)),
            repeats=max(1, int(scenario.benchmark.repeats)),
            log_fn=log,
        )
        path = output_dir / "benchmark_mode4.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return ModeRunResult(mode=int(mode), output_dir=output_dir, summary=report)

    if mode == SimulatorMode.MODE5:
        if client is None:
            raise RuntimeError("mode5 requires OpenAI client")
        report = _run_mode5_benchmark(
            chunk_paths=chunk_paths,
            cache_store=cache_store,
            client=client,
            keywords=keywords,
            stt_model=stt_model,
            analysis_model=analysis_model,
            chunk_seconds=chunk_seconds,
            request_timeout_sec=request_timeout_sec,
            parallel_workers=max(1, int(scenario.benchmark.parallel_workers or precompute_workers)),
            repeats=max(1, int(scenario.benchmark.repeats)),
            profile=mode5_profile,
            target_seq=mode5_target_seq,
            output_dir=output_dir,
            log_fn=log,
        )
        path = output_dir / "benchmark_mode5.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return ModeRunResult(mode=int(mode), output_dir=output_dir, summary=report)

    raise ValueError(f"unsupported simulator mode={int(mode)}")


def _run_mode1(
    *,
    scenario: Scenario,
    chunk_paths: list[Path],
    chunk_seconds: int,
    processor: InsightStageProcessor,
    seed: int | None,
    log_fn: Callable[[str], None],
) -> dict:
    scheduler = FeedScheduler(chunk_seconds=chunk_seconds, feed=scenario.feed, seed=seed)
    events = scheduler.build_events(chunk_paths)

    for out_seq, event in enumerate(events, start=1):
        if event.wait_before_sec > 0:
            time.sleep(event.wait_before_sec)
        processor.process_chunk(out_seq, event.chunk_path)

    log_fn(f"[simulate] mode1 finished: emitted={len(events)}")
    return {
        "mode": 1,
        "emitted_chunks": len(events),
        "feed_mode": scenario.feed.mode,
    }


def _run_mode2_or_3(
    *,
    mode: SimulatorMode,
    scenario: Scenario,
    chunk_paths: list[Path],
    chunk_seconds: int,
    processor: InsightStageProcessor,
    cache_store: SimulationCacheStore,
    keywords: KeywordConfig,
    stt_model: str,
    analysis_model: str,
    seed: int | None,
    log_fn: Callable[[str], None],
) -> dict:
    scheduler = FeedScheduler(chunk_seconds=chunk_seconds, feed=scenario.feed, seed=seed)
    events = scheduler.build_events(chunk_paths)

    keyword_hash = keywords_hash(keywords)
    chunk_sha_by_path = {path: file_sha256(path) for path in chunk_paths}

    elapsed_sec = 0.0
    active_history_mask = ""
    active_history_until = -1.0

    applied_translation_rules = 0
    applied_analysis_rules = 0

    for out_seq, event in enumerate(events, start=1):
        if event.wait_before_sec > 0:
            time.sleep(event.wait_before_sec)
            elapsed_sec += event.wait_before_sec

        source_seq = event.source_seq
        chunk_sha = chunk_sha_by_path[event.chunk_path]

        t_rule = scenario.translation_rule_for(source_seq)
        transcript_delay = 0.0
        transcript_status = "ok"
        transcript_error = ""
        transcript_attempt = 1
        forced_text = ""
        if t_rule is not None:
            applied_translation_rules += 1
            transcript_delay = max(0.0, float(t_rule.delay_sec))
            forced_text = (t_rule.forced_text or "").strip()
            normalized = t_rule.normalized_status()
            if normalized == "timeout":
                transcript_status = "transcript_drop_timeout"
                transcript_error = "simulated timeout"
            elif normalized in {"error", "drop"}:
                transcript_status = "transcript_drop_error"
                transcript_error = "simulated error"

        if transcript_delay > 0:
            time.sleep(transcript_delay)
            elapsed_sec += transcript_delay

        stt_key = cache_store.stt_key(
            chunk_sha256=chunk_sha,
            stt_model=stt_model,
            analysis_model=analysis_model,
            keywords_hash_value=keyword_hash,
            chunk_seconds=chunk_seconds,
        )
        cached_text = cache_store.load_stt(stt_key) or ""

        transcript_text = forced_text or cached_text
        if transcript_status == "ok" and not transcript_text:
            transcript_status = "transcript_drop_error"
            transcript_error = "stt cache missing for chunk"

        a_rule = scenario.analysis_rule_for(source_seq)
        analysis_delay = 0.0
        analysis_status = "ok"
        analysis_error = ""
        analysis_attempt = 1
        forced_result: dict | None = None
        if a_rule is not None:
            applied_analysis_rules += 1
            analysis_delay = max(0.0, float(a_rule.delay_sec))
            forced_result = a_rule.forced_result if a_rule.forced_result else None
            normalized = a_rule.normalized_status()
            if normalized == "timeout":
                analysis_status = "analysis_drop_timeout"
                analysis_error = "simulated timeout"
            elif normalized in {"error", "drop"}:
                analysis_status = "analysis_drop_error"
                analysis_error = "simulated error"

        if analysis_delay > 0:
            time.sleep(analysis_delay)
            elapsed_sec += analysis_delay

        analysis_payload: dict | None = None
        if transcript_status == "ok" and analysis_status == "ok":
            if forced_result is not None:
                analysis_payload = forced_result
            else:
                analysis_key = cache_store.analysis_key(
                    chunk_sha256=chunk_sha,
                    stt_model=stt_model,
                    analysis_model=analysis_model,
                    keywords_hash_value=keyword_hash,
                    chunk_seconds=chunk_seconds,
                )
                analysis_payload = cache_store.load_analysis(analysis_key)
                if analysis_payload is None:
                    analysis_status = "analysis_drop_error"
                    analysis_error = "analysis cache missing for chunk"

        history_mask: str | None = None
        if mode == SimulatorMode.MODE3:
            if scenario.mode3_variant == "controlled_history":
                rule = scenario.history_rule_for(source_seq)
                if rule is not None:
                    history_mask = rule.visibility
                    if rule.hold_sec > 0:
                        active_history_mask = rule.visibility
                        active_history_until = elapsed_sec + float(rule.hold_sec)
                    else:
                        active_history_mask = ""
                        active_history_until = -1.0
                elif active_history_mask and elapsed_sec <= active_history_until:
                    history_mask = active_history_mask
                else:
                    active_history_mask = ""
                    active_history_until = -1.0

        processor.process_simulated_chunk(
            chunk_seq=out_seq,
            chunk_path=event.chunk_path,
            transcript_text=transcript_text,
            transcript_status=transcript_status,
            transcript_error=transcript_error,
            transcript_attempt=transcript_attempt,
            analysis_result=analysis_payload,
            analysis_status=analysis_status,
            analysis_error=analysis_error,
            analysis_attempt=analysis_attempt,
            history_visibility_mask=history_mask,
        )

    log_fn(
        f"[simulate] mode{int(mode)} finished: emitted={len(events)} "
        f"translation_rules={applied_translation_rules} analysis_rules={applied_analysis_rules}"
    )
    return {
        "mode": int(mode),
        "emitted_chunks": len(events),
        "feed_mode": scenario.feed.mode,
        "translation_rules_applied": applied_translation_rules,
        "analysis_rules_applied": applied_analysis_rules,
        "mode3_variant": scenario.mode3_variant if mode == SimulatorMode.MODE3 else "",
    }


def _run_mode4_benchmark(
    *,
    chunk_paths: list[Path],
    client: OpenAIInsightClient,
    stt_model: str,
    request_timeout_sec: float,
    parallel_workers: int,
    repeats: int,
    log_fn: Callable[[str], None],
) -> dict:
    errors: list[str] = []
    transcript_by_chunk: dict[str, dict] = {}
    transcript_lock = threading.Lock()
    serial_samples = _benchmark_serial(
        tasks=[
            _wrap_task_with_error_capture(
                _build_mode4_task(
                    client=client,
                    chunk_path=chunk,
                    stt_model=stt_model,
                    timeout_sec=request_timeout_sec,
                    transcript_sink=transcript_by_chunk,
                    transcript_lock=transcript_lock,
                    source="serial",
                ),
                error_sink=errors,
            )
            for _ in range(repeats)
            for chunk in chunk_paths
        ]
    )

    parallel_samples = _benchmark_parallel(
        tasks=[
            _wrap_task_with_error_capture(
                _build_mode4_task(
                    client=client,
                    chunk_path=chunk,
                    stt_model=stt_model,
                    timeout_sec=request_timeout_sec,
                    transcript_sink=transcript_by_chunk,
                    transcript_lock=transcript_lock,
                    source="parallel",
                ),
                error_sink=errors,
            )
            for _ in range(repeats)
            for chunk in chunk_paths
        ],
        workers=parallel_workers,
    )

    report = {
        "mode": 4,
        "serial": _summarize_samples(serial_samples),
        "parallel": _summarize_samples(parallel_samples),
        "errors": _summarize_error_messages(errors),
        "transcript_samples": _build_mode4_transcript_samples(chunk_paths, transcript_by_chunk),
    }
    log_fn(
        "[simulate] mode4 benchmark "
        f"serial_avg={report['serial']['avg_sec']:.3f}s parallel_avg={report['parallel']['avg_sec']:.3f}s"
    )
    return report


def _run_mode5_benchmark(
    *,
    chunk_paths: list[Path],
    cache_store: SimulationCacheStore,
    client: OpenAIInsightClient,
    keywords: KeywordConfig,
    stt_model: str,
    analysis_model: str,
    chunk_seconds: int,
    request_timeout_sec: float,
    parallel_workers: int,
    repeats: int,
    profile: str,
    target_seq: int | None,
    output_dir: Path,
    log_fn: Callable[[str], None],
) -> dict:
    normalized_profile = _normalize_mode5_profile(profile)

    transcripts, transcript_prep = _prepare_mode5_transcripts(
        chunk_paths=chunk_paths,
        cache_store=cache_store,
        client=client,
        keywords=keywords,
        stt_model=stt_model,
        analysis_model=analysis_model,
        chunk_seconds=chunk_seconds,
        request_timeout_sec=request_timeout_sec,
    )
    all_samples = _build_mode5_samples(chunk_paths=chunk_paths, transcripts=transcripts)
    selected_samples = _select_mode5_samples(
        samples=all_samples,
        profile=normalized_profile,
        target_seq=target_seq,
    )
    serial_repeats = 1 if normalized_profile == "all_chunks_serial_once" else max(1, int(repeats))
    parallel_repeats = 0 if normalized_profile == "all_chunks_serial_once" else max(1, int(repeats))

    errors: list[str] = []
    analysis_samples: list[dict] = []
    analysis_sample_limit = 8
    chunk_results: list[dict] = []
    sample_lock = threading.Lock()
    chunk_result_lock = threading.Lock()
    trace_lock = threading.Lock()
    trace_path = output_dir / "mode5_analysis_trace.jsonl"
    trace_path.write_text("", encoding="utf-8")

    def trace_writer(payload: dict[str, Any]) -> None:
        encoded = json.dumps(_to_jsonable(payload), ensure_ascii=False)
        with trace_lock:
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.write("\n")

    serial_samples = _benchmark_serial(
        tasks=[
            _wrap_task_with_error_capture(
                _build_mode5_task(
                    client=client,
                    analysis_model=analysis_model,
                    keywords=keywords,
                    sample=sample,
                    timeout_sec=request_timeout_sec,
                    sample_sink=analysis_samples,
                    sample_limit=analysis_sample_limit,
                    sample_lock=sample_lock,
                    chunk_result_sink=chunk_results,
                    chunk_result_lock=chunk_result_lock,
                    trace_writer=trace_writer,
                    profile=normalized_profile,
                    source="serial",
                    repeat_index=repeat_idx,
                ),
                error_sink=errors,
            )
            for repeat_idx in range(1, serial_repeats + 1)
            for sample in selected_samples
        ]
    )

    parallel_tasks: list[Callable[[], object]] = []
    if parallel_repeats > 0:
        parallel_tasks = [
            _wrap_task_with_error_capture(
                _build_mode5_task(
                    client=client,
                    analysis_model=analysis_model,
                    keywords=keywords,
                    sample=sample,
                    timeout_sec=request_timeout_sec,
                    sample_sink=analysis_samples,
                    sample_limit=analysis_sample_limit,
                    sample_lock=sample_lock,
                    chunk_result_sink=chunk_results,
                    chunk_result_lock=chunk_result_lock,
                    trace_writer=trace_writer,
                    profile=normalized_profile,
                    source="parallel",
                    repeat_index=repeat_idx,
                ),
                error_sink=errors,
            )
            for repeat_idx in range(1, parallel_repeats + 1)
            for sample in selected_samples
        ]
    parallel_samples = _benchmark_parallel(
        tasks=parallel_tasks,
        workers=parallel_workers,
    )

    report = {
        "mode": 5,
        "profile": normalized_profile,
        "target_seq": int(target_seq) if target_seq is not None else None,
        "analysis_trace_file": trace_path.as_posix(),
        "all_chunk_count": len(all_samples),
        "selected_chunk_count": len(selected_samples),
        "repeats_configured": max(1, int(repeats)),
        "serial_repeats": serial_repeats,
        "parallel_repeats": parallel_repeats,
        "serial": _summarize_samples(serial_samples),
        "parallel": _summarize_samples(parallel_samples),
        "errors": _summarize_error_messages(errors),
        "transcript_prep": transcript_prep,
        "analysis_samples": analysis_samples,
        "chunk_results": _sort_mode5_chunk_results(chunk_results),
    }
    log_fn(
        "[simulate] mode5 benchmark "
        f"profile={normalized_profile} serial_avg={report['serial']['avg_sec']:.3f}s "
        f"parallel_avg={report['parallel']['avg_sec']:.3f}s"
    )
    return report


def _prepare_mode5_transcripts(
    *,
    chunk_paths: list[Path],
    cache_store: SimulationCacheStore,
    client: OpenAIInsightClient,
    keywords: KeywordConfig,
    stt_model: str,
    analysis_model: str,
    chunk_seconds: int,
    request_timeout_sec: float,
) -> tuple[list[dict[str, Any]], dict]:
    keyword_hash = keywords_hash(keywords)
    timeout_sec = max(1.0, float(request_timeout_sec))
    transcripts: list[dict[str, Any]] = []
    prep = {
        "chunk_count": len(chunk_paths),
        "cache_hits": 0,
        "cache_misses": 0,
        "api_calls": 0,
    }

    for seq, chunk_path in enumerate(chunk_paths, start=1):
        chunk_sha = file_sha256(chunk_path)
        key = cache_store.stt_key(
            chunk_sha256=chunk_sha,
            stt_model=stt_model,
            analysis_model=analysis_model,
            keywords_hash_value=keyword_hash,
            chunk_seconds=chunk_seconds,
        )
        cached_text = cache_store.load_stt(key)
        if cached_text:
            prep["cache_hits"] += 1
            transcripts.append(
                {
                    "chunk_seq": seq,
                    "chunk_file": chunk_path.name,
                    "text": cached_text,
                }
            )
            continue

        prep["cache_misses"] += 1
        prep["api_calls"] += 1
        try:
            transcript = client.transcribe_chunk(
                chunk_path=chunk_path,
                stt_model=stt_model,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            raise RuntimeError(
                f"mode5 transcript prepare failed seq={seq} chunk={chunk_path.name}: {exc}"
            ) from exc

        text = (transcript or "").strip()
        if not text:
            raise RuntimeError(
                f"mode5 transcript prepare failed seq={seq} chunk={chunk_path.name}: empty transcript"
            )
        cache_store.store_stt(key, text=text)
        transcripts.append(
            {
                "chunk_seq": seq,
                "chunk_file": chunk_path.name,
                "text": text,
            }
        )

    return transcripts, prep


def _build_mode5_samples(
    *,
    chunk_paths: list[Path],
    transcripts: list[dict[str, Any]],
) -> list[Mode5ChunkSample]:
    samples: list[Mode5ChunkSample] = []
    history: list[str] = []
    for idx, transcript in enumerate(transcripts, start=1):
        seq = int(transcript.get("chunk_seq", idx))
        chunk_file = str(transcript.get("chunk_file", "")).strip()
        if not chunk_file and idx - 1 < len(chunk_paths):
            chunk_file = chunk_paths[idx - 1].name
        text = str(transcript.get("text", "")).strip()
        if not text:
            raise RuntimeError(f"mode5 transcript prepare produced empty transcript seq={seq}")
        history_lines = history[-18:]
        context = "\n".join(history_lines) if history_lines else "无历史文本块"
        samples.append(
            Mode5ChunkSample(
                chunk_seq=seq,
                chunk_file=chunk_file,
                current_text=text,
                context_text=context,
                context_chunk_count=len(history_lines),
            )
        )
        history.append(f"[seq={seq}] {text}")
    return samples


def _select_mode5_samples(
    *,
    samples: list[Mode5ChunkSample],
    profile: str,
    target_seq: int | None,
) -> list[Mode5ChunkSample]:
    if profile != "single_chunk_dual":
        return samples
    if target_seq is None:
        raise RuntimeError("mode5 target seq is required for single_chunk_dual profile")
    for sample in samples:
        if sample.chunk_seq == int(target_seq):
            return [sample]
    raise RuntimeError(f"mode5 target seq out of range: {target_seq} (available chunks={len(samples)})")


def _normalize_mode5_profile(profile: str) -> str:
    normalized = (profile or DEFAULT_MODE5_PROFILE).strip()
    if normalized not in ALLOWED_MODE5_PROFILES:
        raise RuntimeError(f"unsupported mode5 profile: {normalized}")
    return normalized


def _benchmark_serial(tasks: list[Callable[[], object]]) -> list[tuple[bool, float]]:
    out: list[tuple[bool, float]] = []
    for task in tasks:
        started = time.monotonic()
        ok = True
        try:
            _ = task()
        except Exception:
            ok = False
        elapsed = time.monotonic() - started
        out.append((ok, elapsed))
    return out


def _benchmark_parallel(tasks: list[Callable[[], object]], workers: int) -> list[tuple[bool, float]]:
    out: list[tuple[bool, float]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix="sim-bench") as executor:
        future_map = {executor.submit(_timed_call, task): task for task in tasks}
        for future in as_completed(future_map):
            try:
                out.append(future.result())
            except Exception:
                out.append((False, 0.0))
    return out


def _timed_call(task: Callable[[], object]) -> tuple[bool, float]:
    started = time.monotonic()
    ok = True
    try:
        _ = task()
    except Exception:
        ok = False
    return ok, time.monotonic() - started


def _summarize_samples(samples: list[tuple[bool, float]]) -> dict:
    durations = [elapsed for ok, elapsed in samples if ok]
    failures = sum(1 for ok, _ in samples if not ok)
    successes = len(durations)

    if not durations:
        return {
            "count": len(samples),
            "success": successes,
            "fail": failures,
            "avg_sec": 0.0,
            "p95_sec": 0.0,
            "max_sec": 0.0,
            "min_sec": 0.0,
        }

    ordered = sorted(durations)
    p95_index = int(math.ceil(0.95 * len(ordered))) - 1
    p95_index = max(0, min(p95_index, len(ordered) - 1))
    return {
        "count": len(samples),
        "success": successes,
        "fail": failures,
        "avg_sec": float(mean(durations)),
        "p95_sec": float(ordered[p95_index]),
        "max_sec": float(max(durations)),
        "min_sec": float(min(durations)),
    }


def _wrap_task_with_error_capture(
    task: Callable[[], object],
    *,
    error_sink: list[str],
) -> Callable[[], object]:
    def wrapped() -> object:
        try:
            return task()
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc).strip()}"
            error_sink.append(message[:600])
            raise

    return wrapped


def _summarize_error_messages(messages: list[str], limit: int = 5) -> dict:
    if not messages:
        return {"count": 0, "unique_count": 0, "samples": []}

    counts: dict[str, int] = {}
    for message in messages:
        counts[message] = counts.get(message, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    samples = [{"message": message, "count": count} for message, count in ranked[: max(1, int(limit))]]
    return {
        "count": len(messages),
        "unique_count": len(counts),
        "samples": samples,
    }


def _build_mode4_task(
    *,
    client: OpenAIInsightClient,
    chunk_path: Path,
    stt_model: str,
    timeout_sec: float,
    transcript_sink: dict[str, dict],
    transcript_lock: threading.Lock,
    source: str,
) -> Callable[[], str]:
    def task() -> str:
        transcript = client.transcribe_chunk(
            chunk_path=chunk_path,
            stt_model=stt_model,
            timeout_sec=timeout_sec,
        )
        payload = {
            "chunk_file": chunk_path.name,
            "text": (transcript or "").strip(),
            "source": source,
        }
        if payload["text"]:
            with transcript_lock:
                transcript_sink.setdefault(chunk_path.name, payload)
        return transcript

    return task


def _build_mode4_transcript_samples(
    chunk_paths: list[Path],
    transcript_by_chunk: dict[str, dict],
) -> list[dict]:
    out: list[dict] = []
    for chunk_path in chunk_paths:
        payload = transcript_by_chunk.get(chunk_path.name)
        if payload:
            out.append(payload)
    return out


def _build_mode5_task(
    *,
    client: OpenAIInsightClient,
    analysis_model: str,
    keywords: KeywordConfig,
    sample: Mode5ChunkSample,
    timeout_sec: float,
    sample_sink: list[dict],
    sample_limit: int,
    sample_lock: threading.Lock,
    chunk_result_sink: list[dict],
    chunk_result_lock: threading.Lock,
    trace_writer: Callable[[dict[str, Any]], None],
    profile: str,
    source: str,
    repeat_index: int,
) -> Callable[[], InsightModelResult]:
    def trace_hook(trace_payload: dict[str, Any]) -> None:
        trace_writer(
            {
                "profile": profile,
                "source": source,
                "repeat": repeat_index,
                "chunk_seq": sample.chunk_seq,
                "chunk_file": sample.chunk_file,
                "context_chunk_count": sample.context_chunk_count,
                "system_prompt": trace_payload.get("system_prompt", ""),
                "user_prompt": trace_payload.get("user_prompt", ""),
                "request_payload_snapshot": trace_payload.get("request_payload_snapshot", {}),
                "raw_response_text": trace_payload.get("raw_response_text", ""),
                "parsed_ok": bool(trace_payload.get("parsed_ok", False)),
                "parsed_payload": trace_payload.get("parsed_payload", {}),
                "error": str(trace_payload.get("error", "")).strip(),
                "duration_sec": float(trace_payload.get("duration_sec", 0.0)),
            }
        )

    def task() -> InsightModelResult:
        try:
            result = _call_mode5_analyze_text(
                client=client,
                analysis_model=analysis_model,
                keywords=keywords,
                sample=sample,
                timeout_sec=timeout_sec,
                trace_hook=trace_hook,
            )
        except Exception as exc:
            _capture_mode5_chunk_result(
                chunk_result_sink=chunk_result_sink,
                chunk_result_lock=chunk_result_lock,
                sample=sample,
                source=source,
                repeat_index=repeat_index,
                result=None,
                error=str(exc).strip(),
            )
            raise

        _capture_mode5_result_sample(
            sample_sink=sample_sink,
            sample_limit=sample_limit,
            sample_lock=sample_lock,
            source=source,
            current_text=sample.current_text,
            context_text=sample.context_text,
            result=result,
        )
        _capture_mode5_chunk_result(
            chunk_result_sink=chunk_result_sink,
            chunk_result_lock=chunk_result_lock,
            sample=sample,
            source=source,
            repeat_index=repeat_index,
            result=result,
            error="",
        )
        return result

    return task


def _call_mode5_analyze_text(
    *,
    client: OpenAIInsightClient,
    analysis_model: str,
    keywords: KeywordConfig,
    sample: Mode5ChunkSample,
    timeout_sec: float,
    trace_hook: Callable[[dict[str, Any]], None],
) -> InsightModelResult:
    try:
        return client.analyze_text(
            analysis_model=analysis_model,
            keywords=keywords,
            current_text=sample.current_text,
            context_text=sample.context_text,
            timeout_sec=timeout_sec,
            debug_hook=trace_hook,
        )
    except TypeError as exc:
        # Backward-compat for fake clients used in unit tests.
        if "debug_hook" not in str(exc):
            raise
        return client.analyze_text(
            analysis_model=analysis_model,
            keywords=keywords,
            current_text=sample.current_text,
            context_text=sample.context_text,
            timeout_sec=timeout_sec,
        )


def _capture_mode5_chunk_result(
    *,
    chunk_result_sink: list[dict],
    chunk_result_lock: threading.Lock,
    sample: Mode5ChunkSample,
    source: str,
    repeat_index: int,
    result: InsightModelResult | None,
    error: str,
) -> None:
    payload = {
        "chunk_seq": sample.chunk_seq,
        "chunk_file": sample.chunk_file,
        "current_text": sample.current_text,
        "context_text": sample.context_text,
        "context_chunk_count": sample.context_chunk_count,
        "important": bool(result.important) if result is not None else False,
        "summary": (result.summary or "").strip() if result is not None else "",
        "context_summary": (result.context_summary or "").strip() if result is not None else "",
        "matched_terms": list(result.matched_terms or []) if result is not None else [],
        "reason": (result.reason or "").strip() if result is not None else "",
        "success": result is not None and not error,
        "error": error.strip(),
        "source": source,
        "repeat": repeat_index,
    }
    with chunk_result_lock:
        chunk_result_sink.append(payload)


def _capture_mode5_result_sample(
    *,
    sample_sink: list[dict],
    sample_limit: int,
    sample_lock: threading.Lock,
    source: str,
    current_text: str,
    context_text: str,
    result: InsightModelResult,
) -> None:
    payload = {
        "source": source,
        "current_text": _truncate_text(current_text, 160),
        "context_preview": _truncate_text(context_text, 200),
        "important": bool(result.important),
        "summary": (result.summary or "").strip(),
        "context_summary": (result.context_summary or "").strip(),
        "matched_terms": list(result.matched_terms or []),
        "reason": (result.reason or "").strip(),
    }
    with sample_lock:
        if len(sample_sink) >= max(1, int(sample_limit)):
            return
        sample_sink.append(payload)


def _sort_mode5_chunk_results(results: list[dict]) -> list[dict]:
    source_rank = {"serial": 0, "parallel": 1}
    return sorted(
        results,
        key=lambda item: (
            int(item.get("repeat", 0)),
            source_rank.get(str(item.get("source", "")), 9),
            int(item.get("chunk_seq", 0)),
        ),
    )


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return str(value)


def _truncate_text(text: str, max_len: int) -> str:
    raw = (text or "").strip()
    limit = max(8, int(max_len))
    if len(raw) <= limit:
        return raw
    return f"{raw[:limit]}..."
