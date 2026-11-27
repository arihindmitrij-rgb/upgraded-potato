"""Create Ozon MULTI_CLUSTER supply; skip unavailable Krasnodar matrix."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wb_logist.config import load_config
from wb_logist.ozon_api import OzonClient
from wb_logist.sheets import load_distribution

MSK = timezone(timedelta(hours=3))
SKIP_CITY_SUBSTR = ("краснодар",)


def pick_evening_slot(slots: dict) -> dict | None:
    drop = (slots.get("result") or {}).get("drop_off_warehouse_timeslots") or {}
    days = drop.get("days") or []
    if isinstance(drop, list):
        days = []
        for block in drop:
            days.extend(block.get("days") or [])
    candidates: list[tuple[int, dict]] = []
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
            score = 0
            if start_msk.date().isoformat() >= "2026-11-28":
                score += 100
            if start_msk.hour >= 16:
                score += 50
            elif start_msk.hour >= 12:
                score += 20
            score += start_msk.hour
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


def main() -> None:
    cfg = load_config()
    client = OzonClient(cfg)
    drop = int(cfg.ozon_drop_off_warehouse_id or 0)
    dist = load_distribution(cfg, marketplace="ozon", auto_freshest=True)
    products, missing = client.resolve_products([r.vendor_code for r in dist.rows])
    clusters = client.list_clusters()

    by_cluster: dict[int, dict[int, int]] = {}
    skipped_qty = 0
    skipped_cities: set[str] = set()
    for row in dist.rows:
        product = client.lookup_product(products, row.vendor_code)
        if product is None or product.sku is None:
            continue
        for part in row.by_city:
            if part.quantity <= 0:
                continue
            city_fold = part.city.casefold().replace("ё", "е")
            if any(s in city_fold for s in SKIP_CITY_SUBSTR):
                skipped_qty += part.quantity
                skipped_cities.add(part.city)
                continue
            cid = client.match_cluster_id(part.city, clusters)
            if cid is None:
                continue
            bucket = by_cluster.setdefault(cid, {})
            bucket[product.sku] = bucket.get(product.sku, 0) + part.quantity

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
                "warehouse_id": drop,
                "warehouse_type": "SORTING_CENTER",
            },
        },
    }
    print(
        "PREP",
        json.dumps(
            {
                "clusters": len(by_cluster),
                "skipped_cities": sorted(skipped_cities),
                "skipped_qty": skipped_qty,
                "missing_products": missing[:10],
            },
            ensure_ascii=False,
        ),
    )

    created = client._request(
        "/v1/draft/multi-cluster/create", payload, error_code="DRAFT_CREATE"
    )
    draft_id = created.get("draft_id")
    print("DRAFT", draft_id, "errors", created.get("errors"))
    if not draft_id:
        raise SystemExit("no draft_id")

    info = None
    for _ in range(30):
        info = client._request(
            "/v2/draft/create/info", {"draft_id": draft_id}, error_code="DRAFT_INFO"
        )
        print("DRAFT_STATUS", info.get("status"))
        if info.get("status") in {"SUCCESS", "FAILED"}:
            break
        time.sleep(2)
    if not info or info.get("status") != "SUCCESS":
        raise SystemExit(json.dumps(info, ensure_ascii=False)[:2000])

    for cluster in info.get("clusters") or []:
        state = (
            ((cluster.get("warehouses") or [{}])[0].get("availability_status") or {}).get(
                "state"
            )
        )
        print("CLUSTER", cluster.get("cluster_name"), state)

    selected = [
        {"macrolocal_cluster_id": int(cluster["macrolocal_cluster_id"])}
        for cluster in info.get("clusters") or []
    ]
    slots = client._request(
        "/v2/draft/timeslot/info",
        {
            "date_from": "2026-11-28",
            "date_to": "2026-12-05",
            "draft_id": draft_id,
            "supply_type": "MULTI_CLUSTER",
            "selected_cluster_warehouses": selected,
        },
        error_code="TIMESLOT",
    )
    print("TIMESLOT_META", slots.get("error_reason"))
    print("TIMESLOT_RAW", json.dumps(slots, ensure_ascii=False)[:2500])
    chosen = pick_evening_slot(slots)
    if not chosen:
        raise SystemExit("no evening timeslot")
    print("CHOSEN", chosen)

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
        error_code="SUPPLY_CREATE",
    )
    print("CREATE", json.dumps(create_resp, ensure_ascii=False))

    status = None
    for _ in range(40):
        status = client._request(
            "/v2/draft/supply/create/status",
            {"draft_id": draft_id},
            error_code="SUPPLY_STATUS",
        )
        print("STATUS", json.dumps(status, ensure_ascii=False)[:1000])
        if status.get("status") in {"SUCCESS", "FAILED"}:
            break
        time.sleep(2)

    order_id = (status or {}).get("order_id")
    out = {
        "draft_id": draft_id,
        "previous_draft_id": 141216705,
        "drop_off": {"id": drop, "name": "МСК_КАВКАЗСКИЙ_2_ХАБ", "type": "SORTING_CENTER"},
        "skipped_krasnodar": {
            "cities": sorted(skipped_cities),
            "qty": skipped_qty,
            "reason": "NOT_AVAILABLE_MATRIX",
        },
        "timeslot": chosen,
        "selected_cluster_warehouses": selected,
        "create_response": create_resp,
        "status_response": status,
        "order_id": order_id,
        "status": (status or {}).get("status"),
        "error_reasons": (status or {}).get("error_reasons"),
        "ui_path": "seller.ozon.ru → FBO → Заявки на поставку",
        "possible_ui_url": (
            f"https://seller.ozon.ru/app/supply/orders/{order_id}"
            if order_id
            else None
        ),
    }
    path = ROOT / "output" / f"ozon_supply_{draft_id}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SAVED", path)
    print(
        "SUMMARY",
        json.dumps(
            {
                "draft_id": draft_id,
                "order_id": order_id,
                "status": out["status"],
                "timeslot_msk": chosen.get("start_msk"),
                "skipped_krasnodar_qty": skipped_qty,
                "error_reasons": out["error_reasons"],
                "possible_ui_url": out["possible_ui_url"],
            },
            ensure_ascii=False,
        ),
    )


if __name__ == "__main__":
    main()
