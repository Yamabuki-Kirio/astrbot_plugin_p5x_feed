import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_p5x_feed.core import Post, Store, clock, qualifies
from astrbot_plugin_p5x_feed.sources import tieba_post


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"
        self.db = Store(self.path)
        self.cfg = {}
        self.now = 1800000000

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def post(self, likes=49, identity="1", good=False):
        return Post("tieba", identity, "测试", "作者", "简介", "https://tieba.baidu.com/p/1", self.now - 100, likes, 1, good)

    def test_threshold_and_missing(self):
        self.assertFalse(qualifies(self.post(), {}, self.now))
        self.assertTrue(qualifies(self.post(50), {}, self.now))
        self.assertFalse(qualifies(self.post(None), {}, self.now))
        self.assertFalse(qualifies(self.post(50), {}, self.now + 7 * 86400))
        self.assertTrue(qualifies(self.post(None, good=True), {}, self.now))

    def test_baseline_crossing_and_preview(self):
        self.db.ingest("tieba", [self.post(100, "old"), self.post()], {}, self.now)
        self.db.subscribe("group", "20:00", self.now)
        self.db.ingest("tieba", [self.post(100, "old"), self.post(50)], {}, self.now + 10)
        self.assertEqual([p.id for p in self.db.candidates({}, self.now + 10, "group")], ["1"])
        self.assertEqual(len(self.db.candidates({}, self.now + 10)), 2)

    def test_history_does_not_reenter(self):
        self.db.ingest("tieba", [self.post(100)], {}, self.now)
        self.db.subscribe("group", "20:00", self.now)
        self.db.ingest("tieba", [self.post(10)], {}, self.now + 1)
        self.db.ingest("tieba", [self.post(100)], {}, self.now + 2)
        self.assertEqual(self.db.candidates({}, self.now + 2, "group"), [])

    def test_claim_crash_and_group_isolation(self):
        self.db.ingest("tieba", [self.post()], {}, self.now)
        for group in ("a", "b"):
            self.db.subscribe(group, "20:00", self.now)
        self.db.ingest("tieba", [self.post(50)], {}, self.now + 1)
        items = self.db.candidates({}, self.now + 1, "a")
        self.assertTrue(self.db.claim("a", "2027-01-01", items))
        self.assertFalse(self.db.claim("a", "2027-01-01", []))
        self.db.close()
        self.db = Store(self.path)
        self.assertEqual(self.db.candidates({}, self.now + 2, "a"), [])
        self.assertEqual(len(self.db.candidates({}, self.now + 2, "b")), 1)
        self.assertEqual(self.db.db.execute("SELECT state FROM runs").fetchone()[0], "unknown")

    def test_failure_is_not_initial_baseline(self):
        self.db.failure("tieba", self.now, "网络错误")
        self.db.ingest("tieba", [self.post(50)], {}, self.now + 1)
        self.assertEqual(self.db.db.execute("SELECT qualified_at FROM posts").fetchone()[0], 0)

    def test_unsubscribe_disabled_stale_and_blocked(self):
        self.db.ingest("tieba", [self.post()], {}, self.now)
        self.db.subscribe("a", "20:00", self.now)
        self.db.ingest("tieba", [self.post(50)], {}, self.now + 1)
        self.assertEqual(self.db.candidates({"tieba_enabled": False}, self.now + 1), [])
        self.assertEqual(self.db.candidates({"blocked_words": ["测试"]}, self.now + 1), [])
        self.assertEqual(self.db.candidates({}, self.now + 86402), [])
        self.db.unsubscribe("a")
        self.db.subscribe("a", "21:00", self.now + 2)
        self.assertEqual(self.db.candidates({}, self.now + 3, "a"), [])

    def test_theme_likes_and_time_validation(self):
        p = tieba_post({"id": 1, "title": "a", "agree": {"agree_num": 55}}, {"agree": {"agree_num": 25}})
        self.assertEqual(p.likes, 55)
        self.assertEqual(clock("每天9:05"), "09:05")
        for value in ("24:00", "20:60", "-1:00"):
            with self.assertRaises(ValueError):
                clock(value)


if __name__ == "__main__":
    unittest.main()
