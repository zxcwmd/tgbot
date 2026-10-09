"""Посты для inline-режима: что сохраняем из сообщения и как собираем результат."""

import unittest

from posts import Content, PreparedPost, extract_content, inline_result, make_title

MARKUP = {"inline_keyboard": [[{"text": "Сайт", "url": "https://example.com"}]]}


class ExtractContentTests(unittest.TestCase):
    def test_text_keeps_entities(self):
        entities = [{"type": "bold", "offset": 0, "length": 6}]
        content = extract_content({"text": "Скидка 10%", "entities": entities})
        self.assertEqual(content, Content(kind="text", text="Скидка 10%", entities=entities))

    def test_photo_takes_largest_size_and_caption(self):
        raw = {
            "photo": [{"file_id": "small"}, {"file_id": "big"}],
            "caption": "Подпись",
            "caption_entities": [{"type": "italic", "offset": 0, "length": 3}],
        }
        content = extract_content(raw)
        self.assertEqual((content.kind, content.file_id, content.text), ("photo", "big", "Подпись"))
        self.assertEqual(content.entities, [{"type": "italic", "offset": 0, "length": 3}])

    def test_animation_is_not_taken_for_document(self):
        raw = {"animation": {"file_id": "anim"}, "document": {"file_id": "doc"}}
        self.assertEqual(extract_content(raw), Content(kind="animation", file_id="anim"))

    def test_other_media_kinds(self):
        for kind in ("video", "document", "audio", "voice"):
            with self.subTest(kind=kind):
                content = extract_content({kind: {"file_id": f"{kind}-id"}, "caption": "c"})
                self.assertEqual((content.kind, content.file_id, content.text), (kind, f"{kind}-id", "c"))

    def test_unsupported_types_return_none(self):
        for raw in (
            {"sticker": {"file_id": "s"}},
            {"video_note": {"file_id": "v"}},
            {"location": {"latitude": 1, "longitude": 1}},
            {"poll": {"question": "?"}},
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(extract_content(raw))


class TitleTests(unittest.TestCase):
    def test_text_title_is_first_non_empty_line(self):
        self.assertEqual(make_title(Content("text", "\n  Акция  \nДетали")), "Акция")

    def test_long_title_is_cut_with_ellipsis(self):
        title = make_title(Content("text", "а" * 100))
        self.assertEqual(len(title), 60)
        self.assertTrue(title.endswith("…"))

    def test_media_title_uses_label_and_caption(self):
        self.assertEqual(make_title(Content("photo", "Афиша", None, "f")), "Фото: Афиша")
        self.assertEqual(make_title(Content("voice", None, None, "f")), "Голосовое")


class InlineResultTests(unittest.TestCase):
    def test_text_post_becomes_article_with_buttons(self):
        post = PreparedPost(
            post_id=7,
            title="Акция",
            content=Content("text", "Акция\nДо пятницы", [{"type": "bold", "offset": 0, "length": 5}]),
            markup=MARKUP,
        )
        self.assertEqual(
            inline_result(post),
            {
                "type": "article",
                "id": "p7",
                "title": "Акция",
                "input_message_content": {
                    "message_text": "Акция\nДо пятницы",
                    "entities": [{"type": "bold", "offset": 0, "length": 5}],
                },
                "reply_markup": MARKUP,
            },
        )

    def test_photo_post_keeps_caption_and_entities(self):
        post = PreparedPost(
            post_id=3,
            title="Фото: Афиша",
            content=Content("photo", "Афиша", [{"type": "italic", "offset": 0, "length": 5}], "big"),
            markup=None,
        )
        result = inline_result(post)
        self.assertEqual(result["type"], "photo")
        self.assertEqual(result["photo_file_id"], "big")
        self.assertEqual(result["title"], "Фото: Афиша")
        self.assertEqual(result["caption"], "Афиша")
        self.assertEqual(result["caption_entities"], [{"type": "italic", "offset": 0, "length": 5}])
        self.assertNotIn("reply_markup", result)  # без кнопок ключа нет

    def test_animation_becomes_mpeg4_gif(self):
        post = PreparedPost(1, "GIF", Content("animation", None, None, "anim"), None)
        self.assertEqual(inline_result(post), {"type": "mpeg4_gif", "id": "p1", "mpeg4_file_id": "anim", "title": "GIF"})

    def test_audio_has_no_title(self):
        post = PreparedPost(2, "Аудио", Content("audio", None, None, "aud"), None)
        result = inline_result(post)
        self.assertEqual(result, {"type": "audio", "id": "p2", "audio_file_id": "aud"})


if __name__ == "__main__":
    unittest.main()
