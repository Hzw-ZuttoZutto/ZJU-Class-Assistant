from __future__ import annotations

from urllib.parse import urlparse


def normalize_source_url(value: object) -> str:
    return str(value or "").strip()


def is_rtc_stream_url(value: object) -> bool:
    url = normalize_source_url(value)
    if not url:
        return False
    return (urlparse(url).scheme or "").strip().lower() in {"webrtc", "rtc"}


def _append_stream_audio_candidates(stream: object, candidates: list[str]) -> None:
    if stream is None:
        return

    rtc_url = normalize_source_url(getattr(stream, "stream_play", ""))
    if is_rtc_stream_url(rtc_url) and rtc_url not in candidates:
        candidates.append(rtc_url)

    hls_url = normalize_source_url(getattr(stream, "stream_m3u8", ""))
    if hls_url and hls_url not in candidates:
        candidates.append(hls_url)

    fallback_url = normalize_source_url(getattr(stream, "stream_play", ""))
    if fallback_url and fallback_url not in candidates:
        candidates.append(fallback_url)


def _append_stable_audio_candidates(stream: object, candidates: list[str]) -> None:
    if stream is None:
        return

    hls_url = normalize_source_url(getattr(stream, "stream_m3u8", ""))
    if hls_url and hls_url not in candidates:
        candidates.append(hls_url)

    fallback_url = normalize_source_url(getattr(stream, "stream_play", ""))
    if fallback_url and not is_rtc_stream_url(fallback_url) and fallback_url not in candidates:
        candidates.append(fallback_url)


def _snapshot_active_provider(snapshot) -> str:
    return str(getattr(snapshot, "active_provider", "") or "").strip().lower()


def list_teacher_audio_sources(snapshot) -> list[str]:
    if snapshot is None:
        return []

    streams = getattr(snapshot, "streams", None)
    if not isinstance(streams, dict):
        return []

    candidates: list[str] = []
    _append_stream_audio_candidates(streams.get("teacher"), candidates)
    _append_stream_audio_candidates(streams.get("class"), candidates)
    return candidates


def list_tingwu_audio_sources(snapshot) -> list[str]:
    if snapshot is None:
        return []

    streams = getattr(snapshot, "streams", None)
    if not isinstance(streams, dict):
        return []

    candidates: list[str] = []
    append_fn = _append_stream_audio_candidates
    if _snapshot_active_provider(snapshot) == "livingroom":
        append_fn = _append_stable_audio_candidates

    append_fn(streams.get("teacher"), candidates)
    append_fn(streams.get("class"), candidates)
    return candidates


def first_teacher_hls_source(snapshot) -> str:
    if snapshot is None:
        return ""
    streams = getattr(snapshot, "streams", None)
    if not isinstance(streams, dict):
        return ""
    stream = streams.get("teacher")
    if stream is None:
        return ""
    return normalize_source_url(getattr(stream, "stream_m3u8", ""))
