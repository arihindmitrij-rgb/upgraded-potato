"""Тесты генерации ТЗ: баркод текстом, формула стикеров, склад, шаблон цел."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from openpyxl import load_workbook

from wb_logist.config import Config
from wb_logist.excel import build_report
from wb_logist.wb_content import ResolvedItem


def _config(output_dir: Path, **overrides) -> Config:
    values = {
        "spreadsheet_id": "ID",
        "sheet_gid": None,
        "sheet_name": "Проверочный лист (Дима)",
        "wb_api_key": "token",
        "output_dir": output_dir,
        "cache_dir": output_dir,
        "http_timeout": 5.0,
        "cache_ttl_min": 30,
        "support_phone": "+7-000-000-00-00",
        "seller_name": "ИП Тестовый",
        "brand": "",
        "destination_marker": "ВБ",
    }
    values.update(overrides)
    return Config(**values)


def _item(
    vendor: str,
    barcode: str,
    qty: int,
    size: str = "3",
    color: str = "розовый",
) -> ResolvedItem:
    return ResolvedItem(
        vendor_code=vendor,
        quantity=qty,
        barcode=barcode,
        size=size,
        title=f"Товар {vendor}",
        brand="Бренд",
        nm_id=1,
        color=color,
    )


class BuildReportTest(unittest.TestCase):
    def _build(self, items, **config_overrides):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out_dir = Path(tmp.name)
        path = build_report(
            items,
            _config(out_dir, **config_overrides),
            out_dir,
            report_date=date(2026, 11, 19),
        )
        return path, load_workbook(path)

    def test_file_name_and_main_sheet(self) -> None:
        path, workbook = self._build([_item("sw-a", "2054286143980", 20)])

        self.assertEqual(path.name, "ИП Баранов ВБ 19.11.2026.xlsx")
        self.assertIn("ТЗ Экспо Карго", workbook.sheetnames)
        self.assertIn("ТЗ по упаковке", workbook.sheetnames)

    def test_custom_prefix_and_format(self) -> None:
        path, _ = self._build(
            [_item("sw-a", "111", 1)],
            report_prefix="ИП Тестовый/WB",
            report_date_format="%Y-%m-%d",
        )

        self.assertEqual(path.name, "ИП ТестовыйWB 2026-11-19.xlsx")

    def test_product_row(self) -> None:
        _, workbook = self._build([_item("sw-a", "2054286143980", 20)])
        ws = workbook["ТЗ Экспо Карго"]

        self.assertEqual(ws["A2"].value, 1)
        self.assertEqual(ws["B2"].value, "Товар sw-a")
        self.assertEqual(ws["D2"].value, "розовый")
        self.assertEqual(ws["E2"].value, "3")
        self.assertEqual(ws["F2"].value, "sw-a")
        self.assertEqual(ws["G2"].value, "+7-000-000-00-00")

        self.assertEqual(ws["J2"].value, "2054286143980")
        self.assertEqual(ws["J2"].number_format, "@")

        self.assertEqual(ws["K2"].value, 20)
        self.assertEqual(ws["L2"].value, "ИП Тестовый")
        self.assertEqual(ws["P2"].value, "=K2*2")

    def test_leftover_template_rows_cleared(self) -> None:
        _, workbook = self._build([_item("sw-a", "111", 1)])
        ws = workbook["ТЗ Экспо Карго"]

        self.assertIsNone(ws["F5"].value)
        self.assertIsNone(ws["K5"].value)
        self.assertEqual(ws["P5"].value, 0)
        # чек-лист услуг не тронут
        self.assertTrue(str(ws["N5"].value or "").strip())

    def test_warehouse_marker(self) -> None:
        _, workbook = self._build([_item("sw-a", "111", 1)], destination_marker="ВБ")
        ws = workbook["ТЗ Экспо Карго"]

        found = [
            ws.cell(row, 18).value
            for row in range(2, 60)
            if isinstance(ws.cell(row, 14).value, str)
            and "склад" in ws.cell(row, 14).value.casefold()
        ]

        self.assertEqual(found, ["ВБ"])

    def test_size_zero_is_blank(self) -> None:
        _, workbook = self._build([_item("sw-a", "111", 1, size="0")])
        ws = workbook["ТЗ Экспо Карго"]

        self.assertIsNone(ws["E2"].value)

    def test_more_items_than_template_rows(self) -> None:
        items = [_item(f"sw-{i}", f"90{i:09d}", i + 1) for i in range(15)]

        path, workbook = self._build(items)
        ws = workbook["ТЗ Экспо Карго"]

        self.assertEqual(len(items), 15)
        self.assertEqual(ws.cell(16, 6).value, "sw-14")
        self.assertEqual(ws.cell(16, 16).value, "=K16*2")
        marker_rows = [
            ws.cell(row, 18).value
            for row in range(2, 80)
            if isinstance(ws.cell(row, 14).value, str)
            and "склад" in ws.cell(row, 14).value.casefold()
        ]
        self.assertEqual(marker_rows, ["ВБ"])
        self.assertTrue(path.is_file())

    def test_empty_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception) as ctx:
                build_report([], _config(Path(tmp)), Path(tmp))

        self.assertEqual(getattr(ctx.exception, "code", None), "REPORT_EMPTY")


if __name__ == "__main__":
    unittest.main()
