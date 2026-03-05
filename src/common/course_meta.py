from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

from src.common.constants import API_BASE


@dataclass
class CourseMeta:
    course_id: int
    title: str
    teachers: list[str]

    @property
    def primary_teacher(self) -> str:
        if self.teachers:
            return self.teachers[0]
        return ""


def parse_course_data(raw: dict) -> Optional[dict]:
    if raw.get("code") == 0 and isinstance(raw.get("data"), dict):
        return raw["data"]
    if (
        raw.get("success")
        and isinstance(raw.get("result"), dict)
        and raw["result"].get("err") == 0
        and isinstance(raw["result"].get("data"), dict)
    ):
        return raw["result"]["data"]
    return None


def course_teachers(course_data: dict) -> list[str]:
    names: list[str] = []
    if isinstance(course_data.get("teachers"), list):
        for item in course_data["teachers"]:
            if not isinstance(item, dict):
                continue
            realname = item.get("realname") or item.get("name")
            if realname and realname not in names:
                names.append(str(realname))

    realname = course_data.get("realname")
    if realname and realname not in names:
        names.insert(0, str(realname))
    return names


def query_course_detail(
    session: requests.Session,
    token: str,
    timeout: int,
    course_id: int,
    retries: int,
) -> Optional[dict]:
    headers = {"Accept-Language": "zh_cn"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempts = max(retries, 0) + 1
    for _ in range(attempts):
        try:
            resp = session.get(
                f"{API_BASE}/courseapi/v3/multi-search/get-course-detail",
                params={"course_id": course_id},
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException:
            continue

        if resp.status_code != 200:
            continue

        try:
            payload = resp.json()
        except ValueError:
            continue

        return parse_course_data(payload)

    return None


def fetch_course_meta(
    session: requests.Session,
    token: str,
    timeout: int,
    course_id: int,
    retries: int = 1,
) -> Optional[CourseMeta]:
    data = query_course_detail(
        session=session,
        token=token,
        timeout=timeout,
        course_id=course_id,
        retries=retries,
    )
    if not data:
        return None
    title = str(data.get("title") or "").strip()
    teachers = course_teachers(data)
    if not title or not teachers:
        return None
    return CourseMeta(course_id=course_id, title=title, teachers=teachers)
