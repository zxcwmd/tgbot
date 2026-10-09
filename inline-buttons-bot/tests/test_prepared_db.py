"""Сохранённые посты для inline-режима в базе данных."""

import tempfile
import unittest
from pathlib import Path

from db import Database
from posts import Content, PreparedPost

MARKUP = {"inline_keyboard": [[{"text": "Сайт", "url": "https://example.com"}]]}


class PreparedPostsDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "bot.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_text_post_roundtrip(self):
        content = Content("text", "Акция", [{"type": "bold", "offset": 0, "length": 5}], None)
        post_id = self.db.add_prepared("Акция", content, MARKUP)
        self.assertEqual(self.db.list_prepared(), [PreparedPost(post_id, "Акция", content, MARKUP)])

    def test_media_post_without_markup_roundtrip(self):
        content = Content("photo", "Афиша", None, "file-123")
        post_id = self.db.add_prepared("Фото: Афиша", content, None)
        [post] = self.db.list_prepared()
        self.assertEqual(post, PreparedPost(post_id, "Фото: Афиша", content, None))

    def test_newest_first_and_limit(self):
        for number in range(1, 4):
            self.db.add_prepared(f"Пост {number}", Content("text", f"Пост {number}"), None)
        self.assertEqual([p.title for p in self.db.list_prepared()], ["Пост 3", "Пост 2", "Пост 1"])
        self.assertEqual([p.title for p in self.db.list_prepared(limit=2)], ["Пост 3", "Пост 2"])

    def test_search_by_title_ignores_case(self):
        self.db.add_prepared("Скидка 10%", Content("text", "Скидка 10%"), None)
        self.db.add_prepared("Новость", Content("text", "Новость"), None)
        self.assertEqual([p.title for p in self.db.list_prepared(query="  СКИДК ")], ["Скидка 10%"])
        self.assertEqual(self.db.list_prepared(query="нет"), [])

    def test_delete(self):
        post_id = self.db.add_prepared("Пост", Content("text", "Пост"), None)
        self.assertTrue(self.db.delete_prepared(post_id))
        self.assertFalse(self.db.delete_prepared(post_id))
        self.assertEqual(self.db.list_prepared(), [])


if __name__ == "__main__":
    unittest.main()
