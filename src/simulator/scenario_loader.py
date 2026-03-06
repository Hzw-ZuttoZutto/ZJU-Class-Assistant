from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.simulator.models import (
    ALLOWED_MODE6_CONTEXT_REASONS,
    ALLOWED_MODE6_STT_STEP_TYPES,
    BenchmarkConfig,
    DatasetConfig,
    FeedBarrierRule,
    FeedConfig,
    FeedDelayBackfillRule,
    FeedDuplicateRule,
    HistoryRule,
    Mode6Case,
    Mode6CaseConfig,
    Mode6Config,
    Mode6Expected,
    Mode6HistoryArrival,
    Mode6HistoryItem,
    Mode6SttStep,
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
    mode6 = _parse_mode6(payload.get("mode6"))

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
        mode6=mode6,
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


def _parse_mode6(payload: Any) -> Mode6Config:
    if not isinstance(payload, dict):
        return Mode6Config()

    check_interval_sec = max(0.01, float(payload.get("check_interval_sec", 0.2)))
    cases_payload = payload.get("cases")
    out_cases: list[Mode6Case] = []
    for idx, item in enumerate(_coerce_list(cases_payload), start=1):
        if not isinstance(item, dict):
            continue
        case_id = str(item.get("id", f"case_{idx}") or f"case_{idx}").strip() or f"case_{idx}"
        chunk_seq = int(item.get("chunk_seq", 0))
        if chunk_seq <= 0:
            raise ValueError(f"mode6 case '{case_id}' chunk_seq must be >= 1")

        config_payload = item.get("config") if isinstance(item.get("config"), dict) else {}
        case_config = Mode6CaseConfig(
            request_timeout_sec=_coerce_opt_float(config_payload.get("request_timeout_sec")),
            stage_timeout_sec=_coerce_opt_float(config_payload.get("stage_timeout_sec")),
            retry_count=_coerce_opt_int(config_payload.get("retry_count")),
            context_recent_required=_coerce_opt_int(config_payload.get("context_recent_required")),
            context_target_chunks=_coerce_opt_int(config_payload.get("context_target_chunks")),
            context_wait_timeout_sec_1=_coerce_opt_float(config_payload.get("context_wait_timeout_sec_1")),
            context_wait_timeout_sec_2=_coerce_opt_float(config_payload.get("context_wait_timeout_sec_2")),
        )

        stt_script = _parse_mode6_stt_script(item.get("stt_script"), case_id=case_id)
        if not stt_script:
            raise ValueError(f"mode6 case '{case_id}' stt_script must not be empty")

        history_payload = item.get("history") if isinstance(item.get("history"), dict) else {}
        history_initial = _parse_mode6_history_initial(history_payload.get("initial"))
        history_arrivals = _parse_mode6_history_arrivals(history_payload.get("arrivals"))
        _validate_mode6_history_unique(case_id=case_id, initial=history_initial, arrivals=history_arrivals)

        expected_payload = item.get("expected") if isinstance(item.get("expected"), dict) else {}
        expected = _parse_mode6_expected(expected_payload, case_id=case_id)

        out_cases.append(
            Mode6Case(
                id=case_id,
                chunk_seq=chunk_seq,
                config=case_config,
                stt_script=stt_script,
                history_initial=history_initial,
                history_arrivals=history_arrivals,
                expected=expected,
            )
        )
    return Mode6Config(check_interval_sec=check_interval_sec, cases=out_cases)


def _parse_mode6_stt_script(payload: Any, *, case_id: str) -> list[Mode6SttStep]:
    out: list[Mode6SttStep] = []
    for idx, item in enumerate(_coerce_list(payload), start=1):
        if not isinstance(item, dict):
            continue
        raw_type = str(item.get("type", "ok") or "ok").strip().lower()
        if raw_type not in ALLOWED_MODE6_STT_STEP_TYPES:
            allowed = ",".join(sorted(ALLOWED_MODE6_STT_STEP_TYPES))
            raise ValueError(
                f"mode6 case '{case_id}' stt_script[{idx}] invalid type='{raw_type}', allowed={allowed}"
            )
        step = Mode6SttStep(
            type=raw_type,
            text=str(item.get("text", "") or "").strip(),
            error=str(item.get("error", "") or "").strip(),
            delay_sec=max(0.0, float(item.get("delay_sec", 0.0))),
        )
        if step.normalized_type() == "ok" and not step.text:
            raise ValueError(f"mode6 case '{case_id}' stt_script[{idx}] type=ok requires non-empty text")
        out.append(step)
    return out


def _parse_mode6_history_initial(payload: Any) -> list[Mode6HistoryItem]:
    out: list[Mode6HistoryItem] = []
    for item in _coerce_list(payload):
        if not isinstance(item, dict):
            continue
        seq = int(item.get("seq", 0))
        text = str(item.get("text", "") or "").strip()
        if seq <= 0 or not text:
            continue
        out.append(Mode6HistoryItem(seq=seq, text=text))
    return out


def _parse_mode6_history_arrivals(payload: Any) -> list[Mode6HistoryArrival]:
    out: list[Mode6HistoryArrival] = []
    for item in _coerce_list(payload):
        if not isinstance(item, dict):
            continue
        at_sec = float(item.get("at_sec", 0.0))
        if at_sec < 0:
            raise ValueError("mode6 history.arrivals contains negative at_sec")
        seq = int(item.get("seq", 0))
        text = str(item.get("text", "") or "").strip()
        if seq <= 0 or not text:
            continue
        out.append(Mode6HistoryArrival(at_sec=at_sec, seq=seq, text=text))
    out.sort(key=lambda item: (item.at_sec, item.seq))
    return out


def _validate_mode6_history_unique(
    *,
    case_id: str,
    initial: list[Mode6HistoryItem],
    arrivals: list[Mode6HistoryArrival],
) -> None:
    seen: set[int] = set()
    for item in initial:
        if item.seq in seen:
            raise ValueError(f"mode6 case '{case_id}' history.initial has duplicate seq={item.seq}")
        seen.add(item.seq)
    for item in arrivals:
        if item.seq in seen:
            raise ValueError(f"mode6 case '{case_id}' history.arrivals reuses seq={item.seq}")
        seen.add(item.seq)


def _parse_mode6_expected(payload: Any, *, case_id: str) -> Mode6Expected:
    expected = Mode6Expected(
        stt_status=str(payload.get("stt_status", "") or "").strip(),
        stt_attempts=_coerce_opt_int(payload.get("stt_attempts")),
        analysis_called=_coerce_opt_bool(payload.get("analysis_called")),
        context_reason=str(payload.get("context_reason", "") or "").strip(),
        context_chunk_count=_coerce_opt_int(payload.get("context_chunk_count")),
        missing_ranges=_coerce_opt_str_list(payload.get("missing_ranges")),
    )
    if expected.context_reason and expected.context_reason not in ALLOWED_MODE6_CONTEXT_REASONS:
        allowed = ",".join(sorted(ALLOWED_MODE6_CONTEXT_REASONS))
        raise ValueError(
            f"mode6 case '{case_id}' expected.context_reason='{expected.context_reason}' invalid, allowed={allowed}"
        )
    return expected


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


def _coerce_opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_opt_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _coerce_opt_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out
