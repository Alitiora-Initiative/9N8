import tempfile
import unittest
from pathlib import Path

from src.user_store import UserStore


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = UserStore(
            str(Path(self.temp.name) / "db.sqlite3"), max_lots=0.1, max_positions=3,
            default_sl=500, default_tp=1000, default_be_trigger=150, default_be_offset=0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_register_creates_private_safe_defaults(self):
        user = self.store.register(1234, 5678, "trader", "Test")
        self.assertEqual(user["telegram_id"], 1234)
        self.assertEqual(user["max_lots"], 0.1)
        self.assertEqual(user["paper_enabled"], 0)
        self.assertEqual(user["share_stats"], 0)

    def test_settings_are_persisted_per_user(self):
        self.store.register(1, 1, None, "A")
        self.store.register(2, 2, None, "B")
        self.store.update(1, "max_lots", 0.02)
        self.assertEqual(self.store.get_user(1)["max_lots"], 0.02)
        self.assertEqual(self.store.get_user(2)["max_lots"], 0.1)

    def test_group_stats_are_opt_in(self):
        self.store.register(1, 1, None, "A")
        self.store.register(2, 2, None, "B")
        self.store.update(1, "share_stats", 1)
        self.assertEqual(self.store.opted_in_user_ids(), [1])

    def test_report_chat_is_persistent(self):
        self.store.add_report_chat(-100123, "Trade group", "supergroup", 99)
        self.assertEqual(self.store.report_chats()[0]["chat_id"], "-100123")
        self.assertTrue(self.store.remove_report_chat(-100123))
        self.assertEqual(self.store.report_chats(), [])


if __name__ == "__main__":
    unittest.main()
