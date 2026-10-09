"""Хранилище подписчиков и popup-текстов."""

import tempfile
import unittest
from pathlib import Path

from db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # Вложенная папка проверяет, что база создаёт недостающие каталоги.
        self.db = Database(Path(self.tmp.name) / "nested" / "bot.db")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_touch_keeps_opt_out(self):
        self.db.touch_user(10, "alice", "Alice")
        self.assertTrue(self.db.get_user(10).subscribed)

        self.db.set_subscribed(10, False)
        self.db.touch_user(10, "alice_new", "Alice")
        user = self.db.get_user(10)
        self.assertFalse(user.subscribed)
        self.assertEqual(user.username, "alice_new")

    def test_subscribe_resubscribes(self):
        self.db.touch_user(10, "alice", "Alice")
        self.db.set_subscribed(10, False)
        self.db.subscribe(10, "alice", "Alice")
        self.assertTrue(self.db.get_user(10).subscribed)

    def test_find_by_username_ignores_case(self):
        self.db.touch_user(10, "Alice", "A")
        self.assertEqual(self.db.find_by_username("alice").user_id, 10)
        self.assertIsNone(self.db.find_by_username("bob"))

    def test_subscriber_ids_skip_unsubscribed_and_excluded(self):
        for user_id in (1, 10, 11, 12):
            self.db.touch_user(user_id, None, "x")
        self.db.set_subscribed(11, False)
        self.assertEqual(self.db.subscriber_ids(exclude={1}), [10, 12])

    def test_stats_do_not_count_excluded(self):
        for user_id in (1, 10, 11):
            self.db.touch_user(user_id, None, "x")
        self.db.set_subscribed(11, False)
        self.assertEqual(self.db.stats(exclude={1}), (1, 2))

    def test_popups(self):
        popup_id = self.db.add_popup("Привет")
        self.assertEqual(self.db.get_popup(popup_id), "Привет")
        self.assertIsNone(self.db.get_popup(999))


if __name__ == "__main__":
    unittest.main()
