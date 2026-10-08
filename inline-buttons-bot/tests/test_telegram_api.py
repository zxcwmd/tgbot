"""Клиент Telegram Bot API: разбор ответов и ошибок на настоящем локальном HTTP-сервере."""

import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

import telegram_api as tg

TOKEN = "123456:SECRET-TOKEN"


class ErrorMappingTests(unittest.TestCase):
    def test_429_becomes_retry_after(self):
        error = tg.error_from_response(
            429, {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}
        )
        self.assertIsInstance(error, tg.RetryAfterError)
        self.assertEqual(error.retry_after, 7)

    def test_status_codes(self):
        cases = {
            400: tg.BadRequestError,
            401: tg.UnauthorizedError,
            403: tg.ForbiddenError,
            409: tg.ConflictError,
            500: tg.ServerError,
            418: tg.TelegramError,
        }
        for status, error_type in cases.items():
            with self.subTest(status=status):
                error = tg.error_from_response(status, {"description": "text"})
                self.assertIs(type(error), error_type)
                self.assertEqual(error.description, "text")


class TelegramAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        async def send_message(request: web.Request) -> web.Response:
            body = await request.json()
            if body.get("text") == "blocked":
                return web.json_response(
                    {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
                    status=403,
                )
            return web.json_response({"ok": True, "result": {"message_id": 5, "echo": body["text"]}})

        async def not_json(request: web.Request) -> web.Response:
            return web.Response(text="<html>Bad gateway</html>", status=502)

        app = web.Application()
        app.router.add_post(f"/bot{TOKEN}/sendMessage", send_message)
        app.router.add_post(f"/bot{TOKEN}/other", not_json)
        self.server = TestServer(app)
        await self.server.start_server()
        base_url = str(self.server.make_url("")).rstrip("/")
        self.api = tg.TelegramAPI(TOKEN, base_url=base_url)

    async def asyncTearDown(self) -> None:
        await self.api.close()
        await self.server.close()

    async def test_returns_result_field(self):
        result = await self.api.call("sendMessage", chat_id=1, text="привет", reply_markup=None)
        self.assertEqual(result, {"message_id": 5, "echo": "привет"})

    async def test_api_error_is_raised_with_type(self):
        with self.assertRaises(tg.ForbiddenError) as ctx:
            await self.api.call("sendMessage", chat_id=1, text="blocked")
        self.assertEqual(ctx.exception.description, "Forbidden: bot was blocked")

    async def test_non_json_answer_is_server_error(self):
        with self.assertRaises(tg.ServerError):
            await self.api.call("other")

    async def test_network_error_does_not_leak_token(self):
        api = tg.TelegramAPI(TOKEN, base_url="http://127.0.0.1:9")  # порт закрыт
        try:
            with self.assertRaises(tg.NetworkError) as ctx:
                await api.call("sendMessage", chat_id=1, text="x")
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertNotIn(TOKEN, ctx.exception.description)
        finally:
            await api.close()


if __name__ == "__main__":
    unittest.main()
