from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.common.billing import reset_billing_alert_cooldown_for_tests
from src.common.account import TingwuSettings
from src.live.tingwu.process import TingwuJob, _render_summary_markdown, _run_tingwu_job, run_tingwu_remote_preflight


class _FakeResp:
    def __init__(self, *, payload: dict | None = None, content: bytes = b"", status_code: int = 200) -> None:
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    def __init__(self, payload: dict | None = None, content: bytes = b"") -> None:
        self.payload = payload
        self.content = content

    def get(self, _url: str, timeout: float = 30.0) -> _FakeResp:
        _ = timeout
        return _FakeResp(payload=self.payload, content=self.content, status_code=200)


class _FakeLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        self.lines.append(str(message))


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, *, title: str, text: str) -> tuple[bool, str]:
        self.sent.append((str(title), str(text)))
        return True, ""


class _FakeOpenApiClient:
    def __init__(self, _settings: TingwuSettings) -> None:
        self.poll_count = 0

    def create_task(self, *, app_key: str, file_url: str, task_key: str) -> dict:
        _ = (app_key, file_url, task_key)
        return {"Data": {"TaskId": "task-1"}}

    def get_task_info(self, task_id: str) -> dict:
        _ = task_id
        self.poll_count += 1
        if self.poll_count == 1:
            return {"Data": {"TaskStatus": "RUNNING"}}
        return {
            "Data": {
                "TaskStatus": "COMPLETED",
                "Result": {
                    "SummaryUrl": "https://example.test/summary",
                    "ChapterUrl": "https://example.test/chapter",
                },
            }
        }


class _FakeOssClient:
    def __init__(self, _settings: TingwuSettings) -> None:
        self.uploaded = False

    def upload_file_multipart(self, *, object_key: str, file_path: Path, part_size: int = 8 * 1024 * 1024) -> None:
        _ = (object_key, file_path, part_size)
        self.uploaded = True

    def sign_get_url(self, *, object_key: str, expires_sec: int) -> str:
        _ = (object_key, expires_sec)
        return "https://example.test/audio"

    def put_probe_object(self, *, object_key: str, content: bytes) -> None:
        _ = (object_key, content)

    def delete_object(self, *, object_key: str) -> None:
        _ = object_key


class TingwuProcessTests(unittest.TestCase):
    def _settings(self) -> TingwuSettings:
        return TingwuSettings(
            access_key_id="ak",
            access_key_secret="sk",
            app_key="app",
            oss_bucket="bucket",
            oss_region="cn-beijing",
            oss_endpoint="oss-cn-beijing.aliyuncs.com",
        )

    def test_run_tingwu_job_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = root / "audio.mp3"
            audio.write_bytes(b"mp3")
            job = TingwuJob(
                session_dir=root,
                audio_file=audio,
                course_title="课程A",
                teacher_name="老师A",
                started_at_iso="2026-03-11T00:00:00+08:00",
                poll_interval_sec=5.0,
                max_wait_hours=1.0,
            )
            logger = _FakeLogger()
            notifier = _FakeNotifier()
            summary_payload = {"summary": "这是摘要"}
            chapter_payload = {"chapters": [{"title": "第一章"}]}
            responses = {
                "https://example.test/summary": summary_payload,
                "https://example.test/chapter": chapter_payload,
            }

            def _session_factory(pool_size: int = 8):  # noqa: ARG001
                class _Session:
                    def get(self, url: str, timeout: float = 30.0) -> _FakeResp:  # noqa: ARG002
                        return _FakeResp(payload=responses[url], content=b"")

                return _Session()

            with (
                mock.patch("src.live.tingwu.process.resolve_tingwu_settings", return_value=(self._settings(), "")),
                mock.patch("src.live.tingwu.process.TingwuOpenApiClient", _FakeOpenApiClient),
                mock.patch("src.live.tingwu.process.TingwuOssClient", _FakeOssClient),
                mock.patch("src.live.tingwu.process.get_thread_session", side_effect=_session_factory),
                mock.patch("src.live.tingwu.process.time.sleep", return_value=None),
            ):
                code = _run_tingwu_job(job=job, logger=logger, notifier=notifier)  # type: ignore[arg-type]
            self.assertEqual(code, 0)
            self.assertTrue((root / "tingwu_summary.md").exists())
            self.assertTrue((root / "tingwu_results").exists())
            self.assertGreaterEqual(len(notifier.sent), 2)

    def test_run_tingwu_job_failure_writes_error_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = root / "audio.mp3"
            audio.write_bytes(b"mp3")
            job = TingwuJob(
                session_dir=root,
                audio_file=audio,
                course_title="课程A",
                teacher_name="老师A",
                started_at_iso="2026-03-11T00:00:00+08:00",
                poll_interval_sec=5.0,
                max_wait_hours=1.0,
            )
            logger = _FakeLogger()
            notifier = _FakeNotifier()

            class _FailOpenApiClient(_FakeOpenApiClient):
                def create_task(self, *, app_key: str, file_url: str, task_key: str) -> dict:
                    _ = (app_key, file_url, task_key)
                    raise RuntimeError("create failed")

            with (
                mock.patch("src.live.tingwu.process.resolve_tingwu_settings", return_value=(self._settings(), "")),
                mock.patch("src.live.tingwu.process.TingwuOpenApiClient", _FailOpenApiClient),
                mock.patch("src.live.tingwu.process.TingwuOssClient", _FakeOssClient),
            ):
                code = _run_tingwu_job(job=job, logger=logger, notifier=notifier)  # type: ignore[arg-type]

            self.assertEqual(code, 1)
            self.assertTrue((root / "tingwu_process_error.json").exists())
            self.assertGreaterEqual(len(notifier.sent), 1)

    def test_remote_preflight_success(self) -> None:
        with (
            mock.patch("src.live.tingwu.process.resolve_tingwu_settings", return_value=(self._settings(), "")),
            mock.patch("src.live.tingwu.process.TingwuOpenApiClient", _FakeOpenApiClient),
            mock.patch("src.live.tingwu.process.TingwuOssClient", _FakeOssClient),
            mock.patch("src.live.tingwu.process.get_thread_session", return_value=_FakeSession(content=b"tingwu-preflight")),
        ):
            ok, err = run_tingwu_remote_preflight(timeout_sec=5.0)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_run_tingwu_job_billing_failure_on_openapi_sends_specific_alert(self) -> None:
        reset_billing_alert_cooldown_for_tests()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = root / "audio.mp3"
            audio.write_bytes(b"mp3")
            job = TingwuJob(
                session_dir=root,
                audio_file=audio,
                course_title="课程A",
                teacher_name="老师A",
                started_at_iso="2026-03-11T00:00:00+08:00",
                poll_interval_sec=5.0,
                max_wait_hours=1.0,
            )
            logger = _FakeLogger()
            notifier = _FakeNotifier()

            class _BillingOpenApiClient(_FakeOpenApiClient):
                def create_task(self, *, app_key: str, file_url: str, task_key: str) -> dict:
                    _ = (app_key, file_url, task_key)
                    raise RuntimeError("TeaException: BRK.OverdueTenant service status is overdue")

            with (
                mock.patch("src.live.tingwu.process.resolve_tingwu_settings", return_value=(self._settings(), "")),
                mock.patch("src.live.tingwu.process.TingwuOpenApiClient", _BillingOpenApiClient),
                mock.patch("src.live.tingwu.process.TingwuOssClient", _FakeOssClient),
            ):
                code = _run_tingwu_job(job=job, logger=logger, notifier=notifier)  # type: ignore[arg-type]

            self.assertEqual(code, 1)
            titles = [title for title, _text in notifier.sent]
            texts = [text for _title, text in notifier.sent]
            self.assertIn("通义听悟 欠费告警", titles)
            self.assertNotIn("通义听悟处理失败", titles)
            self.assertTrue(any("billing-cost.console.aliyun.com" in text for text in texts))

    def test_run_tingwu_job_billing_failure_on_oss_sends_specific_alert(self) -> None:
        reset_billing_alert_cooldown_for_tests()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = root / "audio.mp3"
            audio.write_bytes(b"mp3")
            job = TingwuJob(
                session_dir=root,
                audio_file=audio,
                course_title="课程A",
                teacher_name="老师A",
                started_at_iso="2026-03-11T00:00:00+08:00",
                poll_interval_sec=5.0,
                max_wait_hours=1.0,
            )
            logger = _FakeLogger()
            notifier = _FakeNotifier()

            class _BillingOssClient(_FakeOssClient):
                def upload_file_multipart(self, *, object_key: str, file_path: Path, part_size: int = 8 * 1024 * 1024) -> None:
                    _ = (object_key, file_path, part_size)
                    raise RuntimeError(
                        "0003-00000806 The operation is not valid for the user account in the current billing state"
                    )

            with (
                mock.patch("src.live.tingwu.process.resolve_tingwu_settings", return_value=(self._settings(), "")),
                mock.patch("src.live.tingwu.process.TingwuOpenApiClient", _FakeOpenApiClient),
                mock.patch("src.live.tingwu.process.TingwuOssClient", _BillingOssClient),
            ):
                code = _run_tingwu_job(job=job, logger=logger, notifier=notifier)  # type: ignore[arg-type]

            self.assertEqual(code, 1)
            titles = [title for title, _text in notifier.sent]
            texts = [text for _title, text in notifier.sent]
            self.assertIn("阿里云 OSS 欠费告警", titles)
            self.assertNotIn("通义听悟处理失败", titles)
            self.assertTrue(any("billing-cost.console.aliyun.com" in text for text in texts))

    def test_render_summary_markdown_prefers_new_tingwu_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audio = root / "audio.mp3"
            audio.write_bytes(b"mp3")
            job = TingwuJob(
                session_dir=root,
                audio_file=audio,
                course_title="课程A",
                teacher_name="老师A",
                started_at_iso="2026-03-11T00:00:00+08:00",
                poll_interval_sec=5.0,
                max_wait_hours=1.0,
            )
            result_payloads = {
                "data.result.autochapters": {
                    "TaskId": "task-1",
                    "AutoChapters": [
                        {
                            "Id": 1,
                            "Start": 1000,
                            "End": 66000,
                            "Headline": "第一章标题",
                            "Summary": "第一章摘要",
                        }
                    ],
                },
                "data.result.meetingassistance": {
                    "TaskId": "task-1",
                    "MeetingAssistance": {
                        "Keywords": ["关键词A", "关键词B"],
                        "KeySentences": [
                            {"Text": "关键句A"},
                            {"Text": "关键句B"},
                        ],
                        "Classifications": {"Meeting": 0.7, "Lecture": 0.3},
                    },
                },
                "data.result.summarization": {
                    "TaskId": "task-1",
                    "Summarization": {
                        "ParagraphSummary": "段落摘要A",
                        "ParagraphTitle": "段落标题A",
                        "ConversationalSummary": [{"SpeakerName": "发言人1", "SpeakerId": "1", "Summary": "发言总结A"}],
                        "QuestionsAnsweringSummary": [{"Question": "Q1", "Answer": "A1"}],
                    },
                },
            }
            summary_path = _render_summary_markdown(
                job=job,
                task_id="task-1",
                final_info={"Data": {"TaskStatus": "COMPLETED"}},
                result_payloads=result_payloads,
            )
            text = summary_path.read_text(encoding="utf-8")
            self.assertIn("段落摘要A", text)
            self.assertIn("第一章标题：第一章摘要", text)
            self.assertIn("关键句A", text)
            self.assertIn("场景分类：Meeting(0.70) / Lecture(0.30)", text)
            self.assertIn("发言人1（SpeakerId=1）：发言总结A", text)


if __name__ == "__main__":
    unittest.main()
