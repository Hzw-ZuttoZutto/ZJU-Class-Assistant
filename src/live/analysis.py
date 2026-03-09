from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from src.auth.cas_client import ZJUAuthClient
from src.common.account import resolve_credentials, resolve_dingtalk_bot_settings
from src.common.course_meta import fetch_course_meta
from src.common.http import create_session
from src.live.insight import (
    DingTalkNotifier,
    DingTalkNotifierMetadata,
    RealtimeInsightConfig,
    RealtimeInsightService,
)
from src.live.insight.stream_pipeline import load_hotwords
from src.live.joiner import JoinRoomClient
from src.live.poller import StreamPoller
from src.live.recording.models import build_session_folder_name


class _NoopDingTalkNotifier:
    def notify_event(self, _event, **_kwargs) -> bool:
        return False

    def stop(self) -> None:
        return


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
    login_session = create_session(pool_size=8)

    try:
        token = auth.login_and_get_token(
            session=login_session,
            username=username,
            password=password,
            center_course_id=args.course_id,
            authcode=args.authcode,
        )
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    if not token:
        print("Login succeeded but token is empty; analysis mode cannot continue.", file=sys.stderr)
        return 1

    course_meta = fetch_course_meta(
        session=create_session(pool_size=8),
        token=token,
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
        token=token,
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

    poller = StreamPoller(
        session=create_session(pool_size=32),
        token=token,
        timeout=args.timeout,
        course_id=args.course_id,
        sub_id=args.sub_id,
        poll_interval=args.poll_interval,
        tenant_code=args.tenant_code,
    )

    dingtalk_enabled = bool(getattr(args, "rt_dingtalk_enabled", False))
    notifier = _NoopDingTalkNotifier()
    if dingtalk_enabled:
        webhook, secret, dingtalk_error = resolve_dingtalk_bot_settings()
        if dingtalk_error:
            print(f"Analysis failed: {dingtalk_error}", file=sys.stderr)
            return 1
        notifier = DingTalkNotifier(
            webhook=webhook,
            secret=secret,
            cooldown_sec=max(0.0, float(args.rt_dingtalk_cooldown_sec)),
            metadata=DingTalkNotifierMetadata(
                course_title=course_meta.title,
                teacher_name=course_meta.primary_teacher,
            ),
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
        dingtalk_send_timeout_sec=5.0,
        dingtalk_send_retry_count=5,
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
    else:
        print("Realtime DingTalk alert disabled.")
    print("Press Ctrl+C to stop.")

    poller.start()
    try:
        insight_service.start()
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        insight_service.stop()
        poller.stop()

    return 0


def _validate_analysis_args(args: argparse.Namespace) -> str:
    asr_model = (getattr(args, "rt_asr_model", None) or "").strip()
    if not asr_model:
        return "stream mode requires explicit --rt-asr-model"
    hotwords_file = Path(getattr(args, "rt_hotwords_file", "config/realtime_hotwords.json")).expanduser().resolve()
    try:
        _ = load_hotwords(hotwords_file, log_fn=lambda _msg: None)
    except ValueError as exc:
        return str(exc)
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
