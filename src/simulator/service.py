from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.common.account import resolve_openai_api_key
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig, format_local_ts
from src.live.insight.openai_client import OpenAIInsightClient
from src.live.insight.stage_processor import InsightStageProcessor
from src.simulator.cache_store import SimulationCacheStore
from src.simulator.mode_runner import run_mode
from src.simulator.models import SimulateRuntimeConfig, SimulatorMode
from src.simulator.precompute import run_precompute, write_precompute_manifest
from src.simulator.preprocessor import collect_input_mp3_files, preprocess_mp3_to_chunks
from src.simulator.scenario_loader import load_scenario


def run_simulate(args: argparse.Namespace) -> int:
    runtime = _build_runtime_config(args)
    mode = runtime.mode

    scenario = load_scenario(runtime.scenario_file, expected_mode=mode)
    effective_seed = runtime.seed if runtime.seed is not None else scenario.seed

    run_session_dir = _build_run_session_dir(runtime.run_dir, scenario.name, mode)
    prepared_chunk_dir = run_session_dir / "_prepared_chunks"
    run_session_dir.mkdir(parents=True, exist_ok=True)

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
    needs_openai = mode in {SimulatorMode.MODE1, SimulatorMode.MODE4, SimulatorMode.MODE5}
    if mode in {SimulatorMode.MODE2, SimulatorMode.MODE3}:
        needs_openai = True

    client = _build_openai_client(runtime, required=needs_openai)
    if needs_openai and client is None:
        return 1

    insight_config = RealtimeInsightConfig(
        enabled=True,
        chunk_seconds=max(2, int(runtime.chunk_seconds)),
        model=runtime.rt_model,
        stt_model=runtime.rt_stt_model,
        keywords_file=runtime.rt_keywords_file,
        request_timeout_sec=max(2.0, float(runtime.rt_request_timeout_sec)),
        retry_count=max(0, int(runtime.rt_retry_count)),
        stage_timeout_sec=max(1.0, float(runtime.rt_stage_timeout_sec)),
        context_target_chunks=max(1, int(180 // max(1, runtime.chunk_seconds))),
        context_min_ready=0,
        context_recent_required=0,
        context_wait_timeout_sec=0.1,
    )

    processor = InsightStageProcessor(
        session_dir=run_session_dir,
        config=insight_config,
        keywords=keywords,
        client=client,
        log_fn=print,
    )

    cache_store = SimulationCacheStore(runtime.sim_root / "cache")

    if mode in {SimulatorMode.MODE2, SimulatorMode.MODE3}:
        if client is None:
            print("[simulate] mode2/mode3 precompute requires OpenAI API key")
            return 1
        workers = max(1, int(scenario.precompute.workers or runtime.precompute_workers))
        print(f"[simulate] precompute mode{int(mode)} started workers={workers}")
        manifest = run_precompute(
            chunk_paths=chunk_paths,
            cache_store=cache_store,
            client=client,
            keywords=keywords,
            stt_model=runtime.rt_stt_model,
            analysis_model=runtime.rt_model,
            chunk_seconds=runtime.chunk_seconds,
            request_timeout_sec=runtime.rt_request_timeout_sec,
            workers=workers,
            log_fn=print,
        )
        write_precompute_manifest(run_session_dir / "precompute_manifest.json", manifest)

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
            request_timeout_sec=runtime.rt_request_timeout_sec,
            precompute_workers=runtime.precompute_workers,
            output_dir=run_session_dir,
            log_fn=print,
            seed_override=effective_seed,
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
    (run_session_dir / "simulate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[simulate] completed mode={int(mode)} output={run_session_dir}")
    return 0


def _build_runtime_config(args: argparse.Namespace) -> SimulateRuntimeConfig:
    mode = SimulatorMode.from_int(int(args.mode))
    scenario_file = Path(args.scenario_file).expanduser().resolve()
    sim_root = Path(args.sim_root).expanduser().resolve()
    mp3_dir = Path(args.mp3_dir).expanduser().resolve()
    run_dir = Path(args.run_dir).expanduser().resolve()
    keywords_file = Path(args.rt_keywords_file).expanduser().resolve()

    return SimulateRuntimeConfig(
        mode=mode,
        scenario_file=scenario_file,
        sim_root=sim_root,
        mp3_dir=mp3_dir,
        run_dir=run_dir,
        chunk_seconds=max(2, int(args.chunk_seconds)),
        precompute_workers=max(1, int(args.precompute_workers)),
        rt_model=(args.rt_model or "").strip() or "gpt-5-mini",
        rt_stt_model=(args.rt_stt_model or "").strip() or "gpt-4o-mini-transcribe",
        rt_keywords_file=keywords_file,
        rt_request_timeout_sec=max(1.0, float(args.rt_request_timeout_sec)),
        rt_stage_timeout_sec=max(1.0, float(args.rt_stage_timeout_sec)),
        rt_retry_count=max(0, int(args.rt_retry_count)),
        seed=int(args.seed) if args.seed is not None else None,
    )


def _build_openai_client(
    runtime: SimulateRuntimeConfig,
    *,
    required: bool,
) -> OpenAIInsightClient | None:
    api_key, key_error = resolve_openai_api_key(env_name="OPENAI_API_KEY")
    if not api_key:
        if required:
            print(f"[simulate] {key_error}")
        return None
    try:
        return OpenAIInsightClient(api_key=api_key, timeout_sec=runtime.rt_request_timeout_sec)
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
