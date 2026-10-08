"""Сценарии бота целиком: команды, создание поста, предпросмотр, рассылка.

Сеть не трогаем: FakeSession запоминает вызовы Bot API и отвечает заглушками.
"""

import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Chat, Message, MessageId, Update

import main
from db import Database

ADMIN = 1
ALICE = 10
BOB = 11
CAROL = 12  # эта подписчица заблокирует бота


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []
        self.blocked: set[int] = set()  # чаты, где бота заблокировали навсегда
        # chat_id -> по одной функции на каждую попытку; каждая создаёт ошибку Telegram
        self.copy_errors: dict[int, list] = {}
        self.fail_answer = False  # отвечать на нажатия кнопок с ошибкой «query is too old»
        self._counter = 1000

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        self.calls.append(method)
        if name == "copyMessage":
            if method.chat_id in self.blocked:
                raise TelegramForbiddenError(
                    method=method, message="Forbidden: bot was blocked by the user"
                )
            queue = self.copy_errors.get(method.chat_id)
            if queue:
                raise queue.pop(0)(method)
            self._counter += 1
            return MessageId(message_id=self._counter)
        if name == "answerCallbackQuery" and self.fail_answer:
            raise TelegramBadRequest(
                method=method,
                message="query is too old and response timeout expired or query id is invalid",
            )
        if name == "sendMessage":
            self._counter += 1
            return Message(
                message_id=self._counter,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
            )
        return True  # answerCallbackQuery, editMessageText и прочее: результат не нужен

    async def close(self) -> None:
        pass

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        raise NotImplementedError("файлы в тестах не скачиваем")
        yield b""  # pragma: no cover — делает функцию генератором


def _user(user_id: int, name: str) -> dict:
    return {"id": user_id, "is_bot": False, "first_name": name, "username": name.lower()}


def _chat(user_id: int, name: str) -> dict:
    return {"id": user_id, "type": "private", "first_name": name}


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._saved_interval = main.SEND_INTERVAL
        main.SEND_INTERVAL = 0  # в тестах не ждём между отправками
        self._bot_logger = logging.getLogger("bot")
        self._saved_level = self._bot_logger.level
        self._bot_logger.setLevel(logging.CRITICAL)  # предупреждения о сбоях не шумят в выводе
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "bot.db"
        self.db = Database(db_path)
        self.config = main.Config(
            token="123456:TEST", admin_ids=frozenset({ADMIN}), db_path=db_path
        )
        self.session = FakeSession()
        self.bot = Bot(token="123456:TEST", session=self.session)
        self.dp = main.create_dispatcher(self.config, self.db)
        self.update_id = 0

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        self.db.close()
        self.tmp.cleanup()
        main.SEND_INTERVAL = self._saved_interval
        self._bot_logger.setLevel(self._saved_level)

    # ---------- помощники

    async def message(
        self,
        user_id: int,
        name: str,
        text: str | None = None,
        *,
        message_id: int = 1,
        photo: bool = False,
    ) -> None:
        self.update_id += 1
        payload: dict = {
            "message_id": message_id,
            "date": 1_700_000_000,
            "chat": _chat(user_id, name),
            "from": _user(user_id, name),
        }
        if text is not None:
            payload["text"] = text
        if photo:
            payload["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 10, "height": 10}]
        update = Update.model_validate({"update_id": self.update_id, "message": payload})
        await self.dp.feed_update(self.bot, update)

    async def press(self, user_id: int, name: str, data: str) -> None:
        self.update_id += 1
        update = Update.model_validate(
            {
                "update_id": self.update_id,
                "callback_query": {
                    "id": f"cb{self.update_id}",
                    "from": _user(user_id, name),
                    "chat_instance": "ci",
                    "data": data,
                    "message": {
                        "message_id": 500 + self.update_id,
                        "date": 1_700_000_000,
                        "chat": _chat(user_id, name),
                        "text": "решение",
                    },
                },
            }
        )
        await self.dp.feed_update(self.bot, update)

    def calls(self, method_name: str) -> list:
        return [c for c in self.session.calls if c.__api_method__ == method_name]

    def texts_to(self, chat_id: int) -> list[str]:
        return [c.text for c in self.calls("sendMessage") if c.chat_id == chat_id]

    def copies_to_users(self) -> list:
        return [c for c in self.calls("copyMessage") if c.chat_id != ADMIN]

    async def register_subscribers(self) -> None:
        await self.message(ALICE, "alice", "/start")
        await self.message(BOB, "bob", "/start")
        await self.message(CAROL, "carol", "/start")

    # ---------- сценарии

    async def test_broadcast_with_url_buttons(self):
        await self.register_subscribers()
        self.session.blocked.add(CAROL)

        await self.message(ADMIN, "admin", "/start")
        await self.message(ADMIN, "admin", "Привет всем!", message_id=50)
        await self.message(
            ADMIN, "admin", "Сайт - https://example.com | Поддержка - https://t.me/support"
        )

        # Предпросмотр: копия поста админу с кнопками и вопрос про получателей.
        preview = self.calls("copyMessage")[-1]
        self.assertEqual((preview.chat_id, preview.from_chat_id, preview.message_id), (ADMIN, ADMIN, 50))
        first_row = preview.reply_markup.inline_keyboard[0]
        self.assertEqual([(b.text, b.url) for b in first_row],
                         [("Сайт", "https://example.com"), ("Поддержка", "https://t.me/support")])
        self.assertTrue(any("Кому отправить" in t for t in self.texts_to(ADMIN)))

        await self.press(ADMIN, "admin", "post:all")
        await main.wait_for_background()

        copies = self.copies_to_users()
        self.assertEqual(sorted(c.chat_id for c in copies), [ALICE, BOB, CAROL])
        for copy in copies:
            self.assertEqual((copy.from_chat_id, copy.message_id), (ADMIN, 50))
            self.assertEqual(copy.reply_markup.inline_keyboard[0][0].url, "https://example.com")

        self.assertFalse(self.db.get_user(CAROL).subscribed)  # заблокировала — отписана
        self.assertTrue(self.db.get_user(ALICE).subscribed)
        report = self.texts_to(ADMIN)[-1]
        self.assertIn("Получателей: 3", report)
        self.assertIn("Доставлено: 2", report)

        # После рассылки черновик сброшен: новое сообщение снова становится черновиком.
        await self.message(ADMIN, "admin", "Ещё один пост", message_id=51)
        self.assertIn("Пост сохранён", self.texts_to(ADMIN)[-1])

    async def test_photo_post_with_skip_goes_without_buttons(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(ADMIN, "admin", photo=True, message_id=95)
        await self.message(ADMIN, "admin", "/skip")
        await self.press(ADMIN, "admin", "post:all")
        await main.wait_for_background()

        copies = self.copies_to_users()
        self.assertEqual([c.chat_id for c in copies], [ALICE])
        self.assertEqual(copies[0].message_id, 95)
        self.assertIsNone(copies[0].reply_markup)

    async def test_popup_button_shows_alert(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(ADMIN, "admin", "Пост", message_id=60)
        await self.message(ADMIN, "admin", "Подробнее - popup: Текст всплывающего окна")

        preview = self.calls("copyMessage")[-1]
        self.assertEqual(preview.reply_markup.inline_keyboard[0][0].callback_data, "pp:1")

        await self.press(ADMIN, "admin", "post:cancel")
        self.assertEqual(self.copies_to_users(), [])
        self.assertEqual(self.calls("copyMessage")[-1].chat_id, ADMIN)  # только предпросмотр

        await self.press(ALICE, "alice", "pp:1")
        answer = self.calls("answerCallbackQuery")[-1]
        self.assertEqual(answer.text, "Текст всплывающего окна")
        self.assertTrue(answer.show_alert)

    async def test_send_to_one_user_by_username(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(ADMIN, "admin", "Личное сообщение", message_id=70)
        await self.message(ADMIN, "admin", "/skip")
        await self.press(ADMIN, "admin", "post:one")

        await self.message(ADMIN, "admin", "@nobody")
        self.assertIn("Не нашёл такого пользователя", self.texts_to(ADMIN)[-1])

        await self.message(ADMIN, "admin", "@Alice")
        await main.wait_for_background()
        copies = self.copies_to_users()
        self.assertEqual([c.chat_id for c in copies], [ALICE])
        self.assertIsNone(copies[0].reply_markup)
        self.assertEqual(self.texts_to(ADMIN)[-1], "✅ Отправлено: @Alice.")

    async def test_unsubscribed_user_is_not_targeted(self):
        await self.message(BOB, "bob", "/start")
        await self.message(BOB, "bob", "/stop")
        await self.message(ADMIN, "admin", "Пост", message_id=71)
        await self.message(ADMIN, "admin", "/skip")
        await self.press(ADMIN, "admin", "post:one")
        await self.message(ADMIN, "admin", "@bob")
        self.assertIn("отписался", self.texts_to(ADMIN)[-1])
        self.assertEqual(self.copies_to_users(), [])

    async def test_stop_excludes_user_from_broadcast(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(BOB, "bob", "/start")
        await self.message(BOB, "bob", "/stop")
        await self.message(ADMIN, "admin", "Новость", message_id=80)
        await self.message(ADMIN, "admin", "/skip")
        await self.press(ADMIN, "admin", "post:all")
        await main.wait_for_background()
        self.assertEqual({c.chat_id for c in self.copies_to_users()}, {ALICE})

    async def test_invalid_buttons_keep_draft_and_cancel_works(self):
        await self.message(ADMIN, "admin", "Пост", message_id=90)
        await self.message(ADMIN, "admin", "просто текст без кнопок")
        self.assertIn("не понял кнопку", self.texts_to(ADMIN)[-1])
        self.assertEqual(self.calls("copyMessage"), [])  # предпросмотра не было

        await self.message(ADMIN, "admin", "/cancel")
        self.assertEqual(self.texts_to(ADMIN)[-1], main.CANCELLED)

        await self.message(ADMIN, "admin", "/cancel")
        self.assertEqual(self.texts_to(ADMIN)[-1], main.NOTHING_TO_CANCEL)

    async def test_regular_user_cannot_create_post(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(ALICE, "alice", "Хочу рассылку")
        self.assertEqual(self.calls("copyMessage"), [])
        self.assertEqual(self.texts_to(ALICE)[-1], main.USER_HINT)

    async def test_stale_decision_does_not_broadcast_again(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(ADMIN, "admin", "Пост", message_id=100)
        await self.message(ADMIN, "admin", "/skip")
        await self.press(ADMIN, "admin", "post:all")
        await self.press(ADMIN, "admin", "post:all")  # повторное нажатие
        await main.wait_for_background()
        self.assertEqual(len(self.copies_to_users()), 1)
        stale = self.calls("answerCallbackQuery")[-1]
        self.assertEqual(stale.text, main.STALE)
        self.assertTrue(stale.show_alert)

    async def test_failed_button_answer_does_not_block_broadcast(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(ADMIN, "admin", "Пост", message_id=110)
        await self.message(ADMIN, "admin", "/skip")
        self.session.fail_answer = True  # Telegram не принимает ответ на нажатие
        await self.press(ADMIN, "admin", "post:all")
        await main.wait_for_background()
        self.assertEqual([c.chat_id for c in self.copies_to_users()], [ALICE])
        self.assertIn("Доставлено: 1", self.texts_to(ADMIN)[-1])

    async def test_retry_after_is_retried(self):
        await self.message(ALICE, "alice", "/start")
        await self.message(ADMIN, "admin", "Пост", message_id=120)
        await self.message(ADMIN, "admin", "/skip")
        self.session.copy_errors[ALICE] = [
            lambda m: TelegramRetryAfter(method=m, message="Too Many Requests", retry_after=0)
        ]
        await self.press(ADMIN, "admin", "post:all")
        await main.wait_for_background()
        alice_attempts = [c for c in self.copies_to_users() if c.chat_id == ALICE]
        self.assertEqual(len(alice_attempts), 2)  # первая попытка — лимит, вторая — успех
        self.assertIn("Доставлено: 1", self.texts_to(ADMIN)[-1])

    async def test_broadcast_stops_after_many_errors(self):
        for user_id, name in ((ALICE, "alice"), (BOB, "bob"), (CAROL, "carol")):
            await self.message(user_id, name, "/start")
        bad = lambda m: TelegramBadRequest(method=m, message="wrong file identifier")  # noqa: E731
        for user_id in (ALICE, BOB, CAROL):
            self.session.copy_errors[user_id] = [bad, bad, bad]
        await self.message(ADMIN, "admin", "Пост", message_id=130)
        await self.message(ADMIN, "admin", "/skip")
        old_limit = main.ABORT_AFTER_ERRORS
        main.ABORT_AFTER_ERRORS = 2
        try:
            await self.press(ADMIN, "admin", "post:all")
            await main.wait_for_background()
        finally:
            main.ABORT_AFTER_ERRORS = old_limit
        self.assertEqual(len(self.copies_to_users()), 2)  # дальше не пошли
        report = self.texts_to(ADMIN)[-1]
        self.assertIn("остановлена", report)
        self.assertIn("Не отправлено: 1", report)

    async def test_start_and_id_work_without_admins(self):
        await self.message(ALICE, "alice", "/id")
        self.assertEqual(self.texts_to(ALICE)[-1], f"Ваш ID: {ALICE}")
        await self.message(ALICE, "alice", "/start")
        self.assertEqual(self.texts_to(ALICE)[-1], main.USER_GREETING)


if __name__ == "__main__":
    unittest.main()
