"""Бот для рассылки постов с inline-кнопками в личные сообщения.

Как это работает:
* Человек нажимает /start — бот запоминает его. Бот может писать только тем, кто
  уже писал ему (это правило Telegram), поэтому получатели сначала нажимают /start.
* Админ (ID из ADMIN_IDS) присылает боту пост — текст, фото, видео, что угодно,
  затем кнопки. Бот показывает предпросмотр и спрашивает: всем подписчикам или одному.
* Пост копируется каждому получателю вместе с кнопками.

Запуск: python main.py (настройки — см. README.md).
"""

import asyncio
import logging
import os
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

from buttons import ButtonSpec, ButtonsParseError, build_markup, parse_buttons, popup_id_from_callback
from db import Database

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("bot")

# Пауза между отправками при рассылке: около 20 сообщений в секунду.
# Telegram допускает примерно 30 сообщений в секунду.
SEND_INTERVAL = 0.05
# Сколько раз пробовать отправить одно сообщение (лимиты Telegram и сетевые сбои).
MAX_ATTEMPTS = 3
# Если столько получателей подряд получили непонятную ошибку, рассылку останавливаем.
ABORT_AFTER_ERRORS = 25

# ---------------------------------------------------------------- тексты

USER_GREETING = "Привет! Вы подписаны на рассылку. Чтобы отписаться, отправьте /stop."
USER_HINT = "Это бот для рассылок. Чтобы подписаться, отправьте /start, чтобы отписаться — /stop."
STOPPED = "Вы отписались от рассылки. Чтобы подписаться снова, отправьте /start."
ADMIN_GREETING = "Привет, админ! Бот готов к рассылкам.\n\n"
ADMIN_HELP = """Как отправить пост с кнопками:
1. Пришли пост: текст, фото, видео или любое другое сообщение. Жирный шрифт и ссылки сохранятся.
2. Пришли кнопки или /skip, если кнопки не нужны.
3. Проверь предпросмотр и выбери, кому отправить.

Формат кнопок (каждая строка — отдельный ряд, кнопки в ряду разделяй «|»):
Текст кнопки - https://ссылка
Текст 1 - https://ссылка1 | Текст 2 - https://ссылка2
Текст кнопки - popup: текст во всплывающем окне (до 200 символов)

Команды:
/cancel — отменить текущий пост
/stats — статистика подписчиков
/id — твой ID
/help — эта справка"""
DRAFT_SAVED = (
    "✅ Пост сохранён. Пришли кнопки (формат — /help) или /skip, если без кнопок.\n"
    "Чтобы начать заново — /cancel."
)
BUTTONS_ERROR_SUFFIX = "Проверь формат (/help) и пришли кнопки ещё раз. Чтобы заменить пост — /cancel."
BUTTONS_HINT = "Нужен текст с кнопками (формат — /help), либо /skip, либо /cancel."
PREVIEW_QUESTION = "👆 Так пост увидят получатели. Кому отправить?"
CHOOSE_HINT = "Выбери действие кнопками под предпросмотром или нажми /cancel."
TARGET_QUESTION = (
    "Пришли @username или числовой ID получателя. Человек должен был нажать /start у бота.\n"
    "Отмена — /cancel."
)
TARGET_HINT = "Пришли @username или числовой ID текстом. Отмена — /cancel."
TARGET_NOT_FOUND = (
    "Не нашёл такого пользователя среди тех, кто писал боту. "
    "Попроси его нажать /start или пришли числовой ID."
)
TARGET_UNSUBSCRIBED = "Этот человек отписался от рассылки, поэтому отправлять ему не буду."
CANCELLED = "Отменено. Пост не отправлен."
NOTHING_TO_CANCEL = "Сейчас нечего отменять."
STALE = "Этот пост уже неактуален. Пришли пост заново."
DRAFT_MISSING = "Черновик не найден (возможно, его удалили). Пришли пост заново."
POPUP_MISSING = "Это окно больше недоступно."
UNKNOWN_COMMAND = "Не понял команду. Список команд — /help."


# ---------------------------------------------------------------- настройки


class ConfigError(Exception):
    """Неверные или отсутствующие настройки."""


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset[int]
    db_path: Path


def load_config() -> Config:
    # Локально настройки берутся из файла .env, на хостинге — из переменных окружения.
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "Не задан TELEGRAM_BOT_TOKEN. Возьми токен у @BotFather и добавь его "
            "в переменные окружения (или в файл .env)."
        )
    raw_admins = os.getenv("ADMIN_IDS", "").strip()
    try:
        admin_ids = frozenset(int(part) for part in raw_admins.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigError("ADMIN_IDS: укажи только числа через запятую, например 123456789.") from exc
    db_path = BASE_DIR / (os.getenv("DB_PATH", "").strip() or "data/bot.db")
    return Config(token=token, admin_ids=admin_ids, db_path=db_path)


# ---------------------------------------------------------------- состояния и фильтры


class PostFlow(StatesGroup):
    """Шаги создания поста админом."""

    buttons = State()  # пост сохранён, ждём кнопки или /skip
    choose = State()  # показан предпросмотр, ждём выбор получателей
    target = State()  # ждём @username или ID для отправки одному


class IsAdmin(BaseFilter):
    """Пропускает только админов из ADMIN_IDS. config подставляется из диспетчера."""

    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return event.from_user is not None and event.from_user.id in config.admin_ids


class NotCommand(BaseFilter):
    """Пропускает всё, кроме команд вида /что-то."""

    async def __call__(self, message: Message) -> bool:
        return not (message.text or "").startswith("/")


class CallbackPrefix(BaseFilter):
    """Пропускает нажатия кнопок, у которых callback_data начинается с prefix."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    async def __call__(self, callback: CallbackQuery) -> bool:
        return bool(callback.data) and callback.data.startswith(self.prefix)


class TrackUserMiddleware(BaseMiddleware):
    """Запоминает каждого, кто написал боту в личку."""

    async def __call__(self, handler: Any, event: Message, data: dict[str, Any]) -> Any:
        user = event.from_user
        if user is not None and event.chat.type == ChatType.PRIVATE:
            data["db"].touch_user(user.id, user.username, user.first_name)
        return await handler(event, data)


# ---------------------------------------------------------------- клавиатуры


def decision_keyboard(recipients: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📢 Всем подписчикам ({recipients})", callback_data="post:all"
                )
            ],
            [
                InlineKeyboardButton(text="👤 Одному человеку", callback_data="post:one"),
                InlineKeyboardButton(text="✖ Отмена", callback_data="post:cancel"),
            ],
        ]
    )


def markup_from_state(data: dict[str, Any]) -> InlineKeyboardMarkup | None:
    stored = data.get("markup")
    return InlineKeyboardMarkup.model_validate(stored) if stored else None


# ---------------------------------------------------------------- обработчики админа


async def draft_received(message: Message, state: FSMContext) -> None:
    """Любое сообщение админа без активного шага становится черновиком поста."""
    await state.set_state(PostFlow.buttons)
    await state.set_data(
        {"admin_chat_id": message.chat.id, "source_message_id": message.message_id}
    )
    await message.answer(DRAFT_SAVED)


async def buttons_received(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    try:
        rows = parse_buttons(message.text or "")
    except ButtonsParseError as exc:
        await message.answer(f"❌ Не получилось разобрать кнопки: {exc}\n{BUTTONS_ERROR_SUFFIX}")
        return
    await show_preview(message, state, db, config, rows)


async def skip_buttons(message: Message, state: FSMContext, db: Database, config: Config) -> None:
    await show_preview(message, state, db, config, rows=[])


async def buttons_hint(message: Message) -> None:
    await message.answer(BUTTONS_HINT)


async def show_preview(
    message: Message,
    state: FSMContext,
    db: Database,
    config: Config,
    rows: list[list[ButtonSpec]],
) -> None:
    """Показать админу пост с кнопками так, как его увидят получатели."""
    data = await state.get_data()
    admin_chat_id = data["admin_chat_id"]
    source_message_id = data["source_message_id"]
    markup = build_markup(rows, db.add_popup) if rows else None
    try:
        await message.bot.copy_message(
            chat_id=admin_chat_id,
            from_chat_id=admin_chat_id,
            message_id=source_message_id,
            reply_markup=markup,
        )
    except TelegramBadRequest as exc:
        if "not found" in exc.message.lower():
            await state.clear()
            await message.answer(DRAFT_MISSING)
        else:
            await message.answer(
                f"❌ Telegram не принял пост с этими кнопками: {exc.message}.\n{BUTTONS_ERROR_SUFFIX}"
            )
        return
    await state.update_data(markup=markup.model_dump(exclude_none=True) if markup else None)
    await state.set_state(PostFlow.choose)
    recipients = len(db.subscriber_ids(config.admin_ids))
    await message.answer(PREVIEW_QUESTION, reply_markup=decision_keyboard(recipients))


async def choose_hint(message: Message) -> None:
    await message.answer(CHOOSE_HINT)


async def _awaiting_decision(state: FSMContext) -> bool:
    return (await state.get_state()) == PostFlow.choose.state


async def _answer_callback(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Ответить на нажатие кнопки. Сбой ответа не должен срывать сценарий."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramAPIError as exc:
        logger.info("Не удалось ответить на нажатие кнопки: %s", exc.message)


async def _edit_message(callback: CallbackQuery, text: str) -> None:
    """Заменить текст сообщения с кнопками (кнопки при этом исчезают)."""
    try:
        await callback.message.edit_text(text)
    except TelegramAPIError as exc:
        logger.info("Не удалось изменить сообщение: %s", exc.message)


async def choose_all(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    config: Config,
    bot: Bot,
    decision_lock: asyncio.Lock,
) -> None:
    # Замок защищает от двойного нажатия: иначе рассылка могла бы запуститься дважды.
    async with decision_lock:
        if not await _awaiting_decision(state):
            await _answer_callback(callback, STALE, show_alert=True)
            return
        data = await state.get_data()
        await state.clear()
    await _answer_callback(callback)
    recipients = db.subscriber_ids(config.admin_ids)
    if not recipients:
        await _edit_message(callback, "Некому отправить: подписчиков пока нет.")
        return
    await _edit_message(
        callback, f"🚀 Рассылка запущена: получателей {len(recipients)}. Отчёт пришлю в конце."
    )
    start_background(
        run_broadcast(
            bot,
            db,
            admin_chat_id=data["admin_chat_id"],
            source_message_id=data["source_message_id"],
            markup=markup_from_state(data),
            recipients=recipients,
        )
    )


async def choose_one(callback: CallbackQuery, state: FSMContext, decision_lock: asyncio.Lock) -> None:
    async with decision_lock:
        if not await _awaiting_decision(state):
            await _answer_callback(callback, STALE, show_alert=True)
            return
        await state.set_state(PostFlow.target)
    await _answer_callback(callback)
    await _edit_message(callback, TARGET_QUESTION)


async def choose_cancel(callback: CallbackQuery, state: FSMContext, decision_lock: asyncio.Lock) -> None:
    async with decision_lock:
        if not await _awaiting_decision(state):
            await _answer_callback(callback, STALE, show_alert=True)
            return
        await state.clear()
    await _answer_callback(callback)
    await _edit_message(callback, CANCELLED)


async def stale_post(callback: CallbackQuery) -> None:
    """Нажата кнопка старого поста (например, после перезапуска бота)."""
    await _answer_callback(callback, STALE, show_alert=True)


async def target_received(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("@") and len(raw) > 1:
        user = db.find_by_username(raw[1:])
        if user is None:
            await message.answer(TARGET_NOT_FOUND)
            return
        user_id = user.user_id
    elif raw.isdigit():
        user_id = int(raw)
        user = db.get_user(user_id)  # если человека нет в базе, всё равно попробуем отправить
    else:
        await message.answer(TARGET_HINT)
        return
    if user is not None and not user.subscribed:
        await message.answer(TARGET_UNSUBSCRIBED)
        return
    data = await state.get_data()
    await state.clear()
    delivery = await copy_to(
        bot,
        user_id,
        source_chat_id=data["admin_chat_id"],
        source_message_id=data["source_message_id"],
        markup=markup_from_state(data),
    )
    apply_outcome(db, user_id, delivery)
    if delivery.outcome is Outcome.SENT:
        await message.answer(f"✅ Отправлено: {raw}.")
    else:
        await message.answer(f"❌ Не получилось отправить {raw}: {delivery.error}.")


async def target_hint(message: Message) -> None:
    await message.answer(TARGET_HINT)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(NOTHING_TO_CANCEL)
        return
    await state.clear()
    await message.answer(CANCELLED)


async def cmd_stats(message: Message, db: Database, config: Config) -> None:
    subscribed, total = db.stats(exclude=config.admin_ids)
    await message.answer(
        f"📊 Подписчиков: {subscribed}\n"
        f"Отписались или заблокировали бота: {total - subscribed}\n"
        f"Всего в базе: {total}"
    )


# ---------------------------------------------------------------- обработчики пользователей


async def popup_pressed(callback: CallbackQuery, db: Database) -> None:
    """Нажата кнопка «popup»: показываем всплывающее окно с заданным текстом."""
    popup_id = popup_id_from_callback(callback.data)
    text = db.get_popup(popup_id) if popup_id is not None else None
    if text is None:
        await _answer_callback(callback, POPUP_MISSING, show_alert=True)
    else:
        await _answer_callback(callback, text, show_alert=True)


async def cmd_start(message: Message, db: Database, config: Config) -> None:
    user = message.from_user
    db.subscribe(user.id, user.username, user.first_name)
    if user.id in config.admin_ids:
        await message.answer(ADMIN_GREETING + ADMIN_HELP)
    else:
        await message.answer(USER_GREETING)


async def cmd_stop(message: Message, db: Database) -> None:
    db.set_subscribed(message.from_user.id, False)
    await message.answer(STOPPED)


async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш ID: {message.from_user.id}")


async def cmd_help(message: Message, config: Config) -> None:
    await message.answer(ADMIN_HELP if message.from_user.id in config.admin_ids else USER_HINT)


async def fallback(message: Message, config: Config) -> None:
    if message.from_user.id in config.admin_ids:
        await message.answer(UNKNOWN_COMMAND)
    else:
        await message.answer(USER_HINT)


# ---------------------------------------------------------------- рассылка


class Outcome(Enum):
    SENT = "sent"
    BLOCKED = "blocked"  # бот заблокирован, аккаунт удалён или человек не нажимал /start
    FAILED = "failed"  # любая другая ошибка


@dataclass(frozen=True)
class Delivery:
    outcome: Outcome
    error: str = ""


async def copy_to(
    bot: Bot,
    chat_id: int,
    source_chat_id: int,
    source_message_id: int,
    markup: InlineKeyboardMarkup | None,
) -> Delivery:
    """Скопировать пост в чат chat_id вместе с кнопками.

    copy_message сохраняет форматирование и медиа, но не показывает «переслано от».
    При лимитах Telegram и сетевых сбоях попытку повторяем.
    """
    for _ in range(MAX_ATTEMPTS):
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=markup,
            )
            return Delivery(Outcome.SENT)
        except TelegramRetryAfter as exc:
            logger.warning("Telegram просит подождать %s сек.", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramNetworkError:
            logger.warning("Сетевая ошибка при отправке, повторяю")
            await asyncio.sleep(2)
        except TelegramForbiddenError:
            return Delivery(Outcome.BLOCKED, "пользователь заблокировал бота или не нажимал /start")
        except TelegramBadRequest as exc:
            if "chat not found" in exc.message.lower():
                return Delivery(Outcome.BLOCKED, "пользователь не нажимал /start")
            return Delivery(Outcome.FAILED, exc.message)
        except TelegramAPIError as exc:
            return Delivery(Outcome.FAILED, exc.message)
    return Delivery(Outcome.FAILED, "Telegram не ответил после нескольких попыток")


def apply_outcome(db: Database, user_id: int, delivery: Delivery) -> None:
    """Если человек заблокировал бота, больше ему не пишем."""
    if delivery.outcome is Outcome.BLOCKED:
        db.set_subscribed(user_id, False)


@dataclass
class BroadcastReport:
    total: int
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    aborted: bool = False

    def render(self) -> str:
        if self.aborted:
            head = "⚠️ Рассылка остановлена: слишком много ошибок подряд."
        else:
            head = "✅ Рассылка завершена."
        lines = [
            head,
            f"Получателей: {self.total}",
            f"Доставлено: {self.sent}",
            f"Недоступны (заблокировали бота или не нажимали /start): {self.blocked}",
            f"Ошибки: {self.failed}",
        ]
        not_processed = self.total - self.sent - self.blocked - self.failed
        if not_processed > 0:
            lines.append(f"Не отправлено: {not_processed}")
        return "\n".join(lines)


async def run_broadcast(
    bot: Bot,
    db: Database,
    *,
    admin_chat_id: int,
    source_message_id: int,
    markup: InlineKeyboardMarkup | None,
    recipients: list[int],
) -> None:
    report = BroadcastReport(total=len(recipients))
    errors_in_row = 0
    for user_id in recipients:
        delivery = await copy_to(bot, user_id, admin_chat_id, source_message_id, markup)
        apply_outcome(db, user_id, delivery)
        if delivery.outcome is Outcome.SENT:
            report.sent += 1
            errors_in_row = 0
        elif delivery.outcome is Outcome.BLOCKED:
            report.blocked += 1
            errors_in_row = 0
        else:
            report.failed += 1
            errors_in_row += 1
            logger.warning("Не удалось отправить сообщение: %s", delivery.error)
            if errors_in_row >= ABORT_AFTER_ERRORS:
                report.aborted = True
                break
        await asyncio.sleep(SEND_INTERVAL)
    logger.info("Рассылка: доставлено %d из %d", report.sent, report.total)
    await bot.send_message(admin_chat_id, report.render())


_background_tasks: set[asyncio.Task] = set()


def start_background(coro: Coroutine[Any, Any, Any]) -> None:
    """Запустить рассылку в фоне, чтобы бот продолжал отвечать на другие сообщения."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_task_error)


def _log_task_error(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Ошибка в фоновой рассылке", exc_info=task.exception())


async def wait_for_background() -> None:
    """Дождаться всех фоновых рассылок (используется в тестах)."""
    while _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)


# ---------------------------------------------------------------- сборка и запуск


def build_admin_router() -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.type == ChatType.PRIVATE, IsAdmin())
    router.callback_query.filter(IsAdmin())
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(skip_buttons, Command("skip"), PostFlow.buttons)
    router.message.register(buttons_received, PostFlow.buttons, F.text, NotCommand())
    router.message.register(buttons_hint, PostFlow.buttons, NotCommand())
    router.message.register(target_received, PostFlow.target, F.text, NotCommand())
    router.message.register(target_hint, PostFlow.target, NotCommand())
    router.message.register(choose_hint, PostFlow.choose, NotCommand())
    router.message.register(draft_received, StateFilter(None), NotCommand())
    router.callback_query.register(choose_all, PostFlow.choose, F.data == "post:all")
    router.callback_query.register(choose_one, PostFlow.choose, F.data == "post:one")
    router.callback_query.register(choose_cancel, PostFlow.choose, F.data == "post:cancel")
    router.callback_query.register(stale_post, CallbackPrefix("post:"))
    return router


def build_popup_router() -> Router:
    router = Router(name="popup")
    router.callback_query.register(popup_pressed, CallbackPrefix("pp:"))
    return router


def build_user_router() -> Router:
    router = Router(name="user")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(cmd_start, Command("start"))
    router.message.register(cmd_stop, Command("stop"))
    router.message.register(cmd_id, Command("id"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(fallback)
    return router


def create_dispatcher(config: Config, db: Database) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["config"] = config
    dp["db"] = db
    dp["decision_lock"] = asyncio.Lock()
    dp.message.outer_middleware(TrackUserMiddleware())
    dp.include_router(build_admin_router())
    dp.include_router(build_popup_router())
    dp.include_router(build_user_router())
    return dp


async def run(config: Config) -> None:
    db = Database(config.db_path)
    bot = Bot(token=config.token)
    dp = create_dispatcher(config, db)
    if not config.admin_ids:
        logger.warning(
            "ADMIN_IDS не задан: админ-функции отключены. "
            "Напиши боту /id и добавь полученное число в ADMIN_IDS."
        )
    try:
        await bot.delete_webhook()  # polling не работает, пока включён webhook
        logger.info("Бот запущен. База данных: %s", config.db_path)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        db.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
