from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import which
from typing import Any
from urllib.parse import quote_plus

from src.common.account import TingwuSettings, resolve_dingtalk_bot_settings, resolve_tingwu_settings
from src.common.http import get_thread_session

_TINGWU_ENDPOINT = "tingwu.cn-beijing.aliyuncs.com"
_TINGWU_VERSION = "2023-09-30"
_RESULT_DIR_NAME = "tingwu_results"


@dataclass(frozen=True)
class TingwuJob:
    session_dir: Path
    audio_file: Path
    course_title: str
    teacher_name: str
    started_at_iso: str
    poll_interval_sec: float
    max_wait_hours: float

    @staticmethod
    def from_path(path: Path) -> "TingwuJob":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("tingwu job file must be JSON object")
        raw_session_dir = str(payload.get("session_dir") or "").strip()
        raw_audio_file = str(payload.get("audio_file") or "").strip()
        if not raw_session_dir:
            raise ValueError("job missing session_dir")
        if not raw_audio_file:
            raise ValueError("job missing audio_file")
        session_dir = Path(raw_session_dir).expanduser().resolve()
        audio_file = Path(raw_audio_file).expanduser().resolve()
        return TingwuJob(
            session_dir=session_dir,
            audio_file=audio_file,
            course_title=str(payload.get("course_title") or "").strip(),
            teacher_name=str(payload.get("teacher_name") or "").strip(),
            started_at_iso=str(payload.get("started_at_iso") or "").strip(),
            poll_interval_sec=max(5.0, float(payload.get("poll_interval_sec", 30.0))),
            max_wait_hours=max(0.5, float(payload.get("max_wait_hours", 6.0))),
        )


class _WorkerLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        print(line)
        with self.path.open("a", encoding="utf-8") as fp:
            fp.write(line)
            fp.write("\n")


class _DingTalkMarkdownSender:
    def __init__(self, *, webhook: str, secret: str, timeout_sec: float = 5.0, retry_count: int = 3) -> None:
        self.webhook = str(webhook or "").strip()
        self.secret = str(secret or "").strip()
        self.timeout_sec = max(1.0, float(timeout_sec))
        self.retry_count = max(1, int(retry_count))
        if not self.webhook or not self.secret:
            raise ValueError("invalid DingTalk settings")

    def send(self, *, title: str, text: str) -> tuple[bool, str]:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": str(title or "").strip() or "通义听悟通知",
                "text": str(text or "").strip() or "通义听悟通知",
            },
        }
        last_error = ""
        for attempt in range(1, self.retry_count + 1):
            try:
                ts_ms = int(time.time() * 1000)
                url = self._signed_url(ts_ms)
                resp = get_thread_session(pool_size=8).post(url, json=payload, timeout=self.timeout_sec)
                resp.raise_for_status()
                body = resp.json()
                if int(body.get("errcode", -1)) != 0:
                    raise RuntimeError(f"errcode={body.get('errcode')} errmsg={body.get('errmsg', '')}")
                return True, ""
            except Exception as exc:
                last_error = str(exc)
                if attempt >= self.retry_count:
                    break
                time.sleep(min(4.0, 0.5 * attempt))
        return False, last_error

    def _signed_url(self, timestamp_ms: int) -> str:
        to_sign = f"{int(timestamp_ms)}\n{self.secret}"
        digest = hmac.new(self.secret.encode("utf-8"), to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
        sep = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{sep}timestamp={int(timestamp_ms)}&sign={sign}"


class TingwuOpenApiClient:
    def __init__(self, settings: TingwuSettings) -> None:
        try:
            from alibabacloud_tea_openapi import models as open_api_models
            from alibabacloud_tea_openapi.client import Client as OpenApiClient
            from alibabacloud_tea_util import models as util_models
        except Exception as exc:
            raise RuntimeError(
                "missing Alibaba Cloud OpenAPI dependencies: "
                "install alibabacloud-tea-openapi and alibabacloud-tea-util"
            ) from exc

        config = open_api_models.Config(
            access_key_id=settings.access_key_id,
            access_key_secret=settings.access_key_secret,
        )
        config.endpoint = _TINGWU_ENDPOINT
        self._client = OpenApiClient(config)
        self._models = open_api_models
        self._runtime = util_models.RuntimeOptions(connect_timeout=5000, read_timeout=10000)

    def create_task(self, *, app_key: str, file_url: str, task_key: str) -> dict[str, Any]:
        body = {
            "AppKey": app_key,
            "Input": {
                "SourceLanguage": "cn",
                "FileUrl": file_url,
                "TaskKey": task_key,
            },
            "Parameters": {
                "Transcoding": {"TargetAudioFormat": "mp3"},
                "Transcription": {
                    "DiarizationEnabled": True,
                    "Diarization": {"SpeakerCount": 0},
                },
                "AutoChaptersEnabled": True,
                "MeetingAssistanceEnabled": True,
                "MeetingAssistance": {"Types": ["Actions", "KeyInformation"]},
                "SummarizationEnabled": True,
                "Summarization": {"Types": ["Paragraph", "Conversational", "QuestionsAnswering"]},
            },
        }
        return self._call_api(
            action="CreateTask",
            pathname="/openapi/tingwu/v2/tasks",
            method="PUT",
            query={"type": "offline"},
            body=body,
        )

    def get_task_info(self, task_id: str) -> dict[str, Any]:
        return self._call_api(
            action="GetTaskInfo",
            pathname=f"/openapi/tingwu/v2/tasks/{task_id}",
            method="GET",
            query={},
            body=None,
        )

    def _call_api(
        self,
        *,
        action: str,
        pathname: str,
        method: str,
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        params = self._models.Params(
            action=action,
            version=_TINGWU_VERSION,
            protocol="HTTPS",
            pathname=pathname,
            method=method,
            auth_type="AK",
            style="ROA",
            req_body_type="json",
            body_type="json",
        )
        request = self._models.OpenApiRequest(query=query or {}, body=body)
        response = self._client.call_api(params, request, self._runtime)
        if isinstance(response, dict):
            body_value = response.get("body")
            if isinstance(body_value, dict):
                return body_value
        if isinstance(response, dict):
            return response
        raise RuntimeError(f"unexpected OpenAPI response type: {type(response).__name__}")


class TingwuOssClient:
    def __init__(self, settings: TingwuSettings) -> None:
        try:
            import oss2
        except Exception as exc:
            raise RuntimeError("missing OSS dependency: install oss2") from exc
        endpoint = str(settings.oss_endpoint or "").strip()
        if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
            endpoint = f"https://{endpoint}"
        auth = oss2.Auth(settings.access_key_id, settings.access_key_secret)
        self._bucket = oss2.Bucket(auth, endpoint, settings.oss_bucket)
        self._oss2 = oss2

    def upload_file_multipart(self, *, object_key: str, file_path: Path, part_size: int = 8 * 1024 * 1024) -> None:
        init = self._bucket.init_multipart_upload(object_key)
        upload_id = init.upload_id
        parts: list[Any] = []
        part_number = 1
        with file_path.open("rb") as fp:
            while True:
                chunk = fp.read(max(1, int(part_size)))
                if not chunk:
                    break
                result = self._bucket.upload_part(object_key, upload_id, part_number, chunk)
                parts.append(self._oss2.models.PartInfo(part_number, result.etag))
                part_number += 1
        self._bucket.complete_multipart_upload(object_key, upload_id, parts)

    def sign_get_url(self, *, object_key: str, expires_sec: int) -> str:
        return self._bucket.sign_url("GET", object_key, max(60, int(expires_sec)))

    def put_probe_object(self, *, object_key: str, content: bytes) -> None:
        self._bucket.put_object(object_key, content)

    def delete_object(self, *, object_key: str) -> None:
        self._bucket.delete_object(object_key)


def validate_tingwu_local_requirements() -> str:
    settings, settings_error = resolve_tingwu_settings()
    if settings is None:
        return settings_error
    ffmpeg = _which("ffmpeg")
    ffprobe = _which("ffprobe")
    if not ffmpeg or not ffprobe:
        return "ffmpeg/ffprobe is required but not found in PATH"
    return ""


def run_tingwu_remote_preflight(*, timeout_sec: float = 8.0) -> tuple[bool, str]:
    settings, settings_error = resolve_tingwu_settings()
    if settings is None:
        return False, settings_error

    try:
        client = TingwuOpenApiClient(settings)
        _ = client.get_task_info("preflight-nonexistent-task-id")
    except Exception as exc:
        message = _format_exception(exc)
        low = message.lower()
        not_found_signals = (
            "notfound",
            "task not found",
            "invalidtask",
            "invalid_task",
            "404",
        )
        if not any(token in low for token in not_found_signals):
            return False, f"tingwu auth probe failed: {message}"

    probe_key = (
        f"tingwu/preflight/{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_"
        f"{os.getpid()}.txt"
    )
    probe_body = b"tingwu-preflight"
    try:
        oss_client = TingwuOssClient(settings)
        oss_client.put_probe_object(object_key=probe_key, content=probe_body)
        signed_url = oss_client.sign_get_url(object_key=probe_key, expires_sec=120)
        resp = get_thread_session(pool_size=4).get(signed_url, timeout=max(3.0, float(timeout_sec)))
        resp.raise_for_status()
        if resp.content != probe_body:
            return False, "oss probe read mismatch"
    except Exception as exc:
        return False, f"oss probe failed: {_format_exception(exc)}"
    finally:
        try:
            oss_client.delete_object(object_key=probe_key)
        except Exception:
            pass
    return True, ""


def run_tingwu_process(args: argparse.Namespace) -> int:
    job_file = Path(str(getattr(args, "job_file", "") or "")).expanduser().resolve()
    if not job_file.exists():
        print(f"Tingwu process failed: job file not found: {job_file}")
        return 1
    try:
        job = TingwuJob.from_path(job_file)
    except Exception as exc:
        print(f"Tingwu process failed: invalid job file: {exc}")
        return 1
    logger = _WorkerLogger(job.session_dir / "tingwu_process.log")
    notifier = _build_notifier(logger=logger)
    return _run_tingwu_job(job=job, logger=logger, notifier=notifier)


def _run_tingwu_job(*, job: TingwuJob, logger: _WorkerLogger, notifier: _DingTalkMarkdownSender | None) -> int:
    started_mono = time.monotonic()
    error_path = job.session_dir / "tingwu_process_error.json"
    try:
        if not job.audio_file.exists() or job.audio_file.stat().st_size <= 0:
            raise RuntimeError(f"audio file missing or empty: {job.audio_file}")
        settings, settings_error = resolve_tingwu_settings()
        if settings is None:
            raise RuntimeError(settings_error)

        object_key = _build_object_key(job=job)
        logger.log(f"[tingwu] upload start object_key={object_key}")
        oss_client = TingwuOssClient(settings)
        oss_client.upload_file_multipart(object_key=object_key, file_path=job.audio_file)
        signed_url = oss_client.sign_get_url(object_key=object_key, expires_sec=24 * 3600)

        openapi_client = TingwuOpenApiClient(settings)
        task_key = _build_task_key(job=job)
        create_payload = openapi_client.create_task(
            app_key=settings.app_key,
            file_url=signed_url,
            task_key=task_key,
        )
        task_id = _extract_task_id(create_payload)
        if not task_id:
            raise RuntimeError(f"create task response missing task id: {json.dumps(create_payload, ensure_ascii=False)}")
        logger.log(f"[tingwu] task created task_id={task_id}")
        _notify_submit(
            notifier=notifier,
            job=job,
            task_id=task_id,
            logger=logger,
        )

        deadline = time.monotonic() + max(1800.0, float(job.max_wait_hours) * 3600.0)
        final_info: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            info = openapi_client.get_task_info(task_id)
            status = _extract_task_status(info).upper()
            logger.log(f"[tingwu] poll task_id={task_id} status={status or 'UNKNOWN'}")
            if status in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
                final_info = info
                break
            if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED"}:
                raise RuntimeError(f"tingwu task failed status={status} payload={json.dumps(info, ensure_ascii=False)}")
            time.sleep(max(5.0, float(job.poll_interval_sec)))

        if final_info is None:
            raise RuntimeError(f"tingwu task timed out after {job.max_wait_hours:.2f}h")

        result_dir = job.session_dir / _RESULT_DIR_NAME
        result_dir.mkdir(parents=True, exist_ok=True)
        result_payloads = _download_result_jsons(info=final_info, result_dir=result_dir, logger=logger)
        summary_path = _render_summary_markdown(
            job=job,
            task_id=task_id,
            final_info=final_info,
            result_payloads=result_payloads,
        )

        elapsed_sec = time.monotonic() - started_mono
        _notify_success(
            notifier=notifier,
            job=job,
            task_id=task_id,
            summary_path=summary_path,
            result_dir=result_dir,
            elapsed_sec=elapsed_sec,
            logger=logger,
        )
        logger.log("[tingwu] process completed")
        return 0
    except Exception as exc:
        message = _format_exception(exc)
        logger.log(f"[tingwu] process failed: {message}")
        _write_error_file(error_path=error_path, job=job, error=message)
        _notify_failure(notifier=notifier, job=job, error=message, error_path=error_path, logger=logger)
        return 1


def _download_result_jsons(*, info: dict[str, Any], result_dir: Path, logger: _WorkerLogger) -> dict[str, Any]:
    url_map = _collect_result_urls(info)
    if not url_map:
        raise RuntimeError("task completed but no result url found in response")
    out: dict[str, Any] = {}
    for key, url in sorted(url_map.items()):
        logger.log(f"[tingwu] download result key={key}")
        resp = get_thread_session(pool_size=8).get(url, timeout=30)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"result is not valid json key={key} url={url}: {exc}") from exc
        file_path = result_dir / f"{key}.json"
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out[key] = payload
    return out


def _render_summary_markdown(
    *,
    job: TingwuJob,
    task_id: str,
    final_info: dict[str, Any],
    result_payloads: dict[str, Any],
) -> Path:
    summary_path = job.session_dir / "tingwu_summary.md"
    lines: list[str] = [
        "# 通义听悟课后汇总",
        "",
        f"- 课程：{job.course_title or 'N/A'} | {job.teacher_name or 'N/A'}",
        f"- TaskId：{task_id or 'N/A'}",
        f"- 任务状态：{_extract_task_status(final_info) or 'N/A'}",
        f"- 音频文件：{job.audio_file}",
        "",
        "## 全文摘要",
        _best_summary_text(result_payloads),
        "",
        "## 章节速览",
    ]
    chapters = _best_chapters(result_payloads)
    if not chapters:
        lines.append("- N/A")
    else:
        for item in chapters:
            lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## 核心要点",
        ]
    )
    points = _best_points(result_payloads)
    if not points:
        lines.append("- N/A")
    else:
        for item in points:
            lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## 关键词",
        ]
    )
    keywords = _best_keywords(result_payloads)
    if not keywords:
        lines.append("- N/A")
    else:
        lines.append("- " + ", ".join(keywords))
    lines.extend(
        [
            "",
            "## 发言总结",
        ]
    )
    speaker_summaries = _best_speaker_summaries(result_payloads)
    if not speaker_summaries:
        lines.append("- N/A")
    else:
        for item in speaker_summaries:
            lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## 原始结果文件",
        ]
    )
    if not result_payloads:
        lines.append("- N/A")
    else:
        for key in sorted(result_payloads.keys()):
            lines.append(f"- {_RESULT_DIR_NAME}/{key}.json")
    summary_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return summary_path


def _best_summary_text(result_payloads: dict[str, Any]) -> str:
    for payload in result_payloads.values():
        summarization = _get_ci_mapping_value(payload, "Summarization")
        if not isinstance(summarization, dict):
            continue
        paragraph_summary = _get_ci_mapping_value(summarization, "ParagraphSummary")
        if isinstance(paragraph_summary, str) and paragraph_summary.strip():
            return paragraph_summary.strip()
        conversational = _get_ci_mapping_value(summarization, "ConversationalSummary")
        if isinstance(conversational, list):
            for item in conversational:
                if isinstance(item, dict):
                    text = str(item.get("Summary") or item.get("summary") or "").strip()
                    if text:
                        return text
                elif isinstance(item, str) and item.strip():
                    return item.strip()
    candidates = ["summary", "abstract", "overview", "conversational", "paragraph", "content", "text"]
    for payload in result_payloads.values():
        value = _find_first_text(payload, candidates)
        if value:
            return value
    return "N/A"


def _best_chapters(result_payloads: dict[str, Any]) -> list[str]:
    for payload in result_payloads.values():
        auto_chapters = _get_ci_mapping_value(payload, "AutoChapters")
        if not isinstance(auto_chapters, list):
            continue
        out: list[str] = []
        for item in auto_chapters:
            line = _format_auto_chapter(item)
            if line:
                out.append(line)
        if out:
            return out[:20]

    for payload in result_payloads.values():
        chapters = _find_first_list(payload, ["chapters", "chapter", "chapterlist", "autochapters"])
        if not chapters:
            continue
        out: list[str] = []
        for item in chapters:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("name") or item.get("summary") or "").strip()
                if title:
                    out.append(title)
            elif isinstance(item, str):
                value = item.strip()
                if value:
                    out.append(value)
        if out:
            return out[:20]
    return []


def _best_points(result_payloads: dict[str, Any]) -> list[str]:
    for payload in result_payloads.values():
        meeting = _get_ci_mapping_value(payload, "MeetingAssistance")
        if not isinstance(meeting, dict):
            continue
        out = _extract_points_from_meeting_assistance(meeting)
        if out:
            return out[:30]

    for payload in result_payloads.values():
        summarization = _get_ci_mapping_value(payload, "Summarization")
        if not isinstance(summarization, dict):
            continue
        out = _extract_points_from_summarization(summarization)
        if out:
            return out[:30]

    for payload in result_payloads.values():
        points = _find_first_list(payload, ["keypoints", "corepoints", "highlights", "actions", "keyinformation"])
        if not points:
            continue
        out: list[str] = []
        for item in points:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or item.get("title") or "").strip()
                if text:
                    out.append(text)
            elif isinstance(item, str):
                value = item.strip()
                if value:
                    out.append(value)
        if out:
            return out[:30]
    return []


def _best_keywords(result_payloads: dict[str, Any]) -> list[str]:
    for payload in result_payloads.values():
        keywords = _find_first_list(payload, ["keywords", "keyword", "terms"])
        if not keywords:
            continue
        out: list[str] = []
        for item in keywords:
            if isinstance(item, dict):
                value = str(item.get("word") or item.get("text") or item.get("keyword") or "").strip()
                if value:
                    out.append(value)
            elif isinstance(item, str):
                value = item.strip()
                if value:
                    out.append(value)
        if out:
            return out[:30]
    return []


def _best_speaker_summaries(result_payloads: dict[str, Any]) -> list[str]:
    for payload in result_payloads.values():
        summarization = _get_ci_mapping_value(payload, "Summarization")
        if not isinstance(summarization, dict):
            continue
        conversational = _get_ci_mapping_value(summarization, "ConversationalSummary")
        if not isinstance(conversational, list):
            continue

        out: list[str] = []
        for idx, item in enumerate(conversational, start=1):
            line = _format_speaker_summary_item(item, idx=idx)
            if line:
                out.append(line)
        if out:
            return out[:20]
    return []


def _format_speaker_summary_item(item: Any, *, idx: int) -> str:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return ""
        return f"发言片段{idx}：{_compact_text(text, max_len=260)}"

    if not isinstance(item, dict):
        return ""

    speaker_name = str(
        item.get("SpeakerName")
        or item.get("speaker_name")
        or item.get("speakerName")
        or item.get("name")
        or ""
    ).strip()
    speaker_id = str(item.get("SpeakerId") or item.get("speaker_id") or item.get("speakerId") or "").strip()
    summary = str(item.get("Summary") or item.get("summary") or item.get("Text") or item.get("text") or "").strip()

    if not summary:
        return ""

    if speaker_name and speaker_id:
        prefix = f"{speaker_name}（SpeakerId={speaker_id}）"
    elif speaker_name:
        prefix = speaker_name
    elif speaker_id:
        prefix = f"SpeakerId={speaker_id}"
    else:
        prefix = f"发言片段{idx}"
    return f"{prefix}：{_compact_text(summary, max_len=260)}"


def _get_ci_mapping_value(obj: Any, key_name: str) -> Any:
    if not isinstance(obj, dict):
        return None
    target = str(key_name).strip().lower()
    for key, value in obj.items():
        if str(key).strip().lower() == target:
            return value
    return None


def _format_auto_chapter(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""

    headline = str(item.get("Headline") or item.get("headline") or item.get("title") or item.get("name") or "").strip()
    summary = str(item.get("Summary") or item.get("summary") or "").strip()
    start_text = _format_milliseconds(item.get("Start"))
    end_text = _format_milliseconds(item.get("End"))

    prefix = ""
    if start_text and end_text:
        prefix = f"[{start_text}-{end_text}] "
    elif start_text:
        prefix = f"[{start_text}] "

    if headline and summary and summary != headline:
        return f"{prefix}{headline}：{summary}"
    return f"{prefix}{headline or summary}".strip()


def _format_milliseconds(raw: Any) -> str:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return ""
    if value < 0:
        return ""
    total_seconds = int(value // 1000)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _extract_points_from_meeting_assistance(meeting: dict[str, Any]) -> list[str]:
    out: list[str] = []
    key_sentences = _get_ci_mapping_value(meeting, "KeySentences")
    if isinstance(key_sentences, list):
        for item in key_sentences:
            text = ""
            if isinstance(item, dict):
                text = str(item.get("Text") or item.get("text") or item.get("Summary") or item.get("summary") or "").strip()
            elif isinstance(item, str):
                text = item.strip()
            if text:
                out.append(_compact_text(text))
            if len(out) >= 12:
                break

    classifications = _get_ci_mapping_value(meeting, "Classifications")
    classification_line = _format_classification_line(classifications)
    if classification_line:
        out.append(classification_line)
    return out


def _extract_points_from_summarization(summarization: dict[str, Any]) -> list[str]:
    out: list[str] = []
    qa_items = _get_ci_mapping_value(summarization, "QuestionsAnsweringSummary")
    if isinstance(qa_items, list):
        for item in qa_items:
            if not isinstance(item, dict):
                continue
            question = str(item.get("Question") or item.get("question") or "").strip()
            answer = str(item.get("Answer") or item.get("answer") or "").strip()
            if question and answer:
                out.append(_compact_text(f"Q: {question} A: {answer}", max_len=220))
            elif answer:
                out.append(_compact_text(answer))
            if len(out) >= 8:
                break
    if out:
        return out

    conversational = _get_ci_mapping_value(summarization, "ConversationalSummary")
    if isinstance(conversational, list):
        for item in conversational:
            if isinstance(item, dict):
                text = str(item.get("Summary") or item.get("summary") or "").strip()
                if text:
                    out.append(_compact_text(text, max_len=220))
            elif isinstance(item, str):
                text = item.strip()
                if text:
                    out.append(_compact_text(text, max_len=220))
            if len(out) >= 8:
                break
    return out


def _format_classification_line(classifications: Any) -> str:
    if not isinstance(classifications, dict):
        return ""
    scored: list[tuple[float, str]] = []
    for label, raw_score in classifications.items():
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        scored.append((score, str(label)))
    if not scored:
        return ""
    scored.sort(reverse=True)
    parts = [f"{label}({score:.2f})" for score, label in scored[:3]]
    return "场景分类：" + " / ".join(parts)


def _compact_text(text: str, *, max_len: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_len:
        return compact
    return compact[: max(1, max_len - 1)].rstrip() + "…"


def _find_first_text(obj: Any, keys: list[str]) -> str:
    queue: list[Any] = [obj]
    key_set = {k.lower() for k in keys}
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for key, value in cur.items():
                key_text = str(key).lower()
                if key_text in key_set and isinstance(value, str) and value.strip():
                    return value.strip()
                queue.append(value)
        elif isinstance(cur, list):
            queue.extend(cur)
    return ""


def _find_first_list(obj: Any, keys: list[str]) -> list[Any]:
    queue: list[Any] = [obj]
    key_set = {k.lower() for k in keys}
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for key, value in cur.items():
                key_text = str(key).lower()
                if key_text in key_set and isinstance(value, list):
                    return value
                queue.append(value)
        elif isinstance(cur, list):
            queue.extend(cur)
    return []


def _collect_result_urls(info: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    queue: list[tuple[str, Any]] = [("", info)]
    while queue:
        prefix, cur = queue.pop(0)
        if isinstance(cur, dict):
            for key, value in cur.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(value, str):
                    url = value.strip()
                    if not url.startswith(("http://", "https://")):
                        continue
                    low_key = str(key).lower()
                    low_path = path.lower()
                    # Support both old-style *Url fields and new-style Data.Result.<Capability> URL fields.
                    if low_key.endswith("url") or ".result." in low_path:
                        if low_key.endswith("url"):
                            name = path[:-3] if len(path) > 3 else path
                        else:
                            name = path
                        safe_name = _safe_result_name(name)
                        out[safe_name] = url
                else:
                    queue.append((path, value))
        elif isinstance(cur, list):
            for idx, item in enumerate(cur):
                queue.append((f"{prefix}_{idx}", item))
    return out


def _safe_result_name(name: str) -> str:
    out = []
    for ch in str(name):
        if ch.isalnum() or ch in {"_", "-", "."}:
            out.append(ch.lower())
        else:
            out.append("_")
    text = "".join(out).strip("_")
    while "__" in text:
        text = text.replace("__", "_")
    return text or "result"


def _build_notifier(*, logger: _WorkerLogger) -> _DingTalkMarkdownSender | None:
    webhook, secret, err = resolve_dingtalk_bot_settings()
    if err:
        logger.log(f"[tingwu] warning: dingtalk disabled: {err}")
        return None
    try:
        return _DingTalkMarkdownSender(webhook=webhook, secret=secret, timeout_sec=5.0, retry_count=3)
    except Exception as exc:
        logger.log(f"[tingwu] warning: dingtalk init failed: {exc}")
        return None


def _notify_submit(
    *,
    notifier: _DingTalkMarkdownSender | None,
    job: TingwuJob,
    task_id: str,
    logger: _WorkerLogger,
) -> None:
    if notifier is None:
        return
    title = "通义听悟任务已提交"
    text = "\n".join(
        [
            "# 通义听悟任务已提交",
            "",
            f"- 课程：{job.course_title or 'N/A'} | {job.teacher_name or 'N/A'}",
            f"- TaskId：{task_id}",
            f"- 音频：{job.audio_file}",
            f"- 时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
    )
    ok, err = notifier.send(title=title, text=text)
    if not ok:
        logger.log(f"[tingwu] dingtalk submit notify failed: {err}")


def _notify_success(
    *,
    notifier: _DingTalkMarkdownSender | None,
    job: TingwuJob,
    task_id: str,
    summary_path: Path,
    result_dir: Path,
    elapsed_sec: float,
    logger: _WorkerLogger,
) -> None:
    if notifier is None:
        return
    title = "通义听悟处理完成"
    text = "\n".join(
        [
            "# 通义听悟处理完成",
            "",
            f"- 课程：{job.course_title or 'N/A'} | {job.teacher_name or 'N/A'}",
            f"- TaskId：{task_id}",
            f"- 处理耗时：{elapsed_sec:.1f}s",
            f"- Markdown：{summary_path}",
            f"- JSON目录：{result_dir}",
            "- 说明：本通知仅发送状态，不附完整 Markdown 正文。",
        ]
    )
    ok, err = notifier.send(title=title, text=text)
    if not ok:
        logger.log(f"[tingwu] dingtalk success notify failed: {err}")


def _notify_failure(
    *,
    notifier: _DingTalkMarkdownSender | None,
    job: TingwuJob,
    error: str,
    error_path: Path,
    logger: _WorkerLogger,
) -> None:
    if notifier is None:
        return
    title = "通义听悟处理失败"
    text = "\n".join(
        [
            "# 通义听悟处理失败",
            "",
            f"- 课程：{job.course_title or 'N/A'} | {job.teacher_name or 'N/A'}",
            f"- 时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 错误：{error}",
            f"- 错误详情文件：{error_path}",
        ]
    )
    ok, err = notifier.send(title=title, text=text)
    if not ok:
        logger.log(f"[tingwu] dingtalk failure notify failed: {err}")


def _build_object_key(*, job: TingwuJob) -> str:
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y/%m/%d")
    folder = _safe_result_name(job.session_dir.name)
    file_name = _safe_result_name(job.audio_file.name)
    return f"tingwu/{day}/{folder}/{file_name}"


def _build_task_key(*, job: TingwuJob) -> str:
    base = _safe_result_name(f"{job.course_title}_{job.teacher_name}_{job.session_dir.name}")
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{base}_{suffix}"


def _extract_task_id(payload: dict[str, Any]) -> str:
    for path in (
        ("Data", "TaskId"),
        ("Data", "taskId"),
        ("data", "taskId"),
        ("TaskId",),
        ("taskId",),
    ):
        value = _get_path(payload, list(path))
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                return text
    return _find_first_key_text(payload, "taskid")


def _extract_task_status(payload: dict[str, Any]) -> str:
    for path in (
        ("Data", "TaskStatus"),
        ("Data", "Status"),
        ("data", "taskStatus"),
        ("TaskStatus",),
        ("Status",),
        ("taskStatus",),
    ):
        value = _get_path(payload, list(path))
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                return text
    return _find_first_key_text(payload, "taskstatus")


def _get_path(obj: Any, path: list[str]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        if key not in cur:
            return None
        cur = cur[key]
    return cur


def _find_first_key_text(obj: Any, key_name: str) -> str:
    target = str(key_name or "").strip().lower()
    queue: list[Any] = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for key, value in cur.items():
                low = str(key).strip().lower().replace("_", "")
                if low == target and isinstance(value, (str, int)):
                    text = str(value).strip()
                    if text:
                        return text
                queue.append(value)
        elif isinstance(cur, list):
            queue.extend(cur)
    return ""


def _write_error_file(*, error_path: Path, job: TingwuJob, error: str) -> None:
    payload = {
        "error": str(error),
        "course_title": job.course_title,
        "teacher_name": job.teacher_name,
        "audio_file": str(job.audio_file),
        "session_dir": str(job.session_dir),
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    error_path.parent.mkdir(parents=True, exist_ok=True)
    error_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _which(name: str) -> str:
    return which(name) or ""


def _format_exception(exc: Exception) -> str:
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        code = str(data.get("Code") or data.get("code") or "").strip()
        message = str(data.get("Message") or data.get("message") or "").strip()
        if code or message:
            if code and message:
                return f"{code}: {message}"
            return code or message
    return str(exc)
