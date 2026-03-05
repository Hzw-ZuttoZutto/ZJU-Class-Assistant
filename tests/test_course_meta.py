from __future__ import annotations

import unittest

from src.common.course_meta import course_teachers, parse_course_data


class CourseMetaTests(unittest.TestCase):
    def test_parse_course_data_v3(self) -> None:
        payload = {"code": 0, "data": {"title": "X"}}
        self.assertEqual(parse_course_data(payload), {"title": "X"})

    def test_parse_course_data_legacy(self) -> None:
        payload = {
            "success": True,
            "result": {"err": 0, "data": {"title": "Y"}},
        }
        self.assertEqual(parse_course_data(payload), {"title": "Y"})

    def test_course_teachers_prioritizes_realname(self) -> None:
        data = {
            "realname": "主讲老师",
            "teachers": [{"name": "助教A"}, {"realname": "助教B"}],
        }
        self.assertEqual(course_teachers(data), ["主讲老师", "助教A", "助教B"])


if __name__ == "__main__":
    unittest.main()
