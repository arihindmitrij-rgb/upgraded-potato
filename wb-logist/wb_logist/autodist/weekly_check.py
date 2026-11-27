"""Read-only weekly check для n8n (пн 09:00): velocity / дни запаса / алерты."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from ..heartbeat import heartbeat_status
from .hub_views import compute_velocity_view


def run_weekly_check(
    *,
    low_cover_days: float = 10.0,
    high_cover_days: float = 42.0,
) -> dict[str, Any]:
    """Не пишет Google sheet. Печатает JSON в stdout для n8n HTTP/Exec."""

    view = compute_velocity_view(limit=200)
    low = []
    high = []
    for row in view["rows"]:
        days = row.get("days_left")
        if days is None:
            if row.get("stock_mp", 0) <= 0 and row.get("velocity", 0) > 0:
                low.append(row)
            continue
        if days < low_cover_days:
            low.append(row)
        if days > high_cover_days:
            high.append(row)

    worker = heartbeat_status()
    payload = {
        "status": "success",
        "kind": "weekly_check",
        "ts": datetime.now(timezone.utc).astimezone().isoformat(),
        "worker": worker,
        "low_cover": low[:40],
        "high_cover": high[:40],
        "counts": {
            "sku": len(view["rows"]),
            "low": len(low),
            "high": len(high),
        },
        "telegram_text": _format_alert(worker, low, high, low_cover_days, high_cover_days),
    }
    return payload


def _format_alert(
    worker: dict[str, Any],
    low: list[dict[str, Any]],
    high: list[dict[str, Any]],
    low_days: float,
    high_days: float,
) -> str:
    lines = ["🗓️ Weekly check автораспределения"]
    if worker.get("ok"):
        lines.append("🟢 Воркер на связи.")
    elif worker.get("age_sec") is None:
        lines.append("⚪ Heartbeat воркера недоступен на этой машине (бот на User-PC).")
    else:
        lines.append("🔴 Воркер не работает — heartbeat протух.")
    lines.append(f"Ниже {low_days:.0f}д запаса: {len(low)}")
    for r in low[:8]:
        lines.append(
            f"• {r['vendor_code']}: {r.get('days_left')}д (v={r.get('velocity')}, stock={r.get('stock_mp')})"
        )
    lines.append(f"Выше {high_days:.0f}д: {len(high)}")
    for r in high[:5]:
        lines.append(
            f"• {r['vendor_code']}: {r.get('days_left')}д"
        )
    lines.append("Sheet не трогал (read-only).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    payload = run_weekly_check()
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
