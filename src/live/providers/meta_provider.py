from __future__ import annotations

from dataclasses import dataclass

import requests

from src.common.constants import API_BASE
from src.common.utils import parse_track_flag, to_int_or_none
from src.live.models import StreamInfo
from src.live.providers.base import ProviderFetchResult
from src.live.providers.common import request_json, to_raw_stream, to_stream_info


# @dataclass
class MetaStreamProvider:
    # session: requests.Session # 这个表示的是类型
    # token: str
    # timeout: int
    # course_id: int
    # sub_id: int
    def __init__(self,session,token,timeout,course_id,sub_id):
        self.session = session
        self.token = token
        self.timeout = timeout
        self.course_id = course_id
        self.sub_id = sub_id

    def fetch(self) -> ProviderFetchResult:
        screen_endpoint = f"{API_BASE}/courseapi/index.php/v2/meta/getscreenstream"
        rtc_endpoint = f"{API_BASE}/courseapi/v2/course-subject-rtc/get-stream"

        screen_body, screen_error = request_json(
            self.session,
            screen_endpoint,
            {
                "course_id": str(self.course_id),
                "sub_id": str(self.sub_id),
                "clear_cache": "1",
            },
            self.timeout,
            self.token,
        )
        rtc_body, rtc_error = request_json(
            self.session,
            rtc_endpoint,
            {
                "course_subject": str(self.sub_id),
                "course": str(self.course_id),
            },
            self.timeout,
            self.token,
        )

        # 如果screen_body 抓取失败, 返回http_error
        if screen_body is None:
            return ProviderFetchResult(
                provider="meta",
                success=False,
                result_err=None,
                result_err_msg="http_error",
                stream_infos=[],
                raw_streams=[],
                error=screen_error,
                diagnostics={"screen_error": screen_error, "rtc_error": rtc_error},
            )
        

        # 处理screen_body
        screen_result_obj = (screen_body.get("result") if isinstance(screen_body.get("result"), dict) else {})
        result_err = to_int_or_none(screen_result_obj.get("err"))
        result_err_msg = str(screen_result_obj.get("errMsg") or "")
        screen_data = (screen_result_obj.get("data") if isinstance(screen_result_obj.get("data"), list) else [])


        infos: list[StreamInfo] = []
        raw_streams: list[dict] = []
        by_stream_id: dict[str, StreamInfo] = {}

        for item in screen_data:
            if not isinstance(item, dict):
                continue

            info = to_stream_info(item, fallback_sub_id=str(self.sub_id))
            infos.append(info)
            raw_streams.append(to_raw_stream(info, source="meta_screen"))

            if info.stream_id:
                by_stream_id[info.stream_id] = info

        # 处理 rtc_data
        rtc_count = 0
        if rtc_body is not None:
            rtc_result_obj = rtc_body.get("result") if isinstance(rtc_body.get("result"), dict) else {}
            rtc_data = (
                rtc_result_obj.get("streams")
                if isinstance(rtc_result_obj.get("streams"), list)
                else []
            )
            rtc_count = len(rtc_data)

            for item in rtc_data:
                if not isinstance(item, dict):
                    continue

                stream_id = str(item.get("stream_id") or "")
                existing = by_stream_id.get(stream_id)
                if existing is not None:
                    rtc_video_track = item.get("video_track")
                    rtc_video_on = parse_track_flag(rtc_video_track)
                    if rtc_video_on is not None:
                        existing.video_track = rtc_video_track
                        existing.video_track_on = rtc_video_on

                    rtc_voice_track = item.get("voice_track")
                    rtc_voice_on = parse_track_flag(rtc_voice_track)
                    if rtc_voice_on is not None:
                        existing.voice_track = rtc_voice_track
                        existing.voice_track_on = rtc_voice_on
                    continue

                stream_play = ""
                if stream_id:
                    stream_play = f"webrtc://mcloudpush.cmc.zju.edu.cn/live/{stream_id}?vhost=video"

                info = to_stream_info(
                    item,
                    fallback_sub_id=str(self.sub_id),
                    stream_m3u8="",
                    stream_play=stream_play,
                )
                infos.append(info)
                raw_streams.append(to_raw_stream(info, source="meta_rtc"))
                if info.stream_id:
                    by_stream_id[info.stream_id] = info

        if rtc_error:
            if result_err_msg:
                result_err_msg += " | "
            result_err_msg += f"rtc_discovery_error: {rtc_error}"

        return ProviderFetchResult(
            provider="meta",
            success=bool(screen_body.get("success")) and result_err in {None, 0},
            result_err=result_err,
            result_err_msg=result_err_msg,
            stream_infos=infos,
            raw_streams=raw_streams,
            error="",
            diagnostics={
                "screen_count": len(screen_data),
                "rtc_count": rtc_count,
                "rtc_error": rtc_error,
            },
        )
