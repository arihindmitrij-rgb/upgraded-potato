"""Генерация ТЗ для фулфилмента по шаблону «ТЗ Экспо Карго».

Шаблон — ваш файл ИП_Баранов_ВБ.xlsx (копия в templates/tz_template.xlsx):
заголовок, чек-лист услуг, лист «ТЗ по упаковке» и оформление сохраняются,
скрипт только заполняет товарные строки.
"""

from __future__ import annotations

import re
from copy import copy
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from openpyxl import load_workbook
from openpyxl.styles import Alignment
from openpyxl.worksheet.worksheet import Worksheet

from .config import PROJECT_ROOT, Config
from .errors import WbLogistError
from .wb_content import ResolvedItem

TEMPLATE_PATH = PROJECT_ROOT / "templates" / "tz_template.xlsx"
SHEET_NAME = "ТЗ Экспо Карго"
FIRST_ROW = 2
LAST_COLUMN = 18  # R

COL_NUM = 1
COL_TITLE = 2
COL_COLOR = 4
COL_SIZE = 5
COL_VENDOR = 6
COL_PHONE = 7
COL_BARCODE = 10
COL_QTY = 11
COL_SELLER = 12
COL_BRAND = 13
COL_SERVICE = 14
COL_YES = 15
COL_STICKERS = 16
COL_COMMENT = 18


def _template_last_product_row(ws: Worksheet, scan_to: int = 80) -> int:
    """Последняя заполненная строка товаров в шаблоне (колонка «Артикул»)."""

    last = 0
    for row in range(FIRST_ROW, scan_to):
        if ws.cell(row, COL_VENDOR).value:
            last = row
    if not last:
        raise WbLogistError(
            "TEMPLATE_BROKEN",
            f"В шаблоне нет товарных строк в колонке «Артикул» (лист «{SHEET_NAME}»).",
        )
    return last


def _copy_row_style(ws: Worksheet, src_row: int, dst_row: int) -> None:
    for column in range(1, LAST_COLUMN + 1):
        source = ws.cell(src_row, column)
        target = ws.cell(dst_row, column)
        if source.has_style:
            target._style = copy(source._style)
        target.alignment = Alignment(
            horizontal=source.alignment.horizontal,
            vertical=source.alignment.vertical,
            wrap_text=source.alignment.wrap_text,
        )


def _find_warehouse_row(ws: Worksheet, scan_to: int = 200) -> int | None:
    for row in range(FIRST_ROW, scan_to):
        value = ws.cell(row, COL_SERVICE).value
        if isinstance(value, str) and "склад" in value.casefold():
            return row
    return None


def fill_template(
    ws: Worksheet,
    items: Sequence[ResolvedItem],
    config: Config,
) -> None:
    """Заполняет товарные строки, не ломая чек-лист услуг и оформление."""

    template_rows = _template_last_product_row(ws) - FIRST_ROW + 1
    extra = len(items) - template_rows

    if extra > 0:
        insert_at = FIRST_ROW + template_rows
        ws.insert_rows(insert_at, amount=extra)
        for offset in range(extra):
            _copy_row_style(ws, insert_at - 1, insert_at + offset)

    for offset, item in enumerate(items):
        row = FIRST_ROW + offset
        ws.cell(row, COL_NUM).value = offset + 1
        ws.cell(row, COL_TITLE).value = item.title or None
        # Цвет — только из Content API (characteristics), не из хвостов шаблона
        ws.cell(row, COL_COLOR).value = item.color or None
        # WB отдаёт "0" для товаров без размерной сетки (размер зашит в артикул)
        size = item.size if item.size and item.size != "0" else None
        ws.cell(row, COL_SIZE).value = size
        ws.cell(row, COL_VENDOR).value = item.vendor_code

        barcode_cell = ws.cell(row, COL_BARCODE)
        barcode_cell.value = str(item.barcode)
        barcode_cell.number_format = "@"  # 13 цифр не должны стать 2,05E+12

        ws.cell(row, COL_QTY).value = item.quantity
        ws.cell(row, COL_STICKERS).value = f"=K{row}*2"

        if config.support_phone:
            ws.cell(row, COL_PHONE).value = config.support_phone
        if config.seller_name:
            ws.cell(row, COL_SELLER).value = config.seller_name
        if config.brand:
            ws.cell(row, COL_BRAND).value = item.brand or config.brand
        elif item.brand:
            ws.cell(row, COL_BRAND).value = item.brand

    # Лишние товарные строки шаблона очищаем, услуги (N) не трогаем
    for row in range(FIRST_ROW + len(items), FIRST_ROW + template_rows):
        for column in (
            COL_NUM,
            COL_TITLE,
            COL_COLOR,
            COL_SIZE,
            COL_VENDOR,
            COL_PHONE,
            COL_BARCODE,
            COL_QTY,
            COL_SELLER,
            COL_BRAND,
            COL_YES,
        ):
            ws.cell(row, column).value = None
        ws.cell(row, COL_STICKERS).value = 0

    warehouse_row = _find_warehouse_row(ws)
    if warehouse_row:
        ws.cell(warehouse_row, COL_COMMENT).value = config.destination_marker


def _safe_filename_part(value: str) -> str:
    """Убирает символы, недопустимые в имени файла Windows."""

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value).strip().strip(".")
    return cleaned or "ТЗ"


def _report_stem(config: Config, created: date) -> str:
    try:
        stamp = created.strftime(config.report_date_format)
    except (ValueError, TypeError) as exc:
        raise WbLogistError(
            "CONFIG_ERROR",
            f"REPORT_DATE_FORMAT некорректен: {config.report_date_format!r}",
        ) from exc
    return f"{_safe_filename_part(config.report_prefix)} {_safe_filename_part(stamp)}"


def build_report(
    items: Sequence[ResolvedItem],
    config: Config,
    out_dir: Path | None = None,
    *,
    report_date: date | None = None,
    template_path: Path | None = None,
) -> Path:
    """Собирает xlsx и возвращает абсолютный путь к нему."""

    if not items:
        raise WbLogistError("REPORT_EMPTY", "Нет позиций для ТЗ — файл не создаю.")

    template = Path(template_path) if template_path else TEMPLATE_PATH
    if not template.is_file():
        raise WbLogistError("TEMPLATE_NOT_FOUND", f"Шаблон не найден: {template}")

    workbook = load_workbook(template)
    if SHEET_NAME in workbook.sheetnames:
        worksheet = workbook[SHEET_NAME]
    else:
        worksheet = workbook.worksheets[0]

    fill_template(worksheet, items, config)

    directory = Path(out_dir) if out_dir else (config.report_dir or config.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = _report_stem(config, report_date or date.today())
    out_path = directory / f"{stem}.xlsx"

    try:
        workbook.save(out_path)
    except PermissionError:
        # Файл открыт в Excel — пишем рядом, с временем, вместо падения
        fallback = directory / f"{stem} {datetime.now():%H-%M-%S}.xlsx"
        workbook.save(fallback)
        out_path = fallback

    return out_path.resolve()
