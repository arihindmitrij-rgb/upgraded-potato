"""WB: заказы/отмены + FBO остатки (in-transit считаем уже на МП)."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from ..errors import WbLogistError

LOG = logging.getLogger("wb_logist.autodist.wb")

def _plausible_vendor(code: str) -> bool:
    """Отсекаем названия товаров, попавшие вместо артикула."""
    text = (code or "").strip()
    if not text or len(text) > 64:
        return False
    if " " in text or "\t" in text:
        return False
    # кириллица в артикуле редка; если >30%% букв кириллицы — это title
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    if cyr >= max(3, int(len(text) * 0.3)):
        return False
    return True



STATS_ORDERS = "https://statistics-api.wildberries.ru/api/v1/supplier/orders"
STATS_STOCKS = "https://statistics-api.wildberries.ru/api/v1/supplier/stocks"
ANALYTICS_FUNNEL = (
    "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
)


@dataclass
class WbSkuStats:
    vendor_code: str
    nm_id: int | None = None
    orders: float = 0.0
    cancels: float = 0.0
    buyouts: float = 0.0
    stock_fbo: int = 0
    daily_orders: list[float] = field(default_factory=list)

    @property
    def clean_orders(self) -> float:
        return max(0.0, self.orders - self.cancels)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": token, "Content-Type": "application/json"}


def _get_json(url: str, token: str, params: dict[str, Any], timeout: float) -> Any:
    try:
        resp = requests.get(
            url, headers=_auth_headers(token), params=params, timeout=timeout
        )
    except requests.RequestException as exc:
        raise WbLogistError("WB_REQUEST_FAILED", str(exc)) from exc
    if resp.status_code == 429:
        time.sleep(60)
        return _get_json(url, token, params, timeout)
    if resp.status_code >= 400:
        raise WbLogistError(
            "WB_API_ERROR",
            f"HTTP {resp.status_code}: {resp.text[:400]}",
        )
    if not resp.content:
        return []
    return resp.json()


def _post_json(url: str, token: str, body: dict[str, Any], timeout: float) -> Any:
    try:
        resp = requests.post(
            url, headers=_auth_headers(token), json=body, timeout=timeout
        )
    except requests.RequestException as exc:
        raise WbLogistError("WB_REQUEST_FAILED", str(exc)) from exc
    if resp.status_code == 429:
        time.sleep(60)
        return _post_json(url, token, body, timeout)
    if resp.status_code >= 400:
        raise WbLogistError(
            "WB_API_ERROR",
            f"HTTP {resp.status_code}: {resp.text[:400]}",
        )
    return resp.json()


def fetch_orders_stats(
    token: str,
    *,
    history_days: int = 35,
    timeout: float = 60.0,
) -> dict[str, WbSkuStats]:
    """Чистые заказы по supplierArticle за окно (statistics API)."""

    date_from = (
        datetime.now(timezone.utc) - timedelta(days=history_days)
    ).strftime("%Y-%m-%dT00:00:00")
    rows = _get_json(
        STATS_ORDERS, token, {"dateFrom": date_from, "flag": 0}, timeout
    )
    if not isinstance(rows, list):
        rows = []

    by_sku: dict[str, WbSkuStats] = {}
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    window_start = (date.today() - timedelta(days=history_days - 1))

    for row in rows:
        vendor = str(row.get("supplierArticle") or "").strip()
        if not vendor or not _plausible_vendor(vendor):
            continue
        item = by_sku.get(vendor)
        if item is None:
            item = WbSkuStats(vendor_code=vendor, nm_id=row.get("nmId"))
            by_sku[vendor] = item
        # isCancel — отмена
        if row.get("isCancel"):
            item.cancels += 1
        else:
            item.orders += 1
        day_raw = str(row.get("date") or row.get("lastChangeDate") or "")[:10]
        try:
            day = date.fromisoformat(day_raw)
        except ValueError:
            continue
        if day < window_start:
            continue
        if not row.get("isCancel"):
            daily[vendor][day.isoformat()] += 1.0

    for vendor, item in by_sku.items():
        series = []
        for i in range(history_days):
            d = (window_start + timedelta(days=i)).isoformat()
            series.append(float(daily[vendor].get(d, 0.0)))
        item.daily_orders = series
    LOG.info("WB orders: %s SKU за %s дн", len(by_sku), history_days)
    return by_sku


def fetch_stocks_stats(
    token: str,
    *,
    timeout: float = 60.0,
) -> dict[str, int]:
    """Текущие FBO остатки (+ in-transit уже в quantity на складах WB)."""

    date_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    rows = _get_json(STATS_STOCKS, token, {"dateFrom": date_from}, timeout)
    if not isinstance(rows, list):
        rows = []
    stocks: dict[str, int] = defaultdict(int)
    for row in rows:
        vendor = str(row.get("supplierArticle") or "").strip()
        if not vendor or not _plausible_vendor(vendor):
            continue
        qty = int(row.get("quantity") or 0)
        # inTransit / quantityFull — считаем уже на МП: берём max(quantity, quantityFull)
        full = int(row.get("quantityFull") or qty)
        stocks[vendor] += max(qty, full)
    LOG.info("WB stocks: %s SKU", len(stocks))
    return dict(stocks)


def fetch_funnel_fallback(
    token: str,
    *,
    history_days: int = 35,
    timeout: float = 90.0,
) -> dict[str, WbSkuStats]:
    """Запасной путь: analytics funnel (если statistics недоступна)."""

    end = date.today()
    start = end - timedelta(days=history_days - 1)
    body = {
        "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
        "limit": 1000,
        "offset": 0,
        "orderBy": {"field": "orderSum", "mode": "desc"},
    }
    data = _post_json(ANALYTICS_FUNNEL, token, body, timeout)
    products = (data.get("data") or {}).get("products") or []
    out: dict[str, WbSkuStats] = {}
    for p in products:
        prod = p.get("product") or {}
        stat = (p.get("statistic") or {}).get("selected") or {}
        vendor = str(prod.get("vendorCode") or "").strip()
        if not vendor or not _plausible_vendor(vendor):
            continue
        stocks = prod.get("stocks") or {}
        out[vendor] = WbSkuStats(
            vendor_code=vendor,
            nm_id=prod.get("nmId"),
            orders=float(stat.get("orderCount") or 0),
            cancels=float(stat.get("cancelCount") or 0),
            buyouts=float(stat.get("buyoutCount") or 0),
            stock_fbo=int(stocks.get("wb") or 0),
        )
    LOG.info("WB funnel fallback: %s SKU", len(out))
    return out


def load_wb_sku_stats(
    token: str,
    *,
    history_days: int = 35,
    timeout: float = 60.0,
) -> dict[str, WbSkuStats]:
    """Основной вход: analytics funnel (statistics API deprecated)."""

    try:
        return fetch_funnel_fallback(
            token, history_days=history_days, timeout=timeout
        )
    except WbLogistError as exc:
        LOG.warning("WB funnel недоступен (%s), пробуем statistics", exc)
        by_sku = fetch_orders_stats(
            token, history_days=history_days, timeout=timeout
        )
        stocks = fetch_stocks_stats(token, timeout=timeout)
        for vendor, qty in stocks.items():
            if vendor not in by_sku:
                by_sku[vendor] = WbSkuStats(vendor_code=vendor)
            by_sku[vendor].stock_fbo = qty
        return by_sku
