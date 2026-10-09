"""Хранилище SQLite: подписчики, тексты popup-окон и сохранённые посты для inline-режима."""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from posts import Content, PreparedPost

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    subscribed  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS popups (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    text  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prepared_posts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    text      TEXT,
    entities  TEXT,
    file_id   TEXT,
    markup    TEXT
);
"""


@dataclass(frozen=True)
class User:
    user_id: int
    username: str | None
    first_name: str | None
    subscribed: bool


def _to_user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        username=row["username"],
        first_name=row["first_name"],
        subscribed=bool(row["subscribed"]),
    )


def _to_post(row: sqlite3.Row) -> PreparedPost:
    content = Content(
        kind=row["kind"],
        text=row["text"],
        entities=_load(row["entities"]),
        file_id=row["file_id"],
    )
    return PreparedPost(
        post_id=row["id"],
        title=row["title"],
        content=content,
        markup=_load(row["markup"]),
    )


def _dump(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _load(text: str | None) -> Any:
    return json.loads(text) if text else None


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def touch_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        """Запомнить пользователя, который написал боту. Статус подписки не меняем."""
        self._conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, subscribed) VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name),
        )
        self._conn.commit()

    def subscribe(self, user_id: int, username: str | None, first_name: str | None) -> None:
        """Подписать пользователя (команда /start)."""
        self._conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, subscribed) VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                subscribed = 1
            """,
            (user_id, username, first_name),
        )
        self._conn.commit()

    def set_subscribed(self, user_id: int, subscribed: bool) -> None:
        self._conn.execute(
            "UPDATE users SET subscribed = ? WHERE user_id = ?",
            (int(subscribed), user_id),
        )
        self._conn.commit()

    def get_user(self, user_id: int) -> User | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return _to_user(row) if row else None

    def find_by_username(self, username: str) -> User | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return _to_user(row) if row else None

    def subscriber_ids(self, exclude: Iterable[int] = ()) -> list[int]:
        """ID всех подписчиков, кроме исключённых (например, админов)."""
        excluded = set(exclude)
        rows = self._conn.execute(
            "SELECT user_id FROM users WHERE subscribed = 1 ORDER BY user_id"
        ).fetchall()
        return [row["user_id"] for row in rows if row["user_id"] not in excluded]

    def stats(self, exclude: Iterable[int] = ()) -> tuple[int, int]:
        """Вернуть (подписчиков, всего пользователей), не считая исключённых (админов)."""
        excluded = set(exclude)
        rows = self._conn.execute("SELECT user_id, subscribed FROM users").fetchall()
        kept = [row for row in rows if row["user_id"] not in excluded]
        return sum(1 for row in kept if row["subscribed"]), len(kept)

    def add_popup(self, text: str) -> int:
        cursor = self._conn.execute("INSERT INTO popups (text) VALUES (?)", (text,))
        self._conn.commit()
        return int(cursor.lastrowid)

    def get_popup(self, popup_id: int) -> str | None:
        row = self._conn.execute("SELECT text FROM popups WHERE id = ?", (popup_id,)).fetchone()
        return row["text"] if row else None

    def add_prepared(self, title: str, content: Content, markup: dict[str, Any] | None) -> int:
        """Сохранить пост для inline-режима. Возвращает его номер."""
        cursor = self._conn.execute(
            "INSERT INTO prepared_posts (title, kind, text, entities, file_id, markup) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, content.kind, content.text, _dump(content.entities), content.file_id, _dump(markup)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_prepared(self, query: str = "", limit: int = 50) -> list[PreparedPost]:
        """Сохранённые посты, новые первыми. query — поиск по названию без учёта регистра."""
        rows = self._conn.execute("SELECT * FROM prepared_posts ORDER BY id DESC").fetchall()
        posts = [_to_post(row) for row in rows]
        needle = query.strip().lower()
        if needle:
            posts = [post for post in posts if needle in post.title.lower()]
        return posts[:limit]

    def delete_prepared(self, post_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM prepared_posts WHERE id = ?", (post_id,))
        self._conn.commit()
        return cursor.rowcount > 0
