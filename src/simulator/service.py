from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.common.account import resolve_openai_client_settings
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig, format_local_ts
from src.live.insight.openai_client import OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor
from src.simulator.cache_store import SimulationCacheStore
from src.simulator.mode1_validation import run_mode1_validation
from src.simulator.mode2_validation import run_mode2_validation
from src.simulator.mode_runner import run_mode
from src.simulator.models import (
    ALLOWED_MODE5_PROFILES,
    DEFAULT_MODE5_PROFILE,
    SimulateRuntimeConfig,
    SimulatorMode,
)
from src.simulator.precompute import run_precompute, write_precompute_manifest
from src.simulator.preprocessor import collect_input_mp3_files, preprocess_mp3_to_chunks
from src.simulator.scenario_loader import load_scenario


def run_simulate(args: argparse.Namespace) -> int:
    try:
        runtime = _build_runtime_config(args)
    except ValueError as exc:
        print(f"[simulate] invalid args: {exc}")
        return 1
    mode = runtime.mode

    scenario = load_scenario(runtime.scenario_file, expected_mode=mode)
    effective_seed = runtime.seed if runtime.seed is not None else scenario.seed

    run_session_dir = _build_run_session_dir(runtime.run_dir, scenario.name, mode)
    prepared_chunk_dir = run_session_dir / "_prepared_chunks"
    run_session_dir.mkdir(parents=True, exist_ok=True)

    if mode == SimulatorMode.MODE6:
        keywords = _load_keywords(runtime.rt_keywords_file)
        insight_config = RealtimeInsightConfig(
            enabled=True,
            chunk_seconds=max(2, int(runtime.chunk_seconds)),
            model=runtime.rt_model,
            stt_model=runtime.rt_stt_model,
            keywords_file=runtime.rt_keywords_file,
            stt_request_timeout_sec=max(1.0, float(runtime.rt_stt_request_timeout_sec)),
            stt_stage_timeout_sec=max(1.0, float(runtime.rt_stt_stage_timeout_sec)),
            stt_retry_count=max(0, int(runtime.rt_stt_retry_count)),
            stt_retry_interval_sec=max(0.0, float(runtime.rt_stt_retry_interval_sec)),
            analysis_request_timeout_sec=max(1.0, float(runtime.rt_analysis_request_timeout_sec)),
            analysis_stage_timeout_sec=max(1.0, float(runtime.rt_analysis_stage_timeout_sec)),
            analysis_retry_count=max(0, int(runtime.rt_analysis_retry_count)),
            analysis_retry_interval_sec=max(0.0, float(runtime.rt_analysis_retry_interval_sec)),
            context_target_chunks=18,
            context_min_ready=0,
            context_recent_required=max(0, int(runtime.rt_context_recent_required)),
            context_wait_timeout_sec=max(
                max(0.0, float(runtime.rt_context_wait_timeout_sec_1)),
                max(0.0, float(runtime.rt_context_wait_timeout_sec_2)),
            ),
            context_wait_timeout_sec_1=max(0.0, float(runtime.rt_context_wait_timeout_sec_1)),
            context_wait_timeout_sec_2=max(0.0, float(runtime.rt_context_wait_timeout_sec_2)),
            context_check_interval_sec=0.2,
            use_dual_context_wait=True,
        )
        processor = InsightStageProcessor(
            session_dir=run_session_dir,
            config=insight_config,
            keywords=keywords,
            client=None,
            log_fn=print,
        )
        cache_store = SimulationCacheStore(runtime.sim_root / "cache")
        try:
            result = run_mode(
                mode=mode,
                scenario=scenario,
                chunk_paths=[],
                chunk_seconds=runtime.chunk_seconds,
                processor=processor,
                cache_store=cache_store,
                client=None,
                keywords=keywords,
                stt_model=runtime.rt_stt_model,
                analysis_model=runtime.rt_model,
                stt_request_timeout_sec=runtime.rt_stt_request_timeout_sec,
                analysis_request_timeout_sec=runtime.rt_analysis_request_timeout_sec,
                precompute_workers=runtime.precompute_workers,
                output_dir=run_session_dir,
                log_fn=print,
                seed_override=effective_seed,
                mode5_profile=runtime.mode5_profile,
                mode5_target_seq=runtime.mode5_target_seq,
            )
        except Exception as exc:
            print(f"[simulate] mode run failed: {exc}")
            return 1

        report = {
            "mode": int(mode),
            "scenario": runtime.scenario_file.as_posix(),
            "scenario_name": scenario.name,
            "seed": effective_seed,
            "chunk_count": int(result.summary.get("case_count", 0)),
            "run_session_dir": run_session_dir.as_posix(),
            "summary": result.summary,
        }
        (run_session_dir / "simulate_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        fail_count = int(result.summary.get("fail_count", 0))
        if fail_count > 0:
            print(f"[simulate] mode6 failed: fail_count={fail_count}, output={run_session_dir}")
            return 1
        print(f"[simulate] completed mode={int(mode)} output={run_session_dir}")
        return 0

    mp3_files = collect_input_mp3_files(runtime.mp3_dir, scenario.dataset)
    if not mp3_files:
        print(f"[simulate] no mp3 file found in {runtime.mp3_dir}")
        return 1

    print(f"[simulate] preprocessing {len(mp3_files)} mp3 file(s) into {runtime.chunk_seconds}s chunks")
    try:
        chunk_paths = preprocess_mp3_to_chunks(
            input_files=mp3_files,
            output_dir=prepared_chunk_dir,
            chunk_seconds=runtime.chunk_seconds,
        )
    except Exception as exc:
        print(f"[simulate] preprocess failed: {exc}")
        return 1

    keywords = _load_keywords(runtime.rt_keywords_file)
    needs_openai = mode in {SimulatorMode.MODE4, SimulatorMode.MODE5}
    if mode == SimulatorMode.MODE1:
        needs_openai = scenario.mode1.runner == "online"
    if mode in {SimulatorMode.MODE2, SimulatorMode.MODE3}:
        needs_openai = True

    client: OpenAIInsightClient | None = None
    if needs_openai:
        client = _build_openai_client(runtime, required=True)
        if client is None:
            return 1

    insight_config = RealtimeInsightConfig(
        enabled=True,
        chunk_seconds=max(2, int(runtime.chunk_seconds)),
        model=runtime.rt_model,
        stt_model=runtime.rt_stt_model,
        keywords_file=runtime.rt_keywords_file,
        stt_request_timeout_sec=max(1.0, float(runtime.rt_stt_request_timeout_sec)),
        stt_stage_timeout_sec=max(1.0, float(runtime.rt_stt_stage_timeout_sec)),
        stt_retry_count=max(0, int(runtime.rt_stt_retry_count)),
        stt_retry_interval_sec=max(0.0, float(runtime.rt_stt_retry_interval_sec)),
        analysis_request_timeout_sec=max(1.0, float(runtime.rt_analysis_request_timeout_sec)),
        analysis_stage_timeout_sec=max(1.0, float(runtime.rt_analysis_stage_timeout_sec)),
        analysis_retry_count=max(0, int(runtime.rt_analysis_retry_count)),
        analysis_retry_interval_sec=max(0.0, float(runtime.rt_analysis_retry_interval_sec)),
        context_target_chunks=max(1, int(180 // max(1, runtime.chunk_seconds))),
        context_min_ready=0,
        context_recent_required=max(0, int(runtime.rt_context_recent_required)),
        context_wait_timeout_sec=max(
            max(0.0, float(runtime.rt_context_wait_timeout_sec_1)),
            max(0.0, float(runtime.rt_context_wait_timeout_sec_2)),
        ),
        context_wait_timeout_sec_1=max(0.0, float(runtime.rt_context_wait_timeout_sec_1)),
        context_wait_timeout_sec_2=max(0.0, float(runtime.rt_context_wait_timeout_sec_2)),
        context_check_interval_sec=0.2,
        use_dual_context_wait=True,
    )

    processor = InsightStageProcessor(
        session_dir=run_session_dir,
        config=insight_config,
        keywords=keywords,
        client=client,
        log_fn=print,
    )

    cache_store = SimulationCacheStore(runtime.sim_root / "cache")
    precompute_manifest: dict | None = None

    if mode in {SimulatorMode.MODE2, SimulatorMode.MODE3}:
        if client is None:
            print("[simulate] mode2/mode3 precompute requires OpenAI API key")
            return 1
        workers = max(1, int(scenario.precompute.workers or runtime.precompute_workers))
        print(f"[simulate] precompute mode{int(mode)} started workers={workers}")
        precompute_manifest = run_precompute(
            chunk_paths=chunk_paths,
            cache_store=cache_store,
            client=client,
            keywords=keywords,
            stt_model=runtime.rt_stt_model,
            analysis_model=runtime.rt_model,
            chunk_seconds=runtime.chunk_seconds,
            stt_request_timeout_sec=runtime.rt_stt_request_timeout_sec,
            analysis_request_timeout_sec=runtime.rt_analysis_request_timeout_sec,
            workers=workers,
            log_fn=print,
        )
        write_precompute_manifest(run_session_dir / "precompute_manifest.json", precompute_manifest)

    try:
        result = run_mode(
            mode=mode,
            scenario=scenario,
            chunk_paths=chunk_paths,
            chunk_seconds=runtime.chunk_seconds,
            processor=processor,
            cache_store=cache_store,
            client=client,
            keywords=keywords,
            stt_model=runtime.rt_stt_model,
            analysis_model=runtime.rt_model,
            stt_request_timeout_sec=runtime.rt_stt_request_timeout_sec,
            analysis_request_timeout_sec=runtime.rt_analysis_request_timeout_sec,
            precompute_workers=runtime.precompute_workers,
            output_dir=run_session_dir,
            log_fn=print,
            seed_override=effective_seed,
            mode5_profile=runtime.mode5_profile,
            mode5_target_seq=runtime.mode5_target_seq,
        )
    except Exception as exc:
        print(f"[simulate] mode run failed: {exc}")
        return 1

    report = {
        "mode": int(mode),
        "scenario": runtime.scenario_file.as_posix(),
        "scenario_name": scenario.name,
        "seed": effective_seed,
        "chunk_count": len(chunk_paths),
        "run_session_dir": run_session_dir.as_posix(),
        "summary": result.summary,
    }

    strict_failed = False
    if mode == SimulatorMode.MODE1:
        validation = run_mode1_validation(
            run_session_dir=run_session_dir,
            run_summary=result.summary,
            config=insight_config,
            validation_config=scenario.mode1.validation,
        )
        report["summary"]["mode1_validation"] = {
            "passed": bool(validation.get("passed", False)),
            "legal_passed": bool(validation.get("legal_passed", False)),
            "coverage_passed": bool(validation.get("coverage_passed", False)),
            "strict_fail": bool(validation.get("strict_fail", False)),
            "failure_count": int(validation.get("failure_count", 0)),
            "missing_branches": list(validation.get("missing_branches", [])),
            "report_file": str(validation.get("report_file", "")),
        }
        if not bool(validation.get("passed", False)):
            failures = validation.get("failures", [])
            if failures:
                print(f"[simulate] mode1 validation first failure: {failures[0]}")
            strict_failed = True

    if mode in {SimulatorMode.MODE2, SimulatorMode.MODE3} and (
        scenario.mode2_validation.strict_fail or scenario.mode2_validation.has_checks()
    ):
        validation = run_mode2_validation(
            scenario=scenario,
            run_summary=result.summary,
            precompute_manifest=precompute_manifest,
            run_session_dir=run_session_dir,
        )
        validation_key = "mode2_validation" if mode == SimulatorMode.MODE2 else "mode3_validation"
        report["summary"][validation_key] = {
            "strict_fail": bool(validation.get("strict_fail", False)),
            "passed": bool(validation.get("passed", False)),
            "failure_count": int(validation.get("failure_count", 0)),
            "report_file": str(validation.get("report_file", "")),
        }
        if not bool(validation.get("passed", False)) and bool(validation.get("strict_fail", False)):
            failures = validation.get("failures", [])
            if failures:
                print(f"[simulate] mode{int(mode)} validation first failure: {failures[0]}")
            strict_failed = True

    (run_session_dir / "simulate_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if strict_failed:
        print(f"[simulate] mode{int(mode)} validation failed (strict), output={run_session_dir}")
        return 1
    print(f"[simulate] completed mode={int(mode)} output={run_session_dir}")
    return 0


def _build_runtime_config(args: argparse.Namespace) -> SimulateRuntimeConfig:
    mode = SimulatorMode.from_int(int(args.mode))
    scenario_file = Path(args.scenario_file).expanduser().resolve()
    sim_root = Path(args.sim_root).expanduser().resolve()
    mp3_dir = Path(args.mp3_dir).expanduser().resolve()
    run_dir = Path(args.run_dir).expanduser().resolve()
    keywords_file = Path(args.rt_keywords_file).expanduser().resolve()

    mode5_profile = str(getattr(args, "mode5_profile", DEFAULT_MODE5_PROFILE) or DEFAULT_MODE5_PROFILE).strip()
    if mode5_profile not in ALLOWED_MODE5_PROFILES:
        raise ValueError(f"unsupported mode5 profile: {mode5_profile}")
    mode5_target_seq = getattr(args, "mode5_target_seq", None)
    if mode5_target_seq is not None:
        try:
            mode5_target_seq = int(mode5_target_seq)
        except (TypeError, ValueError) as exc:
            raise ValueError("mode5 target seq must be integer") from exc
        if mode5_target_seq <= 0:
            raise ValueError("mode5 target seq must be >= 1")
    if mode == SimulatorMode.MODE5 and mode5_profile == "single_chunk_dual" and mode5_target_seq is None:
        raise ValueError("mode5 target seq is required when mode5 profile is single_chunk_dual")
    if mode != SimulatorMode.MODE5 and mode5_target_seq is not None:
        raise ValueError("mode5 target seq is only valid when mode is 5")

    return SimulateRuntimeConfig(
        mode=mode,
        scenario_file=scenario_file,
        sim_root=sim_root,
        mp3_dir=mp3_dir,
        run_dir=run_dir,
        chunk_seconds=max(2, int(args.chunk_seconds)),
        precompute_workers=max(1, int(args.precompute_workers)),
        rt_model=(args.rt_model or "").strip() or "gpt-4.1-mini",
        rt_stt_model=(args.rt_stt_model or "").strip() or "whisper-large-v3",
        rt_keywords_file=keywords_file,
        rt_api_base_url=(args.rt_api_base_url or "").strip(),
        rt_stt_request_timeout_sec=max(1.0, float(args.rt_stt_request_timeout_sec)),
        rt_stt_stage_timeout_sec=max(1.0, float(args.rt_stt_stage_timeout_sec)),
        rt_stt_retry_count=max(0, int(args.rt_stt_retry_count)),
        rt_stt_retry_interval_sec=max(0.0, float(args.rt_stt_retry_interval_sec)),
        rt_analysis_request_timeout_sec=max(1.0, float(args.rt_analysis_request_timeout_sec)),
        rt_analysis_stage_timeout_sec=max(1.0, float(args.rt_analysis_stage_timeout_sec)),
        rt_analysis_retry_count=max(0, int(args.rt_analysis_retry_count)),
        rt_analysis_retry_interval_sec=max(0.0, float(args.rt_analysis_retry_interval_sec)),
        rt_context_recent_required=max(0, int(args.rt_context_recent_required)),
        rt_context_wait_timeout_sec_1=max(0.0, float(args.rt_context_wait_timeout_sec_1)),
        rt_context_wait_timeout_sec_2=max(0.0, float(args.rt_context_wait_timeout_sec_2)),
        seed=int(args.seed) if args.seed is not None else None,
        mode5_profile=mode5_profile,
        mode5_target_seq=mode5_target_seq,
    )


def _build_openai_client(
    runtime: SimulateRuntimeConfig,
    *,
    required: bool,
) -> OpenAIInsightClient | None:
    api_key, resolved_base_url, key_error = resolve_openai_client_settings(
        api_key_env_name="OPENAI_API_KEY",
        base_url_env_name="OPENAI_BASE_URL",
    )
    if not api_key:
        if required:
            print(f"[simulate] {key_error}")
        return None
    base_url = (runtime.rt_api_base_url or "").strip() or resolved_base_url
    try:
        if base_url:
            print(f"[simulate] using OpenAI-compatible base URL: {base_url}")
        return OpenAIInsightClient(
            api_key=api_key,
            timeout_sec=max(
                float(runtime.rt_stt_request_timeout_sec),
                float(runtime.rt_analysis_request_timeout_sec),
            ),
            base_url=base_url,
        )
    except Exception as exc:
        if required:
            print(f"[simulate] failed to initialize OpenAI client: {exc}")
        return None


def _build_run_session_dir(run_dir: Path, scenario_name: str, mode: SimulatorMode) -> Path:
    ts = format_local_ts(datetime.now().astimezone())
    folder = f"{scenario_name}_mode{int(mode)}_{ts}"
    return run_dir / folder


def _load_keywords(path: Path) -> KeywordConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        print(f"[simulate] keyword file not found/readable: {path}; using empty rules")
        return KeywordConfig()
    except json.JSONDecodeError:
        print(f"[simulate] keyword file is invalid JSON: {path}; using empty rules")
        return KeywordConfig()

    if not isinstance(payload, dict):
        print(f"[simulate] keyword file root is not object: {path}; using empty rules")
        return KeywordConfig()

    return KeywordConfig.from_json_dict(payload)
