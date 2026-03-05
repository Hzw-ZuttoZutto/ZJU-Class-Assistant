from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.simulator.models import (
    BenchmarkConfig,
    DatasetConfig,
    FeedBarrierRule,
    FeedConfig,
    FeedDelayBackfillRule,
    FeedDuplicateRule,
    HistoryRule,
    PrecomputeConfig,
    Scenario,
    SimulatorMode,
    StageControlRule,
)


def load_scenario(path: Path, *, expected_mode: SimulatorMode | None = None) -> Scenario:
    payload = _load_yaml_object(path)

    if "mode" not in payload:
        raise ValueError(f"scenario missing required field: mode ({path})")
    mode = SimulatorMode.from_int(int(payload["mode"]))
    if expected_mode is not None and mode != expected_mode:
        raise ValueError(
            f"scenario mode mismatch: expected mode={int(expected_mode)} but got mode={int(mode)}"
        )

    dataset = _parse_dataset(payload.get("dataset"))
    feed = _parse_feed(payload.get("feed"))
    translation_rules, analysis_rules = _parse_controls(payload.get("control"))
    history_rules = _parse_history(payload.get("history"))

    precompute_payload = payload.get("precompute") if isinstance(payload.get("precompute"), dict) else {}
    benchmark_payload = payload.get("benchmark") if isinstance(payload.get("benchmark"), dict) else {}

    precompute = PrecomputeConfig(workers=max(1, int(precompute_payload.get("workers", 4))))
    benchmark = BenchmarkConfig(
        parallel_workers=max(1, int(benchmark_payload.get("parallel_workers", 4))),
        repeats=max(1, int(benchmark_payload.get("repeats", 1))),
    )

    mode3_variant = str(payload.get("mode3_variant", "complete_history") or "complete_history").strip()
    if mode3_variant not in {"complete_history", "controlled_history"}:
        raise ValueError(f"unsupported mode3_variant={mode3_variant}")

    seed_raw = payload.get("seed")
    seed = int(seed_raw) if seed_raw is not None else None

    return Scenario(
        mode=mode,
        name=str(payload.get("name", path.stem) or path.stem),
        dataset=dataset,
        feed=feed,
        translation_rules=translation_rules,
        analysis_rules=analysis_rules,
        history_rules=history_rules,
        precompute=precompute,
        benchmark=benchmark,
        seed=seed,
        mode3_variant=mode3_variant,
    )


def validate_visibility_mask(mask: str) -> None:
    if len(mask) != 18:
        raise ValueError("history visibility mask must contain exactly 18 bits")
    if any(ch not in {"0", "1"} for ch in mask):
        raise ValueError("history visibility mask must contain only 0/1")


def _load_yaml_object(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to read scenario file: {path}: {exc}") from exc

    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid yaml in scenario file: {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"scenario root must be an object: {path}")
    return payload


def _parse_dataset(payload: Any) -> DatasetConfig:
    if not isinstance(payload, dict):
        return DatasetConfig()
    files = _coerce_str_list(payload.get("files"))
    include_glob = str(payload.get("include_glob", "*.mp3") or "*.mp3").strip() or "*.mp3"
    return DatasetConfig(files=files, include_glob=include_glob)


def _parse_feed(payload: Any) -> FeedConfig:
    if not isinstance(payload, dict):
        return FeedConfig()

    jitter_max_sec = payload.get("jitter_max_sec", 0.0)
    jitter_payload = payload.get("jitter")
    if isinstance(jitter_payload, dict) and "max_sec" in jitter_payload:
        jitter_max_sec = jitter_payload.get("max_sec", 0.0)

    duplicate_rules: list[FeedDuplicateRule] = []
    for item in _coerce_list(payload.get("duplicate")):
        if not isinstance(item, dict):
            continue
        seq = int(item.get("seq", 0))
        times = max(1, int(item.get("times", 1)))
        if seq > 0:
            duplicate_rules.append(FeedDuplicateRule(seq=seq, times=times))

    delay_backfill_rules: list[FeedDelayBackfillRule] = []
    for item in _coerce_list(payload.get("delay_backfill")):
        if not isinstance(item, dict):
            continue
        seq = int(item.get("seq", 0))
        delay_sec = max(0.0, float(item.get("delay_sec", 0.0)))
        if seq > 0:
            delay_backfill_rules.append(FeedDelayBackfillRule(seq=seq, delay_sec=delay_sec))

    barriers: list[FeedBarrierRule] = []
    for item in _coerce_list(payload.get("barriers")):
        if not isinstance(item, dict):
            continue
        after_seq = int(item.get("after_seq", 0))
        pause_sec = max(0.0, float(item.get("pause_sec", 0.0)))
        if after_seq > 0 and pause_sec > 0:
            barriers.append(FeedBarrierRule(after_seq=after_seq, pause_sec=pause_sec))

    return FeedConfig(
        mode=str(payload.get("mode", "realtime") or "realtime").strip().lower(),
        speed=max(0.01, float(payload.get("speed", 1.0))),
        jitter_max_sec=max(0.0, float(jitter_max_sec)),
        drop=_coerce_int_list(payload.get("drop")),
        duplicate=duplicate_rules,
        reorder=_coerce_int_list(payload.get("reorder")),
        delay_backfill=delay_backfill_rules,
        barriers=barriers,
    )


def _parse_controls(payload: Any) -> tuple[list[StageControlRule], list[StageControlRule]]:
    if not isinstance(payload, dict):
        return [], []
    translation = _parse_stage_rules(payload.get("translation"))
    analysis = _parse_stage_rules(payload.get("analysis"))
    return translation, analysis


def _parse_stage_rules(payload: Any) -> list[StageControlRule]:
    rules_payload: Any = payload
    if isinstance(payload, dict):
        rules_payload = payload.get("rules", [])

    out: list[StageControlRule] = []
    for item in _coerce_list(rules_payload):
        if not isinstance(item, dict):
            continue
        seq = int(item.get("seq", 0))
        if seq <= 0:
            continue
        out.append(
            StageControlRule(
                seq=seq,
                status=str(item.get("status", "ok") or "ok"),
                delay_sec=max(0.0, float(item.get("delay_sec", 0.0))),
                forced_text=str(item.get("forced_text", "") or ""),
                forced_result=item.get("forced_result") if isinstance(item.get("forced_result"), dict) else {},
            )
        )
    return out


def _parse_history(payload: Any) -> list[HistoryRule]:
    if not isinstance(payload, dict):
        return []

    items = payload.get("by_seq", [])
    out: list[HistoryRule] = []
    for item in _coerce_list(items):
        if not isinstance(item, dict):
            continue
        seq = int(item.get("seq", 0))
        if seq <= 0:
            continue
        visibility = str(item.get("visibility", "")).strip()
        validate_visibility_mask(visibility)
        hold_sec = max(0.0, float(item.get("hold_sec", 0.0)))
        out.append(HistoryRule(seq=seq, visibility=visibility, hold_sec=hold_sec))
    return out


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _coerce_str_list(value: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _coerce_int_list(value: Any) -> list[int]:
    out: list[int] = []
    if not isinstance(value, list):
        return out
    for item in value:
        try:
            num = int(item)
        except (TypeError, ValueError):
            continue
        if num > 0:
            out.append(num)
    return out
