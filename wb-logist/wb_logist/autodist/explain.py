"""Структурированный отчёт «Почему так» → Agent Hub last_run.json."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .allocate import SkuNeed, ms_base_key
from .config import AutodistConfig
from .ozon_clusters import ClusterStock, explain_cluster_decisions
from .velocity import SkuSignal

LOG = logging.getLogger("wb_logist.autodist.explain")

HUB_AGENT_DIR = Path(
    r"C:\Users\User\.cline\data\workspaces\chat\dashboard"
    r"\agent-hub\agents\wb-logist"
)
LAST_RUN_PATH = HUB_AGENT_DIR / "last_run.json"
HUB_STATE_PATH = HUB_AGENT_DIR / "state.json"
CONFIG_HINT = (
    r"C:\Users\User\.cline\data\workspaces\chat\wb-logist\autodist_config.json"
    " (оверрайд) · .env AUTODIST_*"
)


def _days_left(stock_mp: int, v_adj: float) -> float | None:
    if v_adj <= 1e-9:
        return None
    return round(stock_mp / v_adj, 1)


def _reason_ru(
    *,
    sig: SkuSignal | None,
    need: SkuNeed,
    ms_alloc: int,
    wb_qty: int,
    oz_qty: int,
    oz_clusters: list[dict[str, Any]],
    cut_by_velocity: bool,
) -> str:
    parts: list[str] = []
    if need.forced and (sig is None or sig.v_adj <= 1e-9):
        parts.append("новинка без истории (добавлен вручную)")
    if sig is not None:
        parts.append(
            f"скорость {sig.v_adj:.2f} шт/д "
            f"(чистые {sig.clean_orders:.0f} / {sig.days_in_stock:.0f} дн наличия)"
        )
        if abs(sig.season_factor - 1.0) > 1e-6:
            parts.append(f"сезон ×{sig.season_factor:.2f}")
    days = _days_left(need.stock_mp, need.v_adj)
    days_s = "∞" if days is None else f"{days:.0f}"
    parts.append(
        f"запас на МП ~{days_s} д, цель {need.target_cover_days:.0f} д "
        f"→ target {need.target_qty}, сток {need.stock_mp}, need {need.need}"
    )
    if ms_alloc <= 0 and need.need > 0:
        parts.append("МойСклад 0 — отгрузка 0")
    elif cut_by_velocity:
        parts.append(
            f"МойСклад {ms_alloc}, урезано до {need.ship} "
            f"(приоритет быстрых SKU, −{need.cut})"
        )
    else:
        parts.append(f"МойСклад {ms_alloc}, без урезания → ship {need.ship}")
    parts.append(f"WB {wb_qty} (Москва), Ozon {oz_qty}")
    incl = [c for c in oz_clusters if c.get("included") and int(c.get("qty") or 0) > 0]
    excl = [c for c in oz_clusters if not c.get("included")]
    if incl:
        parts.append(
            "кластеры: "
            + ", ".join(f"{c['name']}={c['qty']}" for c in incl[:6])
        )
    if excl and oz_qty > 0:
        parts.append(f"исключено кластеров: {len(excl)}")
    if need.note:
        parts.append(need.note)
    return "; ".join(parts)


def build_sku_explain(
    *,
    need: SkuNeed,
    sig: SkuSignal | None,
    ms_alloc: int,
    wb_qty: int,
    oz_total: int,
    oz_clusters_raw: list[ClusterStock],
    oz_ships: dict[str, int],
    adc: AutodistConfig,
) -> dict[str, Any]:
    decisions = explain_cluster_decisions(
        oz_clusters_raw,
        min_cover_days=adc.ozon_min_cover_days,
        min_need=adc.ozon_min_need,
        target_cover_days=need.target_cover_days or 28.0,
        assigned_qty=oz_ships,
    )
    cut_by_velocity = need.cut > 0
    days = _days_left(need.stock_mp, need.v_adj)
    row = {
        "vendor_code": need.vendor_code,
        "clean_velocity": round(sig.velocity, 4) if sig else 0.0,
        "orders_per_day": round(need.v_adj, 4),
        "days_in_stock": round(sig.days_in_stock, 2) if sig else None,
        "clean_orders": round(sig.clean_orders, 2) if sig else 0.0,
        "season_factor": round(sig.season_factor, 4) if sig else 1.0,
        "days_left_mp": days,
        "target_cover_days": round(need.target_cover_days, 2),
        "cover_ceiling_days": adc.cover_ceiling_days,
        "target_qty": need.target_qty,
        "stock_mp": need.stock_mp,
        "need": need.need,
        "ship": need.ship,
        "cut": need.cut,
        "moysklad_allocatable": ms_alloc,
        "cut_by_velocity_priority": cut_by_velocity,
        "forced": need.forced,
        "wb": {"warehouse": "Москва", "qty": int(wb_qty)},
        "ozon": {
            "total": int(oz_total),
            "clusters": decisions,
        },
        "reason": _reason_ru(
            sig=sig,
            need=need,
            ms_alloc=ms_alloc,
            wb_qty=wb_qty,
            oz_qty=oz_total,
            oz_clusters=decisions,
            cut_by_velocity=cut_by_velocity,
        ),
    }
    return row


def build_run_report(
    *,
    adc: AutodistConfig,
    items: list[SkuNeed],
    signals: Mapping[str, SkuSignal],
    ms_by_base: Mapping[str, int],
    splits: Mapping[str, Mapping[str, int]],
    oz_by_city: Mapping[str, Mapping[str, int]],
    oz_stats: Mapping[str, Any],
    dry_run: bool,
    forced: list[str] | None = None,
    allocation_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    skus: list[dict[str, Any]] = []
    for n in items:
        if not (n.ship > 0 or n.forced or n.cut > 0 or n.need > 0):
            continue
        base = ms_base_key(n.vendor_code)
        ms_alloc = int(ms_by_base.get(base, 0) or 0)
        split = splits.get(n.vendor_code) or {}
        wb_qty = int(split.get("wb") or 0)
        oz_qty = int(split.get("ozon") or 0)
        o = oz_stats.get(n.vendor_code)
        clusters = list(getattr(o, "clusters", None) or [])
        city_map = dict(oz_by_city.get(n.vendor_code) or {})
        skus.append(
            build_sku_explain(
                need=n,
                sig=signals.get(n.vendor_code),
                ms_alloc=ms_alloc,
                wb_qty=wb_qty,
                oz_total=oz_qty,
                oz_clusters_raw=clusters,
                oz_ships=city_map,
                adc=adc,
            )
        )
    skus.sort(key=lambda r: (-int(r.get("ship") or 0), str(r.get("vendor_code"))))
    now = datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "version": 1,
        "view": "explain",
        "updatedAt": now,
        "dry_run": bool(dry_run),
        "forced": list(forced or []),
        "config": {
            "history_days": adc.history_days,
            "cover_ceiling_days": adc.cover_ceiling_days,
            "ozon_min_cover_days": adc.ozon_min_cover_days,
            "ozon_min_need": adc.ozon_min_need,
            "season_min": adc.season_min,
            "season_max": adc.season_max,
            "wb_warehouse_label": adc.wb_warehouse_label,
            "config_path": CONFIG_HINT,
        },
        "allocation": dict(allocation_meta or {}),
        "sku_count": len(skus),
        "skus": skus,
    }


def write_last_run(
    report: dict[str, Any],
    *,
    path: Path | None = None,
    state_path: Path | None = None,
) -> Path:
    """Пишет last_run.json и мягко обновляет state.json (не затирает runs)."""

    target = path or LAST_RUN_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    LOG.info("Explain report → %s (%s SKU)", target, report.get("sku_count"))

    sp = state_path or HUB_STATE_PATH
    existing: dict[str, Any] = {}
    if sp.is_file():
        try:
            existing = json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}

    cfg = report.get("config") or {}
    summary = dict(existing.get("summary") or {})
    summary["explain"] = {
        "updatedAt": report.get("updatedAt"),
        "sku_count": report.get("sku_count"),
        "dry_run": report.get("dry_run"),
        "history_days": cfg.get("history_days"),
        "cover_ceiling_days": cfg.get("cover_ceiling_days"),
        "ozon_min_cover_days": cfg.get("ozon_min_cover_days"),
        "last_run_file": str(target),
    }
    metrics = list(existing.get("metrics") or [])
    # обновим/добавим метрику explain
    found = False
    for m in metrics:
        if m.get("key") == "explain_skus":
            m["value"] = report.get("sku_count") or 0
            m["hint"] = "Почему так · " + str(report.get("updatedAt") or "")[:16]
            found = True
            break
    if not found:
        metrics.append(
            {
                "key": "explain_skus",
                "label": "Пояснения SKU",
                "value": report.get("sku_count") or 0,
                "unit": "",
                "hint": "открой Детали → Почему так",
            }
        )

    dry = report.get("dry_run")
    reco = (
        "Последний распред сохранён. Открой Детали → «Почему так». "
        + ("(dry-run, лист не писался). " if dry else "")
        + f"Тюнинг: {CONFIG_HINT}"
    )
    state = {
        **existing,
        "id": "wb-logist",
        "metrics": metrics,
        "summary": summary,
        "lastRun": {
            "updatedAt": report.get("updatedAt"),
            "sku_count": report.get("sku_count"),
            "dry_run": dry,
            "config": cfg,
            "file": "last_run.json",
        },
        "recommendation": reco,
        "updatedAt": report.get("updatedAt"),
        # view оставляем как был (обычно runs), UI сам переключает вкладку
        "view": existing.get("view") or "runs",
    }
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    events = sp.parent / "events.jsonl"
    try:
        line = json.dumps(
            {
                "ts": report.get("updatedAt"),
                "level": "ok",
                "text": (
                    f"Почему так: {report.get('sku_count')} SKU"
                    + (" [dry]" if dry else "")
                ),
            },
            ensure_ascii=False,
        )
        with events.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        LOG.warning("events.jsonl append failed: %s", exc)

    try:
        from .hub_sync import sync_last_run_to_vps

        sync_last_run_to_vps(target)
    except Exception as exc:  # noqa: BLE001 — sync must not break autodist
        LOG.warning("VPS Hub explain sync skipped: %s", exc)

    return target
