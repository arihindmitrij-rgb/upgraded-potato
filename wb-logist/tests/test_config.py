"""Тесты конфигурации: парсер .env, оверрайды, ошибки значений."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wb_logist.config import load_config, parse_env_file
from wb_logist.errors import WbLogistError


class ParseEnvFileTest(unittest.TestCase):
    def test_quotes_comments_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "\ufeff# комментарий\n"
                "export A=1\n"
                'B="two words"\n'
                "C='x y' # хвост\n"
                "D=plain # хвост\n",
                encoding="utf-8",
            )

            data = parse_env_file(env)

        self.assertEqual(
            data,
            {"A": "1", "B": "two words", "C": "x y", "D": "plain"},
        )

    def test_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("BRAND=\nEMPTY_QUOTED=\"\"\n", encoding="utf-8")

            data = parse_env_file(env)

        self.assertEqual(data, {"BRAND": "", "EMPTY_QUOTED": ""})


class LoadConfigTest(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("SPREADSHEET_ID=abc\nSHEET_GID=42\n", encoding="utf-8")
            out_dir = Path(tmp) / "out"

            with mock.patch.dict("os.environ", {}, clear=True):
                config = load_config(env, {"OUTPUT_DIR": str(out_dir)})

        self.assertEqual(config.spreadsheet_id, "abc")
        self.assertEqual(config.sheet_gid, "42")
        self.assertEqual(config.sheet_name, "Проверочный лист (Дима)")
        self.assertEqual(config.destination_marker, "ВБ")
        self.assertEqual(config.output_dir, out_dir)
        self.assertIsNone(config.wb_api_key)

    def test_env_variable_wins_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("SPREADSHEET_ID=from-file\n", encoding="utf-8")

            with mock.patch.dict("os.environ", {"SPREADSHEET_ID": "from-env"}, clear=True):
                config = load_config(env)

        self.assertEqual(config.spreadsheet_id, "from-env")

    def test_bad_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("HTTP_TIMEOUT=abc\n", encoding="utf-8")

            with mock.patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(WbLogistError) as ctx:
                    load_config(env)

        self.assertEqual(ctx.exception.code, "CONFIG_ERROR")


if __name__ == "__main__":
    unittest.main()
