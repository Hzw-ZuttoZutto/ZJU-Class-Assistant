from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import src.live.insight.stage_processor as stage_processor_module
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig, TranscriptChunk
from src.live.insight.openai_client import InsightModelResult
from src.live.insight.stage_processor import InsightStageProcessor
from src.simulator.models import Mode6Case, Scenario


class _VirtualClock:
    def __init__(self, *, trace_writer: Callable[[dict[str, Any]], None], case_id: str) -> None:
        self.now_sec = 0.0
        self._trace_writer = trace_writer
        self._case_id = case_id

    def monotonic(self) -> float:
        return self.now_sec

    def sleep(self, seconds: float) -> None:
        delta = max(0.0, float(seconds))
        self._trace_writer(
            {
                "case_id": self._case_id,
                "event": "sleep",
                "at_sec": round(self.now_sec, 6),
                "duration_sec": round(delta, 6),
            }
        )
        self.now_sec += delta


class _Mode6ScriptClient:
    def __init__(
        self,
        *,
        case: Mode6Case,
        clock: _VirtualClock,
        trace_writer: Callable[[dict[str, Any]], None],
    ) -> None:
        self.case = case
        self.clock = clock
        self.trace_writer = trace_writer
        self._stt_index = 0
        self._analysis_index = 0
        self.stt_calls = 0
        self.analyze_calls = 0
        self.last_context_text = ""

    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        self.stt_calls += 1
        if self._stt_index >= len(self.case.stt_script):
            raise RuntimeError("mode6 stt_script exhausted")
        step = self.case.stt_script[self._stt_index]
        self._stt_index += 1

        step_type = step.normalized_type()
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "stt_attempt",
                "attempt": self.stt_calls,
                "step_type": step_type,
                "timeout_sec": round(float(timeout_sec), 6),
                "delay_sec": round(float(step.delay_sec), 6),
            }
        )

        if step.delay_sec > 0:
            self.clock.sleep(step.delay_sec)

        if step_type == "ok":
            return step.text
        if step_type == "timeout_request":
            self.clock.sleep(max(0.0, float(timeout_sec)))
            raise TimeoutError(step.error or "scripted timeout")
        raise RuntimeError(step.error or "scripted error")

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        timeout_sec: float,
        debug_hook=None,
    ) -> InsightModelResult:
        self.analyze_calls += 1
        self.last_context_text = context_text
        if self._analysis_index >= len(self.case.analysis_script):
            raise RuntimeError("mode6 analysis_script exhausted")

        step = self.case.analysis_script[self._analysis_index]
        self._analysis_index += 1
        step_type = step.normalized_type()
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "analysis_attempt",
                "attempt": self.analyze_calls,
                "step_type": step_type,
                "timeout_sec": round(float(timeout_sec), 6),
                "delay_sec": round(float(step.delay_sec), 6),
                "context_len": len(context_text),
            }
        )

        if step.delay_sec > 0:
            self.clock.sleep(step.delay_sec)

        if step_type == "ok":
            payload = step.result if isinstance(step.result, dict) else {}
            default_summary = f"mode6-{self.case.id}"
            return InsightModelResult(
                important=bool(payload.get("important", False)),
                summary=str(payload.get("summary", default_summary) or default_summary).strip(),
                context_summary=str(payload.get("context_summary", "mode6") or "mode6").strip(),
                matched_terms=_coerce_str_list(payload.get("matched_terms")),
                reason=str(payload.get("reason", "mode6") or "mode6").strip(),
            )
        if step_type == "timeout_request":
            self.clock.sleep(max(0.0, float(timeout_sec)))
            raise TimeoutError(step.error or "scripted timeout")
        raise RuntimeError(step.error or "scripted error")


class _Mode6Processor(InsightStageProcessor):
    def __init__(
        self,
        *,
        case: Mode6Case,
        config: RealtimeInsightConfig,
        keywords: KeywordConfig,
        client: _Mode6ScriptClient,
        clock: _VirtualClock,
        trace_writer: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(
            session_dir=Path("."),
            config=config,
            keywords=keywords,
            client=client,  # type: ignore[arg-type]
            log_fn=lambda _msg: None,
        )
        self.case = case
        self.clock = clock
        self.trace_writer = trace_writer
        self._transcripts: list[TranscriptChunk] = []
        self._insight_payloads: list[dict[str, Any]] = []
        self._arrival_cursor = 0
        self._context_start_mono: float | None = None
        self._seen_history_seqs: set[int] = set()
        self.last_stt_status = ""
        self.last_stt_attempts = 0
        self.last_analysis_status = ""
        self.last_analysis_attempts = 0
        self.last_analysis_elapsed_sec = 0.0

        for item in case.history_initial:
            self._append_history_chunk(seq=item.seq, text=item.text, source="initial")

    def _append_history_chunk(self, *, seq: int, text: str, source: str) -> None:
        if seq in self._seen_history_seqs:
            return
        self._seen_history_seqs.add(seq)
        transcript = TranscriptChunk(
            chunk_seq=seq,
            chunk_file=f"history_{seq:06d}.mp3",
            ts_local=f"mode6_{self.clock.monotonic():.3f}",
            text=text,
            status="ok",
            error="",
        )
        self._transcripts.append(transcript)
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "history_append",
                "source": source,
                "seq": seq,
                "at_sec": round(self.clock.monotonic(), 6),
            }
        )

    def _flush_due_arrivals(self) -> None:
        if self._context_start_mono is None:
            return
        elapsed = self.clock.monotonic() - self._context_start_mono
        while self._arrival_cursor < len(self.case.history_arrivals):
            item = self.case.history_arrivals[self._arrival_cursor]
            if float(item.at_sec) > elapsed + 1e-9:
                break
            self._arrival_cursor += 1
            self._append_history_chunk(seq=item.seq, text=item.text, source="arrival")

    def append_transcript(self, transcript: TranscriptChunk) -> None:
        self._transcripts.append(transcript)
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "append_transcript",
                "seq": transcript.chunk_seq,
                "status": transcript.status,
                "at_sec": round(self.clock.monotonic(), 6),
            }
        )

    def load_transcript_chunks(self) -> list[TranscriptChunk]:
        self._flush_due_arrivals()
        return list(sorted(self._transcripts, key=lambda item: item.chunk_seq))

    def append_insight_event(self, event) -> None:
        payload = event.to_json_dict()
        self._insight_payloads.append(payload)
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "append_insight",
                "status": payload.get("status", ""),
                "context_chunk_count": payload.get("context_chunk_count", 0),
                "at_sec": round(self.clock.monotonic(), 6),
            }
        )

    def transcribe_with_retry(self, chunk_path: Path) -> tuple[str, str, int, str]:
        text, status, attempts, error = super().transcribe_with_retry(chunk_path)
        self.last_stt_status = status
        self.last_stt_attempts = attempts
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "stt_result",
                "status": status,
                "attempts": attempts,
                "error": error,
            }
        )
        return text, status, attempts, error

    def wait_and_collect_history(self, chunk_seq: int) -> list[TranscriptChunk]:
        self._context_start_mono = self.clock.monotonic()
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "context_wait_start",
                "at_sec": round(self._context_start_mono, 6),
            }
        )
        history = super().wait_and_collect_history(chunk_seq)
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "context_wait_end",
                "reason": self._last_context_reason,
                "history_len": len(history),
                "at_sec": round(self.clock.monotonic(), 6),
            }
        )
        return history

    def analyze_with_retry(
        self,
        *,
        current_text: str,
        context_text: str,
    ) -> tuple[InsightModelResult | None, str, int, str]:
        started = self.clock.monotonic()
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "analysis_retry_start",
                "at_sec": round(started, 6),
            }
        )
        result, status, attempts, error = super().analyze_with_retry(
            current_text=current_text,
            context_text=context_text,
        )
        elapsed = self.clock.monotonic() - started
        self.last_analysis_status = status
        self.last_analysis_attempts = attempts
        self.last_analysis_elapsed_sec = elapsed
        self.trace_writer(
            {
                "case_id": self.case.id,
                "event": "analysis_retry_end",
                "status": status,
                "attempts": attempts,
                "elapsed_sec": round(elapsed, 6),
                "error": error,
            }
        )
        return result, status, attempts, error

    @property
    def last_context_reason(self) -> str:
        return str(self._last_context_reason or "").strip()

    @property
    def last_insight_payload(self) -> dict[str, Any]:
        if not self._insight_payloads:
            return {}
        return self._insight_payloads[-1]


def run_mode6_validation(
    *,
    scenario: Scenario,
    base_config: RealtimeInsightConfig,
    keywords: KeywordConfig,
    output_dir: Path,
    log_fn: Callable[[str], None],
) -> dict:
    if not scenario.mode6.cases:
        raise RuntimeError("mode6 requires non-empty mode6.cases in scenario")

    trace_path = output_dir / "mode6_trace.jsonl"
    report_path = output_dir / "mode6_report.json"
    trace_path.write_text("", encoding="utf-8")

    def trace_writer(payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        with trace_path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")

    case_results: list[dict[str, Any]] = []
    for case in scenario.mode6.cases:
        trace_writer({"case_id": case.id, "event": "case_start"})
        result = _run_one_case(
            case=case,
            base_config=base_config,
            keywords=keywords,
            check_interval_sec=scenario.mode6.check_interval_sec,
            trace_writer=trace_writer,
        )
        case_results.append(result)
        trace_writer({"case_id": case.id, "event": "case_end", "passed": result["passed"]})

    fail_items = [item for item in case_results if not item.get("passed", False)]
    report = {
        "mode": 6,
        "check_interval_sec": float(scenario.mode6.check_interval_sec),
        "case_count": len(case_results),
        "pass_count": len(case_results) - len(fail_items),
        "fail_count": len(fail_items),
        "trace_file": trace_path.as_posix(),
        "cases": case_results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if fail_items:
        first = fail_items[0]
        log_fn(
            "[simulate] mode6 first failure: "
            f"case={first.get('id', '')} failures={'; '.join(first.get('failures', []))}"
        )
    return report


def _run_one_case(
    *,
    case: Mode6Case,
    base_config: RealtimeInsightConfig,
    keywords: KeywordConfig,
    check_interval_sec: float,
    trace_writer: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    clock = _VirtualClock(trace_writer=trace_writer, case_id=case.id)
    client = _Mode6ScriptClient(case=case, clock=clock, trace_writer=trace_writer)
    config = _build_case_config(
        base_config=base_config,
        case=case,
        check_interval_sec=check_interval_sec,
    )
    processor = _Mode6Processor(
        case=case,
        config=config,
        keywords=keywords,
        client=client,
        clock=clock,
        trace_writer=trace_writer,
    )
    chunk_path = Path(f"mode6_case_{case.id}.mp3")

    runtime_error = ""
    try:
        _patch_stage_processor_time(clock=clock)
        try:
            processor.process_chunk(case.chunk_seq, chunk_path)
        finally:
            _unpatch_stage_processor_time()
    except Exception as exc:
        runtime_error = f"{type(exc).__name__}: {exc}"

    actual = {
        "stt_status": processor.last_stt_status,
        "stt_attempts": int(processor.last_stt_attempts),
        "analysis_called": bool(client.analyze_calls > 0),
        "analysis_status": processor.last_analysis_status,
        "analysis_attempts": int(processor.last_analysis_attempts),
        "analysis_elapsed_sec": round(float(processor.last_analysis_elapsed_sec), 6),
        "context_reason": processor.last_context_reason,
        "context_chunk_count": int(processor.last_insight_payload.get("context_chunk_count", 0)),
        "missing_ranges": _extract_missing_ranges(client.last_context_text),
        "runtime_error": runtime_error,
    }
    failures = _evaluate_case(case=case, actual=actual)
    if runtime_error:
        failures.append(runtime_error)
    passed = not failures
    return {
        "id": case.id,
        "chunk_seq": case.chunk_seq,
        "passed": passed,
        "failures": failures,
        "expected": {
            "stt_status": case.expected.stt_status,
            "stt_attempts": case.expected.stt_attempts,
            "analysis_called": case.expected.analysis_called,
            "analysis_status": case.expected.analysis_status,
            "analysis_attempts": case.expected.analysis_attempts,
            "analysis_elapsed_sec_lte": case.expected.analysis_elapsed_sec_lte,
            "context_reason": case.expected.context_reason,
            "context_chunk_count": case.expected.context_chunk_count,
            "missing_ranges": case.expected.missing_ranges,
        },
        "actual": actual,
    }


def _build_case_config(
    *,
    base_config: RealtimeInsightConfig,
    case: Mode6Case,
    check_interval_sec: float,
) -> RealtimeInsightConfig:
    config = replace(base_config)
    if case.config.request_timeout_sec is not None:
        config.request_timeout_sec = max(1.0, float(case.config.request_timeout_sec))
    if case.config.stage_timeout_sec is not None:
        config.stage_timeout_sec = max(1.0, float(case.config.stage_timeout_sec))
    if case.config.retry_count is not None:
        config.retry_count = max(0, int(case.config.retry_count))
    if case.config.context_recent_required is not None:
        config.context_recent_required = max(0, int(case.config.context_recent_required))
    if case.config.context_target_chunks is not None:
        config.context_target_chunks = max(1, int(case.config.context_target_chunks))

    wait1 = (
        float(case.config.context_wait_timeout_sec_1)
        if case.config.context_wait_timeout_sec_1 is not None
        else float(getattr(config, "context_wait_timeout_sec_1", 1.0))
    )
    wait2 = (
        float(case.config.context_wait_timeout_sec_2)
        if case.config.context_wait_timeout_sec_2 is not None
        else float(getattr(config, "context_wait_timeout_sec_2", 5.0))
    )
    config.context_wait_timeout_sec_1 = max(0.0, wait1)
    config.context_wait_timeout_sec_2 = max(0.0, wait2)
    config.context_wait_timeout_sec = max(config.context_wait_timeout_sec_1, config.context_wait_timeout_sec_2)
    config.context_check_interval_sec = max(0.01, float(check_interval_sec))
    config.use_dual_context_wait = True
    config.context_min_ready = 0
    return config


def _extract_missing_ranges(context_text: str) -> list[str]:
    if not context_text:
        return []
    return re.findall(r"\[missing seq=([0-9]+(?:-[0-9]+)?)\]", context_text)


def _evaluate_case(*, case: Mode6Case, actual: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    expected = case.expected
    if expected.stt_status and actual.get("stt_status") != expected.stt_status:
        failures.append(f"stt_status expected={expected.stt_status} actual={actual.get('stt_status')}")
    if expected.stt_attempts is not None and int(actual.get("stt_attempts", 0)) != int(expected.stt_attempts):
        failures.append(f"stt_attempts expected={expected.stt_attempts} actual={actual.get('stt_attempts')}")
    if expected.analysis_called is not None and bool(actual.get("analysis_called")) != bool(expected.analysis_called):
        failures.append(
            f"analysis_called expected={bool(expected.analysis_called)} actual={bool(actual.get('analysis_called'))}"
        )
    if expected.analysis_status and actual.get("analysis_status") != expected.analysis_status:
        failures.append(
            f"analysis_status expected={expected.analysis_status} actual={actual.get('analysis_status')}"
        )
    if expected.analysis_attempts is not None and int(actual.get("analysis_attempts", 0)) != int(
        expected.analysis_attempts
    ):
        failures.append(
            f"analysis_attempts expected={expected.analysis_attempts} actual={actual.get('analysis_attempts')}"
        )
    if expected.analysis_elapsed_sec_lte is not None and float(actual.get("analysis_elapsed_sec", 0.0)) > float(
        expected.analysis_elapsed_sec_lte
    ) + 1e-9:
        failures.append(
            "analysis_elapsed_sec_lte "
            f"expected<={expected.analysis_elapsed_sec_lte} actual={actual.get('analysis_elapsed_sec')}"
        )
    if expected.context_reason and actual.get("context_reason") != expected.context_reason:
        failures.append(f"context_reason expected={expected.context_reason} actual={actual.get('context_reason')}")
    if expected.context_chunk_count is not None and int(actual.get("context_chunk_count", 0)) != int(
        expected.context_chunk_count
    ):
        failures.append(
            f"context_chunk_count expected={expected.context_chunk_count} actual={actual.get('context_chunk_count')}"
        )
    if expected.missing_ranges is not None and list(actual.get("missing_ranges", [])) != list(expected.missing_ranges):
        failures.append(
            f"missing_ranges expected={expected.missing_ranges} actual={actual.get('missing_ranges', [])}"
        )
    return failures


_ORIG_STAGE_MONOTONIC = None
_ORIG_STAGE_SLEEP = None


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _patch_stage_processor_time(*, clock: _VirtualClock) -> None:
    global _ORIG_STAGE_MONOTONIC, _ORIG_STAGE_SLEEP
    _ORIG_STAGE_MONOTONIC = stage_processor_module.time.monotonic
    _ORIG_STAGE_SLEEP = stage_processor_module.time.sleep
    stage_processor_module.time.monotonic = clock.monotonic
    stage_processor_module.time.sleep = clock.sleep


def _unpatch_stage_processor_time() -> None:
    global _ORIG_STAGE_MONOTONIC, _ORIG_STAGE_SLEEP
    if _ORIG_STAGE_MONOTONIC is not None:
        stage_processor_module.time.monotonic = _ORIG_STAGE_MONOTONIC
    if _ORIG_STAGE_SLEEP is not None:
        stage_processor_module.time.sleep = _ORIG_STAGE_SLEEP
    _ORIG_STAGE_MONOTONIC = None
    _ORIG_STAGE_SLEEP = None
