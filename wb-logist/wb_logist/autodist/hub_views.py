"""Agent Hub: скорости продаж + дни запаса (read-only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import load_config
from ..heartbeat import heartbeat_status
from .config import load_autodist_config
from .fetch_ozon import load_ozon_sku_stats
from .fetch_wb import load_wb_sku_stats
from .velocity import build_signal, target_cover_days


HUB_STATE = Path(
    r"C:\Users\User\.cline\data\workspaces\chat\dashboard"
    r"\agent-hub\agents\wb-logist\state.json"
)


def compute_velocity_view(*, limit: int = 40) -> dict[str, Any]:
    cfg = load_config()
    adc = load_autodist_config()
    wb = load_wb_sku_stats(
        cfg.wb_api_key or "", history_days=adc.history_days, timeout=cfg.http_timeout
    )
    oz = {}
    if cfg.ozon_configured:
        oz = load_ozon_sku_stats(
            cfg.ozon_client_id or "",
            cfg.ozon_api_key or "",
            history_days=adc.history_days,
            base=cfg.ozon_api_base,
            timeout=cfg.http_timeout,
        )

    rows = []
    for vendor, w in wb.items():
        o = oz.get(vendor)
        stock = int(w.stock_fbo) + int(o.stock_total if o else 0)
        sig = build_signal(
            vendor,
            clean_orders=w.clean_orders,
            window_days=adc.history_days,
            stock_mp=stock,
            daily_orders=w.daily_orders,
            cover_ceiling=adc.cover_ceiling_days,
        )
        days_left = None
        if sig.v_adj > 1e-9:
            days_left = round(stock / sig.v_adj, 1)
        rows.append(
            {
                "vendor_code": vendor,
                "velocity": round(sig.v_adj, 3),
                "stock_mp": stock,
                "days_left": days_left,
                "clean_orders": round(sig.clean_orders, 1),
            }
        )
    rows.sort(key=lambda r: -(r["velocity"] or 0))
    return {
        "view": "velocity",
        "updatedAt": datetime.now(timezone.utc).astimezone().isoformat(),
        "rows": rows[:limit],
        "worker": heartbeat_status(),
    }


def write_hub_velocity_state(path: Path | None = None) -> Path:
    """Обновляет state.json метриками velocity (не трогает offline overlay)."""

    target = path or HUB_STATE
    view = compute_velocity_view()
    worker = view["worker"]
    existing: dict[str, Any] = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    status = "idle"
    status_text = "Готов"
    reco = "Скорости обновлены. «считай распред» в Telegram — пересчёт листа."
    if not worker.get("ok"):
        status = "down"
        status_text = "Воркер не работает"
        reco = "Telegram-воркер offline. Проверь задачу «WB Logist Bot» / Start Menu."

    top = view["rows"][:6]
    metrics = [
        {
            "key": "skus",
            "label": "SKU в отчёте",
            "value": len(view["rows"]),
            "unit": "",
        },
        {
            "key": "worker",
            "label": "Воркер",
            "value": 1 if worker.get("ok") else 0,
            "unit": "",
            "hint": worker.get("message"),
        },
    ]
    if top:
        metrics.append(
            {
                "key": "top_v",
                "label": "Топ скорость",
                "value": top[0]["velocity"],
                "unit": "шт/д",
                "hint": top[0]["vendor_code"],
            }
        )

    state = {
        **existing,
        "id": "wb-logist",
        "status": status,
        "statusText": status_text,
        "metrics": metrics,
        "summary": {
            "velocity": view["rows"][:20],
            "worker": worker,
        },
        "recommendation": reco,
        "updatedAt": view["updatedAt"],
        "view": "velocity",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
