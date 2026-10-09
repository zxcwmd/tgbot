"""Посты для inline-режима: что сохранять из сообщения и как собирать результаты.

Inline-режим Telegram не умеет копировать сообщения, поэтому пост сохраняется как
содержимое: текст с форматированием или file_id медиа с подписью. Из этого бот
собирает результат, который появляется, когда админ пишет @бота в любом чате.
"""

from dataclasses import dataclass
from typing import Any

# Медиа, которое умеет отправлять inline-режим: тип сообщения -> (тип результата, поле с file_id).
INLINE_MEDIA: dict[str, tuple[str, str]] = {
    "photo": ("photo", "photo_file_id"),
    "video": ("video", "video_file_id"),
    "animation": ("mpeg4_gif", "mpeg4_file_id"),
    "document": ("document", "document_file_id"),
    "audio": ("audio", "audio_file_id"),
    "voice": ("voice", "voice_file_id"),
}
# У кэшированного аудио в inline-результате нет поля title, у остальных медиа оно есть.
TITLED_MEDIA = {"photo", "video", "animation", "document", "voice"}
LABELS = {
    "photo": "Фото",
    "video": "Видео",
    "animation": "GIF",
    "document": "Файл",
    "audio": "Аудио",
    "voice": "Голосовое",
}
TITLE_LIMIT = 60
# Telegram принимает не больше 50 результатов в одном ответе на inline-запрос.
RESULTS_LIMIT = 50


@dataclass(frozen=True)
class Content:
    """Содержимое поста: текст сообщения либо file_id медиа с подписью."""

    kind: str  # "text" или ключ из INLINE_MEDIA
    text: str | None = None  # текст сообщения или подпись к медиа
    entities: list[dict[str, Any]] | None = None  # форматирование текста или подписи
    file_id: str | None = None  # только для медиа


@dataclass(frozen=True)
class PreparedPost:
    post_id: int
    title: str
    content: Content
    markup: dict[str, Any] | None


def extract_content(raw: dict[str, Any]) -> Content | None:
    """Достать содержимое из сообщения Telegram. None — такой тип для inline не поддерживается."""
    if raw.get("text") is not None:
        return Content(kind="text", text=raw["text"], entities=raw.get("entities") or None)
    caption = raw.get("caption")
    caption_entities = raw.get("caption_entities") or None
    if raw.get("photo"):
        # Telegram присылает несколько размеров фото; последний — самый большой.
        return Content("photo", caption, caption_entities, raw["photo"][-1]["file_id"])
    # animation проверяем раньше document: GIF приходит и как animation, и как document.
    for kind in ("video", "animation", "document", "audio", "voice"):
        if kind in raw:
            return Content(kind, caption, caption_entities, raw[kind]["file_id"])
    return None


def make_title(content: Content) -> str:
    """Короткое название поста для списка: первая строка текста или подписи."""
    if content.kind == "text":
        return _short(content.text or "", TITLE_LIMIT) or "Текст"
    label = LABELS[content.kind]
    caption = _short(content.text or "", TITLE_LIMIT - len(label) - 2)
    return f"{label}: {caption}" if caption else label


def _short(text: str, limit: int) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def inline_result(post: PreparedPost) -> dict[str, Any]:
    """Собрать результат inline-запроса (InlineQueryResult) из сохранённого поста."""
    content = post.content
    result_id = f"p{post.post_id}"  # id результата: не длиннее 64 байт
    result: dict[str, Any]
    if content.kind == "text":
        result = {
            "type": "article",
            "id": result_id,
            "title": post.title,
            "input_message_content": _drop_none(
                {"message_text": content.text, "entities": content.entities}
            ),
        }
    else:
        result_type, file_field = INLINE_MEDIA[content.kind]
        result = {"type": result_type, "id": result_id, file_field: content.file_id}
        if content.kind in TITLED_MEDIA:
            result["title"] = post.title
        if content.text:
            result["caption"] = content.text
        if content.entities:
            result["caption_entities"] = content.entities
    if post.markup:
        result["reply_markup"] = post.markup
    return result


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}
