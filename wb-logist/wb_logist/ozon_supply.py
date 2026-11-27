"""Ozon FBO: multi-cluster draft → timeslot → supply/create с auto-skip."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .errors import WbLogistError
from .models import DistributionRow
from .ozon_api import OzonClient, OzonProduct

MSK = timezone(timedelta(hours=3))
UNAVAILABLE_STATES = {
    "NOT_AVAILABLE",
    "WAREHOUSE_SCORING_STATUS_NOT_AVAILABLE",
}
UNAVAILABLE_REASONS = {
    "NOT_AVAILABLE_MATRIX",
    "UNSPECIFIED",  # only together with NOT_AVAILABLE state
}


@dataclass
class OzonAdjustment:
    kind: str  # skipped_city | skipped_sku | reduced_qty
    detail: str
    quantity: int = 0


@dataclass
class OzonSupplyResult:
    draft_id: str | None
    order_id: str | None
    total_items: int
    positions: int
    clusters: int
    dry_run: bool
    message: str
    timeslot: dict[str, str] | None = None
    drop_off_id: int | None = None
    drop_off_name: str = "МСК_КАВКАЗСКИЙ_2_ХАБ"
    order_url: str | None = None
    adjustments: list[OzonAdjustment] = field(default_factory=list)
    errors: tuple[str, ...] = ()


def _pick_evening_slot(slots: dict[str, Any], *, earliest: date) -> dict[str, str] | None:
    drop = (slots.get("result") or {}).get("drop_off_warehouse_timeslots") or {}
    days = drop.get("days") or []
    if isinstance(drop, list):
        days = []
        for block in drop:
            days.extend(block.get("days") or [])
    candidates: list[tuple[int, dict[str, str]]] = []
    for day in days:
        for slot in day.get("timeslots") or []:
            fr = slot.get("from_in_timezone") or ""
            to = slot.get("to_in_timezone") or ""
            try:
                start = datetime.fromisoformat(fr.replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=MSK)
                start_msk = start.astimezone(MSK)
            except ValueError:
                continue
            if start_msk.date() < earliest:
                continue
            score = 0
            if start_msk.hour >= 16:
                score += 50
            elif start_msk.hour >= 12:
                score += 20
            score += start_msk.hour
            # sooner dates preferred among evening-ish
            score += max(0, 30 - (start_msk.date() - earliest).days)
            candidates.append(
                (
                    score,
                    {
                        "from_in_timezone": fr,
                        "to_in_timezone": to,
                        "start_msk": start_msk.isoformat(),
                    },
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: -item[0])
    return candidates[0][1]


def _build_cluster_map(
    client: OzonClient,
    rows: list[DistributionRow],
    products: dict[str, OzonProduct],
    clusters: list[dict[str, Any]],
    *,
    blocked_cluster_ids: set[int],
    blocked_skus: set[int],
    sku_caps: dict[tuple[int, int], int],
) -> tuple[dict[int, dict[int, int]], list[OzonAdjustment], dict[int, str]]:
    """cluster_id -> {sku: qty}; caps apply per (cluster, sku)."""

    by_cluster: dict[int, dict[int, int]] = {}
    adjustments: list[OzonAdjustment] = []
    cluster_names: dict[int, str] = {
        int(c.get("macrolocal_cluster_id") or c.get("id") or -1): str(c.get("name") or "")
        for c in clusters
        if (c.get("macrolocal_cluster_id") or c.get("id")) is not None
    }

    for row in rows:
        product = client.lookup_product(products, row.vendor_code)
        if product is None or product.sku is None:
            adjustments.append(
                OzonAdjustment("skipped_sku", f"нет sku: {row.vendor_code}", 0)
            )
            continue
        if int(product.sku) in blocked_skus:
            continue
        for part in row.by_city:
            if part.quantity <= 0:
                continue
            cluster_id = client.match_cluster_id(part.city, clusters)
            if cluster_id is None:
                adjustments.append(
                    OzonAdjustment(
                        "skipped_city",
                        f"{part.city}: нет кластера ({row.vendor_code})",
                        part.quantity,
                    )
                )
                continue
            if cluster_id in blocked_cluster_ids:
                adjustments.append(
                    OzonAdjustment(
                        "skipped_city",
                        f"{part.city}: недоступен Ozon ({row.vendor_code})",
                        part.quantity,
                    )
                )
                continue
            want = part.quantity
            cap = sku_caps.get((cluster_id, int(product.sku)))
            if cap is not None and cap < want:
                adjustments.append(
                    OzonAdjustment(
                        "reduced_qty",
                        f"{row.vendor_code} @ {part.city}: {want}→{cap}",
                        want - cap,
                    )
                )
                want = cap
            if want <= 0:
                continue
            bucket = by_cluster.setdefault(cluster_id, {})
            bucket[int(product.sku)] = bucket.get(int(product.sku), 0) + want

    return by_cluster, adjustments, cluster_names


def _parse_draft_info_blocks(
    info: dict[str, Any],
) -> tuple[set[int], set[int], dict[tuple[int, int], int], list[str]]:
    """Из /v2/draft/create/info: недоступные кластеры/sku и лимиты qty."""

    blocked_clusters: set[int] = set()
    blocked_skus: set[int] = set()
    caps: dict[tuple[int, int], int] = {}
    notes: list[str] = []

    for cluster in info.get("clusters") or []:
        mid = cluster.get("macrolocal_cluster_id")
        if mid is None:
            continue
        mid = int(mid)
        name = str(cluster.get("cluster_name") or mid)
        for wh in cluster.get("warehouses") or []:
            status = wh.get("availability_status") or {}
            state = str(status.get("state") or "")
            reason = str(status.get("invalid_reason") or "")
            if state in UNAVAILABLE_STATES or (
                "NOT_AVAILABLE" in state and state != "FULL_AVAILABLE"
            ):
                blocked_clusters.add(mid)
                notes.append(f"{name}: {state}/{reason}")
            # partial caps rarely appear here; keep hooks for future fields
            for item in wh.get("items") or []:
                sku = item.get("sku")
                if sku is None:
                    continue
                available = item.get("available_quantity")
                requested = item.get("requested_quantity") or item.get("quantity")
                if available is not None and requested is not None:
                    try:
                        avail_i = int(available)
                        req_i = int(requested)
                    except (TypeError, ValueError):
                        continue
                    if avail_i <= 0:
                        blocked_skus.add(int(sku))
                    elif avail_i < req_i:
                        caps[(mid, int(sku))] = avail_i

    for err in info.get("errors") or []:
        if not isinstance(err, dict):
            continue
        notes.append(str(err.get("message") or err.get("error_message") or err))
        for block in err.get("items_validation") or []:
            mid = block.get("macrolocal_cluster_id")
            for rejected in block.get("rejected_items") or []:
                sku = rejected.get("sku")
                if sku is None:
                    continue
                reasons = ",".join(str(r) for r in (rejected.get("reasons") or []))
                qty = rejected.get("available_quantity")
                if qty is not None and mid is not None:
                    try:
                        caps[(int(mid), int(sku))] = int(qty)
                    except (TypeError, ValueError):
                        blocked_skus.add(int(sku))
                else:
                    blocked_skus.add(int(sku))
                notes.append(f"sku {sku}: {reasons or 'rejected'}")

    return blocked_clusters, blocked_skus, caps, notes


def create_fbo_supply(
    client: OzonClient,
    rows: list[DistributionRow],
    products: dict[str, OzonProduct],
    *,
    dry_run: bool = False,
    earliest_slot: date | None = None,
) -> OzonSupplyResult:
    """Создаёт заявку FBO; недоступное убирает, qty урезает, слот с earliest (вечер)."""

    if not client.configured:
        raise WbLogistError("OZON_NOT_CONFIGURED", "OZON_* не заданы.")

    drop_off = client._config.ozon_drop_off_warehouse_id
    clusters = client.list_clusters()
    blocked_clusters: set[int] = set()
    blocked_skus: set[int] = set()
    sku_caps: dict[tuple[int, int], int] = {}
    all_adjustments: list[OzonAdjustment] = []
    draft_id: int | None = None
    info: dict[str, Any] | None = None

    # до 4 попыток: draft → info → убрать недоступное → снова
    for _attempt in range(4):
        by_cluster, adj, cluster_names = _build_cluster_map(
            client,
            rows,
            products,
            clusters,
            blocked_cluster_ids=blocked_clusters,
            blocked_skus=blocked_skus,
            sku_caps=sku_caps,
        )
        all_adjustments.extend(adj)
        total = sum(sum(items.values()) for items in by_cluster.values())
        if not by_cluster:
            raise WbLogistError(
                "OZON_DRAFT_EMPTY",
                "После отсечения недоступных городов/SKU нечего отправлять.",
            )

        if dry_run or not drop_off:
            return OzonSupplyResult(
                draft_id=None,
                order_id=None,
                total_items=total,
                positions=len({sku for items in by_cluster.values() for sku in items}),
                clusters=len(by_cluster),
                dry_run=True,
                message=(
                    f"Ozon dry-run: {total} шт / {len(by_cluster)} кластеров"
                    + ("" if drop_off else "; нужен OZON_DROP_OFF_WAREHOUSE_ID")
                ),
                drop_off_id=drop_off,
                adjustments=all_adjustments,
            )

        payload = {
            "clusters_info": [
                {
                    "macrolocal_cluster_id": cid,
                    "items": [
                        {"sku": sku, "quantity": qty}
                        for sku, qty in items.items()
                        if qty > 0
                    ],
                }
                for cid, items in by_cluster.items()
            ],
            "deletion_sku_mode": "PARTIAL",
            "delivery_info": {
                "type": "DROPOFF",
                "drop_off_warehouse": {
                    "warehouse_id": drop_off,
                    "warehouse_type": "SORTING_CENTER",
                },
            },
        }
        created = client._request(
            "/v1/draft/multi-cluster/create", payload, error_code="OZON_DRAFT_CREATE_FAILED"
        )
        draft_id = created.get("draft_id")
        if draft_id is None:
            raise WbLogistError("OZON_DRAFT_CREATE_FAILED", "Ozon не вернул draft_id")

        info = None
        for _ in range(30):
            info = client._request(
                "/v2/draft/create/info",
                {"draft_id": draft_id},
                error_code="OZON_DRAFT_INFO_FAILED",
            )
            if info.get("status") in {"SUCCESS", "FAILED"}:
                break
            time.sleep(2)
        if not info or info.get("status") != "SUCCESS":
            raise WbLogistError(
                "OZON_DRAFT_INFO_FAILED",
                f"Черновик {draft_id} не готов: {info.get('status') if info else 'нет ответа'}",
            )

        new_blocked, new_skus, new_caps, notes = _parse_draft_info_blocks(info)
        # только реально недоступные кластеры из scoring
        for cluster in info.get("clusters") or []:
            mid = cluster.get("macrolocal_cluster_id")
            if mid is None:
                continue
            mid = int(mid)
            for wh in cluster.get("warehouses") or []:
                state = str((wh.get("availability_status") or {}).get("state") or "")
                reason = str((wh.get("availability_status") or {}).get("invalid_reason") or "")
                if state in UNAVAILABLE_STATES or (
                    "NOT_AVAILABLE" in state and "FULL_AVAILABLE" not in state
                ):
                    if mid not in blocked_clusters:
                        name = str(cluster.get("cluster_name") or mid)
                        qty = sum(by_cluster.get(mid, {}).values())
                        all_adjustments.append(
                            OzonAdjustment(
                                "skipped_city",
                                f"{name}: {reason or state}",
                                qty,
                            )
                        )
                    blocked_clusters.add(mid)

        for sku in new_skus - blocked_skus:
            all_adjustments.append(
                OzonAdjustment("skipped_sku", f"sku {sku}: отклонён Ozon", 0)
            )
        blocked_skus |= new_skus
        for key, cap in new_caps.items():
            prev = sku_caps.get(key)
            if prev is None or cap < prev:
                sku_caps[key] = cap

        # если всё доступно — выходим на слот
        still_bad = False
        for cluster in info.get("clusters") or []:
            mid = int(cluster.get("macrolocal_cluster_id"))
            for wh in cluster.get("warehouses") or []:
                state = str((wh.get("availability_status") or {}).get("state") or "")
                if state in UNAVAILABLE_STATES or (
                    "NOT_AVAILABLE" in state and "FULL_AVAILABLE" not in state
                ):
                    still_bad = True
        if not still_bad and not new_skus and not new_caps:
            break
        # иначе пересобираем без заблокированного
        continue
    else:
        raise WbLogistError(
            "OZON_DRAFT_CREATE_FAILED",
            "Не удалось получить черновик без недоступных кластеров.",
        )

    assert draft_id is not None and info is not None
    selected = [
        {"macrolocal_cluster_id": int(cluster["macrolocal_cluster_id"])}
        for cluster in info.get("clusters") or []
        if int(cluster["macrolocal_cluster_id"]) not in blocked_clusters
    ]
    # info уже без blocked если пересоздали; берём как есть
    selected = [
        {"macrolocal_cluster_id": int(cluster["macrolocal_cluster_id"])}
        for cluster in info.get("clusters") or []
    ]

    earliest = earliest_slot or (date.today() + timedelta(days=2))
    date_to = earliest + timedelta(days=10)
    slots = client._request(
        "/v2/draft/timeslot/info",
        {
            "date_from": earliest.isoformat(),
            "date_to": date_to.isoformat(),
            "draft_id": draft_id,
            "supply_type": "MULTI_CLUSTER",
            "selected_cluster_warehouses": selected,
        },
        error_code="OZON_TIMESLOT_FAILED",
    )
    chosen = _pick_evening_slot(slots, earliest=earliest)
    if not chosen:
        raise WbLogistError(
            "OZON_TIMESLOT_FAILED",
            f"Нет таймслотов с {earliest.isoformat()} (предпочтительно вечер).",
        )

    create_resp = client._request(
        "/v2/draft/supply/create",
        {
            "draft_id": draft_id,
            "selected_cluster_warehouses": selected,
            "timeslot": {
                "from_in_timezone": chosen["from_in_timezone"],
                "to_in_timezone": chosen["to_in_timezone"],
            },
            "supply_type": "MULTI_CLUSTER",
        },
        error_code="OZON_SUPPLY_CREATE_FAILED",
    )
    if create_resp.get("error_reasons"):
        reasons = [r for r in create_resp["error_reasons"] if r and r != "UNSPECIFIED"]
        if reasons:
            raise WbLogistError(
                "OZON_SUPPLY_CREATE_FAILED",
                "Ozon supply/create: " + ", ".join(reasons),
            )

    status: dict[str, Any] | None = None
    for _ in range(40):
        status = client._request(
            "/v2/draft/supply/create/status",
            {"draft_id": draft_id},
            error_code="OZON_SUPPLY_STATUS_FAILED",
        )
        if status.get("status") in {"SUCCESS", "FAILED"}:
            break
        time.sleep(2)

    if not status or status.get("status") != "SUCCESS":
        raise WbLogistError(
            "OZON_SUPPLY_STATUS_FAILED",
            f"Заявка не создана: {status}",
        )

    order_id = status.get("order_id")
    order_s = str(order_id) if order_id else None
    total_items = sum(
        sum(items.values())
        for items in _build_cluster_map(
            client,
            rows,
            products,
            clusters,
            blocked_cluster_ids=blocked_clusters,
            blocked_skus=blocked_skus,
            sku_caps=sku_caps,
        )[0].values()
    )
    positions = len(
        {
            sku
            for items in _build_cluster_map(
                client,
                rows,
                products,
                clusters,
                blocked_cluster_ids=blocked_clusters,
                blocked_skus=blocked_skus,
                sku_caps=sku_caps,
            )[0].values()
            for sku in items
        }
    )

    return OzonSupplyResult(
        draft_id=str(draft_id),
        order_id=order_s,
        total_items=total_items,
        positions=positions,
        clusters=len(selected),
        dry_run=False,
        message=f"Заявка Ozon создана: {order_s}",
        timeslot=chosen,
        drop_off_id=drop_off,
        order_url=(
            f"https://seller.ozon.ru/app/supply/orders/{order_s}" if order_s else None
        ),
        adjustments=all_adjustments,
    )


def cancel_supply_order(client: OzonClient, order_id: int) -> dict[str, Any]:
    """Отменяет одну заявку order_id; чужие не трогает."""

    created = client._request(
        "/v1/supply-order/cancel",
        {"order_id": int(order_id)},
        error_code="OZON_CANCEL_FAILED",
    )
    operation_id = created.get("operation_id")
    status: dict[str, Any] | None = None
    for _ in range(40):
        if not operation_id:
            break
        status = client._request(
            "/v1/supply-order/cancel/status",
            {"operation_id": operation_id},
            error_code="OZON_CANCEL_STATUS_FAILED",
        )
        if status.get("status") in {"SUCCESS", "ERROR"}:
            break
        time.sleep(2)
    return {
        "order_id": order_id,
        "operation_id": operation_id,
        "cancel_response": created,
        "status_response": status,
        "cancelled": bool(
            status
            and status.get("status") == "SUCCESS"
            and ((status.get("result") or {}).get("is_order_cancelled"))
        ),
    }
