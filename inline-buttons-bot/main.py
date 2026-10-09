"""Бот для рассылки постов с inline-кнопками в личные сообщения.

Как это работает:
* Человек нажимает /start — бот запоминает его. Бот может писать только тем, кто
  уже писал ему (это правило Telegram), поэтому получатели сначала нажимают /start.
* Админ (ID из ADMIN_IDS) присылает боту пост — текст, фото, видео, что угодно,
  затем кнопки. Бот показывает предпросмотр и предлагает: отправить всем подписчикам,
  одному человеку или сохранить пост для inline-режима.
* Сохранённый пост админ отправляет из любого чата: пишет @бота и выбирает пост.
  Такое сообщение уходит от аккаунта админа, с кнопками.

Бот работает через long polling и клиент Bot API из telegram_api.py.
aiogram здесь не используется: он занимает ~165 МБ памяти только на импорт.

Запуск: python main.py (настройки — см. README.md).
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from buttons import ButtonSpec, ButtonsParseError, build_markup, parse_buttons, popup_id_from_callback
from db import Database
from posts import RESULTS_LIMIT, Content, extract_content, inline_result, make_title
from telegram_api import (
    POLL_TIMEOUT,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NetworkError,
    RetryAfterError,
    TelegramAPI,
    TelegramError,
    UnauthorizedError,
)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("bot")

# Какие обновления просим у Telegram. Без inline_query бот не увидит @-запросы.
ALLOWED_UPDATES = ("message", "callback_query", "inline_query")
# Пауза между отправками при рассылке: около 20 сообщений в секунду, с запасом до лимита Telegram.
SEND_INTERVAL = 0.05
# Сколько раз пробовать отправить одно сообщение (лимиты Telegram и сетевые сбои).
MAX_ATTEMPTS = 3
# Если столько получателей подряд получили непонятную ошибку, рассылку останавливаем.
ABORT_AFTER_ERRORS = 25
DECISIONS = ("post:all", "post:one", "post:save", "post:cancel")

# ---------------------------------------------------------------- тексты

USER_GREETING = "Привет! Вы подписаны на рассылку. Чтобы отписаться, отправьте /stop."
USER_HINT = "Это бот для рассылок. Чтобы подписаться, отправьте /start, чтобы отписаться — /stop."
STOPPED = "Вы отписались от рассылки. Чтобы подписаться снова, отправьте /start."
ADMIN_GREETING = "Привет, админ! Бот готов к рассылкам.\n\n"
ADMIN_HELP = """Как отправить пост с кнопками:
1. Пришли пост: текст, фото, видео или любое другое сообщение. Жирный шрифт и ссылки сохранятся.
2. Пришли кнопки или /skip, если кнопки не нужны.
3. Проверь предпросмотр и выбери: всем подписчикам, одному человеку или «Сохранить для инлайна».

Формат кнопок (каждая строка — отдельный ряд, кнопки в ряду разделяй «|»):
Текст кнопки - https://ссылка
Текст 1 - https://ссылка1 | Текст 2 - https://ссылка2
Текст кнопки - popup: текст во всплывающем окне (до 200 символов)

Сохранённый пост отправляется из любого чата: напиши в нём @бот и выбери пост из списка.

Команды:
/cancel — отменить текущий пост
/skip — пост без кнопок
/posts — сохранённые посты
/delpost <номер> — удалить сохранённый пост
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
UNSUPPORTED_FOR_INLINE = (
    "Этот тип сообщения нельзя сохранить для инлайн-режима. "
    "Можно текст, фото, видео, GIF, файл, аудио и голосовое."
)
NO_SAVED_POSTS = "Сохранённых постов пока нет. Подготовь пост и выбери «💾 Сохранить для инлайна»."
DELPOST_USAGE = "Укажи номер поста: /delpost 3. Номера смотри в /posts."


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


# ---------------------------------------------------------------- состояние и входящие события


@dataclass
class Flow:
    """Шаг создания поста, на котором сейчас находится админ."""

    step: str  # "buttons" — ждём кнопки; "choose" — ждём выбор; "target" — ждём получателя
    source_message_id: int  # id черновика в чате с админом
    content: Content | None = None  # для inline-режима; None — тип не поддерживается
    markup: dict[str, Any] | None = None


@dataclass
class App:
    """Всё, что нужно обработчикам: настройки, база, клиент Bot API и шаги админов."""

    config: Config
    db: Database
    api: Any  # TelegramAPI; в тестах подменяется заглушкой
    flows: dict[int, Flow] = field(default_factory=dict)
    bot_username: str | None = None  # без «@»; заполняется при запуске через getMe


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    chat_type: str
    message_id: int
    user_id: int
    username: str | None
    first_name: str | None
    text: str | None
    content: Content | None  # что можно сохранить для inline-режима


@dataclass(frozen=True)
class IncomingPress:
    callback_id: str
    user_id: int
    data: str | None
    chat_id: int | None  # под сообщениями, отправленными через inline, чата нет
    message_id: int | None


@dataclass(frozen=True)
class IncomingInlineQuery:
    id: str
    user_id: int
    text: str


def parse_message(raw: dict[str, Any]) -> IncomingMessage:
    chat = raw["chat"]
    sender = raw.get("from") or {}
    return IncomingMessage(
        chat_id=chat["id"],
        chat_type=chat["type"],
        message_id=raw["message_id"],
        user_id=sender.get("id", chat["id"]),
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        text=raw.get("text"),
        content=extract_content(raw),
    )


def parse_press(raw: dict[str, Any]) -> IncomingPress:
    message = raw.get("message") or {}
    return IncomingPress(
        callback_id=raw["id"],
        user_id=raw["from"]["id"],
        data=raw.get("data"),
        chat_id=(message.get("chat") or {}).get("id"),
        message_id=message.get("message_id"),
    )


def parse_inline(raw: dict[str, Any]) -> IncomingInlineQuery:
    return IncomingInlineQuery(id=raw["id"], user_id=raw["from"]["id"], text=raw.get("query", ""))


def parse_command(text: str | None) -> str | None:
    """Имя команды без «/» и без «@бота»: '/start@my_bot arg' -> 'start'. Не команда — None."""
    if not text or not text.startswith("/"):
        return None
    head = text.split(maxsplit=1)[0]
    return head[1:].split("@", 1)[0].lower()


def command_args(text: str | None) -> str:
    """Текст после команды: '/delpost 3' -> '3'."""
    parts = (text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def is_admin(app: App, user_id: int) -> bool:
    return user_id in app.config.admin_ids


# ---------------------------------------------------------------- отправка и ответы


async def send(app: App, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
    await app.api.call("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)


async def answer(app: App, press: IncomingPress, text: str | None = None, alert: bool = False) -> None:
    """Ответить на нажатие кнопки. Сбой ответа не должен срывать сценарий."""
    try:
        await app.api.call(
            "answerCallbackQuery", callback_query_id=press.callback_id, text=text, show_alert=alert
        )
    except TelegramError as exc:
        logger.info("Не удалось ответить на нажатие кнопки: %s", exc.description)


async def edit(app: App, press: IncomingPress, text: str) -> None:
    """Заменить текст сообщения с кнопками. Без клавиатуры кнопки исчезают."""
    if press.chat_id is None or press.message_id is None:
        return
    try:
        await app.api.call("editMessageText", chat_id=press.chat_id, message_id=press.message_id, text=text)
    except TelegramError as exc:
        logger.info("Не удалось изменить сообщение: %s", exc.description)


def decision_keyboard(recipients: int, can_save: bool) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [{"text": f"📢 Всем подписчикам ({recipients})", "callback_data": "post:all"}],
        [{"text": "👤 Одному человеку", "callback_data": "post:one"}],
    ]
    if can_save:
        rows.append([{"text": "💾 Сохранить для инлайна", "callback_data": "post:save"}])
    rows.append([{"text": "✖ Отмена", "callback_data": "post:cancel"}])
    return {"inline_keyboard": rows}


def saved_text(post_id: int, bot_username: str | None) -> str:
    where = f"@{bot_username}" if bot_username else "юзернейм бота (его подскажет @BotFather)"
    return (
        f"💾 Пост №{post_id} сохранён.\n"
        f"Чтобы отправить его человеку: открой чат с ним, напиши {where} "
        "и выбери пост из списка. Сохранённые посты — /posts."
    )


# ---------------------------------------------------------------- команды пользователей


async def cmd_start(app: App, msg: IncomingMessage) -> None:
    app.db.subscribe(msg.user_id, msg.username, msg.first_name)
    if is_admin(app, msg.user_id):
        await send(app, msg.chat_id, ADMIN_GREETING + ADMIN_HELP)
    else:
        await send(app, msg.chat_id, USER_GREETING)


async def cmd_stop(app: App, msg: IncomingMessage) -> None:
    app.db.set_subscribed(msg.user_id, False)
    await send(app, msg.chat_id, STOPPED)


async def cmd_id(app: App, msg: IncomingMessage) -> None:
    await send(app, msg.chat_id, f"Ваш ID: {msg.user_id}")


async def cmd_help(app: App, msg: IncomingMessage) -> None:
    await send(app, msg.chat_id, ADMIN_HELP if is_admin(app, msg.user_id) else USER_HINT)


# ---------------------------------------------------------------- команды и сообщения админа


async def cmd_cancel(app: App, msg: IncomingMessage) -> None:
    if msg.user_id not in app.flows:
        await send(app, msg.chat_id, NOTHING_TO_CANCEL)
        return
    del app.flows[msg.user_id]
    await send(app, msg.chat_id, CANCELLED)


async def cmd_stats(app: App, msg: IncomingMessage) -> None:
    subscribed, total = app.db.stats(exclude=app.config.admin_ids)
    await send(
        app,
        msg.chat_id,
        f"📊 Подписчиков: {subscribed}\n"
        f"Отписались или заблокировали бота: {total - subscribed}\n"
        f"Всего в базе: {total}",
    )


async def cmd_posts(app: App, msg: IncomingMessage) -> None:
    posts = app.db.list_prepared(limit=RESULTS_LIMIT)
    if not posts:
        await send(app, msg.chat_id, NO_SAVED_POSTS)
        return
    lines = [f"№{post.post_id} · {post.title}" for post in posts]
    await send(app, msg.chat_id, "Сохранённые посты (удалить: /delpost <номер>):\n" + "\n".join(lines))


async def cmd_delpost(app: App, msg: IncomingMessage) -> None:
    raw = command_args(msg.text)
    if not raw.isdigit():
        await send(app, msg.chat_id, DELPOST_USAGE)
        return
    post_id = int(raw)
    if app.db.delete_prepared(post_id):
        await send(app, msg.chat_id, f"🗑 Пост №{post_id} удалён.")
    else:
        await send(app, msg.chat_id, f"Поста №{post_id} нет. Номера смотри в /posts.")


async def start_draft(app: App, msg: IncomingMessage) -> None:
    """Любое сообщение админа без активного шага становится черновиком поста."""
    app.flows[msg.user_id] = Flow(step="buttons", source_message_id=msg.message_id, content=msg.content)
    await send(app, msg.chat_id, DRAFT_SAVED)


async def buttons_received(app: App, msg: IncomingMessage, flow: Flow) -> None:
    try:
        rows = parse_buttons(msg.text or "")
    except ButtonsParseError as exc:
        await send(app, msg.chat_id, f"❌ Не получилось разобрать кнопки: {exc}\n{BUTTONS_ERROR_SUFFIX}")
        return
    await show_preview(app, msg, flow, rows)


async def show_preview(
    app: App, msg: IncomingMessage, flow: Flow, rows: list[list[ButtonSpec]]
) -> None:
    """Показать админу пост с кнопками так, как его увидят получатели."""
    markup = build_markup(rows, app.db.add_popup) if rows else None
    try:
        await app.api.call(
            "copyMessage",
            chat_id=msg.chat_id,
            from_chat_id=msg.chat_id,
            message_id=flow.source_message_id,
            reply_markup=markup,
        )
    except BadRequestError as exc:
        if "not found" in exc.description.lower():
            app.flows.pop(msg.user_id, None)
            await send(app, msg.chat_id, DRAFT_MISSING)
        else:
            await send(
                app,
                msg.chat_id,
                f"❌ Telegram не принял пост с этими кнопками: {exc.description}.\n{BUTTONS_ERROR_SUFFIX}",
            )
        return
    flow.markup = markup
    flow.step = "choose"
    recipients = len(app.db.subscriber_ids(app.config.admin_ids))
    await send(
        app,
        msg.chat_id,
        PREVIEW_QUESTION,
        reply_markup=decision_keyboard(recipients, can_save=flow.content is not None),
    )


async def target_received(app: App, msg: IncomingMessage, flow: Flow) -> None:
    raw = (msg.text or "").strip()
    if raw.startswith("@") and len(raw) > 1:
        user = app.db.find_by_username(raw[1:])
        if user is None:
            await send(app, msg.chat_id, TARGET_NOT_FOUND)
            return
        user_id = user.user_id
    elif raw.isdigit():
        user_id = int(raw)
        user = app.db.get_user(user_id)  # если человека нет в базе, всё равно попробуем отправить
    else:
        await send(app, msg.chat_id, TARGET_HINT)
        return
    if user is not None and not user.subscribed:
        await send(app, msg.chat_id, TARGET_UNSUBSCRIBED)
        return
    app.flows.pop(msg.user_id, None)
    delivery = await copy_to(app.api, user_id, msg.chat_id, flow.source_message_id, flow.markup)
    apply_outcome(app.db, user_id, delivery)
    if delivery.outcome is Outcome.SENT:
        await send(app, msg.chat_id, f"✅ Отправлено: {raw}.")
    else:
        await send(app, msg.chat_id, f"❌ Не получилось отправить {raw}: {delivery.error}.")


async def admin_message(app: App, msg: IncomingMessage, command: str | None) -> bool:
    """Сообщения админа. Возвращает True, если сообщение обработано."""
    flow = app.flows.get(msg.user_id)
    if command == "cancel":
        await cmd_cancel(app, msg)
        return True
    if command == "stats":
        await cmd_stats(app, msg)
        return True
    if command == "posts":
        await cmd_posts(app, msg)
        return True
    if command == "delpost":
        await cmd_delpost(app, msg)
        return True
    if command == "skip" and flow is not None and flow.step == "buttons":
        await show_preview(app, msg, flow, rows=[])
        return True
    if command is not None:
        return False  # остальные команды обработают пользовательские обработчики
    if flow is None:
        await start_draft(app, msg)
        return True
    if flow.step == "buttons":
        if msg.text is not None:
            await buttons_received(app, msg, flow)
        else:
            await send(app, msg.chat_id, BUTTONS_HINT)
        return True
    if flow.step == "target":
        if msg.text is not None:
            await target_received(app, msg, flow)
        else:
            await send(app, msg.chat_id, TARGET_HINT)
        return True
    await send(app, msg.chat_id, CHOOSE_HINT)  # шаг "choose"
    return True


# ---------------------------------------------------------------- маршрутизация


async def user_message(app: App, msg: IncomingMessage, command: str | None) -> bool:
    handlers = {"start": cmd_start, "stop": cmd_stop, "id": cmd_id, "help": cmd_help}
    handler = handlers.get(command or "")
    if handler is None:
        return False
    await handler(app, msg)
    return True


async def fallback(app: App, msg: IncomingMessage) -> None:
    text = UNKNOWN_COMMAND if is_admin(app, msg.user_id) else USER_HINT
    await send(app, msg.chat_id, text)


async def handle_message(app: App, msg: IncomingMessage) -> None:
    if msg.chat_type != "private":
        return  # бот работает только в личных сообщениях
    app.db.touch_user(msg.user_id, msg.username, msg.first_name)
    command = parse_command(msg.text)
    if is_admin(app, msg.user_id) and await admin_message(app, msg, command):
        return
    if await user_message(app, msg, command):
        return
    await fallback(app, msg)


async def handle_decision(app: App, press: IncomingPress, flow: Flow, data: str) -> None:
    if data == "post:cancel":
        app.flows.pop(press.user_id, None)
        await answer(app, press)
        await edit(app, press, CANCELLED)
    elif data == "post:one":
        flow.step = "target"
        await answer(app, press)
        await edit(app, press, TARGET_QUESTION)
    elif data == "post:save":
        await save_for_inline(app, press, flow)
    else:  # post:all
        await start_broadcast(app, press, flow)


async def save_for_inline(app: App, press: IncomingPress, flow: Flow) -> None:
    if flow.content is None:
        await answer(app, press, UNSUPPORTED_FOR_INLINE, alert=True)
        return
    app.flows.pop(press.user_id, None)
    post_id = app.db.add_prepared(make_title(flow.content), flow.content, flow.markup)
    await answer(app, press)
    await edit(app, press, saved_text(post_id, app.bot_username))


async def start_broadcast(app: App, press: IncomingPress, flow: Flow) -> None:
    # Состояние сбрасываем до любых ожиданий: повторное нажатие уже не запустит рассылку.
    app.flows.pop(press.user_id, None)
    await answer(app, press)
    recipients = app.db.subscriber_ids(app.config.admin_ids)
    if not recipients:
        await edit(app, press, "Некому отправить: подписчиков пока нет.")
        return
    await edit(app, press, f"🚀 Рассылка запущена: получателей {len(recipients)}. Отчёт пришлю в конце.")
    start_background(
        run_broadcast(
            app,
            admin_chat_id=press.user_id,
            source_message_id=flow.source_message_id,
            markup=flow.markup,
            recipients=recipients,
        )
    )


async def popup_pressed(app: App, press: IncomingPress) -> None:
    """Нажата popup-кнопка: показываем всплывающее окно с заданным текстом."""
    popup_id = popup_id_from_callback(press.data)
    text = app.db.get_popup(popup_id) if popup_id is not None else None
    if text is None:
        await answer(app, press, POPUP_MISSING, alert=True)
    else:
        await answer(app, press, text, alert=True)


async def handle_press(app: App, press: IncomingPress) -> None:
    data = press.data or ""
    if is_admin(app, press.user_id) and data.startswith("post:"):
        flow = app.flows.get(press.user_id)
        if flow is not None and flow.step == "choose" and data in DECISIONS:
            await handle_decision(app, press, flow, data)
        else:
            await answer(app, press, STALE, alert=True)  # пост уже неактуален
    elif data.startswith("pp:"):
        await popup_pressed(app, press)


async def handle_inline(app: App, query: IncomingInlineQuery) -> None:
    """@бот в любом чате: админ видит свои сохранённые посты, остальные — пустой список."""
    admin = is_admin(app, query.user_id)
    posts = app.db.list_prepared(query=query.text, limit=RESULTS_LIMIT) if admin else []
    results = [inline_result(post) for post in posts]
    button = None
    if admin and not results:
        button = {"text": "Подготовить пост", "start_parameter": "prepare"}
    await app.api.call(
        "answerInlineQuery",
        inline_query_id=query.id,
        results=results,
        cache_time=0,
        is_personal=True,
        button=button,
    )


async def handle_update(app: App, update: dict[str, Any]) -> None:
    if "message" in update:
        await handle_message(app, parse_message(update["message"]))
    elif "callback_query" in update:
        await handle_press(app, parse_press(update["callback_query"]))
    elif "inline_query" in update:
        await handle_inline(app, parse_inline(update["inline_query"]))


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
    api: Any,
    chat_id: int,
    source_chat_id: int,
    source_message_id: int,
    markup: dict[str, Any] | None,
) -> Delivery:
    """Скопировать пост в чат chat_id вместе с кнопками.

    copyMessage сохраняет форматирование и медиа, но не показывает «переслано от».
    При лимитах Telegram и сетевых сбоях попытку повторяем.
    """
    for _ in range(MAX_ATTEMPTS):
        try:
            await api.call(
                "copyMessage",
                chat_id=chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=markup,
            )
            return Delivery(Outcome.SENT)
        except RetryAfterError as exc:
            logger.warning("Telegram просит подождать %s сек.", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
        except NetworkError:
            logger.warning("Сетевая ошибка при отправке, повторяю")
            await asyncio.sleep(2)
        except ForbiddenError:
            return Delivery(Outcome.BLOCKED, "пользователь заблокировал бота или не нажимал /start")
        except BadRequestError as exc:
            if "chat not found" in exc.description.lower():
                return Delivery(Outcome.BLOCKED, "пользователь не нажимал /start")
            return Delivery(Outcome.FAILED, exc.description)
        except TelegramError as exc:
            return Delivery(Outcome.FAILED, exc.description)
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
    app: App,
    *,
    admin_chat_id: int,
    source_message_id: int,
    markup: dict[str, Any] | None,
    recipients: list[int],
) -> None:
    report = BroadcastReport(total=len(recipients))
    errors_in_row = 0
    for user_id in recipients:
        delivery = await copy_to(app.api, user_id, admin_chat_id, source_message_id, markup)
        apply_outcome(app.db, user_id, delivery)
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
    await send(app, admin_chat_id, report.render())


_background_tasks: set[asyncio.Task] = set()


def start_background(coro: Any) -> None:
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


# ---------------------------------------------------------------- получение обновлений и запуск


async def call_with_retries(api: TelegramAPI, method: str, **params: Any) -> Any:
    """Вызов метода с повтором при сетевых сбоях: при старте бот не должен падать из-за сети."""
    delay = 1.0
    while True:
        try:
            return await api.call(method, **params)
        except NetworkError:
            logger.warning("Нет связи с Telegram, повторю %s через %s с", method, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def poll(app: App) -> None:
    """Получать обновления через getUpdates и передавать их обработчикам."""
    offset: int | None = None
    delay = 1.0
    while True:
        try:
            updates = await app.api.call(
                "getUpdates",
                offset=offset,
                timeout=POLL_TIMEOUT,
                allowed_updates=list(ALLOWED_UPDATES),
                http_timeout=POLL_TIMEOUT + 20,
            )
            delay = 1.0
        except UnauthorizedError:
            raise  # токен неверен: повторять бессмысленно
        except RetryAfterError as exc:
            await asyncio.sleep(exc.retry_after)
            continue
        except ConflictError:
            logger.error("Этот токен уже используется другим запущенным экземпляром бота.")
            await asyncio.sleep(15)
            continue
        except TelegramError as exc:
            logger.warning("Ошибка связи с Telegram: %s. Повторю через %s с.", exc.description, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        for update in updates:
            offset = update["update_id"] + 1  # сдвигаем offset до обработки, чтобы сбойное обновление не зацикливалось
            try:
                await handle_update(app, update)
            except TelegramError as exc:
                logger.warning("Telegram отклонил действие: %s", exc.description)
            except Exception:  # один сбой не должен останавливать бота
                logger.exception("Не удалось обработать обновление")


async def run(config: Config) -> None:
    db = Database(config.db_path)
    api = TelegramAPI(config.token)
    app = App(config=config, db=db, api=api)
    if not config.admin_ids:
        logger.warning(
            "ADMIN_IDS не задан: админ-функции отключены. "
            "Напиши боту /id и добавь полученное число в ADMIN_IDS."
        )
    try:
        await call_with_retries(api, "deleteWebhook")
        me = await call_with_retries(api, "getMe")
        app.bot_username = me.get("username")
        if not me.get("supports_inline_queries"):
            logger.warning(
                "Инлайн-режим выключен: сохранённые посты не появятся в @%s. "
                "Включи его командой /setinline в @BotFather.",
                app.bot_username,
            )
        logger.info("Бот @%s запущен. База данных: %s", app.bot_username, config.db_path)
        await poll(app)
    finally:
        await api.close()
        db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    try:
        asyncio.run(run(config))
    except UnauthorizedError:
        logger.error(
            "Telegram не принял токен (ошибка 401). Проверь TELEGRAM_BOT_TOKEN: "
            "он мог быть задан с ошибкой или перевыпущен в @BotFather."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
