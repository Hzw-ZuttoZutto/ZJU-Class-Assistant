from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.simulator.models import Scenario


def run_mode2_validation(
    *,
    scenario: Scenario,
    run_summary: dict[str, Any],
    precompute_manifest: dict[str, Any] | None,
    run_session_dir: Path,
) -> dict[str, Any]:
    cfg = scenario.mode2_validation
    failures: list[str] = []
    checks_executed = 0

    def _expect(field: str, *, expected: Any, actual: Any) -> None:
        nonlocal checks_executed
        checks_executed += 1
        if actual != expected:
            failures.append(f"{field} expected={expected} actual={actual}")

    precompute_actual = _extract_precompute_actual(precompute_manifest)
    precompute_expected = cfg.precompute
    if precompute_expected.stt_failures is not None:
        _expect(
            "precompute.stt.failures",
            expected=precompute_expected.stt_failures,
            actual=precompute_actual["stt_failures"],
        )
    if precompute_expected.analysis_failures is not None:
        _expect(
            "precompute.analysis.failures",
            expected=precompute_expected.analysis_failures,
            actual=precompute_actual["analysis_failures"],
        )
    if precompute_expected.stt_misses is not None:
        _expect(
            "precompute.stt.misses",
            expected=precompute_expected.stt_misses,
            actual=precompute_actual["stt_misses"],
        )
    if precompute_expected.stt_computed is not None:
        _expect(
            "precompute.stt.computed",
            expected=precompute_expected.stt_computed,
            actual=precompute_actual["stt_computed"],
        )
    if precompute_expected.analysis_misses is not None:
        _expect(
            "precompute.analysis.misses",
            expected=precompute_expected.analysis_misses,
            actual=precompute_actual["analysis_misses"],
        )
    if precompute_expected.analysis_computed is not None:
        _expect(
            "precompute.analysis.computed",
            expected=precompute_expected.analysis_computed,
            actual=precompute_actual["analysis_computed"],
        )

    run_expected = cfg.run
    trace_file = str(run_summary.get("trace_file", "") or "")
    run_actual = {
        "emitted_chunks": _to_opt_int(run_summary.get("emitted_chunks")),
        "translation_rules_applied": _to_opt_int(run_summary.get("translation_rules_applied")),
        "analysis_rules_applied": _to_opt_int(run_summary.get("analysis_rules_applied")),
        "mode3_variant": str(run_summary.get("mode3_variant", "") or ""),
        "trace_file": trace_file,
    }
    if run_expected.emitted_chunks is not None:
        _expect(
            "run.emitted_chunks",
            expected=run_expected.emitted_chunks,
            actual=run_actual["emitted_chunks"],
        )
    if run_expected.translation_rules_applied is not None:
        _expect(
            "run.translation_rules_applied",
            expected=run_expected.translation_rules_applied,
            actual=run_actual["translation_rules_applied"],
        )
    if run_expected.analysis_rules_applied is not None:
        _expect(
            "run.analysis_rules_applied",
            expected=run_expected.analysis_rules_applied,
            actual=run_actual["analysis_rules_applied"],
        )
    if run_expected.mode3_variant:
        _expect(
            "run.mode3_variant",
            expected=run_expected.mode3_variant,
            actual=run_actual["mode3_variant"],
        )

    transcript_by_seq = _load_jsonl_by_seq(run_session_dir / "realtime_transcripts.jsonl")
    insight_by_seq = _load_jsonl_by_seq(run_session_dir / "realtime_insights.jsonl")
    trace_by_seq = _load_trace_by_seq(Path(trace_file)) if trace_file else {}
    seq_actual: list[dict[str, Any]] = []
    for item in cfg.seq:
        transcript = transcript_by_seq.get(item.seq)
        insight = insight_by_seq.get(item.seq)
        trace = trace_by_seq.get(item.seq, {})
        insight_present = item.seq in insight_by_seq
        seq_actual.append(
            {
                "seq": item.seq,
                "transcript_status": str((transcript or {}).get("status", "")),
                "transcript_text": str((transcript or {}).get("text", "")),
                "insight_present": insight_present,
                "insight_status": str((insight or {}).get("status", "")),
                "context_chunk_count": _to_opt_int((insight or {}).get("context_chunk_count")),
                "history_visibility_mask": str(trace.get("history_visibility_mask", "")),
                "forced_text_applied": bool(trace.get("forced_text_applied", False)),
                "forced_result_applied": bool(trace.get("forced_result_applied", False)),
            }
        )

        if item.transcript_status:
            checks_executed += 1
            if transcript is None:
                failures.append(
                    f"seq[{item.seq}].transcript_status expected={item.transcript_status} actual=<missing>"
                )
            elif str(transcript.get("status", "")) != item.transcript_status:
                failures.append(
                    f"seq[{item.seq}].transcript_status expected={item.transcript_status} "
                    f"actual={transcript.get('status', '')}"
                )
        if item.insight_present is not None:
            _expect(
                f"seq[{item.seq}].insight_present",
                expected=bool(item.insight_present),
                actual=insight_present,
            )
        if item.insight_status:
            checks_executed += 1
            if insight is None:
                failures.append(f"seq[{item.seq}].insight_status expected={item.insight_status} actual=<missing>")
            else:
                actual_status = str(insight.get("status", ""))
                if actual_status != item.insight_status:
                    failures.append(
                        f"seq[{item.seq}].insight_status expected={item.insight_status} actual={actual_status}"
                    )
        if item.context_chunk_count is not None:
            checks_executed += 1
            if insight is None:
                failures.append(
                    f"seq[{item.seq}].context_chunk_count expected={item.context_chunk_count} actual=<missing>"
                )
            else:
                actual_count = _to_opt_int(insight.get("context_chunk_count"))
                if actual_count != item.context_chunk_count:
                    failures.append(
                        f"seq[{item.seq}].context_chunk_count expected={item.context_chunk_count} actual={actual_count}"
                    )
        if item.history_visibility_mask:
            checks_executed += 1
            actual_mask = str(trace.get("history_visibility_mask", ""))
            if actual_mask != item.history_visibility_mask:
                failures.append(
                    f"seq[{item.seq}].history_visibility_mask expected={item.history_visibility_mask!r} "
                    f"actual={actual_mask!r}"
                )
        if item.forced_text_exact:
            checks_executed += 1
            if transcript is None:
                failures.append(f"seq[{item.seq}].forced_text_exact expected text but transcript is missing")
            else:
                actual_text = str(transcript.get("text", ""))
                if actual_text != item.forced_text_exact:
                    failures.append(
                        f"seq[{item.seq}].forced_text_exact expected={item.forced_text_exact!r} actual={actual_text!r}"
                    )
        if item.forced_text_applied is not None:
            _expect(
                f"seq[{item.seq}].forced_text_applied",
                expected=bool(item.forced_text_applied),
                actual=bool(trace.get("forced_text_applied", False)),
            )
        if item.forced_result_applied is not None:
            _expect(
                f"seq[{item.seq}].forced_result_applied",
                expected=bool(item.forced_result_applied),
                actual=bool(trace.get("forced_result_applied", False)),
            )

    passed = not failures
    report = {
        "mode": int(scenario.mode),
        "strict_fail": bool(cfg.strict_fail),
        "checks_executed": checks_executed,
        "passed": passed,
        "failure_count": len(failures),
        "failures": failures,
        "actual": {
            "precompute": precompute_actual,
            "run": run_actual,
            "seq": seq_actual,
        },
    }
    report_path = run_session_dir / "validation_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_file"] = report_path.as_posix()
    return report


def _extract_precompute_actual(manifest: dict[str, Any] | None) -> dict[str, int | None]:
    if not isinstance(manifest, dict):
        return {
            "stt_failures": None,
            "analysis_failures": None,
            "stt_misses": None,
            "stt_computed": None,
            "analysis_misses": None,
            "analysis_computed": None,
        }
    stt = manifest.get("stt") if isinstance(manifest.get("stt"), dict) else {}
    analysis = manifest.get("analysis") if isinstance(manifest.get("analysis"), dict) else {}
    return {
        "stt_failures": _to_opt_int(stt.get("failures")),
        "analysis_failures": _to_opt_int(analysis.get("failures")),
        "stt_misses": _to_opt_int(stt.get("misses")),
        "stt_computed": _to_opt_int(stt.get("computed")),
        "analysis_misses": _to_opt_int(analysis.get("misses")),
        "analysis_computed": _to_opt_int(analysis.get("computed")),
    }


def _load_jsonl_by_seq(path: Path) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        seq = _to_opt_int(payload.get("chunk_seq"))
        if seq is None or seq <= 0:
            continue
        out[seq] = payload
    return out


def _load_trace_by_seq(path: Path) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        seq = _to_opt_int(payload.get("out_seq"))
        if seq is None or seq <= 0:
            seq = _to_opt_int(payload.get("chunk_seq"))
        if seq is None or seq <= 0:
            continue
        out[seq] = payload
    return out


def _to_opt_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
