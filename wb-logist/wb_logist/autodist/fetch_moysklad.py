"""МойСклад: только ready sellable (свободный остаток к отгрузке)."""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

from ..errors import WbLogistError

LOG = logging.getLogger("wb_logist.autodist.moysklad")
BASE = "https://api.moysklad.ru/api/remap/1.2"
_REV_SUFFIX = re.compile(r"-(\d+)$")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
    }


def _get(path: str, token: str, *, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
    url = BASE.rstrip("/") + "/" + path.lstrip("/")
    try:
        resp = requests.get(url, headers=_headers(token), params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        raise WbLogistError("MS_REQUEST_FAILED", str(exc)) from exc
    if resp.status_code >= 400:
        raise WbLogistError(
            "MS_API_ERROR",
            f"HTTP {resp.status_code}: {resp.text[:400]}",
        )
    return resp.json()


def fetch_ready_sellable(
    token: str,
    *,
    timeout: float = 60.0,
) -> dict[str, int]:
    """Ключи: name (артикул продавца), article (nmId), code.

    В МойСклад готовые часы лежат в 
ame как sw-y36-pink (без -4 суффикса WB).
    quantity — свободно к отгрузке.
    """

    out: dict[str, int] = {}
    offset = 0
    limit = 1000
    while True:
        data = _get(
            "report/stock/all",
            token,
            params={"limit": limit, "offset": offset},
            timeout=timeout,
        )
        rows = data.get("rows") or []
        if not rows:
            break
        for row in rows:
            qty = int(float(row.get("quantity") or 0))
            if qty <= 0:
                continue
            for key in ("name", "article", "code", "externalCode"):
                val = str(row.get(key) or "").strip()
                if not val:
                    continue
                # не суммируем дубли ключей с разных полей один row — max
                out[val] = max(out.get(val, 0), qty)
                out[val.casefold()] = max(out.get(val.casefold(), 0), qty)
        meta = data.get("meta") or {}
        size = int(meta.get("size") or 0)
        offset += limit
        if offset >= size or len(rows) < limit:
            break
        if offset > 100000:
            break
    LOG.info("МойСклад ready sellable: %s ключей", len(out))
    return out


def _candidates(vendor_code: str) -> list[str]:
    code = (vendor_code or "").strip()
    if not code:
        return []
    out = [code, code.casefold()]
    # sw-y36-pink-4 → sw-y36-pink
    stripped = _REV_SUFFIX.sub("", code)
    if stripped and stripped != code:
        out.extend([stripped, stripped.casefold()])
    return out


def lookup_qty(stock: dict[str, int], vendor_code: str) -> int:
    for key in _candidates(vendor_code):
        if key in stock:
            return int(stock[key])
    return 0
