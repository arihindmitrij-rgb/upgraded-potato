"""Тесты чтения Google-таблицы: разбор CSV, поиск листа по имени, ошибки."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from wb_logist.config import Config
from wb_logist.errors import WbLogistError
from wb_logist.sheets import (
    SheetRef,
    find_freshest_baranov_sheet,
    http_get_text,
    load_distribution,
    normalize_sheet_name,
    parse_baranov_sheet_name,
    parse_distribution_csv,
    parse_quantity,
    resolve_sheet_gid,
)

REAL_CSV = "\n".join(
    [
        "Артикул продавца,Количество",
        "sw-y36-black-4,50",
        "sw-y36-blue-3,0",
        "sw-y20-black-5,100",
        "sw-c86-black-red-3,50",
        "sw-c86-black-blue-3,50",
        "sw-c86-pink-4,50",
        "sw-c86-black-gray-3,50",
        "sw-y66-blue-3,25",
        "sw-y36-pink-4,100",
        "sw-y20-pink-2,25",
        "sw-y66-orange-4,10",
        "sw-y66-gray-5,10",
    ]
)

# Образец «Баранов ВБ 05.11.2026»: склады-колонки, без «Количество»
WB_WAREHOUSE_CSV = "\n".join(
    [
        "Артикул продавца,СЦ Домодедово 2,,Казань,Питер",
        "sw-y20-pink-2,0,,,",
        "sw-y36-pink-4,100,,,",
        "sw-y36-black-4,30,,,",
        "sw-c86-black-gray-3,нет же?,нет)),",
        "sw-c86-black-blue-3,50,,,",
        "sw-y20-black-5,сколько есть,,,",
        # пустая колонка в шапке — спейсер; 10 под Казань, 5 под Питер
        "sw-mixed,30 /10,,10,5",
    ]
)

HTMLVIEW = (
    'items.push({name: "\\u0411\\u0430\\u0440\\u0430\\u043d\\u043e\\u0432 17.11.2026 ", '
    'pageUrl: "https:\\/\\/docs.google.com\\/spreadsheets\\/d\\/ID\\/htmlview\\/sheet'
    '?headers\\x3dtrue&gid=965771581", gid: "965771581",initialSheet: false});\n'
    'items.push({name: "Баранов ВБ 27.10", '
    'pageUrl: "https://docs.google.com/spreadsheets/d/ID/htmlview/sheet'
    '?headers=true&gid=105443037", gid: "105443037",initialSheet: false});\n'
    'items.push({name: "Баранов ВБ 05.11.2026", '
    'pageUrl: "https://docs.google.com/spreadsheets/d/ID/htmlview/sheet'
    '?headers=true&gid=987430236", gid: "987430236",initialSheet: false});\n'
    'items.push({name: "Проверочный лист(Дима)", '
    'pageUrl: "https://docs.google.com/spreadsheets/d/ID/htmlview/sheet'
    '?headers=true&gid=696867362", gid: "696867362",initialSheet: true});'
)


class ParseDistributionTest(unittest.TestCase):
    def test_real_sheet(self) -> None:
        distribution = parse_distribution_csv(REAL_CSV, source="sheet", sheet_label="gid=696867362")

        self.assertEqual(distribution.positions, 11)
        self.assertEqual(distribution.total_items, 520)
        self.assertEqual(
            [(s.vendor_code, s.reason) for s in distribution.skipped],
            [("sw-y36-blue-3", "количество 0")],
        )
        self.assertEqual(distribution.rows[0].vendor_code, "sw-y36-black-4")
        self.assertEqual(distribution.rows[0].quantity, 50)

    def test_header_offset_and_semicolon(self) -> None:
        text = "Отчёт на 19.11\n\nАртикул;Количество\nsw-a;3\nsw-b;4\n"

        distribution = parse_distribution_csv(text)

        self.assertEqual(distribution.total_items, 7)
        self.assertEqual(distribution.positions, 2)

    def test_duplicates_are_summed(self) -> None:
        text = "Артикул продавца,Количество\nsw-a,2\nsw-a,3\n"

        distribution = parse_distribution_csv(text)

        self.assertEqual(distribution.positions, 1)
        self.assertEqual(distribution.rows[0].quantity, 5)

    def test_missing_quantity_column(self) -> None:
        text = "Артикул продавца,Всего находится на складах\nsw-a,2\n"

        # «Всего находится…» не meta и не Количество → режим складов, 2 берётся
        distribution = parse_distribution_csv(text)
        self.assertEqual(distribution.total_items, 2)

    def test_all_zero_quantity_is_error(self) -> None:
        text = "Артикул продавца,Количество\nsw-a,0\nsw-b,\n"

        with self.assertRaises(WbLogistError) as ctx:
            parse_distribution_csv(text)

        self.assertEqual(ctx.exception.code, "SHEET_EMPTY")

    def test_warehouse_sheet_sums_clean_numbers(self) -> None:
        distribution = parse_distribution_csv(
            WB_WAREHOUSE_CSV, sheet_label="Баранов ВБ 05.11.2026"
        )

        by_code = {row.vendor_code: row for row in distribution.rows}
        self.assertEqual(by_code["sw-y36-pink-4"].quantity, 100)
        self.assertEqual(by_code["sw-y36-black-4"].quantity, 30)
        self.assertEqual(by_code["sw-c86-black-blue-3"].quantity, 50)
        # «30 /10» не берём; чистая «10» и «5» — берём
        self.assertEqual(by_code["sw-mixed"].quantity, 15)
        self.assertEqual(
            {c.city: c.quantity for c in by_code["sw-mixed"].by_city},
            {"Казань": 10, "Питер": 5},
        )

        skipped = {s.vendor_code: s.reason for s in distribution.skipped}
        self.assertIn("sw-y20-pink-2", skipped)  # ноль
        self.assertIn("sw-c86-black-gray-3", skipped)  # текст
        self.assertIn("sw-y20-black-5", skipped)  # текст


class ParseQuantityTest(unittest.TestCase):
    def test_formats(self) -> None:
        self.assertEqual(parse_quantity("1 000"), 1000)
        self.assertEqual(parse_quantity("1\xa0000"), 1000)
        self.assertEqual(parse_quantity("5,0"), 5)
        self.assertEqual(parse_quantity("7.6"), 8)
        self.assertEqual(parse_quantity("0"), 0)
        self.assertIsNone(parse_quantity(""))
        # рядом с числом текст / дробь — не берём
        self.assertIsNone(parse_quantity("10 шт"))
        self.assertIsNone(parse_quantity("30 /10"))
        self.assertIsNone(parse_quantity("нет же?"))
        self.assertIsNone(parse_quantity("сколько есть"))

    def test_negative(self) -> None:
        with self.assertRaises(WbLogistError) as ctx:
            parse_quantity("-5")

        self.assertEqual(ctx.exception.code, "SHEET_BAD_QUANTITY")


class SheetNameTest(unittest.TestCase):
    def test_normalize_ignores_spaces_and_brackets(self) -> None:
        self.assertEqual(
            normalize_sheet_name("Проверочный лист(Дима)"),
            normalize_sheet_name("Проверочный лист (Дима)"),
        )

    def test_resolve_gid_by_name(self) -> None:
        sheet = resolve_sheet_gid("ID", "Проверочный лист (Дима)", lambda url: HTMLVIEW)

        self.assertEqual(sheet.gid, "696867362")
        self.assertEqual(sheet.name, "Проверочный лист(Дима)")

    def test_resolve_gid_decodes_js_escapes(self) -> None:
        sheet = resolve_sheet_gid("ID", "Баранов 17.11.2026", lambda url: HTMLVIEW)

        self.assertEqual(sheet.gid, "965771581")

    def test_unknown_sheet_lists_available(self) -> None:
        with self.assertRaises(WbLogistError) as ctx:
            resolve_sheet_gid("ID", "Нет такого листа", lambda url: HTMLVIEW)

        self.assertEqual(ctx.exception.code, "SHEET_NOT_FOUND")
        self.assertIn("696867362", ctx.exception.message)

    def test_parse_baranov_sheet_name(self) -> None:
        parsed = parse_baranov_sheet_name("Баранов ВБ 05.11.2026")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.marketplace, "wb")
        self.assertEqual(parsed.sheet_date, date(2026, 11, 5))

        short = parse_baranov_sheet_name(
            "Баранов ВБ 27.10", today=date(2026, 11, 26)
        )
        self.assertIsNotNone(short)
        assert short is not None
        self.assertEqual(short.sheet_date, date(2026, 10, 27))

    def test_freshest_wb_sheet(self) -> None:
        sheets = [
            SheetRef("Баранов ВБ 27.10", "105443037"),
            SheetRef("Баранов ВБ 05.11.2026", "987430236"),
            SheetRef("Баранов ОЗОН 18.11.2026", "2005299093"),
            SheetRef("Проверочный лист(Дима)", "696867362"),
        ]
        picked = find_freshest_baranov_sheet(
            sheets, "wb", today=date(2026, 11, 26)
        )
        self.assertEqual(picked.gid, "987430236")
        self.assertEqual(picked.name, "Баранов ВБ 05.11.2026")


class LoadDistributionTest(unittest.TestCase):
    def _config(self, **overrides):
        values = {
            "spreadsheet_id": "ID",
            "sheet_gid": None,
            "sheet_name": "Проверочный лист (Дима)",
            "wb_api_key": None,
            "output_dir": Path("."),
            "cache_dir": Path("."),
            "http_timeout": 5.0,
            "cache_ttl_min": 30,
            "support_phone": "",
            "seller_name": "",
            "brand": "",
            "destination_marker": "ВБ",
        }
        values.update(overrides)
        return Config(**values)

    def test_resolves_by_name_then_reads_csv(self) -> None:
        seen: list[str] = []

        def getter(url: str) -> str:
            seen.append(url)
            return HTMLVIEW if "htmlview" in url else REAL_CSV

        distribution = load_distribution(
            self._config(), sheet_name="Проверочный лист (Дима)", http_get=getter
        )

        self.assertEqual(distribution.total_items, 520)
        self.assertEqual(distribution.sheet_label, "Проверочный лист(Дима) (gid=696867362)")
        self.assertIn("export?format=csv&gid=696867362", seen[-1])

    def test_auto_freshest_wb(self) -> None:
        def getter(url: str) -> str:
            return HTMLVIEW if "htmlview" in url else WB_WAREHOUSE_CSV

        distribution = load_distribution(
            self._config(),
            marketplace="wb",
            auto_freshest=True,
            http_get=getter,
            today=date(2026, 11, 26),
        )

        self.assertIn("Баранов ВБ 05.11.2026", distribution.sheet_label)
        self.assertIn("987430236", distribution.sheet_label)
        self.assertEqual(
            {r.vendor_code: r.quantity for r in distribution.rows}["sw-y36-pink-4"],
            100,
        )

    def test_uses_gid_without_htmlview(self) -> None:
        seen: list[str] = []

        def getter(url: str) -> str:
            seen.append(url)
            return REAL_CSV

        distribution = load_distribution(
            self._config(sheet_gid="42"), gid="42", http_get=getter
        )

        self.assertEqual(distribution.total_items, 520)
        self.assertEqual(len(seen), 1)
        self.assertIn("gid=42", seen[0])

    def test_html_instead_of_csv_is_error(self) -> None:
        fake = mock.Mock(status_code=200, content=b"<html>nope</html>")

        with mock.patch("requests.get", return_value=fake):
            with self.assertRaises(WbLogistError) as ctx:
                http_get_text("https://example.com", 1)

        self.assertEqual(ctx.exception.code, "SHEET_NOT_PUBLIC")


if __name__ == "__main__":
    unittest.main()
