from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from src.common.constants import API_BASE

COURSE_DETAIL_URL = "https://classroom.zju.edu.cn/coursedetail"
LIVE_TEXT = "直播中"


@dataclass
class LiveCheckResult:
    course_id: int
    is_live: bool
    checked: bool
    attempts: int
    elapsed_sec: float
    last_error: str
    hint: str
    sub_id: str = ""


def auth_headers(token: str) -> dict[str, str]:
    headers = {"Accept-Language": "zh_cn"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def contains_live_text(value: Any) -> bool:
    if isinstance(value, str):
        return LIVE_TEXT in value
    if isinstance(value, dict):
        return any(contains_live_text(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_live_text(v) for v in value)
    return False


def _looks_like_live_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if "list" in payload and isinstance(payload.get("list"), list):
        return True
    if "code" in payload and "msg" in payload:
        return True
    return False


def _normalize_sub_id(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text


def _extract_any_sub_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    list_obj = payload.get("list")
    if not isinstance(list_obj, list):
        return ""
    for item in list_obj:
        if not isinstance(item, dict):
            continue
        sub_id = _normalize_sub_id(item.get("sub_id"))
        if sub_id:
            return sub_id
    return ""


def _extract_live_sub_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    list_obj = payload.get("list")
    if not isinstance(list_obj, list):
        return ""
    for item in list_obj:
        if not isinstance(item, dict):
            continue
        if not contains_live_text(item):
            continue
        sub_id = _normalize_sub_id(item.get("sub_id"))
        if sub_id:
            return sub_id
    return ""


def check_course_live_status(
    session: requests.Session,
    token: str,
    timeout: int,
    tenant_code: str,
    course_id: int,
    max_wait_sec: float,
    interval_sec: float,
) -> LiveCheckResult:
    started = time.monotonic()
    attempts = 0
    last_error = ""
    max_wait_sec = max(0.0, float(max_wait_sec))
    interval_sec = max(0.0, float(interval_sec))

    while True:
        attempts += 1
        static_live_hit = False
        try:
            detail_resp = session.get(
                COURSE_DETAIL_URL,
                params={"course_id": str(course_id), "tenant_code": str(tenant_code)},
                timeout=timeout,
            )
            detail_resp.raise_for_status()
            detail_resp.encoding = "utf-8"
            detail_html = detail_resp.text
            if LIVE_TEXT in detail_html:
                static_live_hit = True
        except requests.RequestException as exc:
            last_error = f"coursedetail_http_error: {exc}"

        try:
            live_resp = session.get(
                f"{API_BASE}/courseapi/v2/course-live/search-live-course-list",
                params={
                    "all": "1",
                    "course_id": str(course_id),
                    "need_time_quantum": "1",
                    "unique_course": "1",
                    "with_sub_duration": "1",
                    "with_sub_data": "1",
                    "with_room_data": "1",
                    "show_all": "1",
                    "show_delete": "2",
                },
                headers=auth_headers(token),
                timeout=timeout,
            )
            live_resp.raise_for_status()
            payload = live_resp.json()
        except requests.RequestException as exc:
            last_error = f"live_api_http_error: {exc}"
        except ValueError as exc:
            last_error = f"live_api_json_error: {exc}"
        else:
            live_sub_id = _extract_live_sub_id(payload)
            fallback_sub_id = _extract_any_sub_id(payload)
            if live_sub_id:
                return LiveCheckResult(
                    course_id=course_id,
                    is_live=True,
                    checked=True,
                    attempts=attempts,
                    elapsed_sec=round(time.monotonic() - started, 3),
                    last_error="",
                    hint="dynamic_api_live_text",
                    sub_id=live_sub_id,
                )
            if contains_live_text(payload):
                return LiveCheckResult(
                    course_id=course_id,
                    is_live=True,
                    checked=True,
                    attempts=attempts,
                    elapsed_sec=round(time.monotonic() - started, 3),
                    last_error="",
                    hint="dynamic_api_live_text",
                    sub_id=fallback_sub_id,
                )
            if static_live_hit:
                return LiveCheckResult(
                    course_id=course_id,
                    is_live=True,
                    checked=True,
                    attempts=attempts,
                    elapsed_sec=round(time.monotonic() - started, 3),
                    last_error="",
                    hint="static_html_live_text",
                    sub_id=fallback_sub_id,
                )
            if _looks_like_live_payload(payload):
                return LiveCheckResult(
                    course_id=course_id,
                    is_live=False,
                    checked=True,
                    attempts=attempts,
                    elapsed_sec=round(time.monotonic() - started, 3),
                    last_error="",
                    hint="dynamic_api_no_live_text",
                )
            last_error = "live_api_payload_unrecognized"

        if static_live_hit:
            return LiveCheckResult(
                course_id=course_id,
                is_live=True,
                checked=True,
                attempts=attempts,
                elapsed_sec=round(time.monotonic() - started, 3),
                last_error="",
                hint="static_html_live_text",
            )

        elapsed = time.monotonic() - started
        if elapsed >= max_wait_sec:
            return LiveCheckResult(
                course_id=course_id,
                is_live=False,
                checked=False,
                attempts=attempts,
                elapsed_sec=round(elapsed, 3),
                last_error=last_error or "dynamic_status_unavailable",
                hint="dynamic_status_unavailable",
            )
        if interval_sec > 0:
            time.sleep(interval_sec)
