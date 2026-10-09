"""Чтение настроек из переменных окружения."""

import os
import unittest
from unittest import mock

import main


class LoadConfigTests(unittest.TestCase):
    def _load(self, env: dict[str, str]) -> main.Config:
        # .env не подгружаем, чтобы тесты не зависели от локального файла.
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(main, "load_dotenv", lambda *args, **kwargs: False),
        ):
            return main.load_config()

    def test_reads_token_and_admins(self):
        config = self._load({"TELEGRAM_BOT_TOKEN": "1:abc", "ADMIN_IDS": "123, 456"})
        self.assertEqual(config.token, "1:abc")
        self.assertEqual(config.admin_ids, frozenset({123, 456}))
        self.assertEqual(config.db_path, main.BASE_DIR / "data" / "bot.db")

    def test_admin_ids_are_optional(self):
        config = self._load({"TELEGRAM_BOT_TOKEN": "1:abc"})
        self.assertEqual(config.admin_ids, frozenset())

    def test_db_path_override(self):
        config = self._load({"TELEGRAM_BOT_TOKEN": "1:abc", "DB_PATH": "/tmp/x.db"})
        self.assertEqual(str(config.db_path), "/tmp/x.db")

    def test_missing_token_is_error(self):
        with self.assertRaisesRegex(main.ConfigError, "TELEGRAM_BOT_TOKEN"):
            self._load({"ADMIN_IDS": "1"})

    def test_bad_admin_ids_is_error(self):
        with self.assertRaisesRegex(main.ConfigError, "ADMIN_IDS"):
            self._load({"TELEGRAM_BOT_TOKEN": "1:abc", "ADMIN_IDS": "@ivan"})


if __name__ == "__main__":
    unittest.main()
