"""Ozon: остатки/спрос по кластерам (грубо для v1)."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..errors import WbLogistError
from .ozon_clusters import ClusterStock

LOG = logging.getLogger("wb_logist.autodist.ozon")

STOCK_ON_WH = "/v2/analytics/stock_on_warehouses"
ANALYTICS_DATA = "/v1/analytics/data"
PRODUCT_INFO_LIST = "/v3/product/info/list"


@dataclass
class OzonSkuStats:
    offer_id: str
    stock_total: int = 0
    orders: float = 0.0
    clusters: list[ClusterStock] = field(default_factory=list)


def _headers(client_id: str, api_key: str) -> dict[str, str]:
    return {
        "Client-Id": str(client_id),
        "Api-Key": str(api_key),
        "Content-Type": "application/json",
    }


def _post(
    base: str,
    path: str,
    client_id: str,
    api_key: str,
    body: dict[str, Any],
    timeout: float,
) -> Any:
    url = base.rstrip("/") + path
    try:
        resp = requests.post(
            url, headers=_headers(client_id, api_key), json=body, timeout=timeout
        )
    except requests.RequestException as exc:
        raise WbLogistError("OZON_REQUEST_FAILED", str(exc)) from exc
    if resp.status_code >= 400:
        raise WbLogistError(
            "OZON_API_ERROR",
            f"HTTP {resp.status_code} {path}: {resp.text[:400]}",
        )
    return resp.json()


def fetch_stocks_by_warehouse(
    client_id: str,
    api_key: str,
    *,
    base: str = "https://api-seller.ozon.ru",
    timeout: float = 60.0,
) -> dict[str, OzonSkuStats]:
    """Остатки по складам/кластерам → группировка по offer_id."""

    # API пагинирует; limit до 1000
    offset = 0
    by_offer: dict[str, OzonSkuStats] = {}
    wh_qty: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    while True:
        body = {"limit": 1000, "offset": offset, "warehouse_type": "ALL"}
        try:
            data = _post(base, STOCK_ON_WH, client_id, api_key, body, timeout)
        except WbLogistError as exc:
            # старые кабинеты — другой контракт; пробуем без warehouse_type
            if offset == 0:
                LOG.warning("stock_on_warehouses retry: %s", exc)
                data = _post(
                    base,
                    STOCK_ON_WH,
                    client_id,
                    api_key,
                    {"limit": 1000, "offset": 0},
                    timeout,
                )
            else:
                raise
        rows = data.get("rows") or data.get("result") or []
        if isinstance(rows, dict):
            rows = rows.get("rows") or []
        if not rows:
            break
        for row in rows:
            offer = str(
                row.get("item_code")
                or row.get("offer_id")
                or row.get("sku")
                or ""
            ).strip()
            if not offer:
                continue
            wh = str(
                row.get("warehouse_name")
                or row.get("cluster_name")
                or row.get("warehouse")
                or "unknown"
            )
            free = int(
                row.get("free_to_sell_amount")
                or row.get("free_to_sell")
                or row.get("present")
                or row.get("stock")
                or 0
            )
            wh_qty[offer][wh] += max(0, free)
        if len(rows) < 1000:
            break
        offset += 1000
        if offset > 20000:
            break

    for offer, clusters in wh_qty.items():
        total = sum(clusters.values())
        by_offer[offer] = OzonSkuStats(
            offer_id=offer,
            stock_total=total,
            clusters=[
                ClusterStock(name=name, stock=qty) for name, qty in clusters.items()
            ],
        )
    LOG.info("Ozon stocks: %s offer_id", len(by_offer))
    return by_offer


def fetch_orders_approx(
    client_id: str,
    api_key: str,
    *,
    history_days: int = 35,
    base: str = "https://api-seller.ozon.ru",
    timeout: float = 60.0,
) -> dict[str, float]:
    """Грубый спрос по offer_id через analytics/data (ordered_units)."""

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=history_days)
    body = {
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": end.strftime("%Y-%m-%d"),
        "metrics": ["ordered_units"],
        "dimension": ["sku"],
        "limit": 1000,
        "offset": 0,
    }
    try:
        data = _post(base, ANALYTICS_DATA, client_id, api_key, body, timeout)
    except WbLogistError as exc:
        LOG.warning("Ozon analytics недоступна: %s", exc)
        return {}

    result = data.get("result") or data
    rows = result.get("data") or []
    out: dict[str, float] = {}
    for row in rows:
        dims = row.get("dimensions") or []
        metrics = row.get("metrics") or []
        offer = ""
        for d in dims:
            if str(d.get("id") or "").lower() in {"sku", "offer_id"}:
                offer = str(d.get("name") or d.get("id") or "").strip()
            elif not offer:
                offer = str(d.get("name") or "").strip()
        if not offer:
            continue
        qty = float(metrics[0]) if metrics else 0.0
        out[offer] = out.get(offer, 0.0) + qty
    LOG.info("Ozon orders approx: %s SKU", len(out))
    return out


def load_ozon_sku_stats(
    client_id: str,
    api_key: str,
    *,
    history_days: int = 35,
    base: str = "https://api-seller.ozon.ru",
    timeout: float = 60.0,
) -> dict[str, OzonSkuStats]:
    stocks = fetch_stocks_by_warehouse(
        client_id, api_key, base=base, timeout=timeout
    )
    orders = fetch_orders_approx(
        client_id,
        api_key,
        history_days=history_days,
        base=base,
        timeout=timeout,
    )
    # Размазать глобальный velocity по кластерам пропорционально стоку (грубо)
    for offer, stats in stocks.items():
        stats.orders = float(orders.get(offer, 0.0))
        v_day = stats.orders / max(1.0, float(history_days))
        total_stock = max(1, stats.stock_total)
        enriched: list[ClusterStock] = []
        for c in stats.clusters:
            share = c.stock / total_stock if stats.stock_total else 1.0 / max(1, len(stats.clusters))
            # Если сток 0 везде — равномерно
            if stats.stock_total == 0 and stats.clusters:
                share = 1.0 / len(stats.clusters)
            enriched.append(
                ClusterStock(name=c.name, stock=c.stock, velocity=v_day * share)
            )
        stats.clusters = enriched

    for offer, qty in orders.items():
        if offer not in stocks:
            stocks[offer] = OzonSkuStats(offer_id=offer, orders=float(qty))
    return stocks
