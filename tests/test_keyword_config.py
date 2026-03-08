from __future__ import annotations

import json
import unittest

from src.live.insight.models import KeywordConfig


class KeywordConfigTests(unittest.TestCase):
    def test_from_json_dict_supports_grouped_rules(self) -> None:
        config = KeywordConfig.from_json_dict(
            {
                "version": 2,
                "global_negative_terms": ["闲聊"],
                "groups": [
                    {
                        "id": "sign_in",
                        "label": "签到",
                        "aliases": ["签到"],
                        "phrases": ["现在开始签到"],
                        "detail_cues": ["签到码"],
                    },
                    {
                        "id": "lab",
                        "label": "实验",
                        "aliases": ["实验"],
                        "phrases": ["现在开始实验"],
                        "detail_cues": ["实验台号"],
                    },
                ],
            }
        )

        self.assertEqual(config.version, 2)
        self.assertTrue(config.has_grouped_rules)
        self.assertEqual(config.effective_negative_terms(), ["闲聊"])
        self.assertEqual([group.id for group in config.groups], ["sign_in", "lab"])
        payload = json.loads(config.prompt_text())
        self.assertEqual(payload["rule_style"], "grouped")
        self.assertEqual(payload["groups"][1]["id"], "lab")

    def test_from_json_dict_keeps_legacy_rules(self) -> None:
        config = KeywordConfig.from_json_dict(
            {
                "version": 1,
                "important_terms": ["作业"],
                "important_phrases": ["布置今天的作业"],
                "negative_terms": ["闲聊"],
            }
        )

        self.assertFalse(config.has_grouped_rules)
        self.assertEqual(config.important_terms, ["作业"])
        self.assertEqual(config.effective_negative_terms(), ["闲聊"])
        payload = json.loads(config.prompt_text())
        self.assertEqual(payload["rule_style"], "legacy")
        self.assertEqual(payload["important_terms"], ["作业"])


if __name__ == "__main__":
    unittest.main()
