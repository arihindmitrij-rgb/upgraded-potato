"""Поставки FBW: черновик поставки (без слота) и добавление товаров.

Актуальный API WB (2026): «Черновики поставок»
  POST /api/supplies/v1/drafts            -> {"draftId": "uuid"}
  POST /api/supplies/v1/drafts/{id}/items -> {"items": [{"sku", "quantity"}]}
  DELETE /api/supplies/v1/drafts/{id}     -> 204

Имя черновика и склад в API не передаются, слот/таймслот не бронируется —
дату и коэффициент вы выбираете в кабинете WB.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import requests

from .config import Config
from .errors import WbLogistError
from .wb_content import ResolvedItem

DRAFTS_PATH = "/api/supplies/v1/drafts"
ITEMS_CHUNK = 1000
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class DraftResult:
    draft_id: str
    total_items: int
    chunks: int
    reused: bool = False


def _sleep_seconds(attempt: int, response: requests.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
    return float(2**attempt)


class SuppliesClient:
    """Клиент «Поставки FBW» с ретраями и откатом."""

    def __init__(
        self,
        config: Config,
        session: requests.Session | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._sleep = sleep

    @property
    def _headers(self) -> dict[str, str]:
        if not self._config.wb_api_key:
            raise WbLogistError(
                "CONFIG_ERROR",
                "WB_API_KEY_STANDARD не задан: впишите ключ в .env "
                "(категории «Контент» + «Поставки»).",
            )
        return {"Authorization": self._config.wb_api_key}

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        error_code: str,
    ) -> tuple[int, Any]:
        url = self._config.supplies_api_base + path
        last_error = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers=self._headers,
                    timeout=self._config.http_timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if 200 <= response.status_code < 300:
                    if response.content:
                        try:
                            return response.status_code, response.json()
                        except ValueError:
                            return response.status_code, None
                    return response.status_code, None
                if response.status_code == 401:
                    raise WbLogistError(
                        "AUTH_401",
                        "WB отклонил токен (401). Проверьте WB_API_KEY_STANDARD.",
                    )
                if response.status_code == 403:
                    raise WbLogistError(
                        "FORBIDDEN_403",
                        "У токена нет доступа к поставкам (403). "
                        "Нужна категория «Поставки».",
                    )
                last_error = f"HTTP {response.status_code}: {response.text[:400]}"
                if (response.status_code == 429 or response.status_code >= 500) and (
                    attempt < MAX_ATTEMPTS
                ):
                    self._sleep(_sleep_seconds(attempt, response))
                    continue
                break

            if attempt < MAX_ATTEMPTS:
                self._sleep(_sleep_seconds(attempt, None))

        raise WbLogistError(error_code, f"Поставки API: {last_error}")


    def create_draft(self) -> str:
        """Создаёт пустой черновик и возвращает его draftId."""

        _, data = self._request("POST", DRAFTS_PATH, error_code="SUPPLY_CREATE_FAILED")
        draft_id = (data or {}).get("draftId") if isinstance(data, dict) else None
        if not draft_id:
            raise WbLogistError(
                "SUPPLY_CREATE_FAILED", f"WB не вернул draftId: {str(data)[:300]}"
            )
        return str(draft_id)

    def add_items(self, draft_id: str, items: Sequence[ResolvedItem]) -> int:
        """Добавляет товары чанками по 1000. Возвращает число чанков."""

        chunks = 0
        prepared = [
            {"sku": str(item.barcode), "quantity": int(item.quantity)}
            for item in items
        ]

        for start in range(0, len(prepared), ITEMS_CHUNK):
            batch = prepared[start : start + ITEMS_CHUNK]
            _, data = self._request(
                "POST",
                f"{DRAFTS_PATH}/{draft_id}/items",
                payload={"items": batch},
                error_code="SUPPLY_ADD_FAILED",
            )
            chunks += 1

            results = (data or {}).get("results") if isinstance(data, dict) else None
            if results:
                details = "; ".join(
                    f"{row.get('sku')}: {(row.get('error') or {}).get('detail')}"
                    for row in results[:5]
                    if isinstance(row, dict)
                )
                raise WbLogistError(
                    "SUPPLY_ITEMS_REJECTED",
                    f"WB отклонил баркоды ({len(results)}): {details}",
                )

        return chunks

    def delete_draft(self, draft_id: str) -> bool:
        """Удаляет черновик (откат). Возвращает False, если не получилось."""

        try:
            self._request(
                "DELETE",
                f"{DRAFTS_PATH}/{draft_id}",
                error_code="SUPPLY_DELETE_FAILED",
            )
        except WbLogistError:
            return False
        return True

    def recent_drafts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Последние черновики — нужны для проверки дублей."""

        _, data = self._request(
            "GET",
            DRAFTS_PATH,
            params={"limit": limit, "sort": "createDt", "order": "desc"},
            error_code="SUPPLY_LIST_FAILED",
        )
        if not isinstance(data, dict):
            return []
        return data.get("drafts") or []

    def draft_items(self, draft_id: str) -> dict[str, int]:
        """sku -> quantity внутри черновика."""

        _, data = self._request(
            "GET",
            f"{DRAFTS_PATH}/{draft_id}/items",
            error_code="SUPPLY_ITEMS_READ_FAILED",
        )
        if not isinstance(data, dict):
            return {}
        result: dict[str, int] = {}
        for row in data.get("items") or []:
            sku = str((row or {}).get("sku") or "").strip()
            if sku:
                result[sku] = result.get(sku, 0) + int((row or {}).get("quantity") or 0)
        return result

    def find_duplicate(
        self,
        items: Sequence[ResolvedItem],
        *,
        lookback_hours: int = 24,
        now: datetime | None = None,
    ) -> str | None:
        """Ищет свежий черновик с ровно таким же набором sku -> quantity.

        ``now`` нужен только для детерминированных тестов; в проде используется
        текущее время.
        """

        wanted = {str(item.barcode): int(item.quantity) for item in items}
        total = sum(wanted.values())
        current = now or datetime.now(timezone.utc)
        cutoff = current - timedelta(hours=lookback_hours)

        for draft in self.recent_drafts():
            draft_id = str(draft.get("draftId") or "")
            if not draft_id:
                continue

            created_raw = str(draft.get("createdAt") or "")
            try:
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except ValueError:
                created = None
            if created is not None and created < cutoff:
                continue

            if int(draft.get("itemQuantity") or 0) != total:
                continue
            if int(draft.get("skuQuantity") or 0) != len(wanted):
                continue

            try:
                if self.draft_items(draft_id) == wanted:
                    return draft_id
            except WbLogistError:
                continue

        return None

    def create_supply(
        self,
        items: Sequence[ResolvedItem],
        *,
        allow_duplicate: bool = False,
        now: datetime | None = None,
    ) -> DraftResult:
        """Черновик + товары. При любой ошибке черновик удаляется (откат)."""

        if not items:
            raise WbLogistError("SUPPLY_EMPTY", "Нечего добавлять в поставку.")

        total_items = sum(item.quantity for item in items)

        if not allow_duplicate:
            existing = self.find_duplicate(items, now=now)
            if existing:
                return DraftResult(
                    draft_id=existing,
                    total_items=total_items,
                    chunks=0,
                    reused=True,
                )

        draft_id = self.create_draft()
        try:
            chunks = self.add_items(draft_id, items)
        except WbLogistError as exc:
            rolled_back = self.delete_draft(draft_id)
            suffix = (
                "Черновик удалён."
                if rolled_back
                else "Автоудаление не удалось — удалите черновик вручную."
            )
            raise WbLogistError(exc.code, f"{exc.message} {suffix}") from exc

        return DraftResult(
            draft_id=draft_id,
            total_items=sum(item.quantity for item in items),
            chunks=chunks,
        )

