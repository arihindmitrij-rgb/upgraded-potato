"""Оркестратор «считай распред» → compute → sheet → summary."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from datetime import date
from typing import Any

from ..config import Config, load_config
from ..errors import WbLogistError
from .allocate import clamp_shared_ms_pool, compute_needs, ms_base_key, split_wb_ozon
from .config import AutodistConfig, load_autodist_config
from .fetch_moysklad import fetch_ready_sellable, lookup_qty
from .fetch_ozon import load_ozon_sku_stats
from .fetch_wb import load_wb_sku_stats
from .explain import build_run_report, write_last_run
from .ozon_clusters import distribute_ozon_qty
from .sheet_writer import write_ozon_distribution, write_wb_distribution
from .velocity import SkuSignal, build_signal

LOG = logging.getLogger("wb_logist.autodist")

_AUTODIST_RE = re.compile(
    r"^\s*считай\s+распред\b",
    re.IGNORECASE,
)
_ADD_RE = re.compile(
    r"добавь\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def is_autodist_command(text: str) -> bool:
    return bool(_AUTODIST_RE.search(text or ""))


def parse_add_skus(text: str) -> list[str]:
    """«считай распред, добавь art1, art2» → список SKU."""

    match = _ADD_RE.search(text or "")
    if not match:
        return []
    raw = match.group(1)
    parts = re.split(r"[,;\n]+", raw)
    skus = []
    for p in parts:
        code = re.sub(r"\s+", "", p.strip())
        code = code.strip(" .")
        if code:
            skus.append(code)
    return skus


def run_autodist(
    *,
    add_skus: list[str] | None = None,
    dry_run: bool | None = None,
    config: Config | None = None,
    ad: AutodistConfig | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Полный пересчёт. dry_run=True — без записи в Google sheet."""

    cfg = config or load_config()
    adc = ad or load_autodist_config()
    forced = set(add_skus or [])
    do_dry = adc.dry_run if dry_run is None else dry_run

    if not cfg.wb_api_key:
        raise WbLogistError("CONFIG_ERROR", "WB_API_KEY_STANDARD не задан")
    if not cfg.ozon_configured:
        raise WbLogistError("CONFIG_ERROR", "OZON_CLIENT_ID / OZON_API_KEY не заданы")
    if not adc.moysklad_token:
        raise WbLogistError("CONFIG_ERROR", "MOYSKLAD_TOKEN не задан")

    LOG.info(
        "Autodist start: history=%s cover_cap=%s forced=%s dry=%s",
        adc.history_days,
        adc.cover_ceiling_days,
        sorted(forced),
        do_dry,
    )

    wb = load_wb_sku_stats(
        cfg.wb_api_key, history_days=adc.history_days, timeout=cfg.http_timeout
    )
    oz = load_ozon_sku_stats(
        cfg.ozon_client_id or "",
        cfg.ozon_api_key or "",
        history_days=adc.history_days,
        base=cfg.ozon_api_base,
        timeout=cfg.http_timeout,
    )
    ms = fetch_ready_sellable(adc.moysklad_token, timeout=cfg.http_timeout)

    # Объединяем сигналы: WB как основной velocity; stock_mp = WB FBO + Ozon FBO
    vendors = set(wb.keys()) | set(oz.keys()) | forced
    signals: list[SkuSignal] = []
    signals_by_sku: dict[str, SkuSignal] = {}
    for vendor in sorted(vendors):
        w = wb.get(vendor)
        o = oz.get(vendor)
        clean = float(w.clean_orders) if w else 0.0
        # если на WB нет заказов — возьмём Ozon orders как слабый сигнал
        if clean <= 0 and o:
            clean = float(o.orders)
        stock_mp = int(w.stock_fbo if w else 0) + int(o.stock_total if o else 0)
        daily = list(w.daily_orders) if w else None
        buyout = None
        if w and w.orders > 0:
            buyout = w.buyouts / w.orders if w.buyouts else None
        # Новинки без продаж: всё равно сигнал с нулями (forced обработает allocate)
        if clean <= 0 and stock_mp <= 0 and vendor not in {s.casefold() for s in forced} and vendor.casefold() not in {s.casefold() for s in forced}:
            # пропускаем мёртвые SKU без стока и спроса, кроме forced
            if vendor.casefold() not in {x.casefold() for x in forced}:
                continue
        sig = build_signal(
            vendor,
            clean_orders=clean,
            window_days=adc.history_days,
            stock_mp=stock_mp,
            daily_orders=daily,
            buyout_rate=buyout,
            cover_ceiling=adc.cover_ceiling_days,
            season_lo=adc.season_min,
            season_hi=adc.season_max,
        )
        signals.append(sig)
        signals_by_sku[vendor] = sig

    needs = compute_needs(
        signals,
        cover_ceiling=adc.cover_ceiling_days,
        forced_skus=forced,
    )

    # МойСклад: name без ревизии -N; ревизии делят один пул
    from .allocate import AllocationResult

    per_sku_ms = {n.vendor_code: lookup_qty(ms, n.vendor_code) for n in needs}
    for n in needs:
        if n.forced and n.need == 0:
            avail = per_sku_ms.get(n.vendor_code, 0)
            if avail > 0:
                n.need = avail
                n.target_qty = avail
                n.note = (n.note + "; need=MS ready").strip("; ")

    ms_by_base: dict[str, int] = {}
    for n in needs:
        base = ms_base_key(n.vendor_code)
        if base not in ms_by_base:
            ms_by_base[base] = lookup_qty(ms, base) or per_sku_ms.get(n.vendor_code, 0)

    matched = sum(1 for n in needs if per_sku_ms.get(n.vendor_code, 0) > 0)
    if matched == 0:
        LOG.warning("MS не сопоставлен ни с одним SKU — ship=0")
        for n in needs:
            n.ship = 0
            n.cut = n.need
        allocation = AllocationResult(
            items=needs,
            allocatable=0,
            total_need=sum(n.need for n in needs),
            total_ship=0,
            shortfall=sum(n.need for n in needs),
        )
    else:
        clamp_shared_ms_pool(needs, ms_by_base)
        allocation = AllocationResult(
            items=needs,
            allocatable=sum(ms_by_base.values()),
            total_need=sum(n.need for n in needs),
            total_ship=sum(n.ship for n in needs),
            shortfall=max(0, sum(n.cut for n in needs)),
        )

    splits = split_wb_ozon(allocation.items)
    wb_qty = {sku: v["wb"] for sku, v in splits.items() if v["wb"] > 0}
    oz_qty_total = {sku: v["ozon"] for sku, v in splits.items() if v["ozon"] > 0}

    # Ozon по кластерам
    oz_by_city: dict[str, dict[str, int]] = {}
    for sku, total in oz_qty_total.items():
        o = oz.get(sku)
        clusters = list(o.clusters) if o else []
        ships = distribute_ozon_qty(
            total,
            clusters,
            min_cover_days=adc.ozon_min_cover_days,
            min_need=adc.ozon_min_need,
        )
        if not ships:
            # нет разбивки/нужды — одна колонка-дефолт (правка на листе ок)
            oz_by_city[sku] = {"Москва, МО и Дальние регионы": int(total)}
            continue
        oz_by_city[sku] = {s.name: s.qty for s in ships}

    auth_note = ""
    try:
        wb_write = write_wb_distribution(
            cfg, adc, wb_qty, dry_run=do_dry, today=today
        )
        oz_write = write_ozon_distribution(
            cfg, adc, oz_by_city, dry_run=do_dry, today=today
        )
    except WbLogistError as exc:
        if exc.code == "GOOGLE_AUTH_MISSING" and not do_dry:
            LOG.warning("Google auth missing — fallback dry-run: %s", exc)
            auth_note = (
                "⚠️ Sheet не записан: нужен одноразовый Google OAuth "
                "(tools/google_oauth_bootstrap.py). Ниже — расчёт (dry-run)."
            )
            wb_write = write_wb_distribution(
                cfg, adc, wb_qty, dry_run=True, today=today
            )
            oz_write = write_ozon_distribution(
                cfg, adc, oz_by_city, dry_run=True, today=today
            )
            do_dry = True
        else:
            raise

    cut_items = [
        {
            "vendor_code": n.vendor_code,
            "need": n.need,
            "ship": n.ship,
            "cut": n.cut,
            "v_adj": round(n.v_adj, 3),
        }
        for n in allocation.items
        if n.cut > 0
    ]

    summary_lines = []
    if auth_note:
        summary_lines.append(auth_note)
    summary_lines += [
        f"SKU к отгрузке: {sum(1 for n in allocation.items if n.ship > 0)}",
        f"WB шт: {sum(wb_qty.values())} → лист «{wb_write.get('sheet')}»"
        + (" (dry-run)" if wb_write.get("dry_run") else ""),
        f"Ozon шт: {sum(sum(c.values()) for c in oz_by_city.values())} "
        f"→ лист «{oz_write.get('sheet')}»"
        + (" (dry-run)" if oz_write.get("dry_run") else ""),
        f"МойСклад пул/need/ship: {allocation.allocatable}/{allocation.total_need}/{allocation.total_ship}",
    ]
    if cut_items:
        summary_lines.append(f"Урезано из-за MS: {len(cut_items)} SKU")
    if forced:
        summary_lines.append("Добавлены вручную: " + ", ".join(sorted(forced)))

    alloc_meta = {
        "allocatable": allocation.allocatable,
        "total_need": allocation.total_need,
        "total_ship": allocation.total_ship,
        "shortfall": allocation.shortfall,
    }
    explain_report = build_run_report(
        adc=adc,
        items=allocation.items,
        signals=signals_by_sku,
        ms_by_base=ms_by_base,
        splits=splits,
        oz_by_city=oz_by_city,
        oz_stats=oz,
        dry_run=do_dry,
        forced=sorted(forced),
        allocation_meta=alloc_meta,
    )
    try:
        explain_path = write_last_run(explain_report)
        summary_lines.append(f"Пояснения → Hub last_run.json ({explain_report.get('sku_count')} SKU)")
    except OSError as exc:
        LOG.warning("Не удалось записать Hub explain: %s", exc)
        explain_path = None

    return {
        "status": "success",
        "dry_run": do_dry,
        "summary": "\n".join(summary_lines),
        "wb": wb_write,
        "ozon": oz_write,
        "allocation": {
            **alloc_meta,
            "items": [
                {
                    "vendor_code": n.vendor_code,
                    "v_adj": round(n.v_adj, 4),
                    "stock_mp": n.stock_mp,
                    "target_qty": n.target_qty,
                    "need": n.need,
                    "ship": n.ship,
                    "cut": n.cut,
                    "forced": n.forced,
                    "wb": splits.get(n.vendor_code, {}).get("wb", 0),
                    "ozon": splits.get(n.vendor_code, {}).get("ozon", 0),
                    "note": n.note,
                }
                for n in allocation.items
                if n.ship > 0 or n.forced or n.cut > 0
            ],
        },
        "cuts": cut_items[:30],
        "forced": sorted(forced),
        "explain_path": str(explain_path) if explain_path else None,
        "explain": {
            "sku_count": explain_report.get("sku_count"),
            "updatedAt": explain_report.get("updatedAt"),
            "config": explain_report.get("config"),
        },
    }


def format_telegram_summary(payload: dict[str, Any]) -> str:
    import html

    lines = ["📊 <b>Распределение посчитано</b>", ""]
    for line in str(payload.get("summary") or "").splitlines():
        lines.append(html.escape(line))
    items = (payload.get("allocation") or {}).get("items") or []
    top = sorted(items, key=lambda x: -int(x.get("ship") or 0))[:12]
    if top:
        lines.append("")
        lines.append("<b>Топ отгрузки:</b>")
        for it in top:
            lines.append(
                f"• <code>{html.escape(str(it.get('vendor_code')))}</code> — "
                f"{it.get('ship')} шт "
                f"(WB {it.get('wb')}/Ozon {it.get('ozon')}, "
                f"v={it.get('v_adj')})"
            )
    cuts = payload.get("cuts") or []
    if cuts:
        lines.append("")
        lines.append("<b>Урезано (МойСклад):</b>")
        for it in cuts[:8]:
            lines.append(
                f"• <code>{html.escape(str(it.get('vendor_code')))}</code> "
                f"need {it.get('need')} → ship {it.get('ship')}"
            )
    if payload.get("dry_run"):
        lines.append("")
        lines.append("⚠️ Dry-run: Google sheet не менялся.")
    else:
        lines.append("")
        lines.append("Лист обновлён. Дальше: «делай вб» / «делай озон».")
    return "\n".join(lines)
