"""Тесты Telegram-клиента: файл уходит вложением, а не как file_id."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wb_logist.errors import WbLogistError
from wb_logist.telegram import TelegramClient


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class SendDocumentTest(unittest.TestCase):
    def test_document_goes_as_multipart_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ИП Баранов ВБ 19.11.2026.xlsx"
            path.write_bytes(b"fake-xlsx")

            with mock.patch(
                "requests.post", return_value=FakeResponse({"ok": True, "result": {}})
            ) as post:
                TelegramClient("token").send_document(1, path, "caption")

        args, kwargs = post.call_args
        self.assertIn("files", kwargs)
        self.assertIn("document", kwargs["files"])
        self.assertNotIn("document", kwargs.get("data") or {})

    def test_missing_file(self) -> None:
        with self.assertRaises(WbLogistError) as ctx:
            TelegramClient("token").send_document(
                1, Path("nope.xlsx"), "caption"
            )

        self.assertEqual(ctx.exception.code, "TELEGRAM_FILE_MISSING")


class SendMessageFallbackTest(unittest.TestCase):
    def test_html_parse_error_retries_without_parse_mode(self) -> None:
        bad = FakeResponse(
            {"ok": False, "description": "Bad Request: can't parse entities"}
        )
        good = FakeResponse({"ok": True, "result": {"message_id": 1}})

        with mock.patch("requests.post", side_effect=[bad, good]) as post:
            TelegramClient("token").send_message(1, "text <b>")

        self.assertEqual(post.call_count, 2)
        self.assertNotIn("parse_mode", post.call_args_list[1].kwargs["data"])


if __name__ == "__main__":
    unittest.main()
