import json, sys, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class TestDifyClient(unittest.TestCase):

    def test_format_dialogue_interleaves_messages(self):
        from monitor import format_dialogue
        messages = [
            {"query": "超声刀多少钱", "answer": "宝宝留个微信～"},
            {"query": "你没回答我", "answer": "亲爱的价格区间在2100左右"},
        ]
        result = format_dialogue(messages)
        self.assertIn("[顾客] 超声刀多少钱", result)
        self.assertIn("[AI] 宝宝留个微信～", result)
        self.assertIn("[顾客] 你没回答我", result)

    def test_format_dialogue_skips_empty(self):
        from monitor import format_dialogue
        messages = [{"query": "你好", "answer": ""}]
        result = format_dialogue(messages)
        self.assertIn("[顾客] 你好", result)
        self.assertNotIn("[AI]", result)

    @patch("monitor._get_all_member_ids")
    @patch("monitor.requests.Session")
    def test_fetch_conversations_filters_by_since(self, mock_session_cls, mock_member_ids):
        from monitor import fetch_conversations
        mock_member_ids.return_value = ["1001"]
        now = datetime.now()
        old_ts = (now - timedelta(hours=48)).timestamp()
        new_ts = (now - timedelta(hours=1)).timestamp()

        session = MagicMock()
        session.get.return_value.json.return_value = {
            "data": [
                {"id": "new1", "updated_at": new_ts},
                {"id": "old1", "updated_at": old_ts},
            ],
            "has_more": False,
        }
        session.get.return_value.raise_for_status = MagicMock()
        mock_session_cls.return_value = session

        since = now - timedelta(hours=24)
        result = fetch_conversations(since)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "new1")


class TestConversionDetection(unittest.TestCase):

    @patch("monitor.llm_local")
    def test_detect_conversion_true(self, mock_llm):
        from monitor import detect_conversion
        mock_llm.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content='{"留资": true}'))
        ]
        self.assertTrue(detect_conversion("[顾客] 我微信是 abc123"))

    @patch("monitor.llm_local")
    def test_detect_conversion_false(self, mock_llm):
        from monitor import detect_conversion
        mock_llm.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content='{"留资": false}'))
        ]
        self.assertFalse(detect_conversion("[顾客] 超声刀多少钱"))

    @patch("monitor.llm_local")
    def test_detect_conversion_fallback_on_bad_json(self, mock_llm):
        from monitor import detect_conversion
        mock_llm.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="true"))
        ]
        self.assertTrue(detect_conversion("任意对话"))


class TestScoring(unittest.TestCase):

    @patch("monitor.llm_ds")
    def test_score_returns_dict(self, mock_llm):
        from monitor import score_conversation
        mock_llm.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content=json.dumps({
                "score": 0.35,
                "problems": ["重复追问"],
                "violations": [{"rule": "禁止...", "evidence": "x", "impact": "y"}],
                "bad_turn": "AI第3条",
                "suggestion": "补充FAQ"
            })))
        ]
        result = score_conversation(
            "[顾客] 多少钱\n[AI] 留个微信\n[顾客] 你没回答",
            system_prompt="测试规则",
        )
        self.assertEqual(result["score"], 0.35)
        self.assertIn("重复追问", result["problems"])
        self.assertEqual(len(result["violations"]), 1)

    @patch("monitor.llm_ds")
    def test_score_fallback_on_bad_json(self, mock_llm):
        from monitor import score_conversation
        mock_llm.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="解析失败的文本"))
        ]
        result = score_conversation("任意对话", system_prompt="x")
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["violations"], [])

    @patch("monitor.llm_ds")
    def test_score_passes_system_prompt_into_request(self, mock_llm):
        from monitor import score_conversation
        mock_llm.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content='{"score":1.0,"problems":[],"violations":[]}'))
        ]
        score_conversation("dummy", system_prompt="禁止使用我们")
        sent_prompt = mock_llm.chat.completions.create.call_args[1]["messages"][0]["content"]
        self.assertIn("禁止使用我们", sent_prompt)


class TestNormalizeMessages(unittest.TestCase):

    def test_normalize_messages_pairs_to_openai_roles(self):
        from monitor import normalize_messages
        result = normalize_messages([
            {"query": "你好", "answer": "亲爱的"},
            {"query": "丽珠兰多少钱", "answer": "黑盒3400元"},
        ])
        self.assertEqual(result, [
            {"role": "user",      "content": "你好"},
            {"role": "assistant", "content": "亲爱的"},
            {"role": "user",      "content": "丽珠兰多少钱"},
            {"role": "assistant", "content": "黑盒3400元"},
        ])

    def test_normalize_messages_skips_empty(self):
        from monitor import normalize_messages
        result = normalize_messages([{"query": "你好", "answer": ""}])
        self.assertEqual(result, [{"role": "user", "content": "你好"}])


class TestStats(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def test_append_and_load(self):
        import monitor as m
        from pathlib import Path
        orig_stats  = m.STATS
        orig_reports = m.REPORTS
        m.STATS   = Path(self.tmpdir) / "stats.json"
        m.REPORTS = Path(self.tmpdir)

        try:
            m.append_stats("2026-04-27", 83, 31, 11)
            data = m.load_stats()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["date"], "2026-04-27")
            self.assertAlmostEqual(data[0]["rate"], 0.373, places=2)
            self.assertEqual(data[0]["bad"], 11)
        finally:
            m.STATS   = orig_stats
            m.REPORTS = orig_reports

    def test_append_overwrites_same_date(self):
        import monitor as m
        from pathlib import Path
        orig_stats  = m.STATS
        orig_reports = m.REPORTS
        m.STATS   = Path(self.tmpdir) / "stats.json"
        m.REPORTS = Path(self.tmpdir)
        try:
            m.append_stats("2026-04-27", 80, 28, 9)
            m.append_stats("2026-04-27", 83, 31, 11)
            data = m.load_stats()
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["total"], 83)
        finally:
            m.STATS   = orig_stats
            m.REPORTS = orig_reports


class TestDailyReport(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def _run_report(self, results):
        import monitor as m
        from pathlib import Path
        orig = m.REPORTS
        m.REPORTS = Path(self.tmpdir)
        try:
            return m.generate_daily_report("2026-04-27", results)
        finally:
            m.REPORTS = orig

    def test_report_contains_rate(self):
        results = [
            {"id": "abc123", "converted": True,  "score": {"score": 0.9, "problems": [], "bad_turn": "", "suggestion": ""}},
            {"id": "def456", "converted": False, "score": {"score": 0.3, "problems": ["重复追问"], "bad_turn": "AI回复", "suggestion": "补FAQ"}},
        ]
        path = self._run_report(results)
        content = path.read_text(encoding="utf-8")
        self.assertIn("50.0%", content)
        self.assertIn("重复追问", content)
        self.assertIn("补FAQ", content)

    def test_report_marks_conversion_status(self):
        results = [
            {"id": "abc123", "converted": False, "score": {"score": 0.3, "problems": [], "bad_turn": "x", "suggestion": "y"}},
            {"id": "def456", "converted": True,  "score": {"score": 0.4, "problems": [], "bad_turn": "x", "suggestion": "y"}},
        ]
        path = self._run_report(results)
        content = path.read_text(encoding="utf-8")
        self.assertIn("未留资", content)
        self.assertIn("已留资", content)

    def test_report_no_bad_section_when_all_good(self):
        results = [
            {"id": "abc123", "converted": True, "score": {"score": 0.9, "problems": [], "bad_turn": "", "suggestion": ""}},
        ]
        path = self._run_report(results)
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("劣质对话详情", content)


class TestStructuredReport(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def test_structured_report_includes_messages_and_violations(self):
        import monitor as m, json
        from pathlib import Path
        orig = m.REPORTS
        m.REPORTS = Path(self.tmpdir)
        try:
            results = [
                {
                    "id": "abc123",
                    "user_id": "5001",
                    "converted": False,
                    "messages": [
                        {"role": "user", "content": "丽珠兰多少钱"},
                        {"role": "assistant", "content": "约3400"},
                    ],
                    "score": {
                        "score": 0.3,
                        "problems": ["编造价格"],
                        "violations": [{"rule": "不得自行报具体价格", "evidence": "约3400", "impact": "可信度下降"}],
                        "customer_turn": "丽珠兰多少钱",
                        "bad_turn": "约3400",
                        "suggestion": "引导留微信",
                    },
                },
            ]
            path = m.save_structured_report("2026-04-27", results, threshold=0.6)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["summary"]["bad_count"], 1)
            self.assertFalse(data["bad_conversations"][0]["converted"])
            self.assertEqual(len(data["bad_conversations"][0]["messages"]), 2)
            self.assertEqual(
                data["bad_conversations"][0]["violations"][0]["rule"],
                "不得自行报具体价格",
            )
        finally:
            m.REPORTS = orig


class TestWeeklyReport(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def test_weekly_report_shows_trend(self):
        import monitor as m
        import datetime
        from pathlib import Path
        orig_stats   = m.STATS
        orig_reports = m.REPORTS
        m.STATS   = Path(self.tmpdir) / "stats.json"
        m.REPORTS = Path(self.tmpdir)
        try:
            today = datetime.date.today()
            for i in range(7):
                day = today - datetime.timedelta(days=6 - i)
                m.append_stats(day.isoformat(), 80, 20 + i, 5)
            prev_monday = today - datetime.timedelta(days=today.weekday() + 7)
            for i in range(7):
                day = prev_monday + datetime.timedelta(days=i)
                m.append_stats(day.isoformat(), 80, 15, 8)

            path = m.generate_weekly_report()
            content = path.read_text(encoding="utf-8")
            self.assertIn("留资率趋势", content)
            self.assertIn("↑", content)
        finally:
            m.STATS   = orig_stats
            m.REPORTS = orig_reports


if __name__ == "__main__":
    unittest.main()
