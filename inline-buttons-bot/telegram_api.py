"""Минимальный клиент Telegram Bot API на aiohttp.

Почему не aiogram: при импорте aiogram занимает около 165 МБ памяти. На бесплатном
хостинге с небольшим лимитом такой процесс падает с OOMKilled. Боту нужны лишь
несколько методов Bot API, поэтому они вызываются обычными HTTP-запросами.
"""

import asyncio
import json
from typing import Any

import aiohttp

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT = 25  # секунд: сколько Telegram держит запрос getUpdates


class TelegramError(Exception):
    """Telegram ответил ошибкой или не ответил вовсе."""

    def __init__(self, description: str, error_code: int = 0) -> None:
        super().__init__(description)
        self.description = description
        self.error_code = error_code


class BadRequestError(TelegramError):
    """Неверные параметры, например неверная ссылка в кнопке (400)."""


class UnauthorizedError(TelegramError):
    """Токен не принят Telegram (401)."""


class ForbiddenError(TelegramError):
    """Бот заблокирован пользователем или не может написать ему (403)."""


class ConflictError(TelegramError):
    """С этим токеном уже работает другой экземпляр бота (409)."""


class RetryAfterError(TelegramError):
    """Слишком много запросов (429): подождите retry_after секунд."""

    def __init__(self, description: str, retry_after: int) -> None:
        super().__init__(description, 429)
        self.retry_after = retry_after


class ServerError(TelegramError):
    """Сбой на стороне Telegram (5xx или ответ не в формате JSON)."""


class NetworkError(TelegramError):
    """Нет связи с Telegram: сеть, DNS или таймаут."""


def error_from_response(status: int, payload: dict[str, Any]) -> TelegramError:
    """Подобрать тип исключения по коду ответа Telegram."""
    description = str(payload.get("description") or f"HTTP {status}")
    if status == 429:
        retry_after = (payload.get("parameters") or {}).get("retry_after", 1)
        return RetryAfterError(description, int(retry_after))
    by_status: dict[int, type[TelegramError]] = {
        400: BadRequestError,
        401: UnauthorizedError,
        403: ForbiddenError,
        409: ConflictError,
    }
    if status in by_status:
        return by_status[status](description, status)
    if status >= 500:
        return ServerError(description, status)
    return TelegramError(description, status)


class TelegramAPI:
    """Вызовы методов Bot API: await api.call("sendMessage", chat_id=1, text="привет")."""

    def __init__(self, token: str, base_url: str = API_BASE) -> None:
        # Токен входит только в адрес запроса. В сообщения об ошибках он не попадает.
        self._prefix = f"{base_url}/bot{token}/"
        self._session: aiohttp.ClientSession | None = None

    async def call(self, method: str, *, http_timeout: float = 30, **params: Any) -> Any:
        """Вызвать метод и вернуть поле result. Параметры со значением None не отправляются."""
        payload = {key: value for key, value in params.items() if value is not None}
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                self._prefix + method,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=http_timeout),
            ) as response:
                status = response.status
                body = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Сообщение намеренно без адреса: в нём может оказаться токен.
            raise NetworkError(f"нет связи с Telegram ({type(exc).__name__})") from None
        try:
            data = json.loads(body)
        except ValueError:
            raise ServerError(f"HTTP {status}: ответ Telegram не в формате JSON", status) from None
        if not isinstance(data, dict):
            raise ServerError(f"HTTP {status}: неожиданный ответ", status)
        if not data.get("ok"):
            raise error_from_response(status, data)
        return data.get("result")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
