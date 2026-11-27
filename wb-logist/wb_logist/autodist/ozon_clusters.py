"""Ozon: только кластеры с явной нехваткой; слабые не добиваем."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ClusterStock:
    name: str
    stock: int
    velocity: float = 0.0  # шт/день локально, если есть


@dataclass
class ClusterShip:
    name: str
    qty: int
    cover_days: float | None
    reason: str


def cluster_exclude_reason(
    cluster: ClusterStock,
    *,
    min_cover_days: float = 14.0,
    min_need: int = 1,
    target_cover_days: float = 28.0,
) -> str | None:
    """None если кластер нужен; иначе русская причина исключения."""

    v = max(0.0, float(cluster.velocity))
    stock = max(0, int(cluster.stock))
    if v <= 1e-9:
        return "исключён: нет локального спроса"
    cover = stock / v
    if cover >= min_cover_days:
        return f"исключён: запас ~{cover:.0f} д ≥ порога {min_cover_days:.0f} д"
    target = int(round(v * target_cover_days))
    need = max(0, target - stock)
    if need < min_need:
        return f"исключён: need {need} < мин. {min_need}"
    return None


def cluster_needed(
    cluster: ClusterStock,
    *,
    min_cover_days: float = 14.0,
    min_need: int = 1,
    target_cover_days: float = 28.0,
) -> int:
    """Сколько нужно довезти в кластер; 0 если не «явно нужен»."""

    if (
        cluster_exclude_reason(
            cluster,
            min_cover_days=min_cover_days,
            min_need=min_need,
            target_cover_days=target_cover_days,
        )
        is not None
    ):
        return 0
    v = max(0.0, float(cluster.velocity))
    stock = max(0, int(cluster.stock))
    target = int(round(v * target_cover_days))
    return max(0, target - stock)


def explain_cluster_decisions(
    clusters: list[ClusterStock],
    *,
    min_cover_days: float = 14.0,
    min_need: int = 1,
    target_cover_days: float = 28.0,
    assigned_qty: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Включённые и исключённые кластеры с qty/reason для Hub."""

    assigned = assigned_qty or {}
    out: list[dict[str, Any]] = []
    for c in clusters:
        excl = cluster_exclude_reason(
            c,
            min_cover_days=min_cover_days,
            min_need=min_need,
            target_cover_days=target_cover_days,
        )
        cover = (c.stock / c.velocity) if c.velocity > 0 else None
        qty = int(assigned.get(c.name) or 0)
        if excl is None:
            n = cluster_needed(
                c,
                min_cover_days=min_cover_days,
                min_need=min_need,
                target_cover_days=target_cover_days,
            )
            reason = f"включён: cover<{min_cover_days:.0f} д, need={n}"
            if qty > 0:
                reason += f", qty={qty}"
            out.append(
                {
                    "name": c.name,
                    "included": True,
                    "qty": qty,
                    "stock": c.stock,
                    "velocity": round(c.velocity, 4),
                    "cover_days": round(cover, 1) if cover is not None else None,
                    "reason": reason,
                }
            )
        else:
            out.append(
                {
                    "name": c.name,
                    "included": False,
                    "qty": 0,
                    "stock": c.stock,
                    "velocity": round(c.velocity, 4),
                    "cover_days": round(cover, 1) if cover is not None else None,
                    "reason": excl,
                }
            )
    # Колонки без кластерной статистики (fallback на листе)
    for name, qty in assigned.items():
        if any(d["name"] == name for d in out):
            continue
        out.append(
            {
                "name": name,
                "included": qty > 0,
                "qty": int(qty),
                "stock": None,
                "velocity": None,
                "cover_days": None,
                "reason": "fallback: нет разбивки кластеров, одна колонка",
            }
        )
    return out


def distribute_ozon_qty(
    total_qty: int,
    clusters: list[ClusterStock],
    *,
    min_cover_days: float = 14.0,
    min_need: int = 1,
    target_cover_days: float = 28.0,
) -> list[ClusterShip]:
    """Раскладывает qty только по кластерам с нуждой; доли ~ пропорционально need."""

    total_qty = max(0, int(total_qty))
    if total_qty <= 0:
        return []

    needs: list[tuple[ClusterStock, int]] = []
    for c in clusters:
        n = cluster_needed(
            c,
            min_cover_days=min_cover_days,
            min_need=min_need,
            target_cover_days=target_cover_days,
        )
        if n > 0:
            needs.append((c, n))

    if not needs:
        return []

    sum_need = sum(n for _, n in needs)
    ships: list[ClusterShip] = []
    assigned = 0
    # Сначала пропорционально, остаток — самому голодному
    ranked = sorted(needs, key=lambda x: -x[1])
    for i, (c, n) in enumerate(ranked):
        if i == len(ranked) - 1:
            qty = total_qty - assigned
        else:
            qty = int(round(total_qty * (n / sum_need)))
            qty = min(qty, total_qty - assigned)
        if qty <= 0:
            continue
        cover = (c.stock / c.velocity) if c.velocity > 0 else None
        ships.append(
            ClusterShip(
                name=c.name,
                qty=qty,
                cover_days=cover,
                reason=f"cover<{min_cover_days:.0f}d, need={n}",
            )
        )
        assigned += qty
        if assigned >= total_qty:
            break
    return ships
