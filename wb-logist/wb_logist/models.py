"""Модели данных, общие для всех модулей."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CityQty:
    """Количество по городу/складу-колонке (для Ozon-разбивки)."""

    city: str
    quantity: int


@dataclass(frozen=True)
class DistributionRow:
    """Строка распределения: артикул продавца и количество к отправке."""

    vendor_code: str
    quantity: int
    source_row: int
    by_city: tuple[CityQty, ...] = ()


@dataclass(frozen=True)
class SkippedRow:
    """Строка, которую не отправляем (например, количество = 0)."""

    vendor_code: str
    reason: str
    source_row: int


@dataclass(frozen=True)
class Distribution:
    """Результат чтения таблицы распределения."""

    rows: tuple[DistributionRow, ...]
    skipped: tuple[SkippedRow, ...]
    sheet_label: str
    source: str

    @property
    def positions(self) -> int:
        """Сколько уникальных артикулов уйдёт в поставку."""

        return len(self.rows)

    @property
    def total_items(self) -> int:
        """Суммарное количество штук."""

        return sum(row.quantity for row in self.rows)


@dataclass(frozen=True)
class ProductBarcode:
    """Разрешение артикула продавца в баркод через Content API."""

    vendor_code: str
    barcode: str
    size: str
    title: str
    nm_id: int | None = None
