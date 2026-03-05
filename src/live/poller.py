from __future__ import annotations

import threading

import requests

from src.common.utils import now_utc_iso
from src.live.models import StreamInfo, WatchSnapshot
from src.live.providers import (
    LivingRoomStreamProvider,
    MetaStreamProvider,
    ProviderFetchResult,
)
from src.live_ppt import select_ppt_stream
from src.live_video import select_teacher_stream


class StreamPoller:
    def __init__(
        self,
        session: requests.Session,
        token: str,
        timeout: int,
        course_id: int,
        sub_id: int,
        poll_interval: float,
        tenant_code: str = "112",
    ) -> None:
        self.session = session
        self.token = token
        self.timeout = timeout
        self.course_id = course_id
        self.sub_id = sub_id
        self.tenant_code = tenant_code
        self.poll_interval = max(3.0, poll_interval)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stream-poller", daemon=True)

        self._poll_total = 0
        self._poll_failures = 0
        self._consecutive_poll_failures = 0

        self._meta_provider = MetaStreamProvider(
            session=self.session,
            token=self.token,
            timeout=self.timeout,
            course_id=self.course_id,
            sub_id=self.sub_id,
        )
        self._livingroom_provider = LivingRoomStreamProvider(
            session=self.session,
            token=self.token,
            timeout=self.timeout,
            course_id=self.course_id,
            sub_id=self.sub_id,
            tenant_code=self.tenant_code,
        )

        self._snapshot = WatchSnapshot(
            updated_at_utc=now_utc_iso(),
            success=False,
            result_err=None,
            result_err_msg="",
            stream_count=0,
            streams={},
            raw_streams=[],
            active_provider="",
            provider_diagnostics={},
            error="poller not started",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=3)

    def get_snapshot(self) -> WatchSnapshot:
        with self._lock:
            return self._snapshot

    def get_metrics(self) -> dict:
        with self._lock:
            return {
                "poll_total": self._poll_total,
                "poll_failures": self._poll_failures,
                "consecutive_poll_failures": self._consecutive_poll_failures,
                "last_updated_at_utc": self._snapshot.updated_at_utc,
                "last_error": self._snapshot.error,
                "active_provider": self._snapshot.active_provider,
            }

    def _set_snapshot(self, snapshot: WatchSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._poll_total += 1
            if snapshot.error:
                self._poll_failures += 1
                self._consecutive_poll_failures += 1
            else:
                self._consecutive_poll_failures = 0

    def _run(self) -> None:
        while not self._stop_event.is_set():
            snapshot = self._fetch_once()
            self._set_snapshot(snapshot)
            self._stop_event.wait(self.poll_interval)
    # 定时轮询上游接口
    def _fetch_once(self) -> WatchSnapshot:
        meta = self._meta_provider.fetch()
        provider_diag: dict[str, dict] = {"meta": self._provider_diag_dict(meta)}

        #如果meta是硬失败，直接返回失败快照
        if meta.result_err_msg == "http_error" and meta.error:
            return WatchSnapshot(
                updated_at_utc=now_utc_iso(),
                success=False,
                result_err=meta.result_err,
                result_err_msg=meta.result_err_msg,
                stream_count=0,
                streams={},
                raw_streams=[],
                active_provider="meta",
                provider_diagnostics=provider_diag,
                error=meta.error,
            )

        infos: list[StreamInfo] = list(meta.stream_infos)
        raw_streams: list[dict] = list(meta.raw_streams)

        livingroom: ProviderFetchResult | None = None
        if not self._has_playable_stream(infos):
            livingroom = self._livingroom_provider.fetch()
            provider_diag["livingroom"] = self._provider_diag_dict(livingroom)
            infos.extend(livingroom.stream_infos)
            raw_streams.extend(livingroom.raw_streams)

        first_by_type: dict[str, StreamInfo] = {}
        for info in infos:
            if info.type_name not in first_by_type:
                first_by_type[info.type_name] = info

        selected_teacher = select_teacher_stream(infos)
        selected_ppt = select_ppt_stream(infos)

        streams = dict(first_by_type)
        if selected_teacher is not None:
            streams["teacher"] = selected_teacher
        if selected_ppt is not None:
            streams["ppt"] = selected_ppt

        result_err = meta.result_err
        result_err_msg = meta.result_err_msg
        if livingroom is not None and livingroom.result_err_msg:
            if result_err_msg:
                result_err_msg += " | "
            result_err_msg += f"livingroom: {livingroom.result_err_msg}"

        error = ""
        if not streams:
            if livingroom is not None and livingroom.error:
                error = livingroom.error
            elif meta.error:
                error = meta.error
            else:
                error = "no playable stream discovered from providers"

        active_provider = self._detect_active_provider(streams, meta, livingroom)
        success = (meta.success or (livingroom.success if livingroom else False)) and not bool(error)

        return WatchSnapshot(
            updated_at_utc=now_utc_iso(),
            success=success,
            result_err=result_err,
            result_err_msg=result_err_msg,
            stream_count=len(raw_streams),
            streams=streams,
            raw_streams=raw_streams,
            active_provider=active_provider,
            provider_diagnostics=provider_diag,
            error=error,
        )

    @staticmethod
    def _provider_diag_dict(result: ProviderFetchResult) -> dict:
        return {
            "provider": result.provider,
            "success": result.success,
            "result_err": result.result_err,
            "result_err_msg": result.result_err_msg,
            "error": result.error,
            "stream_count": len(result.stream_infos),
            "diagnostics": result.diagnostics,
        }

    @staticmethod
    def _has_playable_stream(infos: list[StreamInfo]) -> bool:
        for info in infos:
            if info.stream_m3u8:
                return True
        return False

    @staticmethod
    def _detect_active_provider(
        streams: dict[str, StreamInfo],
        meta: ProviderFetchResult,
        livingroom: ProviderFetchResult | None,
    ) -> str:
        selected_urls = {s.stream_m3u8 for s in streams.values() if s.stream_m3u8}
        if livingroom is not None:
            livingroom_urls = {s.stream_m3u8 for s in livingroom.stream_infos if s.stream_m3u8}
            if selected_urls & livingroom_urls:
                return "livingroom"
        if selected_urls:
            return "meta"
        if livingroom is not None and livingroom.stream_infos:
            return "livingroom"
        if meta.stream_infos:
            return "meta"
        return ""
