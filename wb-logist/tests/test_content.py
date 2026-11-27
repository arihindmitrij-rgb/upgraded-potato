"""Тесты Content API-логики: пагинация, индекс карточек, разрешение баркодов."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from wb_logist.config import Config
from wb_logist.models import DistributionRow
from wb_logist.wb_content import (
    ContentClient,
    build_index,
    extract_color,
    resolve_rows,
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append(json)
        return self._responses.pop(0)


def _config(**overrides) -> Config:
    values = {
        "spreadsheet_id": "ID",
        "sheet_gid": None,
        "sheet_name": "Проверочный лист (Дима)",
        "wb_api_key": "token",
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


def _card(
    vendor: str,
    size: str,
    *skus: str,
    title: str = "Товар",
    colors: list[str] | None = None,
) -> dict:
    characteristics = []
    if colors is not None:
        characteristics.append({"name": "Цвет", "value": colors})
    return {
        "nmID": 1,
        "vendorCode": vendor,
        "title": title,
        "brand": "Бренд",
        "characteristics": characteristics,
        "sizes": [{"techSize": size, "wbSize": size, "skus": list(skus)}],
    }


class FetchCardsTest(unittest.TestCase):
    def test_pagination_stops_when_page_is_short(self) -> None:
        first_page = [_card(f"v{i}", "0", f"10{i}") for i in range(100)]
        second_page = [_card("last", "0", "999")]
        session = FakeSession(
            [
                FakeResponse({"cards": first_page, "cursor": {"total": 100, "nmID": 5, "updatedAt": "t"}}),
                FakeResponse({"cards": second_page, "cursor": {"total": 1, "nmID": 6, "updatedAt": "t2"}}),
            ]
        )

        cards = ContentClient(_config(), session=session, sleep=lambda _: None).fetch_cards()

        self.assertEqual(len(cards), 101)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[1]["settings"]["cursor"]["nmID"], 5)

    def test_missing_token(self) -> None:
        client = ContentClient(_config(wb_api_key=None), session=FakeSession([]))

        with self.assertRaises(Exception) as ctx:
            client.fetch_cards()

        self.assertEqual(getattr(ctx.exception, "code", None), "CONFIG_ERROR")


class ResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = build_index(
            [
                _card("sw-a", "3", "111", title="Часы A"),
                _card("sw-b", "4", "222"),
                _card("sw-c", "S", "333"),
                _card("sw-c", "M", "444"),
                _card("sw-empty", "0"),
            ]
        )

    def test_single_barcode(self) -> None:
        rows = [DistributionRow("sw-a", 5, 2)]

        items, errors = resolve_rows(rows, self.index)

        self.assertEqual(errors, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].barcode, "111")
        self.assertEqual(items[0].size, "3")
        self.assertEqual(items[0].title, "Часы A")

    def test_color_from_characteristics(self) -> None:
        index = build_index(
            [_card("sw-y36-pink-4", "0", "999", colors=["розовый", "черный"])]
        )
        items, errors = resolve_rows(
            [DistributionRow("sw-y36-pink-4", 1, 2)], index
        )
        self.assertEqual(errors, [])
        self.assertEqual(items[0].color, "розовый")

    def test_missing_card(self) -> None:
        items, errors = resolve_rows([DistributionRow("nope", 1, 2)], self.index)

        self.assertEqual(items, [])
        self.assertIn("карточка не найдена", errors[0])

    def test_ambiguous_sizes(self) -> None:
        items, errors = resolve_rows([DistributionRow("sw-c", 1, 2)], self.index)

        self.assertEqual(items, [])
        self.assertIn("несколько размеров", errors[0])

    def test_card_without_sku(self) -> None:
        items, errors = resolve_rows([DistributionRow("sw-empty", 1, 2)], self.index)

        self.assertEqual(items, [])
        self.assertIn("карточка не найдена", errors[0])

    def test_case_insensitive_fallback(self) -> None:
        items, errors = resolve_rows([DistributionRow("SW-A", 2, 2)], self.index)

        self.assertEqual(errors, [])
        self.assertEqual(items[0].barcode, "111")


class ExtractColorTest(unittest.TestCase):
    def test_single_and_hint(self) -> None:
        self.assertEqual(
            extract_color("sw-a", [{"name": "Цвет", "value": ["черный"]}]),
            "черный",
        )
        self.assertEqual(
            extract_color(
                "sw-c86-pink-4",
                [{"name": "Цвет", "value": ["розовый", "черный"]}],
            ),
            "розовый",
        )
        self.assertEqual(
            extract_color(
                "sw-c86-black-red-3",
                [{"name": "Цвет", "value": ["черный", "красный", "белый"]}],
            ),
            "черный-красный",
        )
        self.assertEqual(extract_color("sw-a", []), "")


if __name__ == "__main__":
    unittest.main()
