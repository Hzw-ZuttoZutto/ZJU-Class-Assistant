from __future__ import annotations

import unittest

from src.live.insight.models import KeywordConfig
from src.live.insight.prompting import (
    HISTORY_CONTEXT_HEADER,
    NO_HISTORY_CONTEXT_LINE,
    build_history_context_block,
    build_system_prompt,
    build_user_prompt,
)


class PromptingTests(unittest.TestCase):
    def test_build_system_prompt_uses_runtime_chunk_seconds(self) -> None:
        prompt = build_system_prompt(17.5)
        self.assertIn("当前17.5秒文本", prompt)
        self.assertNotIn("当前10秒文本", prompt)

    def test_build_system_prompt_uses_segment_text_when_seconds_missing(self) -> None:
        prompt = build_system_prompt(0.0)
        self.assertIn("当前文本段", prompt)
        self.assertNotIn("当前0.1秒文本", prompt)

    def test_build_user_prompt_contains_strong_context_boundaries(self) -> None:
        prompt = build_user_prompt(
            keywords=KeywordConfig(),
            current_text="现在开始签到",
            context_text="无历史文本块",
            chunk_seconds=17.5,
        )
        self.assertIn(HISTORY_CONTEXT_HEADER, prompt)
        self.assertIn("当前待判定区", prompt)
        self.assertIn("最终 important 只能由本区决定", prompt)
        self.assertIn("17.5 秒", prompt)

    def test_build_user_prompt_omits_seconds_when_disabled(self) -> None:
        prompt = build_user_prompt(
            keywords=KeywordConfig(),
            current_text="现在开始签到",
            context_text="无历史文本块",
            chunk_seconds=0.0,
        )
        self.assertIn("当前待判定区", prompt)
        self.assertNotIn("0.1 秒", prompt)

    def test_build_history_context_block_wraps_empty_and_raw_history(self) -> None:
        empty = build_history_context_block("")
        self.assertIn(NO_HISTORY_CONTEXT_LINE, empty)

        wrapped = build_history_context_block("[seq=1][20260308_120000] hello")
        self.assertIn(HISTORY_CONTEXT_HEADER, wrapped)
        self.assertIn("[seq=1][20260308_120000] hello", wrapped)


if __name__ == "__main__":
    unittest.main()
