"""Target → need → урезание по velocity при нехватке МойСклад. Без мин.100/короб."""

from __future__ import annotations

from dataclasses import dataclass, field

from .velocity import SkuSignal, target_cover_days


@dataclass
class SkuNeed:
    vendor_code: str
    v_adj: float
    stock_mp: int
    target_cover_days: float
    target_qty: int
    need: int
    ship: int = 0
    cut: int = 0
    forced: bool = False
    note: str = ""


@dataclass
class AllocationResult:
    items: list[SkuNeed] = field(default_factory=list)
    allocatable: int = 0
    total_need: int = 0
    total_ship: int = 0
    shortfall: int = 0


def compute_needs(
    signals: list[SkuSignal],
    *,
    cover_ceiling: int = 42,
    forced_skus: set[str] | None = None,
    default_forced_cover_days: float = 28.0,
) -> list[SkuNeed]:
    forced = {s.casefold() for s in (forced_skus or set())}
    items: list[SkuNeed] = []
    seen: set[str] = set()

    for sig in signals:
        cover = target_cover_days(
            good_sales_cover=_derived_cover(sig),
            ceiling=cover_ceiling,
        )
        target = int(round(sig.v_adj * cover))
        need = max(0, target - sig.stock_mp)
        items.append(
            SkuNeed(
                vendor_code=sig.vendor_code,
                v_adj=sig.v_adj,
                stock_mp=sig.stock_mp,
                target_cover_days=cover,
                target_qty=target,
                need=need,
            )
        )
        seen.add(sig.vendor_code.casefold())

    # Новинки без истории: добавь → минимальный target = cover * tiny velocity 0,
    # need оставим 0 пока нет MS mapping — pipeline выставит need из MS при желании.
    for code in sorted(forced):
        if code in seen:
            continue
        # Найдём оригинальный регистр
        original = next((s for s in (forced_skus or set()) if s.casefold() == code), code)
        items.append(
            SkuNeed(
                vendor_code=original,
                v_adj=0.0,
                stock_mp=0,
                target_cover_days=default_forced_cover_days,
                target_qty=0,
                need=0,
                forced=True,
                note="добавлен вручную (нет истории продаж)",
            )
        )
    return items


def _derived_cover(sig: SkuSignal) -> float:
    """Целевой cover из «хороших продаж»: при высокой скорости ближе к потолку."""

    # База 28 дней; быстрее SKU → чуть выше cover (до ceiling снаружи)
    if sig.v_adj <= 0:
        return 21.0
    if sig.v_adj >= 5:
        return 35.0
    if sig.v_adj >= 2:
        return 30.0
    return 28.0


def allocate_pool(needs: list[SkuNeed], allocatable: int) -> AllocationResult:
    """Сначала закрываем быстрые SKU (выше v_adj). Forced с need=0 не едят пул."""

    pool = max(0, int(allocatable))
    ordered = sorted(
        needs,
        key=lambda n: (0 if n.forced and n.need == 0 else 1, -n.v_adj, n.vendor_code),
    )
    total_need = sum(n.need for n in ordered)
    remaining = pool

    for item in ordered:
        if item.need <= 0:
            item.ship = 0
            item.cut = 0
            continue
        take = min(item.need, remaining)
        item.ship = take
        item.cut = item.need - take
        remaining -= take

    total_ship = sum(n.ship for n in ordered)
    return AllocationResult(
        items=ordered,
        allocatable=pool,
        total_need=total_need,
        total_ship=total_ship,
        shortfall=max(0, total_need - total_ship),
    )


def split_wb_ozon(
    items: list[SkuNeed],
    *,
    wb_share: float = 0.55,
) -> dict[str, dict[str, int]]:
    """Грубое деление ship на WB/Ozon по SKU.

    Если у SKU нет сигнала с Ozon — всё на WB. Доли правятся на листе.
    """

    out: dict[str, dict[str, int]] = {}
    for item in items:
        if item.ship <= 0:
            continue
        wb_qty = int(round(item.ship * wb_share))
        oz_qty = item.ship - wb_qty
        # При ship=1 и share~0.55 → wb=1, oz=0 — ок для мелких
        if item.ship == 1:
            wb_qty, oz_qty = 1, 0
        out[item.vendor_code] = {"wb": max(0, wb_qty), "ozon": max(0, oz_qty)}
    return out


def ms_base_key(vendor_code: str) -> str:
    import re
    return re.sub(r"-(\d+)$", "", (vendor_code or "").strip())


def clamp_shared_ms_pool(items: list[SkuNeed], ms_qty_by_base: dict[str, int]) -> list[SkuNeed]:
    """Несколько ревизий (sw-y36-pink-3/4) делят один остаток МойСклад."""

    from collections import defaultdict

    groups: dict[str, list[SkuNeed]] = defaultdict(list)
    for item in items:
        groups[ms_base_key(item.vendor_code)].append(item)

    for base, group in groups.items():
        pool = int(ms_qty_by_base.get(base, 0) or ms_qty_by_base.get(base.casefold(), 0) or 0)
        # need уже может быть > pool на каждый SKU — сначала clamp need
        for it in group:
            if pool <= 0:
                it.need = 0
            else:
                it.need = min(it.need, pool)
        # затем allocate внутри группы по velocity
        ordered = sorted(group, key=lambda n: (-n.v_adj, n.vendor_code))
        left = pool
        for it in ordered:
            take = min(it.need, left)
            it.ship = take
            it.cut = max(0, it.need - take)
            left -= take
    return items
