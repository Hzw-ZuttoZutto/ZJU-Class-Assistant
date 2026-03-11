from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from src.auth import LoginTokenManager
from src.auth.cas_client import ZJUAuthClient
from src.common.account import resolve_credentials, resolve_dingtalk_bot_settings
from src.common.course_meta import fetch_course_meta
from src.common.http import create_session, get_thread_session
from src.live.insight import (
    AnalysisRuntimeObserver,
    DingTalkNotifier,
    DingTalkNotifierMetadata,
    RealtimeInsightConfig,
    RealtimeInsightService,
)
from src.live.insight.stream_pipeline import load_hotwords
from src.live.joiner import JoinRoomClient
from src.live.poller import StreamPoller
from src.live.recording.models import build_session_folder_name
from src.live.tingwu import (
    AudioOnlyRecorderService,
    AudioRecordingConfig,
    AudioSessionMeta,
    validate_tingwu_local_requirements,
)

_RUNTIME_ENABLE_DATA_STALL_ALERT = False
_RUNTIME_DATA_STALL_THRESHOLD_SEC = 60.0


def run_analysis(args: argparse.Namespace) -> int:
    validation_error = _validate_analysis_args(args)
    if validation_error:
        print(f"Analysis failed: {validation_error}", file=sys.stderr)
        return 1

    username, password, cred_error = resolve_credentials(args.username, args.password)
    if cred_error:
        print(f"Credential error: {cred_error}", file=sys.stderr)
        return 1

    auth = ZJUAuthClient(timeout=args.timeout, tenant_code=args.tenant_code)
    token_manager = LoginTokenManager(
        auth_client=auth,
        username=username,
        password=password,
        center_course_id=args.course_id,
        authcode=args.authcode,
        refresh_cooldown_sec=30.0,
        session_factory=lambda: create_session(pool_size=8),
    )

    try:
        ok, refresh_error = token_manager.refresh("initial_login", force=True)
        if not ok:
            raise RuntimeError(refresh_error or "token refresh failed")
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    token = token_manager.get_token()
    if not token:
        print("Login succeeded but token is empty; analysis mode cannot continue.", file=sys.stderr)
        return 1

    course_meta = fetch_course_meta(
        session=create_session(pool_size=8),
        token=token_manager.get_token(),
        timeout=args.timeout,
        course_id=args.course_id,
        retries=1,
    )
    if course_meta is None:
        print(
            f"Analysis failed: course metadata unavailable for course_id={args.course_id}; "
            "title/teacher are required for session naming.",
            file=sys.stderr,
        )
        return 1

    join_result = JoinRoomClient(
        session=create_session(pool_size=8),
        token=token_manager.get_token(),
        timeout=args.timeout,
        sub_id=args.sub_id,
        user_id=username,
        realname=username,
    ).try_join()
    if join_result.attempted:
        if join_result.success:
            print(f"Join room ok (stream_id={join_result.stream_id}).")
        else:
            print(
                f"Warning: join room failed ({join_result.message}). "
                "Analysis may still work if upstream stream is public.",
                file=sys.stderr,
            )

    session_started_at = datetime.now().astimezone()
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else Path.cwd()
    session_dir = output_root / build_session_folder_name(
        course_title=course_meta.title,
        teacher_name=course_meta.primary_teacher,
        started_at=session_started_at,
    )
    session_dir.mkdir(parents=True, exist_ok=True)

    poller = StreamPoller(
        session=create_session(pool_size=32),
        token=token_manager.get_token(),
        timeout=args.timeout,
        course_id=args.course_id,
        sub_id=args.sub_id,
        poll_interval=args.poll_interval,
        tenant_code=args.tenant_code,
        token_provider=token_manager.get_token,
        token_refresher=token_manager.refresh,
    )

    dingtalk_enabled = bool(getattr(args, "rt_dingtalk_enabled", False))
    dingtalk_queue_size = max(1, int(getattr(args, "rt_dingtalk_queue_size", 500)))
    webhook, secret, dingtalk_error = resolve_dingtalk_bot_settings()
    if dingtalk_error:
        print(f"Analysis failed: {dingtalk_error}", file=sys.stderr)
        return 1
    log_rotate_max_bytes = max(1024 * 1024, int(getattr(args, "rt_log_rotate_max_bytes", 64 * 1024 * 1024)))
    log_rotate_backup_count = max(1, int(getattr(args, "rt_log_rotate_backup_count", 20)))
    notifier_metadata = DingTalkNotifierMetadata(
        course_title=course_meta.title,
        teacher_name=course_meta.primary_teacher,
    )
    notifier = DingTalkNotifier(
        webhook=webhook,
        secret=secret,
        cooldown_sec=max(0.0, float(args.rt_dingtalk_cooldown_sec)),
        queue_size=dingtalk_queue_size,
        metadata=notifier_metadata,
        trace_path=session_dir / "realtime_dingtalk_trace.jsonl",
        log_rotate_max_bytes=log_rotate_max_bytes,
        log_rotate_backup_count=log_rotate_backup_count,
        log_fn=print,
    )
    runtime_notifier = DingTalkNotifier(
        webhook=webhook,
        secret=secret,
        cooldown_sec=0.0,
        queue_size=dingtalk_queue_size,
        metadata=notifier_metadata,
        trace_path=session_dir / "realtime_runtime_dingtalk_trace.jsonl",
        log_rotate_max_bytes=log_rotate_max_bytes,
        log_rotate_backup_count=log_rotate_backup_count,
        log_fn=print,
    )

    asr_scene = str(getattr(args, "rt_asr_scene", "zh") or "zh").strip().lower() or "zh"
    asr_model = (getattr(args, "rt_asr_model", "") or "").strip()
    translation_targets = _parse_csv_values(getattr(args, "rt_translation_target_languages", "zh"))
    insight_config = RealtimeInsightConfig(
        enabled=True,
        pipeline_mode="stream",
        chunk_seconds=0.0,
        context_window_seconds=0,
        model=(args.rt_model or "").strip() or "gpt-4.1-mini",
        stt_model="",
        asr_scene=asr_scene,
        asr_model=asr_model,
        hotwords_file=Path(getattr(args, "rt_hotwords_file", "config/realtime_hotwords.json"))
        .expanduser()
        .resolve(),
        window_sentences=max(1, int(getattr(args, "rt_window_sentences", 8))),
        stream_analysis_workers=max(1, int(getattr(args, "rt_stream_analysis_workers", 32))),
        stream_queue_size=max(1, int(getattr(args, "rt_stream_queue_size", 100))),
        asr_endpoint=(getattr(args, "rt_asr_endpoint", "") or "").strip()
        or "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        translation_target_languages=translation_targets,
        keywords_file=Path(args.rt_keywords_file).expanduser().resolve(),
        api_base_url=(args.rt_api_base_url or "").strip(),
        stt_request_timeout_sec=8.0,
        stt_stage_timeout_sec=32.0,
        stt_retry_count=0,
        stt_retry_interval_sec=0.0,
        analysis_request_timeout_sec=max(1.0, float(args.rt_analysis_request_timeout_sec)),
        analysis_stage_timeout_sec=max(1.0, float(args.rt_analysis_stage_timeout_sec)),
        analysis_retry_count=max(0, int(args.rt_analysis_retry_count)),
        analysis_retry_interval_sec=max(0.0, float(args.rt_analysis_retry_interval_sec)),
        alert_threshold=max(0, min(100, int(args.rt_alert_threshold))),
        dingtalk_enabled=dingtalk_enabled,
        dingtalk_cooldown_sec=max(0.0, float(args.rt_dingtalk_cooldown_sec)),
        dingtalk_queue_size=dingtalk_queue_size,
        dingtalk_send_timeout_sec=5.0,
        dingtalk_send_retry_count=5,
        log_rotate_max_bytes=log_rotate_max_bytes,
        log_rotate_backup_count=log_rotate_backup_count,
        max_concurrency=1,
        context_min_ready=0,
        context_recent_required=max(0, int(args.rt_context_recent_required)),
        context_wait_timeout_sec_1=max(0.0, float(args.rt_context_wait_timeout_sec_1)),
        context_wait_timeout_sec_2=max(0.0, float(args.rt_context_wait_timeout_sec_2)),
        context_wait_timeout_sec=max(
            max(0.0, float(args.rt_context_wait_timeout_sec_1)),
            max(0.0, float(args.rt_context_wait_timeout_sec_2)),
        ),
        use_dual_context_wait=True,
        context_target_chunks=max(1, int(getattr(args, "rt_window_sentences", 8))),
    )

    insight_service = RealtimeInsightService(
        poller=poller,
        session_dir=session_dir,
        config=insight_config,
        notifier=notifier,
    )
    runtime_observer = AnalysisRuntimeObserver(
        session_dir=session_dir,
        notifier=runtime_notifier,
        heartbeat_interval_sec=10.0,
        p0_cooldown_sec=15.0,
        p1_cooldown_sec=45.0,
        enable_data_stall_alert=_RUNTIME_ENABLE_DATA_STALL_ALERT,
        data_stall_threshold_sec=_RUNTIME_DATA_STALL_THRESHOLD_SEC,
        reconnect_p1_threshold_sec=20.0,
        reconnect_p0_threshold_sec=60.0,
        log_rotate_max_bytes=log_rotate_max_bytes,
        log_rotate_backup_count=log_rotate_backup_count,
        log_fn=print,
    )

    def _collect_runtime_snapshot() -> dict[str, object]:
        poller_metrics: dict[str, object] = {}
        poller_metrics_getter = getattr(poller, "get_metrics", None)
        if callable(poller_metrics_getter):
            try:
                payload = poller_metrics_getter()
            except Exception as exc:
                print(f"[analysis][runtime] poller metrics failed: {exc}", file=sys.stderr)
                payload = {}
            if isinstance(payload, dict):
                poller_metrics = payload

        snapshot: dict[str, object] = {
            "poller_running": bool(poller.is_running()),
            "insight_running": bool(insight_service.is_running()),
            "poller_metrics": poller_metrics,
            "stream_metrics": {},
            "stage_metrics": {},
        }
        runtime_getter = getattr(insight_service, "get_runtime_snapshot", None)
        if callable(runtime_getter):
            try:
                payload = runtime_getter()
            except Exception as exc:
                print(f"[analysis][runtime] snapshot failed: {exc}", file=sys.stderr)
                payload = {}
            if isinstance(payload, dict):
                stream_metrics = payload.get("stream_metrics")
                stage_metrics = payload.get("stage_metrics")
                service_running = payload.get("service_running")
                if isinstance(stream_metrics, dict):
                    snapshot["stream_metrics"] = stream_metrics
                if isinstance(stage_metrics, dict):
                    snapshot["stage_metrics"] = stage_metrics
                if isinstance(service_running, bool):
                    snapshot["insight_running"] = bool(service_running)
        return snapshot

    print(
        "Analysis started(stream): "
        f"course={course_meta.title}, teacher={course_meta.primary_teacher}, "
        f"session_dir={session_dir}"
    )
    print(
        "Realtime insight(stream): "
        f"asr_scene={insight_config.asr_scene}, asr_model={insight_config.asr_model}, "
        f"analysis_model={insight_config.model}, window_sentences={insight_config.window_sentences}, "
        f"analysis_workers={insight_config.stream_analysis_workers}, "
        f"queue_size={insight_config.stream_queue_size}, hotwords={insight_config.hotwords_file}"
    )
    if insight_config.dingtalk_enabled:
        print(
            "Realtime DingTalk alert enabled: "
            f"cooldown={insight_config.dingtalk_cooldown_sec:.1f}s"
        )
    data_stall_status = (
        f"data_stall_threshold={_RUNTIME_DATA_STALL_THRESHOLD_SEC:.0f}s"
        if _RUNTIME_ENABLE_DATA_STALL_ALERT
        else "data_stall=off"
    )
    print(
        "Realtime runtime monitor enabled: "
        f"heartbeat=10s, alert_cooldown(P0=15s,P1=45s), "
        f"{data_stall_status}"
    )
    print("Press Ctrl+C to stop.")

    tingwu_enabled = bool(getattr(args, "tingwu_enabled", False))
    recorder: AudioOnlyRecorderService | None = None
    tingwu_audio_file: Path | None = None

    poller.start()
    watchdog_base_sec = 1.0
    watchdog_max_sec = 30.0
    watchdog_backoff_sec = watchdog_base_sec
    watchdog_next_retry_at = 0.0
    try:
        if tingwu_enabled:
            recorder = AudioOnlyRecorderService(
                poller=poller,
                config=AudioRecordingConfig(poll_interval_sec=1.0, max_lag_sec=10.0),
                session_meta=AudioSessionMeta(
                    course_title=course_meta.title,
                    teacher_name=course_meta.primary_teacher,
                    session_dir=session_dir,
                    started_at=session_started_at,
                ),
                log_fn=print,
            )
            ok, check_error = recorder.startup_check(timeout_sec=20.0)
            if not ok:
                print(f"Analysis failed: Tingwu audio startup check failed: {check_error}", file=sys.stderr)
                return 1
            recorder.start()
            print("[analysis] Tingwu audio recorder enabled")

        insight_service.start()
        while True:
            time.sleep(0.5)
            runtime_snapshot = _collect_runtime_snapshot()
            runtime_observer.observe(runtime_snapshot)
            poller_running = bool(runtime_snapshot.get("poller_running", False))
            insight_running = bool(runtime_snapshot.get("insight_running", False))
            if poller_running and insight_running:
                watchdog_backoff_sec = watchdog_base_sec
                watchdog_next_retry_at = 0.0
                continue

            now = time.monotonic()
            if now < watchdog_next_retry_at:
                continue

            restart_ok = True
            if not poller_running:
                print("[analysis][watchdog] poller thread stopped; restarting")
                try:
                    poller.start()
                except Exception as exc:
                    restart_ok = False
                    print(f"[analysis][watchdog] poller restart failed: {exc}", file=sys.stderr)
                    runtime_observer.notify_watchdog_restart_failed(
                        component="poller",
                        error=str(exc),
                        snapshot=_collect_runtime_snapshot(),
                    )

            if not insight_running:
                print("[analysis][watchdog] insight thread stopped; restarting")
                try:
                    insight_service.start()
                except Exception as exc:
                    restart_ok = False
                    print(f"[analysis][watchdog] insight restart failed: {exc}", file=sys.stderr)
                    runtime_observer.notify_watchdog_restart_failed(
                        component="insight",
                        error=str(exc),
                        snapshot=_collect_runtime_snapshot(),
                    )

            if restart_ok and poller.is_running() and insight_service.is_running():
                watchdog_backoff_sec = watchdog_base_sec
                watchdog_next_retry_at = 0.0
                continue

            print(
                f"[analysis][watchdog] recovery pending; retry in {watchdog_backoff_sec:.1f}s",
                file=sys.stderr,
            )
            runtime_observer.notify_watchdog_recovery_pending(
                retry_in_sec=watchdog_backoff_sec,
                snapshot=_collect_runtime_snapshot(),
            )
            watchdog_next_retry_at = now + watchdog_backoff_sec
            watchdog_backoff_sec = min(watchdog_max_sec, watchdog_backoff_sec * 2.0)
    except KeyboardInterrupt:
        pass
    finally:
        runtime_observer.close()
        insight_service.stop()
        if recorder is not None:
            result = recorder.stop()
            if result.success and result.final_mp3_path is not None:
                tingwu_audio_file = result.final_mp3_path
                print(f"[analysis] Tingwu audio finalized: {tingwu_audio_file}")
            else:
                message = f"Tingwu audio recording failed: {result.error} report={result.report_path}"
                print(f"[analysis] {message}", file=sys.stderr)
                _send_markdown_status(
                    webhook=webhook,
                    secret=secret,
                    title="analysis 听悟录音失败",
                    text="\n".join(
                        [
                            "# analysis 听悟录音失败",
                            "",
                            f"- 课程：{course_meta.title} | {course_meta.primary_teacher}",
                            f"- 错误：{result.error}",
                            f"- 报告：{result.report_path}",
                        ]
                    ),
                )
        poller.stop()

    if tingwu_enabled and tingwu_audio_file is not None:
        job_path = session_dir / "tingwu_job.json"
        job_payload = {
            "version": "v1",
            "created_at_iso": datetime.now().astimezone().isoformat(),
            "session_dir": str(session_dir),
            "audio_file": str(tingwu_audio_file),
            "course_title": course_meta.title,
            "teacher_name": course_meta.primary_teacher,
            "started_at_iso": session_started_at.isoformat(),
            "poll_interval_sec": max(5.0, float(getattr(args, "tingwu_poll_interval_sec", 30.0))),
            "max_wait_hours": max(0.5, float(getattr(args, "tingwu_max_wait_hours", 6.0))),
        }
        job_path.write_text(json.dumps(job_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "src.main",
            "tingwu-process",
            "--job-file",
            str(job_path),
        ]
        try:
            subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"[analysis] Tingwu worker launched: job_file={job_path}")
        except Exception as exc:
            error_text = f"launch tingwu worker failed: {exc}"
            print(f"[analysis] {error_text}", file=sys.stderr)
            _send_markdown_status(
                webhook=webhook,
                secret=secret,
                title="analysis 听悟子进程拉起失败",
                text="\n".join(
                    [
                        "# analysis 听悟子进程拉起失败",
                        "",
                        f"- 课程：{course_meta.title} | {course_meta.primary_teacher}",
                        f"- 错误：{error_text}",
                        f"- Job文件：{job_path}",
                    ]
                ),
            )

    return 0


def _validate_analysis_args(args: argparse.Namespace) -> str:
    if not bool(getattr(args, "rt_dingtalk_enabled", False)):
        return "analysis mode requires --rt-dingtalk-enabled and valid DingTalk bot settings"
    asr_model = (getattr(args, "rt_asr_model", None) or "").strip()
    if not asr_model:
        return "stream mode requires explicit --rt-asr-model"
    hotwords_file = Path(getattr(args, "rt_hotwords_file", "config/realtime_hotwords.json")).expanduser().resolve()
    try:
        _ = load_hotwords(hotwords_file, log_fn=lambda _msg: None)
    except ValueError as exc:
        return str(exc)
    rotate_max_bytes = int(getattr(args, "rt_log_rotate_max_bytes", 64 * 1024 * 1024))
    rotate_backup_count = int(getattr(args, "rt_log_rotate_backup_count", 20))
    if rotate_max_bytes < 1024 * 1024:
        return "--rt-log-rotate-max-bytes must be >= 1048576"
    if rotate_backup_count < 1:
        return "--rt-log-rotate-backup-count must be >= 1"
    dingtalk_queue_size = int(getattr(args, "rt_dingtalk_queue_size", 500))
    if dingtalk_queue_size < 1:
        return "--rt-dingtalk-queue-size must be >= 1"
    if bool(getattr(args, "tingwu_enabled", False)):
        poll_interval = float(getattr(args, "tingwu_poll_interval_sec", 30.0))
        max_wait_hours = float(getattr(args, "tingwu_max_wait_hours", 6.0))
        if poll_interval < 5.0:
            return "--tingwu-poll-interval-sec must be >= 5"
        if max_wait_hours <= 0:
            return "--tingwu-max-wait-hours must be > 0"
        local_err = validate_tingwu_local_requirements()
        if local_err:
            return local_err
    return ""


def _parse_csv_values(raw: object) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return ["zh"]
    out: list[str] = []
    for item in text.split(","):
        value = str(item or "").strip()
        if value:
            out.append(value)
    return out or ["zh"]


def _send_markdown_status(*, webhook: str, secret: str, title: str, text: str) -> None:
    url = (webhook or "").strip()
    sec = (secret or "").strip()
    if not url or not sec:
        return
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": str(title or "").strip() or "analysis 通知",
            "text": str(text or "").strip() or "analysis 通知",
        },
    }
    for attempt in range(1, 4):
        try:
            ts_ms = int(time.time() * 1000)
            to_sign = f"{ts_ms}\n{sec}"
            digest = hmac.new(sec.encode("utf-8"), to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
            sep = "&" if "?" in url else "?"
            signed_url = f"{url}{sep}timestamp={ts_ms}&sign={sign}"
            resp = get_thread_session(pool_size=4).post(signed_url, json=payload, timeout=5.0)
            resp.raise_for_status()
            body = resp.json()
            if int(body.get("errcode", -1)) != 0:
                raise RuntimeError(f"errcode={body.get('errcode')} errmsg={body.get('errmsg', '')}")
            return
        except Exception:
            if attempt >= 3:
                return
            time.sleep(min(3.0, attempt * 0.5))
