"""Ozon Seller API: smoke-test, товары, черновик поставки multi-cluster.

Без ключей методы тихо возвращают статус «не настроено» — пайплайн WB не блокируют.
Черновик создаётся только при заполненных OZON_* и явном (не dry-run) запуске.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Sequence

import requests

from .config import Config
from .errors import WbLogistError
from .models import DistributionRow
from .wb_content import ResolvedItem, color_from_vendor_code

PRODUCT_LIST_PATH = "/v3/product/list"
PRODUCT_INFO_PATH = "/v3/product/info/list"
CLUSTER_LIST_PATH = "/v1/cluster/list"
WAREHOUSE_FBO_LIST_PATH = "/v1/warehouse/fbo/list"
MULTI_CLUSTER_DRAFT_PATH = "/v1/draft/multi-cluster/create"
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class OzonSmokeResult:
    ok: bool
    configured: bool
    message: str
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class OzonProduct:
    offer_id: str
    product_id: int | None
    sku: int | None
    title: str
    barcode: str
    color: str = ""


@dataclass(frozen=True)
class OzonDraftResult:
    draft_id: str | None
    total_items: int
    clusters: int
    dry_run: bool
    message: str
    errors: tuple[str, ...] = ()


def _sleep_seconds(attempt: int, response: requests.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
    return float(2**attempt)


def _normalize_city(name: str) -> str:
    text = (name or "").replace("ё", "е").replace("Ё", "Е")
    text = " ".join(text.split()).casefold()
    return text


class OzonClient:
    """Тонкий клиент Seller API."""

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
    def configured(self) -> bool:
        return self._config.ozon_configured

    @property
    def _headers(self) -> dict[str, str]:
        if not self._config.ozon_client_id or not self._config.ozon_api_key:
            raise WbLogistError(
                "OZON_NOT_CONFIGURED",
                "OZON_CLIENT_ID / OZON_API_KEY не заданы в .env.",
            )
        return {
            "Client-Id": self._config.ozon_client_id,
            "Api-Key": self._config.ozon_api_key,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        error_code: str = "OZON_API_ERROR",
    ) -> Any:
        url = self._config.ozon_api_base.rstrip("/") + path
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.post(
                    url,
                    json=payload or {},
                    headers=self._headers,
                    timeout=self._config.http_timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if 200 <= response.status_code < 300:
                    if not response.content:
                        return {}
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise WbLogistError(
                            error_code, f"Ozon вернул не-JSON: {response.text[:200]}"
                        ) from exc
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    self._sleep(_sleep_seconds(attempt, response))
                    continue
                raise WbLogistError(
                    error_code,
                    f"Ozon API {path}: HTTP {response.status_code}: {response.text[:300]}",
                )
            self._sleep(_sleep_seconds(attempt, None))
        raise WbLogistError(error_code, f"Ozon API {path}: {last_error}")

    def smoke_test(self) -> OzonSmokeResult:
        """Один тихий smoke: product/list limit 1. Без ключей — не ок, без исключения."""

        if not self.configured:
            return OzonSmokeResult(
                ok=False,
                configured=False,
                message="OZON_CLIENT_ID / OZON_API_KEY пусты — API пропущен",
            )
        try:
            data = self._request(
                PRODUCT_LIST_PATH,
                {"filter": {"visibility": "ALL"}, "limit": 1, "last_id": ""},
                error_code="OZON_SMOKE_FAILED",
            )
            total = None
            if isinstance(data, dict):
                result = data.get("result") or {}
                if isinstance(result, dict):
                    total = result.get("total")
            return OzonSmokeResult(
                ok=True,
                configured=True,
                message="Ozon API OK (product/list)",
                detail={"total": total},
            )
        except WbLogistError as exc:
            return OzonSmokeResult(
                ok=False,
                configured=True,
                message=f"Ozon API ошибка: {exc.message}",
            )

    def resolve_products(
        self, offer_ids: Sequence[str]
    ) -> tuple[dict[str, OzonProduct], list[str]]:
        """offer_id → sku/barcode/title через product/info/list."""

        mapping: dict[str, OzonProduct] = {}
        missing: list[str] = []
        unique = [oid for oid in dict.fromkeys(offer_ids) if oid]
        if not unique:
            return mapping, missing

        for offset in range(0, len(unique), 100):
            chunk = unique[offset : offset + 100]
            data = self._request(
                PRODUCT_INFO_PATH,
                {"offer_id": chunk},
                error_code="OZON_PRODUCT_RESOLVE_FAILED",
            )
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                items = (data.get("result") or {}).get("items") if isinstance(data, dict) else []
            found: set[str] = set()
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                offer_id = str(item.get("offer_id") or "").strip()
                if not offer_id:
                    continue
                sku = item.get("sku")
                sources = item.get("sources") or []
                if sku is None and isinstance(sources, list):
                    for src in sources:
                        if isinstance(src, dict) and src.get("sku") is not None:
                            sku = src.get("sku")
                            break
                barcodes = item.get("barcodes") or []
                barcode = ""
                if isinstance(barcodes, list) and barcodes:
                    barcode = str(barcodes[0])
                elif item.get("barcode"):
                    barcode = str(item.get("barcode"))
                color = color_from_vendor_code(offer_id)
                mapping[offer_id] = OzonProduct(
                    offer_id=offer_id,
                    product_id=int(item["id"]) if item.get("id") is not None else None,
                    sku=int(sku) if sku is not None else None,
                    title=str(item.get("name") or item.get("title") or ""),
                    barcode=barcode,
                    color=color,
                )
                found.add(offer_id)
            for offer_id in chunk:
                if offer_id not in found:
                    missing.append(offer_id)
        # case-insensitive aliases for sheet offer_ids
        folded = {key.casefold(): key for key in list(mapping)}
        for offer_id in unique:
            if offer_id in mapping:
                continue
            original = folded.get(offer_id.casefold())
            if original and original in mapping:
                mapping[offer_id] = mapping[original]
                if offer_id in missing:
                    missing.remove(offer_id)
        return mapping, missing

    def list_clusters(self) -> list[dict[str, Any]]:
        data = self._request(
            CLUSTER_LIST_PATH,
            {"cluster_type": "CLUSTER_TYPE_OZON"},
            error_code="OZON_CLUSTER_LIST_FAILED",
        )
        clusters = data.get("clusters") if isinstance(data, dict) else None
        return list(clusters) if isinstance(clusters, list) else []

    def match_cluster_id(
        self, city: str, clusters: Sequence[dict[str, Any]]
    ) -> int | None:
        """Сопоставляет колонку листа с macrolocal_cluster_id Ozon."""

        target = _normalize_city(city)
        if not target:
            return None

        best_id: int | None = None
        best_score = 0
        for cluster in clusters:
            name = str(cluster.get("name") or "")
            norm = _normalize_city(name)
            # для /v1/draft/multi-cluster/create нужен macrolocal_cluster_id
            cluster_id = cluster.get("macrolocal_cluster_id")
            if cluster_id is None:
                cluster_id = cluster.get("id")
            if cluster_id is None:
                continue
            score = 0
            if target == norm:
                score = 100
            elif target in norm or norm in target:
                score = 80
            else:
                tokens = [t for t in target.replace(",", " ").split() if len(t) > 3]
                hits = sum(1 for t in tokens if t in norm)
                if hits:
                    score = 40 + hits * 10
            if score > best_score:
                best_score = score
                best_id = int(cluster_id)
        return best_id if best_score >= 40 else None

    def lookup_product(
        self, products: dict[str, OzonProduct], offer_id: str
    ) -> OzonProduct | None:
        product = products.get(offer_id)
        if product is not None:
            return product
        folded = offer_id.casefold()
        for key, value in products.items():
            if key.casefold() == folded:
                return value
        return None

    def create_multi_cluster_draft(
        self,
        rows: Sequence[DistributionRow],
        products: dict[str, OzonProduct],
        *,
        dry_run: bool = True,
    ) -> OzonDraftResult:
        """Один черновик на все города с разбивкой по кластерам."""

        clusters = self.list_clusters() if not dry_run or self.configured else []
        # даже в dry-run полезно проверить маппинг, если ключи есть
        if self.configured and not clusters:
            clusters = self.list_clusters()

        by_cluster: dict[int, dict[int, int]] = {}
        unmatched_cities: set[str] = set()
        total = 0
        errors: list[str] = []

        for row in rows:
            product = self.lookup_product(products, row.vendor_code)
            if product is None or product.sku is None:
                errors.append(f"нет sku: {row.vendor_code}")
                continue
            city_parts = row.by_city
            if not city_parts:
                errors.append(f"нет городов: {row.vendor_code}")
                continue
            for part in city_parts:
                if part.quantity <= 0:
                    continue
                cluster_id = self.match_cluster_id(part.city, clusters)
                if cluster_id is None:
                    unmatched_cities.add(part.city)
                    continue
                bucket = by_cluster.setdefault(cluster_id, {})
                bucket[product.sku] = bucket.get(product.sku, 0) + part.quantity
                total += part.quantity

        if unmatched_cities:
            errors.append(
                "не сопоставлены города: " + ", ".join(sorted(unmatched_cities)[:10])
            )

        if dry_run:
            drop_ready = bool(self._config.ozon_drop_off_warehouse_id)
            return OzonDraftResult(
                draft_id=None,
                total_items=total,
                clusters=len(by_cluster),
                dry_run=True,
                message=(
                    f"Ozon dry-run: {total} шт в {len(by_cluster)} кластерах "
                    "(черновик не создавался"
                    + (
                        "; для live нужен OZON_DROP_OFF_WAREHOUSE_ID"
                        if not drop_ready
                        else ""
                    )
                    + ")."
                ),
                errors=tuple(errors),
            )

        if not by_cluster:
            raise WbLogistError(
                "OZON_DRAFT_EMPTY",
                "Нечего отправлять в черновик Ozon: "
                + ("; ".join(errors[:5]) if errors else "пустая разбивка"),
            )

        drop_off = self._config.ozon_drop_off_warehouse_id
        if not drop_off:
            raise WbLogistError(
                "OZON_NOT_CONFIGURED",
                "Для черновика Ozon нужен OZON_DROP_OFF_WAREHOUSE_ID в .env "
                "(id точки из /v1/warehouse/fbo/list).",
            )

        clusters_info = [
            {
                "macrolocal_cluster_id": cluster_id,
                "items": [
                    {"sku": sku, "quantity": qty}
                    for sku, qty in items.items()
                    if qty > 0
                ],
            }
            for cluster_id, items in by_cluster.items()
        ]
        payload = {
            "clusters_info": clusters_info,
            "deletion_sku_mode": "PARTIAL",
            "delivery_info": {
                "type": "DROPOFF",
                "drop_off_warehouse": {
                    "warehouse_id": drop_off,
                    # Кавказский хаб и подобные — SORTING_CENTER
                    "warehouse_type": "SORTING_CENTER",
                },
            },
        }
        data = self._request(
            MULTI_CLUSTER_DRAFT_PATH,
            payload,
            error_code="OZON_DRAFT_CREATE_FAILED",
        )
        draft_id = data.get("draft_id") if isinstance(data, dict) else None
        api_errors = []
        if isinstance(data, dict):
            for err in data.get("errors") or []:
                if isinstance(err, dict):
                    api_errors.append(str(err.get("message") or err.get("error_message") or err))
        return OzonDraftResult(
            draft_id=str(draft_id) if draft_id is not None else None,
            total_items=total,
            clusters=len(by_cluster),
            dry_run=False,
            message=(
                f"Черновик Ozon создан: {draft_id}"
                if draft_id is not None
                else "Ozon ответил без draft_id"
            ),
            errors=tuple(errors + api_errors),
        )


def resolve_ozon_rows(
    rows: Sequence[DistributionRow],
    products: dict[str, OzonProduct],
) -> tuple[list[ResolvedItem], list[str]]:
    """Собирает ResolvedItem для Excel (сумма qty; баркод/название из Ozon)."""

    items: list[ResolvedItem] = []
    errors: list[str] = []
    folded = {key.casefold(): value for key, value in products.items()}
    for row in rows:
        product = products.get(row.vendor_code) or folded.get(row.vendor_code.casefold())
        if product is None:
            items.append(
                ResolvedItem(
                    vendor_code=row.vendor_code,
                    quantity=row.quantity,
                    barcode="",
                    size="",
                    title=row.vendor_code,
                    brand="",
                    nm_id=None,
                    color=color_from_vendor_code(row.vendor_code),
                )
            )
            continue
        if not product.barcode and product.sku is None:
            errors.append(f"{row.vendor_code}: нет barcode/sku")
        items.append(
            ResolvedItem(
                vendor_code=row.vendor_code,
                quantity=row.quantity,
                barcode=product.barcode or (str(product.sku) if product.sku else ""),
                size="",
                title=product.title or row.vendor_code,
                brand="",
                nm_id=product.product_id,
                color=product.color or color_from_vendor_code(row.vendor_code),
            )
        )
    return items, errors


def quiet_smoke(config: Config) -> OzonSmokeResult:
    """Один раз проверить API; без ключей — короткий статус, без исключений."""

    return OzonClient(config).smoke_test()
