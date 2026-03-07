from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.live.insight.models import RealtimeInsightConfig
from src.simulator.models import ALLOWED_MODE1_REQUIRED_BRANCHES, Mode1ValidationConfig

_TRANSCRIPT_OK = "ok"
_TRANSCRIPT_ALLOWED = {"ok", "transcript_drop_timeout", "transcript_drop_error"}
_INSIGHT_ALLOWED = {"ok", "analysis_drop_timeout", "analysis_drop_error"}
_CONTEXT_REASON_DUAL = {"full18_ready", "timeout_wait_full18", "timeout_wait_recent4"}
_CONTEXT_REASON_LEGACY = {"legacy_ready", "legacy_timeout"}


@dataclass
class _HistoryItem:
    seq: int
    order: int


def run_mode1_validation(
    *,
    run_session_dir: Path,
    run_summary: dict[str, Any],
    config: RealtimeInsightConfig,
    validation_config: Mode1ValidationConfig | None = None,
) -> dict[str, Any]:
    validation = validation_config or Mode1ValidationConfig()
    strict_fail = bool(validation.strict_fail)

    required_branches: list[str] = []
    for branch in validation.required_branches:
        text = str(branch or "").strip()
        if text and text in ALLOWED_MODE1_REQUIRED_BRANCHES and text not in required_branches:
            required_branches.append(text)

    emitted_chunks = int(run_summary.get("emitted_chunks", 0) or 0)
    emitted_seqs = _coerce_emitted_seqs(run_summary.get("emitted_seqs"), fallback_count=emitted_chunks)

    transcript_rows = _load_jsonl_rows(run_session_dir / "realtime_transcripts.jsonl")
    insight_rows = _load_jsonl_rows(run_session_dir / "realtime_insights.jsonl")

    checks_executed = 0
    legal_failures: list[str] = []
    actual_rows: list[dict[str, Any]] = []
    observed_branches: set[str] = set()

    use_dual = bool(getattr(config, "use_dual_context_wait", True))
    allowed_context_reason = _CONTEXT_REASON_DUAL if use_dual else _CONTEXT_REASON_LEGACY
    target_chunks = max(1, int(config.context_target_chunks))
    recent_required = max(0, int(getattr(config, "context_recent_required", 0)))
    stt_attempt_limit = max(1, int(config.stt_retry_count))
    analysis_attempt_limit = max(1, int(config.analysis_retry_count))
    stt_stage_budget = float(config.stt_stage_timeout_sec)
    analysis_stage_budget = float(config.analysis_stage_timeout_sec)

    checks_executed += 1
    if len(emitted_seqs) != emitted_chunks:
        legal_failures.append(
            f"run_summary emitted_seqs size mismatch: emitted_chunks={emitted_chunks} emitted_seqs={len(emitted_seqs)}"
        )

    checks_executed += 1
    if len(transcript_rows) != emitted_chunks:
        legal_failures.append(
            f"transcript rows mismatch: expected={emitted_chunks} actual={len(transcript_rows)}"
        )

    insight_cursor = 0
    prior_ok_history: list[_HistoryItem] = []

    for idx in range(emitted_chunks):
        event_index = idx + 1
        expected_seq = emitted_seqs[idx] if idx < len(emitted_seqs) else event_index
        transcript = transcript_rows[idx] if idx < len(transcript_rows) else {}
        transcript_status = str(transcript.get("status", ""))
        transcript_attempt = _to_int(transcript.get("attempt_count"))
        transcript_elapsed = _to_float(transcript.get("elapsed_sec"))
        transcript_seq = _to_int(transcript.get("chunk_seq"))

        history_all = [item for item in prior_ok_history if item.seq < expected_seq]
        history_trimmed = sorted(history_all, key=lambda item: (item.seq, item.order))[-target_chunks:]
        expected_context_count = len(history_trimmed)
        expected_missing_ranges = _missing_seq_ranges(
            available={item.seq for item in history_trimmed},
            chunk_seq=expected_seq,
            target_chunks=target_chunks,
        )
        recent_ready = _recent_ready(
            available={item.seq for item in history_all},
            chunk_seq=expected_seq,
            recent_required=recent_required,
        )
        full_ready = _full_ready(
            available={item.seq for item in history_all},
            chunk_seq=expected_seq,
            target_chunks=target_chunks,
        )

        row = {
            "event_index": event_index,
            "expected_chunk_seq": expected_seq,
            "transcript_present": bool(transcript),
            "transcript_seq": transcript_seq,
            "transcript_status": transcript_status,
            "transcript_attempt_count": transcript_attempt,
            "transcript_elapsed_sec": transcript_elapsed,
            "insight_present": False,
            "insight_seq": 0,
            "insight_status": "",
            "insight_attempt_count": 0,
            "analysis_elapsed_sec": 0.0,
            "context_chunk_count": 0,
            "context_reason": "",
            "context_missing_ranges": [],
        }

        checks_executed += 1
        if not transcript:
            legal_failures.append(f"event[{event_index}] transcript missing")
            actual_rows.append(row)
            continue

        checks_executed += 1
        if transcript_seq != expected_seq:
            legal_failures.append(
                f"event[{event_index}] transcript seq mismatch: actual={transcript_seq} expected={expected_seq}"
            )

        checks_executed += 1
        if transcript_status not in _TRANSCRIPT_ALLOWED:
            legal_failures.append(f"event[{event_index}] transcript_status invalid: {transcript_status}")

        checks_executed += 1
        if transcript_attempt < 0 or transcript_attempt > stt_attempt_limit:
            legal_failures.append(
                f"event[{event_index}] transcript attempt_count invalid: {transcript_attempt} "
                f"(limit={stt_attempt_limit})"
            )

        checks_executed += 1
        if transcript_elapsed > stt_stage_budget + 1e-6:
            legal_failures.append(
                f"event[{event_index}] transcript elapsed exceeds budget: {transcript_elapsed:.6f}s "
                f"(budget={stt_stage_budget:.6f}s)"
            )

        if transcript_status == "ok":
            observed_branches.add("transcript.ok")
        elif transcript_status == "transcript_drop_timeout":
            observed_branches.add("transcript.drop_timeout")
        elif transcript_status == "transcript_drop_error":
            observed_branches.add("transcript.drop_error")

        transcript_text = str(transcript.get("text", "") or "").strip()
        checks_executed += 1
        if transcript_status == _TRANSCRIPT_OK and not transcript_text:
            legal_failures.append(f"event[{event_index}] transcript status=ok but text is empty")

        if transcript_attempt > 1:
            observed_branches.add("retry.stt_gt1")

        if transcript_status != _TRANSCRIPT_OK:
            actual_rows.append(row)
            continue

        prior_ok_history.append(_HistoryItem(seq=expected_seq, order=event_index))

        insight = insight_rows[insight_cursor] if insight_cursor < len(insight_rows) else {}
        if insight:
            insight_cursor += 1

        row["insight_present"] = bool(insight)
        if not insight:
            checks_executed += 1
            legal_failures.append(f"event[{event_index}] insight missing while transcript_status=ok")
            actual_rows.append(row)
            continue

        insight_seq = _to_int(insight.get("chunk_seq"))
        insight_status = str(insight.get("status", ""))
        insight_attempt = _to_int(insight.get("attempt_count"))
        insight_elapsed = _to_float(insight.get("analysis_elapsed_sec"))
        context_chunk_count = _to_int(insight.get("context_chunk_count"))
        context_reason = str(insight.get("context_reason", ""))
        context_missing_ranges = _to_str_list(insight.get("context_missing_ranges"))
        insight_recovery = bool(insight.get("is_recovery", False))

        row.update(
            {
                "insight_seq": insight_seq,
                "insight_status": insight_status,
                "insight_attempt_count": insight_attempt,
                "analysis_elapsed_sec": insight_elapsed,
                "context_chunk_count": context_chunk_count,
                "context_reason": context_reason,
                "context_missing_ranges": context_missing_ranges,
            }
        )

        checks_executed += 1
        if insight_seq != expected_seq:
            legal_failures.append(
                f"event[{event_index}] insight seq mismatch: actual={insight_seq} expected={expected_seq}"
            )

        checks_executed += 1
        if insight_status not in _INSIGHT_ALLOWED:
            legal_failures.append(f"event[{event_index}] insight_status invalid: {insight_status}")

        checks_executed += 1
        if insight_attempt <= 0 or insight_attempt > analysis_attempt_limit:
            legal_failures.append(
                f"event[{event_index}] analysis attempt_count invalid: {insight_attempt} "
                f"(limit={analysis_attempt_limit})"
            )

        checks_executed += 1
        if insight_elapsed > analysis_stage_budget + 1e-6:
            legal_failures.append(
                f"event[{event_index}] analysis elapsed exceeds budget: {insight_elapsed:.6f}s "
                f"(budget={analysis_stage_budget:.6f}s)"
            )

        checks_executed += 1
        if context_reason not in allowed_context_reason:
            legal_failures.append(f"event[{event_index}] context_reason invalid: {context_reason}")

        checks_executed += 1
        if context_chunk_count != expected_context_count:
            legal_failures.append(
                f"event[{event_index}] context_chunk_count mismatch: actual={context_chunk_count} "
                f"expected={expected_context_count}"
            )

        checks_executed += 1
        if context_missing_ranges != expected_missing_ranges:
            legal_failures.append(
                f"event[{event_index}] context_missing_ranges mismatch: "
                f"actual={context_missing_ranges} expected={expected_missing_ranges}"
            )

        checks_executed += 1
        if context_reason == "full18_ready" and (not recent_ready or not full_ready):
            legal_failures.append(
                f"event[{event_index}] context_reason=full18_ready but recent_ready={recent_ready} full_ready={full_ready}"
            )

        checks_executed += 1
        if context_reason == "timeout_wait_full18" and (not recent_ready or full_ready):
            legal_failures.append(
                f"event[{event_index}] context_reason=timeout_wait_full18 inconsistent "
                f"(recent_ready={recent_ready}, full_ready={full_ready})"
            )

        checks_executed += 1
        if context_reason == "timeout_wait_recent4" and recent_ready:
            legal_failures.append(
                f"event[{event_index}] context_reason=timeout_wait_recent4 but recent context is ready"
            )

        if insight_status == "ok":
            observed_branches.add("analysis.ok")
        elif insight_status == "analysis_drop_timeout":
            observed_branches.add("analysis.drop_timeout")
        elif insight_status == "analysis_drop_error":
            observed_branches.add("analysis.drop_error")

        if context_reason == "full18_ready":
            observed_branches.add("context.full18_ready")
        elif context_reason == "timeout_wait_full18":
            observed_branches.add("context.timeout_wait_full18")
        elif context_reason == "timeout_wait_recent4":
            observed_branches.add("context.timeout_wait_recent4")

        if insight_recovery:
            observed_branches.add("recovery.true")
        else:
            observed_branches.add("recovery.false")

        if insight_attempt > 1:
            observed_branches.add("retry.analysis_gt1")

        actual_rows.append(row)

    checks_executed += 1
    if insight_cursor != len(insight_rows):
        legal_failures.append(
            f"insight rows mismatch: consumed={insight_cursor} actual={len(insight_rows)}"
        )

    missing_branches = [item for item in required_branches if item not in observed_branches]
    legal_passed = not legal_failures
    coverage_passed = not missing_branches
    passed = legal_passed and (coverage_passed or not strict_fail)

    failures = list(legal_failures)
    if strict_fail and missing_branches:
        failures.append(
            "required branch coverage missing: "
            + ", ".join(missing_branches)
        )

    report = {
        "mode": 1,
        "passed": passed,
        "legal_passed": legal_passed,
        "coverage_passed": coverage_passed,
        "strict_fail": strict_fail,
        "checks_executed": checks_executed,
        "failure_count": len(failures),
        "failures": failures,
        "observed_branches": sorted(observed_branches),
        "required_branches": required_branches,
        "missing_branches": missing_branches,
        "actual": {
            "emitted_chunks": emitted_chunks,
            "emitted_seqs": emitted_seqs,
            "rows": actual_rows,
        },
        "limits": {
            "stt_attempt_limit": stt_attempt_limit,
            "analysis_attempt_limit": analysis_attempt_limit,
            "stt_stage_timeout_sec": stt_stage_budget,
            "analysis_stage_timeout_sec": analysis_stage_budget,
            "context_target_chunks": target_chunks,
            "context_recent_required": recent_required,
        },
    }
    path = run_session_dir / "mode1_validation_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_file"] = path.as_posix()
    return report


def _recent_ready(*, available: set[int], chunk_seq: int, recent_required: int) -> bool:
    recent_required = min(max(0, int(recent_required)), max(0, int(chunk_seq) - 1))
    if recent_required <= 0:
        return True
    start = max(1, chunk_seq - recent_required)
    return all(seq in available for seq in range(start, chunk_seq))


def _full_ready(*, available: set[int], chunk_seq: int, target_chunks: int) -> bool:
    target = min(max(1, int(target_chunks)), max(0, int(chunk_seq) - 1))
    if target <= 0:
        return True
    start = max(1, chunk_seq - target)
    return all(seq in available for seq in range(start, chunk_seq))


def _missing_seq_ranges(*, available: set[int], chunk_seq: int, target_chunks: int) -> list[str]:
    if chunk_seq <= 1:
        return []
    start = max(1, chunk_seq - max(1, int(target_chunks)))
    ranges: list[str] = []
    missing_start: int | None = None
    for seq in range(start, chunk_seq):
        if seq in available:
            if missing_start is not None:
                end = seq - 1
                ranges.append(str(missing_start) if missing_start == end else f"{missing_start}-{end}")
                missing_start = None
            continue
        if missing_start is None:
            missing_start = seq
    if missing_start is not None:
        end = chunk_seq - 1
        ranges.append(str(missing_start) if missing_start == end else f"{missing_start}-{end}")
    return ranges


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _coerce_emitted_seqs(value: Any, *, fallback_count: int) -> list[int]:
    if not isinstance(value, list):
        return [idx for idx in range(1, max(0, int(fallback_count)) + 1)]
    out: list[int] = []
    for item in value:
        try:
            seq = int(item)
        except (TypeError, ValueError):
            continue
        if seq > 0:
            out.append(seq)
    return out


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _to_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out
