"""Тесты черновика поставки: создание, чанки, откат при отказе WB."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wb_logist.config import Config
from wb_logist.errors import WbLogistError
from wb_logist.wb_content import ResolvedItem
from wb_logist.wb_supplies import SuppliesClient


class FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.content = body
        self.text = body.decode("utf-8")

    def json(self) -> dict | None:
        return self._payload


class FakeSession:
    def __init__(
        self,
        responses: list[FakeResponse | Exception],
        drafts: list[dict] | None = None,
        items: dict[str, int] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._drafts = drafts or []
        self._items = items or {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append((method, url, json))
        if method == "GET":
            if url.endswith("/items"):
                rows = [{"sku": sku, "quantity": qty} for sku, qty in self._items.items()]
                return FakeResponse({"items": rows})
            return FakeResponse({"drafts": self._drafts})

        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def _item(barcode: str, quantity: int) -> ResolvedItem:
    return ResolvedItem(
        vendor_code=f"v-{barcode}",
        quantity=quantity,
        barcode=barcode,
        size="",
        title="Товар",
        brand="",
        nm_id=1,
    )


class CreateSupplyTest(unittest.TestCase):
    def test_happy_path(self) -> None:
        session = FakeSession(
            [FakeResponse({"draftId": "draft-1"}), FakeResponse({"results": []})]
        )

        result = SuppliesClient(
            _config(), session=session, sleep=lambda _: None
        ).create_supply([_item("111", 5), _item("222", 7)])

        self.assertEqual(result.draft_id, "draft-1")
        self.assertEqual(result.total_items, 12)
        self.assertEqual(result.chunks, 1)

        posts = [call for call in session.calls if call[0] == "POST"]
        method, url, payload = posts[1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/supplies/v1/drafts/draft-1/items"))
        self.assertEqual(
            payload,
            {"items": [{"sku": "111", "quantity": 5}, {"sku": "222", "quantity": 7}]},
        )

    def test_draft_has_no_body(self) -> None:
        session = FakeSession(
            [FakeResponse({"draftId": "d"}), FakeResponse({"results": []})]
        )

        SuppliesClient(_config(), session=session, sleep=lambda _: None).create_supply(
            [_item("111", 1)]
        )

        first_post = next(call for call in session.calls if call[0] == "POST")
        self.assertIsNone(first_post[2])

    def test_items_are_chunked_by_1000(self) -> None:
        items = [_item(f"{i:013d}", 1) for i in range(1500)]
        session = FakeSession(
            [
                FakeResponse({"draftId": "d"}),
                FakeResponse({"results": []}),
                FakeResponse({"results": []}),
            ]
        )

        result = SuppliesClient(
            _config(), session=session, sleep=lambda _: None
        ).create_supply(items)

        posts = [call for call in session.calls if call[0] == "POST"]
        self.assertEqual(result.chunks, 2)
        self.assertEqual(len(posts[1][2]["items"]), 1000)
        self.assertEqual(len(posts[2][2]["items"]), 500)


    def test_rejected_barcode_rolls_back(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"draftId": "d"}),
                FakeResponse(
                    {
                        "results": [
                            {
                                "sku": "999",
                                "error": {
                                    "title": "Invalid sku",
                                    "detail": "Нет карточки",
                                },
                            }
                        ]
                    }
                ),
                FakeResponse(status_code=204),
            ]
        )

        with self.assertRaises(WbLogistError) as ctx:
            SuppliesClient(
                _config(), session=session, sleep=lambda _: None
            ).create_supply([_item("999", 1)])

        self.assertEqual(ctx.exception.code, "SUPPLY_ITEMS_REJECTED")
        self.assertIn("Нет карточки", ctx.exception.message)
        self.assertIn("Черновик удалён", ctx.exception.message)
        self.assertEqual(session.calls[-1][0], "DELETE")

    def test_failed_delete_is_reported(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"draftId": "d"}),
                FakeResponse({"results": [{"sku": "1", "error": {"detail": "bad"}}]}),
                FakeResponse({"status": 500}, status_code=500),
                FakeResponse({"status": 500}, status_code=500),
                FakeResponse({"status": 500}, status_code=500),
            ]
        )

        with self.assertRaises(WbLogistError) as ctx:
            SuppliesClient(
                _config(), session=session, sleep=lambda _: None
            ).create_supply([_item("1", 1)])

        self.assertIn("удалите черновик вручную", ctx.exception.message)

    def test_create_failure(self) -> None:
        session = FakeSession([FakeResponse({"title": "bad"}, status_code=400)])

        with self.assertRaises(WbLogistError) as ctx:
            SuppliesClient(
                _config(), session=session, sleep=lambda _: None
            ).create_supply([_item("1", 1)])

        self.assertEqual(ctx.exception.code, "SUPPLY_CREATE_FAILED")
        self.assertEqual(len([c for c in session.calls if c[0] == "POST"]), 1)

    def test_401(self) -> None:
        session = FakeSession([FakeResponse({}, status_code=401)])

        with self.assertRaises(WbLogistError) as ctx:
            SuppliesClient(
                _config(), session=session, sleep=lambda _: None
            ).create_supply([_item("1", 1)])

        self.assertEqual(ctx.exception.code, "AUTH_401")

    def test_empty_items(self) -> None:
        with self.assertRaises(WbLogistError) as ctx:
            SuppliesClient(
                _config(), session=FakeSession([]), sleep=lambda _: None
            ).create_supply([])

        self.assertEqual(ctx.exception.code, "SUPPLY_EMPTY")


class DuplicateGuardTest(unittest.TestCase):
    # Фиксированное «сейчас»: совпадает с createdAt фикстур, поэтому черновики
    # считаются свежими независимо от реальной даты запуска тестов.
    NOW = datetime(2026, 11, 19, 17, 0, 0, tzinfo=timezone.utc)

    def _draft_meta(self, draft_id: str, total: int, skus: int) -> dict:
        return {
            "draftId": draft_id,
            "itemQuantity": total,
            "skuQuantity": skus,
            "createdAt": "2026-11-19T16:00:00.000Z",
        }

    def test_reuses_existing_identical_draft(self) -> None:
        session = FakeSession(
            [],
            drafts=[self._draft_meta("already-here", 12, 2)],
            items={"111": 5, "222": 7},
        )

        result = SuppliesClient(
            _config(), session=session, sleep=lambda _: None
        ).create_supply([_item("111", 5), _item("222", 7)], now=self.NOW)

        self.assertTrue(result.reused)
        self.assertEqual(result.draft_id, "already-here")
        self.assertEqual(result.total_items, 12)
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_different_quantities_are_not_duplicate(self) -> None:
        session = FakeSession(
            [FakeResponse({"draftId": "new"}), FakeResponse({"results": []})],
            drafts=[self._draft_meta("old", 12, 2)],
            items={"111": 5, "222": 8},
        )

        result = SuppliesClient(
            _config(), session=session, sleep=lambda _: None
        ).create_supply([_item("111", 5), _item("222", 7)], now=self.NOW)

        self.assertFalse(result.reused)
        self.assertEqual(result.draft_id, "new")

    def test_allow_duplicate_skips_check(self) -> None:
        session = FakeSession(
            [FakeResponse({"draftId": "new"}), FakeResponse({"results": []})],
            drafts=[self._draft_meta("already-here", 12, 2)],
            items={"111": 5, "222": 7},
        )

        result = SuppliesClient(
            _config(), session=session, sleep=lambda _: None
        ).create_supply(
            [_item("111", 5), _item("222", 7)], allow_duplicate=True, now=self.NOW
        )

        self.assertFalse(result.reused)
        self.assertEqual(result.draft_id, "new")
        self.assertEqual([call[0] for call in session.calls], ["POST", "POST"])


if __name__ == "__main__":
    unittest.main()

