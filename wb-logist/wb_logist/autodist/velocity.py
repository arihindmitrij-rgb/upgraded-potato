"""Velocity с OOS-коррекцией и лёгким сезонным множителем."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkuSignal:
    vendor_code: str
    clean_orders: float
    days_in_stock: float
    velocity: float
    season_factor: float
    v_adj: float
    stock_mp: int
    buyout_rate: float | None = None


def compute_velocity(
    clean_orders: float,
    days_in_stock: float,
    *,
    season_factor: float = 1.0,
) -> tuple[float, float]:
    """Возвращает (velocity, v_adj). days_in_stock >= 1."""

    days = max(1.0, float(days_in_stock))
    velocity = max(0.0, float(clean_orders)) / days
    v_adj = velocity * float(season_factor)
    return velocity, v_adj


def estimate_days_in_stock(
    window_days: int,
    *,
    daily_orders: list[float] | None = None,
    stock_now: int = 0,
) -> float:
    """Эвристика OOS: дни с заказами + дни с остатком; иначе окно целиком.

    Без дневной истории остатков: если stock_now==0 и в конце окна нет заказов —
    считаем «хвост нулей» как OOS и не делим на него.
    """

    window = max(1, int(window_days))
    if not daily_orders:
        return float(window)

    series = list(daily_orders[-window:])
    if len(series) < window:
        series = [0.0] * (window - len(series)) + series

    # Дни с наличием ≈ дни с заказами, плюс текущий stock
    active = sum(1 for x in series if x > 0)
    if stock_now > 0:
        # были на стоке всё окно, кроме явных провалов: берём max(active, window*0.5)
        return float(max(active, int(window * 0.5), 1))

    # stock=0: отрезаем хвостовые нули как OOS
    trim = 0
    for x in reversed(series):
        if x > 0:
            break
        trim += 1
    in_stock = max(1, window - trim)
    # если заказов не было совсем — оставляем 1, чтобы не делить на 0
    if active == 0:
        return 1.0
    return float(max(active, in_stock if trim else window))


def season_factor_from_history(
    recent_velocity: float,
    baseline_velocity: float,
    *,
    lo: float = 0.7,
    hi: float = 1.3,
) -> float:
    """Лёгкий множитель: текущий темп vs «хорошая» база."""

    if baseline_velocity <= 1e-9:
        return 1.0
    ratio = recent_velocity / baseline_velocity
    return max(lo, min(hi, ratio))


def target_cover_days(
    *,
    good_sales_cover: float | None,
    ceiling: int = 42,
) -> float:
    derived = float(good_sales_cover) if good_sales_cover and good_sales_cover > 0 else 28.0
    return min(derived, float(ceiling))


def build_signal(
    vendor_code: str,
    *,
    clean_orders: float,
    window_days: int,
    stock_mp: int,
    daily_orders: list[float] | None = None,
    baseline_orders: float | None = None,
    buyout_rate: float | None = None,
    cover_ceiling: int = 42,
    season_lo: float = 0.7,
    season_hi: float = 1.3,
) -> SkuSignal:
    days = estimate_days_in_stock(
        window_days, daily_orders=daily_orders, stock_now=stock_mp
    )
    velocity, _ = compute_velocity(clean_orders, days, season_factor=1.0)
    baseline_v = None
    if baseline_orders is not None:
        baseline_v = max(0.0, float(baseline_orders)) / max(1.0, float(window_days))
    s = season_factor_from_history(
        velocity, baseline_v if baseline_v is not None else velocity,
        lo=season_lo, hi=season_hi,
    )
    _, v_adj = compute_velocity(clean_orders, days, season_factor=s)
    return SkuSignal(
        vendor_code=vendor_code,
        clean_orders=float(clean_orders),
        days_in_stock=days,
        velocity=velocity,
        season_factor=s,
        v_adj=v_adj,
        stock_mp=int(stock_mp),
        buyout_rate=buyout_rate,
    )
