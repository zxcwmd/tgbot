"""Разбор текста с кнопками и сборка inline-клавиатуры.

Админ присылает кнопки текстом. Каждая строка — отдельный ряд, кнопки в ряду
разделяются символом «|»:

    Текст кнопки - https://ссылка
    Кнопка 1 - https://ссылка1 | Кнопка 2 - https://ссылка2
    Подробнее - popup: текст во всплывающем окне

Название и значение разделяются дефисом с пробелами вокруг (" - ").
Значение — ссылка (https://, http:// или tg://) либо «popup: текст».
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

POPUP_PREFIX = "popup:"
# Лимит Telegram на текст всплывающего окна (answerCallbackQuery.text): 0–200 символов.
POPUP_MAX_LENGTH = 200
# callback_data popup-кнопки имеет вид "pp:<id>" — это заметно меньше лимита 64 байта.
CALLBACK_PREFIX = "pp:"

_SEPARATOR_RE = re.compile(r"\s+-\s+")
_WHITESPACE_RE = re.compile(r"\s")


class ButtonsParseError(ValueError):
    """Кнопки не удалось разобрать. Текст ошибки можно показать админу."""


@dataclass(frozen=True)
class ButtonSpec:
    """Одна кнопка: либо ссылка (url), либо всплывающее окно (popup)."""

    label: str
    url: str | None = None
    popup: str | None = None


def parse_buttons(text: str) -> list[list[ButtonSpec]]:
    """Разобрать текст с кнопками в список рядов. Пустые строки пропускаются."""
    rows: list[list[ButtonSpec]] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = [_parse_button(part.strip()) for part in line.split("|")]
        except ButtonsParseError as exc:
            raise ButtonsParseError(f"строка {line_no}: {exc}") from None
        rows.append(row)
    if not rows:
        raise ButtonsParseError("не нашёл ни одной кнопки.")
    return rows


def _parse_button(part: str) -> ButtonSpec:
    if not part:
        raise ButtonsParseError("пустая кнопка или лишний символ «|».")
    # Перебираем все места с " - ": так названия вида «Сайт - главная» тоже работают.
    for match in _SEPARATOR_RE.finditer(part):
        label = part[: match.start()].strip()
        value = part[match.end():].strip()
        if not label:
            break
        if value.lower().startswith(POPUP_PREFIX):
            popup = value[len(POPUP_PREFIX):].strip()
            if not popup:
                raise ButtonsParseError(f"у кнопки «{label}» пустой текст popup.")
            if len(popup) > POPUP_MAX_LENGTH:
                raise ButtonsParseError(
                    f"текст popup у кнопки «{label}» длиннее {POPUP_MAX_LENGTH} символов."
                )
            return ButtonSpec(label=label, popup=popup)
        if _is_url(value):
            return ButtonSpec(label=label, url=value)
    raise ButtonsParseError(
        f"не понял кнопку «{part}». Нужно: Текст - https://ссылка "
        "или Текст - popup: текст."
    )


def _is_url(value: str) -> bool:
    if not value or _WHITESPACE_RE.search(value):
        return False
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        return bool(parsed.netloc)
    return parsed.scheme == "tg"


def build_markup(
    rows: Sequence[Sequence[ButtonSpec]],
    save_popup: Callable[[str], int],
) -> InlineKeyboardMarkup:
    """Собрать inline-клавиатуру.

    save_popup сохраняет текст всплывающего окна и возвращает его id;
    по нажатию кнопки бот найдёт текст по этому id.
    """
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        buttons: list[InlineKeyboardButton] = []
        for spec in row:
            if spec.url is not None:
                buttons.append(InlineKeyboardButton(text=spec.label, url=spec.url))
            else:
                popup_id = save_popup(spec.popup or "")
                buttons.append(
                    InlineKeyboardButton(
                        text=spec.label,
                        callback_data=f"{CALLBACK_PREFIX}{popup_id}",
                    )
                )
        keyboard.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def popup_id_from_callback(data: str | None) -> int | None:
    """Вытащить id popup из callback_data, либо None, если это не popup-кнопка."""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    number = data[len(CALLBACK_PREFIX):]
    return int(number) if number.isdigit() else None
