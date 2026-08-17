import unittest
from datetime import date, timedelta

from weekly_report import build_topics, title_phrases


class WeeklyTopicExtractionTests(unittest.TestCase):
    def test_question_template_does_not_leak_partial_phrase(self):
        phrases = title_phrases("如何看待国家这一次的扫黑除恶专项行动？")
        self.assertNotIn("何看待", phrases)
        self.assertNotIn("如何看", phrases)

    def test_scope_modifier_is_not_a_topic(self):
        self.assertNotIn("大规模", title_phrases("手机之后可能都要大规模涨价"))

    def test_complete_entity_beats_truncated_window(self):
        titles = [
            "宇树科技发行价格150.80元每股",
            "宇树科技首次公开发行受到关注",
            "宇树科技下周打新",
        ]
        start = date(2026, 8, 1)
        items = [
            {"date": start + timedelta(days=i % 3), "source": f"source-{i}", "title": title, "url": ""}
            for i, title in enumerate(titles * 3)
        ]
        topics = build_topics(items)
        names = [topic["name"] for topic in topics]
        self.assertIn("宇树科技", names)
        self.assertNotIn("宇树科", names)


if __name__ == "__main__":
    unittest.main()
