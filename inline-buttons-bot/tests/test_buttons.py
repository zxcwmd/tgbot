"""Разбор текста с кнопками и сборка клавиатуры."""

import unittest

from buttons import (
    CALLBACK_PREFIX,
    POPUP_MAX_LENGTH,
    ButtonSpec,
    ButtonsParseError,
    build_markup,
    parse_buttons,
    popup_id_from_callback,
)


class ParseButtonsTests(unittest.TestCase):
    def test_single_url_button(self):
        self.assertEqual(
            parse_buttons("Сайт - https://example.com"),
            [[ButtonSpec(label="Сайт", url="https://example.com")]],
        )

    def test_rows_and_several_buttons_in_row(self):
        text = (
            "Сайт - https://example.com | Поддержка - https://t.me/support\n"
            "\n"
            "Открыть - tg://resolve?domain=example\n"
        )
        self.assertEqual(
            parse_buttons(text),
            [
                [
                    ButtonSpec(label="Сайт", url="https://example.com"),
                    ButtonSpec(label="Поддержка", url="https://t.me/support"),
                ],
                [ButtonSpec(label="Открыть", url="tg://resolve?domain=example")],
            ],
        )

    def test_label_may_contain_dash(self):
        self.assertEqual(
            parse_buttons("Сайт - главная - https://example.com"),
            [[ButtonSpec(label="Сайт - главная", url="https://example.com")]],
        )

    def test_popup_text_may_contain_dashes(self):
        self.assertEqual(
            parse_buttons("Цена - popup: 100 - 200 рублей"),
            [[ButtonSpec(label="Цена", popup="100 - 200 рублей")]],
        )

    def test_popup_text_length_limit(self):
        with self.assertRaisesRegex(ButtonsParseError, "длиннее 200"):
            parse_buttons(f"Кнопка - popup: {'a' * (POPUP_MAX_LENGTH + 1)}")
        rows = parse_buttons(f"Кнопка - popup: {'a' * POPUP_MAX_LENGTH}")
        self.assertEqual(len(rows[0][0].popup), POPUP_MAX_LENGTH)

    def test_invalid_input(self):
        cases = {
            "": "не нашёл ни одной кнопки",
            "   \n  ": "не нашёл ни одной кнопки",
            "Сайт - example.com": "не понял кнопку",
            "Сайт https://example.com": "не понял кнопку",
            "- https://example.com": "не понял кнопку",
            "Файл - ftp://example.com": "не понял кнопку",
            "Сайт - https://": "не понял кнопку",
            "Сайт - https://exa mple.com": "не понял кнопку",
            "Сайт - https://example.com |": "пустая кнопка",
            "Цена - popup:   ": "пустой текст popup",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(ButtonsParseError, fragment):
                    parse_buttons(text)

    def test_error_points_to_line(self):
        with self.assertRaisesRegex(ButtonsParseError, "строка 2"):
            parse_buttons("Сайт - https://example.com\nПлохо")

    def test_parse_error_is_value_error(self):
        self.assertTrue(issubclass(ButtonsParseError, ValueError))


class BuildMarkupTests(unittest.TestCase):
    def test_url_and_popup_buttons(self):
        saved: list[str] = []

        def save_popup(text: str) -> int:
            saved.append(text)
            return len(saved)

        rows = parse_buttons("Сайт - https://example.com | Подробнее - popup: Текст окна")
        markup = build_markup(rows, save_popup)

        self.assertEqual(saved, ["Текст окна"])
        self.assertEqual(
            markup,
            {
                "inline_keyboard": [
                    [
                        {"text": "Сайт", "url": "https://example.com"},
                        {"text": "Подробнее", "callback_data": "pp:1"},
                    ]
                ]
            },
        )


class PopupCallbackTests(unittest.TestCase):
    def test_popup_id_from_callback(self):
        self.assertEqual(popup_id_from_callback("pp:42"), 42)
        self.assertIsNone(popup_id_from_callback("pp:"))
        self.assertIsNone(popup_id_from_callback("post:all"))
        self.assertIsNone(popup_id_from_callback(None))

    def test_callback_data_fits_telegram_limit(self):
        self.assertLessEqual(len(f"{CALLBACK_PREFIX}{10**12}".encode()), 64)


if __name__ == "__main__":
    unittest.main()
