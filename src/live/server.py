from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from src.auth.cas_client import ZJUAuthClient
from src.common.account import resolve_credentials, resolve_dingtalk_bot_settings
from src.common.constants import HLS_JS_CANDIDATE_URLS
from src.common.course_meta import fetch_course_meta
from src.common.http import create_session
from src.live.insight import (
    DingTalkNotifier,
    DingTalkNotifierMetadata,
    RealtimeInsightConfig,
    RealtimeInsightService,
)
from src.live.joiner import JoinRoomClient
from src.live.poller import StreamPoller
from src.live.proxy import ProxyEngine
from src.live.recording import LiveRecorderService, RecordingConfig, SessionMeta
from src.live.templates import render_index_html, render_player_html


def prepare_hls_js(timeout: int) -> str:
    for url in HLS_JS_CANDIDATE_URLS:
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            text = resp.text
            if "class Hls" in text or "function Hls" in text or "var Hls" in text:
                return text
        except requests.RequestException:
            continue
    return ""


class WatchRequestHandler(BaseHTTPRequestHandler):
    poller: StreamPoller
    proxy_engine: ProxyEngine
    course_id: int
    sub_id: int
    poll_interval: float
    hls_js: str
    hls_max_buffer: int

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            return self._write_html(
                render_index_html(self.course_id, self.sub_id, self.poll_interval)
            )

        if path == "/player":
            role = (q.get("role", ["teacher"])[0] or "teacher").strip().lower()
            if role not in {"teacher", "ppt"}:
                role = "teacher"
            return self._write_html(render_player_html(role, self.hls_max_buffer))

        if path == "/static/hls.min.js":
            if not self.hls_js:
                return self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "hls.js unavailable")
            return self._write_js(self.hls_js)

        if path == "/api/streams":
            snap = self.poller.get_snapshot()
            return self._write_json(snap.to_json_dict())

        if path == "/api/stream":
            role = (q.get("role", ["teacher"])[0] or "teacher").strip().lower()
            if role not in {"teacher", "ppt"}:
                role = "teacher"
            snap = self.poller.get_snapshot()
            stream = snap.streams.get(role)
            return self._write_json(
                {
                    "role": role,
                    "updated_at_utc": snap.updated_at_utc,
                    "success": snap.success,
                    "result_err": snap.result_err,
                    "result_err_msg": snap.result_err_msg,
                    "active_provider": snap.active_provider,
                    "provider_diagnostics": snap.provider_diagnostics,
                    "error": snap.error,
                    "stream": asdict(stream) if stream else None,
                }
            )

        if path == "/api/metrics":
            return self._write_json(
                {
                    "poller": self.poller.get_metrics(),
                    "proxy": self.proxy_engine.get_metrics(),
                }
            )

        if path == "/proxy/m3u8":
            role = (q.get("role", ["teacher"])[0] or "teacher").strip().lower()
            if role not in {"teacher", "ppt"}:
                role = "teacher"
            snap = self.poller.get_snapshot()
            stream = snap.streams.get(role)
            return self.proxy_engine.proxy_playlist(self, role=role, stream=stream)

        if path == "/proxy/asset":
            upstream = (q.get("u", [""])[0] or "").strip()
            return self.proxy_engine.proxy_asset(self, upstream)

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _write_html(self, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_js(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_watch(args: argparse.Namespace) -> int:
    username, password, cred_error = resolve_credentials(args.username, args.password)
    if cred_error:
        print(f"Credential error: {cred_error}", file=sys.stderr)
        return 1

    # 拉取ZJU统一身份认真登录页
    auth = ZJUAuthClient(timeout=args.timeout, tenant_code=args.tenant_code)
    login_session = create_session(pool_size=8)

    try:
        # 登录并获得token
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
        print("Login succeeded but token is empty; watch mode cannot continue.", file=sys.stderr)
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
            f"Watch failed: course metadata unavailable for course_id={args.course_id}; "
            "title/teacher are required for recording naming.",
            file=sys.stderr,
        )
        return 1

    # 进入直播房间？
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
                "Playback may still work if upstream stream is public.",
                file=sys.stderr,
            )

    # 每间隔args.poll_interval进行一次轮询，拉取上游流信息
    poller = StreamPoller(
        session=create_session(pool_size=32),
        token=token,
        timeout=args.timeout,
        course_id=args.course_id,
        sub_id=args.sub_id,
        poll_interval=args.poll_interval,
        tenant_code=args.tenant_code,
    )
    poller.start()
    recorder: LiveRecorderService | None = None
    insight_service: RealtimeInsightService | None = None
    server: ThreadingHTTPServer | None = None
    try:
        watch_started_at = datetime.now().astimezone()
        record_root = Path(args.record_dir).expanduser().resolve() if args.record_dir else Path.cwd()
        session_dir = LiveRecorderService.build_session_dir(
            record_dir=str(record_root),
            course_title=course_meta.title,
            teacher_name=course_meta.primary_teacher,
            started_at=watch_started_at,
        )
        recorder = LiveRecorderService(
            poller=poller,
            config=RecordingConfig(
                root_dir=record_root,
                segment_minutes=max(0, int(args.record_segment_minutes)),
                startup_av_timeout=max(1.0, float(args.record_startup_av_timeout)),
                recovery_window_sec=max(1.0, float(args.record_recovery_window_sec)),
            ),
            session_meta=SessionMeta(
                course_title=course_meta.title,
                teacher_name=course_meta.primary_teacher,
                watch_started_at=watch_started_at,
                session_dir=session_dir,
            ),
        )

        ok, check_error = recorder.startup_check(timeout_sec=max(1.0, args.record_startup_av_timeout))
        if not ok:
            print(f"Watch failed: startup AV check failed: {check_error}", file=sys.stderr)
            return 1

        print(
            f"Recording enabled: course={course_meta.title}, teacher={course_meta.primary_teacher}, "
            f"output_dir={session_dir}"
        )
        recorder.start()
        if args.rt_insight_enabled:
            pipeline_mode = str(getattr(args, "rt_pipeline_mode", "chunk") or "chunk").strip().lower() or "chunk"
            keywords_file = Path(args.rt_keywords_file).expanduser().resolve()
            chunk_seconds = max(2, int(args.rt_chunk_seconds))
            context_target_chunks = max(1, int(args.rt_context_window_seconds) // max(1, chunk_seconds))
            translation_targets = _parse_csv_values(getattr(args, "rt_translation_target_languages", "zh"))
            notifier = None
            dingtalk_enabled = bool(args.rt_dingtalk_enabled)
            if pipeline_mode == "stream" and not dingtalk_enabled:
                print(
                    "Watch failed: stream mode requires DingTalk alert; pass --rt-dingtalk-enabled and configure bot.",
                    file=sys.stderr,
                )
                return 1
            if dingtalk_enabled:
                webhook, secret, dingtalk_error = resolve_dingtalk_bot_settings()
                if dingtalk_error:
                    print(f"Watch failed: {dingtalk_error}", file=sys.stderr)
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
            insight_config = RealtimeInsightConfig(
                enabled=True,
                pipeline_mode=pipeline_mode,
                chunk_seconds=chunk_seconds,
                context_window_seconds=max(30, int(args.rt_context_window_seconds)),
                model=(args.rt_model or "").strip() or "gpt-4.1-mini",
                stt_model=(args.rt_stt_model or "").strip() or "whisper-large-v3",
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
                keywords_file=keywords_file,
                api_base_url=(args.rt_api_base_url or "").strip(),
                stt_request_timeout_sec=max(1.0, float(args.rt_stt_request_timeout_sec)),
                stt_stage_timeout_sec=max(1.0, float(args.rt_stt_stage_timeout_sec)),
                stt_retry_count=max(0, int(args.rt_stt_retry_count)),
                stt_retry_interval_sec=max(0.0, float(args.rt_stt_retry_interval_sec)),
                analysis_request_timeout_sec=max(1.0, float(args.rt_analysis_request_timeout_sec)),
                analysis_stage_timeout_sec=max(1.0, float(args.rt_analysis_stage_timeout_sec)),
                analysis_retry_count=max(0, int(args.rt_analysis_retry_count)),
                analysis_retry_interval_sec=max(0.0, float(args.rt_analysis_retry_interval_sec)),
                alert_threshold=max(0, min(100, int(args.rt_alert_threshold))),
                dingtalk_enabled=dingtalk_enabled,
                dingtalk_cooldown_sec=max(0.0, float(args.rt_dingtalk_cooldown_sec)),
                dingtalk_send_timeout_sec=5.0,
                dingtalk_send_retry_count=5,
                max_concurrency=max(1, int(args.rt_max_concurrency)),
                context_min_ready=max(0, int(args.rt_context_min_ready)),
                context_recent_required=max(0, int(args.rt_context_recent_required)),
                context_wait_timeout_sec_1=max(0.0, float(args.rt_context_wait_timeout_sec_1)),
                context_wait_timeout_sec_2=max(0.0, float(args.rt_context_wait_timeout_sec_2)),
                context_wait_timeout_sec=max(
                    max(0.0, float(args.rt_context_wait_timeout_sec_1)),
                    max(0.0, float(args.rt_context_wait_timeout_sec_2)),
                ),
                use_dual_context_wait=True,
                context_target_chunks=(
                    max(1, int(getattr(args, "rt_window_sentences", 8)))
                    if pipeline_mode == "stream"
                    else max(1, context_target_chunks)
                ),
            )
            insight_service = RealtimeInsightService(
                poller=poller,
                session_dir=session_dir,
                config=insight_config,
                notifier=notifier,
            )
            if pipeline_mode == "stream":
                print(
                    "Realtime insight enabled(stream): "
                    f"asr_scene={insight_config.asr_scene}, asr_model={insight_config.asr_model or '(auto)'}, "
                    f"analysis_model={insight_config.model}, window_sentences={insight_config.window_sentences}, "
                    f"analysis_workers={insight_config.stream_analysis_workers}, "
                    f"queue_size={insight_config.stream_queue_size}, hotwords={insight_config.hotwords_file}"
                )
            else:
                print(
                    "Realtime insight enabled(chunk): "
                    f"stt_model={insight_config.stt_model}, analysis_model={insight_config.model}, "
                    f"chunk={insight_config.chunk_seconds}s, context_chunks={insight_config.context_target_chunks}, "
                    f"workers={insight_config.max_concurrency}, keywords={keywords_file}"
                )
            if insight_config.dingtalk_enabled:
                print(
                    "Realtime DingTalk alert enabled: "
                    f"cooldown={insight_config.dingtalk_cooldown_sec:.1f}s"
                )
            insight_service.start()

        proxy_engine = ProxyEngine(
            session=create_session(pool_size=64),
            upstream_timeout=args.timeout,
            playlist_retries=args.playlist_retries,
            asset_retries=args.asset_retries,
            stale_playlist_grace=args.stale_playlist_grace,
        )

        hls_js = prepare_hls_js(timeout=args.timeout)
        if not hls_js:
            print(
                "Warning: failed to preload hls.js. "
                "Chrome/Edge may not play HLS unless native support is available.",
                file=sys.stderr,
            )

        handler_cls = type(
            "WatchHandler",
            (WatchRequestHandler,),
            {
                "poller": poller,
                "proxy_engine": proxy_engine,
                "course_id": args.course_id,
                "sub_id": args.sub_id,
                "poll_interval": max(3.0, args.poll_interval),
                "hls_js": hls_js,
                "hls_max_buffer": args.hls_max_buffer,
            },
        )

        server = ThreadingHTTPServer((args.host, args.port), handler_cls)
        base_url = f"http://{args.host}:{args.port}"
        open_base_url = args.open_base_url.strip() if args.open_base_url else base_url

        print(f"Watch server started at: {base_url}")
        print(f"Teacher player: {open_base_url}/player?role=teacher")
        print(f"PPT player:     {open_base_url}/player?role=ppt")
        print(f"Metrics API:    {open_base_url}/api/metrics")
        print("Press Ctrl+C to stop.")

        if not args.no_browser:
            try:
                webbrowser.open(f"{open_base_url}/player?role=teacher")
                time.sleep(0.15)
                webbrowser.open(f"{open_base_url}/player?role=ppt")
                webbrowser.open(open_base_url)
            except Exception as exc:
                print(f"Open browser failed: {exc}", file=sys.stderr)

        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
    finally:
        if server is not None:
            server.server_close()
        if insight_service is not None:
            insight_service.stop()
        if recorder is not None:
            recorder.stop()
        poller.stop()

    return 0


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
