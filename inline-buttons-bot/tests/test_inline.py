"""Inline-режим: сохранение поста, поиск через @бота, /posts и /delpost.

Сценарии прогоняются через FakeAPI из test_flow: сети нет.
"""

import logging
import tempfile
import unittest
from pathlib import Path

import main
from db import Database
from tests.test_flow import FakeAPI, press_update

ADMIN = 1
ALICE = 10


def _user(user_id: int, name: str) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": name, "username": name.lower()}


class InlineFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._saved_interval = main.SEND_INTERVAL
        main.SEND_INTERVAL = 0
        self._bot_logger = logging.getLogger("bot")
        self._saved_level = self._bot_logger.level
        self._bot_logger.setLevel(logging.CRITICAL)
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "bot.db"
        self.db = Database(db_path)
        config = main.Config(token="123456:TEST", admin_ids=frozenset({ADMIN}), db_path=db_path)
        self.api = FakeAPI()
        self.app = main.App(config=config, db=self.db, api=self.api, bot_username="testbot")
        self.update_id = 0

    async def asyncTearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()
        main.SEND_INTERVAL = self._saved_interval
        self._bot_logger.setLevel(self._saved_level)

    # ---------- помощники

    def _next_id(self) -> int:
        self.update_id += 1
        return self.update_id

    async def raw_message(self, user_id: int, *, message_id: int = 1, **fields) -> None:
        message = {
            "message_id": message_id,
            "date": 1_700_000_000,
            "chat": {"id": user_id, "type": "private", "first_name": "x"},
            "from": _user(user_id, "x"),
            **fields,
        }
        await main.handle_update(self.app, {"update_id": self._next_id(), "message": message})

    async def text(self, user_id: int, text: str, message_id: int = 1) -> None:
        await self.raw_message(user_id, message_id=message_id, text=text)

    async def press(self, user_id: int, data: str) -> None:
        await main.handle_update(self.app, press_update(self._next_id(), user_id, "x", data))

    async def inline_press(self, user_id: int, data: str) -> None:
        update = press_update(self._next_id(), user_id, "x", data)
        del update["callback_query"]["message"]  # нажатие под сообщением, отправленным через inline
        await main.handle_update(self.app, update)

    async def inline(self, user_id: int, query: str) -> dict:
        """Отправить @-запрос и вернуть параметры answerInlineQuery."""
        update_id = self._next_id()
        update = {
            "update_id": update_id,
            "inline_query": {"id": f"iq{update_id}", "from": _user(user_id, "x"), "query": query, "offset": ""},
        }
        await main.handle_update(self.app, update)
        return self.calls("answerInlineQuery")[-1]

    def calls(self, method: str) -> list[dict]:
        return [payload for name, payload in self.api.calls if name == method]

    def texts_to(self, chat_id: int) -> list[str]:
        return [payload["text"] for payload in self.calls("sendMessage") if payload["chat_id"] == chat_id]

    async def save_text_post(self, text: str, message_id: int = 1) -> None:
        """Подготовить текстовый пост без кнопок и сохранить его для inline-режима."""
        await self.text(ADMIN, text, message_id=message_id)
        await self.text(ADMIN, "/skip")
        await self.press(ADMIN, "post:save")

    # ---------- сценарии

    async def test_text_post_saved_and_sent_from_inline(self):
        await self.raw_message(
            ADMIN,
            message_id=200,
            text="Акция: скидка 10%\nДо пятницы",
            entities=[{"type": "bold", "offset": 0, "length": 6}],
        )
        await self.text(ADMIN, "Сайт - https://example.com | Окно - popup: Скидка действует до пятницы")
        await self.press(ADMIN, "post:save")

        saved = self.calls("editMessageText")[-1]["text"]
        self.assertIn("Пост №1 сохранён", saved)
        self.assertIn("@testbot", saved)

        answer = await self.inline(ADMIN, "")
        results = answer["results"]
        self.assertEqual(len(results), 1)
        article = results[0]
        self.assertEqual(article["type"], "article")
        self.assertEqual(article["title"], "Акция: скидка 10%")
        self.assertEqual(
            article["input_message_content"],
            {
                "message_text": "Акция: скидка 10%\nДо пятницы",
                "entities": [{"type": "bold", "offset": 0, "length": 6}],
            },
        )
        row = article["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(row[0], {"text": "Сайт", "url": "https://example.com"})
        self.assertEqual(row[1], {"text": "Окно", "callback_data": "pp:1"})
        self.assertEqual((answer["cache_time"], answer["is_personal"]), (0, True))

    async def test_popup_button_of_inline_message_shows_alert(self):
        await self.text(ADMIN, "Пост с окном", message_id=601)
        await self.text(ADMIN, "Окно - popup: Работает!")  # popup-текст попадает в базу
        await self.press(ADMIN, "post:save")
        await self.inline_press(ALICE, "pp:1")  # нажатие под сообщением, отправленным через inline
        answer = self.calls("answerCallbackQuery")[-1]
        self.assertEqual(answer["text"], "Работает!")
        self.assertTrue(answer["show_alert"])

    async def test_photo_post_saved_with_caption(self):
        await self.raw_message(
            ADMIN,
            message_id=210,
            photo=[
                {"file_id": "small", "file_unique_id": "a", "width": 1, "height": 1},
                {"file_id": "big", "file_unique_id": "b", "width": 9, "height": 9},
            ],
            caption="Афиша на субботу",
        )
        await self.text(ADMIN, "/skip")
        await self.press(ADMIN, "post:save")
        [result] = (await self.inline(ADMIN, ""))["results"]
        self.assertEqual(result["type"], "photo")
        self.assertEqual(result["photo_file_id"], "big")
        self.assertEqual(result["caption"], "Афиша на субботу")
        self.assertEqual(result["title"], "Фото: Афиша на субботу")
        self.assertNotIn("reply_markup", result)

    async def test_unsupported_type_has_no_save_button(self):
        await self.raw_message(
            ADMIN,
            message_id=220,
            sticker={"file_id": "st", "file_unique_id": "s", "type": "regular", "width": 512, "height": 512},
        )
        await self.text(ADMIN, "/skip")
        keyboard = self.calls("sendMessage")[-1]["reply_markup"]
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertNotIn("post:save", callbacks)

        await self.press(ADMIN, "post:save")  # старое нажатие всё равно не должно сохранить пост
        self.assertEqual(self.calls("answerCallbackQuery")[-1]["text"], main.UNSUPPORTED_FOR_INLINE)
        self.assertEqual(self.db.list_prepared(), [])

    async def test_inline_search_filters_by_title(self):
        await self.save_text_post("Скидка 10% до пятницы", message_id=301)
        await self.save_text_post("Новая коллекция", message_id=302)
        self.assertEqual(len((await self.inline(ADMIN, "скидка"))["results"]), 1)
        self.assertEqual(len((await self.inline(ADMIN, ""))["results"]), 2)
        empty = await self.inline(ADMIN, "нет такого")
        self.assertEqual(empty["results"], [])

    async def test_admin_without_posts_gets_prepare_button(self):
        answer = await self.inline(ADMIN, "")
        self.assertEqual(answer["results"], [])
        self.assertEqual(answer["button"], {"text": "Подготовить пост", "start_parameter": "prepare"})

    async def test_other_users_get_empty_inline_list(self):
        await self.save_text_post("Секрет админа")
        answer = await self.inline(ALICE, "")
        self.assertEqual(answer["results"], [])
        self.assertNotIn("button", answer)

    async def test_posts_list_and_delete(self):
        await self.save_text_post("Первый пост", message_id=401)
        await self.save_text_post("Второй пост", message_id=402)

        await self.text(ADMIN, "/posts")
        listing = self.texts_to(ADMIN)[-1]
        self.assertIn("№1 · Первый пост", listing)
        self.assertIn("№2 · Второй пост", listing)

        await self.text(ADMIN, "/delpost 1")
        self.assertEqual(self.texts_to(ADMIN)[-1], "🗑 Пост №1 удалён.")
        await self.text(ADMIN, "/posts")
        self.assertNotIn("№1", self.texts_to(ADMIN)[-1])

        await self.text(ADMIN, "/delpost abc")
        self.assertEqual(self.texts_to(ADMIN)[-1], main.DELPOST_USAGE)
        await self.text(ADMIN, "/delpost 99")
        self.assertEqual(self.texts_to(ADMIN)[-1], "Поста №99 нет. Номера смотри в /posts.")

        self.assertEqual([p.title for p in self.db.list_prepared()], ["Второй пост"])

    async def test_posts_commands_are_admin_only(self):
        await self.text(ALICE, "/posts")
        self.assertEqual(self.texts_to(ALICE)[-1], main.USER_HINT)
        await self.text(ALICE, "/delpost 1")
        self.assertEqual(self.texts_to(ALICE)[-1], main.USER_HINT)

    async def test_cancel_before_save_keeps_nothing(self):
        await self.text(ADMIN, "Черновик", message_id=501)
        await self.text(ADMIN, "/skip")
        await self.press(ADMIN, "post:cancel")
        self.assertEqual(self.db.list_prepared(), [])

    def test_inline_queries_are_allowed_in_polling(self):
        self.assertIn("inline_query", main.ALLOWED_UPDATES)


if __name__ == "__main__":
    unittest.main()
