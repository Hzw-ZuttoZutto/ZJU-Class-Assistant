from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.live.insight.models import KeywordConfig
from src.live.insight.openai_client import OpenAIInsightClient


class _FakeAudioTranscriptions:
    def create(self, **kwargs):
        return type("Resp", (), {"text": "今天讲了微积分和导数"})()


class _FakeAudio:
    def __init__(self) -> None:
        self.transcriptions = _FakeAudioTranscriptions()


class _FakeResponses:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text

    def create(self, **kwargs):
        return type("Resp", (), {"output_text": self.output_text})()


class _FakeOpenAI:
    def __init__(self, *, api_key: str, timeout: float, max_retries: int = 0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.audio = _FakeAudio()
        self.responses = _FakeResponses(
            '{"important": true, "summary": "老师刚布置了微积分作业", '
            '"context_summary": "老师明确要求记录题号和提交方式", '
            '"matched_terms": ["微积分", "作业"], "reason": "keyword_hit", '
            '"event_type": "homework", "headline": "记录作业要求", '
            '"immediate_action": "现在记下题号和提交方式", '
            '"key_details": ["第一大题", "提交到学习平台"]}'
        )


class OpenAIInsightClientTests(unittest.TestCase):
    def test_transcribe_and_analyze_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            chunk = Path(td) / "chunk.mp3"
            chunk.write_bytes(b"audio-bytes")
            with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=_FakeOpenAI):
                client = OpenAIInsightClient(api_key="k", timeout_sec=12.0)
                text = client.transcribe_chunk(
                    chunk_path=chunk,
                    stt_model="gpt-4o-mini-transcribe",
                    timeout_sec=5.0,
                )
                result = client.analyze_text(
                    analysis_model="gpt-5-mini",
                    keywords=KeywordConfig(important_terms=["微积分"]),
                    current_text=text,
                    context_text="无历史文本块",
                    chunk_seconds=10.0,
                    timeout_sec=5.0,
                )
        self.assertIn("微积分", text)
        self.assertTrue(result.important)
        self.assertEqual(result.event_type, "homework")
        self.assertEqual(result.headline, "记录作业要求")
        self.assertEqual(result.immediate_action, "现在记下题号和提交方式")
        self.assertEqual(result.key_details, ["第一大题", "提交到学习平台"])

    def test_transcribe_timeout_raises(self) -> None:
        class _TimeoutAudioTranscriptions:
            def create(self, **kwargs):
                raise TimeoutError("transcribe timeout")

        class _TimeoutOpenAI:
            def __init__(self, *, api_key: str, timeout: float, max_retries: int = 0) -> None:
                self.audio = type("Audio", (), {"transcriptions": _TimeoutAudioTranscriptions()})()
                self.responses = _FakeResponses("{}")

        with tempfile.TemporaryDirectory() as td:
            chunk = Path(td) / "chunk.mp3"
            chunk.write_bytes(b"audio-bytes")
            with mock.patch(
                "src.live.insight.openai_client._load_openai_cls",
                return_value=_TimeoutOpenAI,
            ):
                client = OpenAIInsightClient(api_key="k", timeout_sec=12.0)
                with self.assertRaises(TimeoutError):
                    client.transcribe_chunk(
                        chunk_path=chunk,
                        stt_model="gpt-4o-mini-transcribe",
                        timeout_sec=2.0,
                    )

    def test_analyze_invalid_json(self) -> None:
        class _BadOpenAI:
            def __init__(self, *, api_key: str, timeout: float, max_retries: int = 0) -> None:
                self.audio = _FakeAudio()
                self.responses = _FakeResponses("not-json")

        with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=_BadOpenAI):
            client = OpenAIInsightClient(api_key="k", timeout_sec=12.0)
            with self.assertRaises(ValueError):
                client.analyze_text(
                    analysis_model="gpt-5-mini",
                    keywords=KeywordConfig(),
                    current_text="文本",
                    context_text="无历史文本块",
                    chunk_seconds=10.0,
                    timeout_sec=2.0,
                )

    def test_analyze_temperature_fallback_for_gpt5(self) -> None:
        class _FallbackResponses:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                if "temperature" in kwargs:
                    raise ValueError("Unsupported parameter: 'temperature' is not supported with this model.")
                return type(
                    "Resp",
                    (),
                    {
                        "output_text": (
                            '{"important": false, "summary": "当前没有什么重要内容", '
                            '"context_summary": "无重要内容", "matched_terms": [], "reason": "none"}'
                        )
                    },
                )()

        class _FallbackOpenAI:
            def __init__(self, *, api_key: str, timeout: float, max_retries: int = 0) -> None:
                self.audio = _FakeAudio()
                self.responses = _FallbackResponses()

        with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=_FallbackOpenAI):
            client = OpenAIInsightClient(api_key="k", timeout_sec=12.0)
            result = client.analyze_text(
                analysis_model="gpt-5-mini",
                keywords=KeywordConfig(),
                current_text="现在开始签到",
                context_text="无历史文本块",
                chunk_seconds=10.0,
                timeout_sec=2.0,
            )
            self.assertFalse(result.important)

    def test_init_supports_custom_base_url(self) -> None:
        captured: dict = {}

        class _CaptureOpenAI:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)
                self.audio = _FakeAudio()
                self.responses = _FakeResponses("{}")

        with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=_CaptureOpenAI):
            _ = OpenAIInsightClient(
                api_key="k",
                timeout_sec=12.0,
                base_url="https://aihubmix.com/v1",
            )
        self.assertEqual(captured.get("base_url"), "https://aihubmix.com/v1")
        self.assertEqual(captured.get("max_retries"), 0)

    def test_analyze_debug_hook_captures_each_attempt(self) -> None:
        class _Resp:
            def __init__(self, text: str, *, status: str = "", reason: str = "") -> None:
                self.output_text = text
                self._status = status
                self._reason = reason

            def model_dump(self) -> dict:
                payload: dict = {"output_text": self.output_text}
                if self._status:
                    payload["status"] = self._status
                    payload["incomplete_details"] = {"reason": self._reason}
                return payload

        class _DebugResponses:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return _Resp("not-json", status="incomplete", reason="max_output_tokens")
                return _Resp(
                    '{"important": false, "summary": "当前没有什么重要内容", '
                    '"context_summary": "无重要内容", "matched_terms": [], "reason": "none"}'
                )

        class _DebugOpenAI:
            def __init__(self, *, api_key: str, timeout: float, max_retries: int = 0) -> None:
                self.audio = _FakeAudio()
                self.responses = _DebugResponses()

        events: list[dict] = []
        with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=_DebugOpenAI):
            client = OpenAIInsightClient(api_key="k", timeout_sec=12.0)
            result = client.analyze_text(
                analysis_model="gpt-5-mini",
                keywords=KeywordConfig(),
                current_text="课程继续",
                context_text="无历史文本块",
                chunk_seconds=17.5,
                timeout_sec=2.0,
                debug_hook=events.append,
            )
        self.assertFalse(result.important)
        self.assertEqual(len(events), 2)
        self.assertFalse(events[0]["parsed_ok"])
        self.assertIn("not-json", events[0]["raw_response_text"])
        self.assertTrue(events[1]["parsed_ok"])
        self.assertEqual(events[1]["chunk_seconds"], 17.5)
        self.assertEqual(events[1]["current_text"], "课程继续")
        self.assertIn("无历史文本块", events[1]["context_text"])
        self.assertIn("request_payload_snapshot", events[1])
        self.assertGreater(
            int(events[1]["request_payload_snapshot"].get("max_output_tokens", 0)),
            int(events[0]["request_payload_snapshot"].get("max_output_tokens", 0)),
        )

    def test_analyze_gpt41_payload_uses_medium_verbosity_without_reasoning(self) -> None:
        class _CaptureResponses:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return type(
                    "Resp",
                    (),
                    {
                        "output_text": (
                            '{"important": false, "summary": "none", '
                            '"context_summary": "none", "matched_terms": [], "reason": "none"}'
                        )
                    },
                )()

        class _CaptureOpenAI:
            def __init__(self, *, api_key: str, timeout: float, max_retries: int = 0) -> None:
                self.audio = _FakeAudio()
                self.responses = _CaptureResponses()

        with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=_CaptureOpenAI):
            client = OpenAIInsightClient(api_key="k", timeout_sec=12.0)
            _ = client.analyze_text(
                analysis_model="gpt-4.1",
                keywords=KeywordConfig(),
                current_text="class starts",
                context_text="none",
                chunk_seconds=10.0,
                timeout_sec=2.0,
            )
            calls = client.client.responses.calls
            self.assertGreaterEqual(len(calls), 1)
            request = calls[0]
            self.assertEqual(request.get("text", {}).get("verbosity"), "medium")
            self.assertNotIn("reasoning", request)

    def test_analyze_unsupported_value_fallback_switches_verbosity(self) -> None:
        class _ValueFallbackResponses:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                verbosity = str(kwargs.get("text", {}).get("verbosity", "")).strip().lower()
                if verbosity == "low":
                    raise ValueError(
                        "Unsupported value: 'low' is not supported with the 'gpt-4.1-mini' model. "
                        "Supported values are: 'medium'."
                    )
                return type(
                    "Resp",
                    (),
                    {
                        "output_text": (
                            '{"important": false, "summary": "ok", '
                            '"context_summary": "ok", "matched_terms": [], "reason": "fallback"}'
                        )
                    },
                )()

        class _ValueFallbackOpenAI:
            def __init__(self, *, api_key: str, timeout: float, max_retries: int = 0) -> None:
                self.audio = _FakeAudio()
                self.responses = _ValueFallbackResponses()

        with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=_ValueFallbackOpenAI):
            client = OpenAIInsightClient(api_key="k", timeout_sec=12.0)
            result = client.analyze_text(
                analysis_model="gpt-5-mini",
                keywords=KeywordConfig(),
                current_text="current",
                context_text="history",
                chunk_seconds=10.0,
                timeout_sec=2.0,
            )
            self.assertFalse(result.important)
            calls = client.client.responses.calls
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(calls[0].get("text", {}).get("verbosity"), "low")
            self.assertEqual(calls[1].get("text", {}).get("verbosity"), "medium")


if __name__ == "__main__":
    unittest.main()
