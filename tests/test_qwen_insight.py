import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.common.account import resolve_effective_llm_base_url, resolve_openai_client_settings
from src.live.insight.models import KeywordConfig
from src.live.insight.openai_client import OpenAIInsightClient


class QwenInsightTests(unittest.TestCase):
    def settings(self, content="", env=None, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            account = Path(directory) / ".account"
            account.write_text(content)
            with mock.patch("src.common.account.default_account_file", return_value=account), mock.patch.dict(
                "os.environ", env or {}, clear=True
            ):
                return resolve_openai_client_settings(model_name="qwen3.7-flash", **kwargs)

    def test_dedicated_key_and_default_endpoint(self):
        key, base, error = self.settings(
            "QWEN_API_KEY=qwen-key\nDASHSCOPE_API_KEY=asr-key\nOPENAI_API_KEY=other-key\n"
            "OPENAI_BASE_URL=https://aihubmix.com/v1\n"
        )
        self.assertEqual((key, error), ("qwen-key", ""))
        self.assertEqual(base, "https://dashscope.aliyuncs.com/compatible-mode/v1")

    def test_dashscope_env_fallback(self):
        key, _, error = self.settings(env={"DASHSCOPE_API_KEY": "dashscope-key"})
        self.assertEqual((key, error), ("dashscope-key", ""))

    def test_custom_env_and_endpoint(self):
        result = self.settings(
            env={"MY_QWEN_KEY": "custom-key", "MY_QWEN_URL": "https://example.test/v1"},
            api_key_env_name="MY_QWEN_KEY", base_url_env_name="MY_QWEN_URL",
        )
        self.assertEqual(result, ("custom-key", "https://example.test/v1", ""))

    def test_does_not_use_unrelated_provider_key(self):
        key, _, error = self.settings("OPENAI_API_KEY=other-key\nZAI_API_KEY=glm-key\n")
        self.assertEqual(key, "")
        self.assertIn("missing Qwen API key", error)

    def test_endpoint_resolution(self):
        for explicit, expected in [
            ("https://aihubmix.com/v1", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ("https://example.test/v1", "https://example.test/v1"),
        ]:
            with self.subTest(explicit=explicit):
                self.assertEqual(resolve_effective_llm_base_url(
                    model_name="qwen3.7-flash", explicit_base_url=explicit,
                    resolved_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                ), expected)

    def test_chat_endpoint_payload_and_important_result(self):
        sdk = mock.Mock()
        sdk.chat.completions.create.return_value = {"choices": [{"message": {"content":
            '{"important":true,"summary":"立即签到","context_summary":"老师要求现在签到",'
            '"matched_terms":["签到"],"reason":"keyword_hit","event_type":"sign_in",'
            '"headline":"立即签到","immediate_action":"输入1234签到","key_details":["签到码1234"]}'
        }}]}
        with mock.patch("src.live.insight.openai_client._load_openai_cls", return_value=mock.Mock(return_value=sdk)):
            client = OpenAIInsightClient(api_key="test-key", timeout_sec=40)
            result = client.analyze_text(
                analysis_model="qwen3.7-flash", keywords=KeywordConfig(),
                current_text="现在签到，签到码1234。", context_text="无历史文本块",
                chunk_seconds=0, timeout_sec=30,
            )
        self.assertTrue(result.important)
        self.assertEqual(result.event_type, "sign_in")
        self.assertEqual(result.key_details, ["签到码1234"])
        sdk.responses.create.assert_not_called()
        sdk.chat.completions.create.assert_called_once()
        request = sdk.chat.completions.create.call_args.kwargs
        self.assertEqual(request["extra_body"], {"enable_thinking": False})
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["timeout"], 30)
        self.assertEqual(request["temperature"], 0.1)
        self.assertEqual(request["max_tokens"], 1200)
        for key in ["input", "reasoning", "text", "max_output_tokens"]:
            self.assertNotIn(key, request)


if __name__ == "__main__":
    unittest.main()
