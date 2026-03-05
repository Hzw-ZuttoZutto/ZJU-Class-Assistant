from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Callable

from src.live.insight.models import KeywordConfig
from src.live.insight.openai_client import InsightModelResult, OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor
from src.simulator.cache_store import SimulationCacheStore, file_sha256, keywords_hash
from src.simulator.feed_scheduler import FeedScheduler
from src.simulator.models import Scenario, SimulatorMode


@dataclass
class ModeRunResult:
    mode: int
    output_dir: Path
    summary: dict


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
            client=client,
            keywords=keywords,
            analysis_model=analysis_model,
            request_timeout_sec=request_timeout_sec,
            parallel_workers=max(1, int(scenario.benchmark.parallel_workers or precompute_workers)),
            repeats=max(1, int(scenario.benchmark.repeats)),
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
    serial_samples = _benchmark_serial(
        tasks=[
            lambda chunk=chunk: client.transcribe_chunk(
                chunk_path=chunk,
                stt_model=stt_model,
                timeout_sec=request_timeout_sec,
            )
            for _ in range(repeats)
            for chunk in chunk_paths
        ]
    )

    parallel_samples = _benchmark_parallel(
        tasks=[
            lambda chunk=chunk: client.transcribe_chunk(
                chunk_path=chunk,
                stt_model=stt_model,
                timeout_sec=request_timeout_sec,
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
    }
    log_fn(
        "[simulate] mode4 benchmark "
        f"serial_avg={report['serial']['avg_sec']:.3f}s parallel_avg={report['parallel']['avg_sec']:.3f}s"
    )
    return report


def _run_mode5_benchmark(
    *,
    chunk_paths: list[Path],
    client: OpenAIInsightClient,
    keywords: KeywordConfig,
    analysis_model: str,
    request_timeout_sec: float,
    parallel_workers: int,
    repeats: int,
    log_fn: Callable[[str], None],
) -> dict:
    samples: list[tuple[str, str]] = []
    history: list[str] = []
    for seq, _chunk in enumerate(chunk_paths, start=1):
        current = f"第{seq}个10秒块模拟文本，包含课堂信息与术语。"
        context = "\n".join(history[-18:]) if history else "无历史文本块"
        samples.append((current, context))
        history.append(f"[seq={seq}] {current}")

    serial_samples = _benchmark_serial(
        tasks=[
            lambda pair=pair: client.analyze_text(
                analysis_model=analysis_model,
                keywords=keywords,
                current_text=pair[0],
                context_text=pair[1],
                timeout_sec=request_timeout_sec,
            )
            for _ in range(repeats)
            for pair in samples
        ]
    )

    parallel_samples = _benchmark_parallel(
        tasks=[
            lambda pair=pair: client.analyze_text(
                analysis_model=analysis_model,
                keywords=keywords,
                current_text=pair[0],
                context_text=pair[1],
                timeout_sec=request_timeout_sec,
            )
            for _ in range(repeats)
            for pair in samples
        ],
        workers=parallel_workers,
    )

    report = {
        "mode": 5,
        "serial": _summarize_samples(serial_samples),
        "parallel": _summarize_samples(parallel_samples),
    }
    log_fn(
        "[simulate] mode5 benchmark "
        f"serial_avg={report['serial']['avg_sec']:.3f}s parallel_avg={report['parallel']['avg_sec']:.3f}s"
    )
    return report


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
