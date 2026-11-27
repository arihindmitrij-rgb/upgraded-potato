"""Content API WB: артикул продавца -> баркоды (SKU) и данные карточки."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import requests

from .config import Config
from .errors import WbLogistError
from .models import DistributionRow

CARDS_ENDPOINT = "/content/v2/get/cards/list"
PAGE_LIMIT = 100
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class CardVariant:
    """Один баркод карточки (размер × SKU)."""

    vendor_code: str
    barcode: str
    size: str
    title: str
    brand: str
    nm_id: int | None
    color: str = ""


@dataclass(frozen=True)
class ResolvedItem:
    """Строка распределения, к которой найден баркод."""

    vendor_code: str
    quantity: int
    barcode: str
    size: str
    title: str
    brand: str
    nm_id: int | None
    color: str = ""


# Подсказки цвета из артикула продавца → русские варианты из характеристик WB
_VENDOR_COLOR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("black", ("черный", "чёрный")),
    ("pink", ("розовый",)),
    ("blue", ("синий", "голубой")),
    ("gray", ("серый",)),
    ("grey", ("серый",)),
    ("orange", ("оранжевый",)),
    ("red", ("красный",)),
    ("white", ("белый",)),
    ("green", ("зеленый", "зелёный")),
    ("yellow", ("желтый", "жёлтый")),
    ("purple", ("фиолетовый", "сиреневый")),
)


def _char_values(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    return [text] if text else []


def color_from_vendor_code(vendor_code: str) -> str:
    """Цвет по подсказкам в артикуле (pink/black/…), если API не отдал характеристику."""

    vendor = (vendor_code or "").casefold()
    found: list[str] = []
    for hint, russian in _VENDOR_COLOR_HINTS:
        if hint in vendor:
            label = russian[0]
            if label not in found:
                found.append(label)
    return "-".join(found)


def extract_color(vendor_code: str, characteristics: object) -> str:
    """Цвет из characteristics карточки; при нескольких — предпочитаем совпадение с артикулом."""

    values: list[str] = []
    if isinstance(characteristics, list):
        for item in characteristics:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("charcName") or "").casefold()
            if "цвет" not in name and "color" not in name:
                continue
            raw = item["value"] if "value" in item else item.get("charcValues")
            values.extend(_char_values(raw))

    # уникальные, порядок сохраняем
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)

    if not unique:
        return color_from_vendor_code(vendor_code)
    if len(unique) == 1:
        return unique[0]

    vendor = (vendor_code or "").casefold()
    matched: list[str] = []
    for hint, russian in _VENDOR_COLOR_HINTS:
        if hint not in vendor:
            continue
        for value in unique:
            fold = value.casefold().replace("ё", "е")
            if any(
                fold == r.casefold().replace("ё", "е")
                or r.casefold().replace("ё", "е") in fold
                for r in russian
            ):
                if value not in matched:
                    matched.append(value)
    if matched:
        return "-".join(matched)
    return "-".join(unique)


def _sleep_seconds(attempt: int, response: requests.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
    return float(2**attempt)


class ContentClient:
    """Тонкий клиент Content API с ретраями на 429/5xx."""

    def __init__(
        self,
        config: Config,
        session: requests.Session | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._sleep = sleep

    @property
    def _headers(self) -> dict[str, str]:
        if not self._config.wb_api_key:
            raise WbLogistError(
                "CONFIG_ERROR",
                "WB_API_KEY_STANDARD не задан: впишите ключ в .env "
                "(категории «Контент» + «Поставки»).",
            )
        return {"Authorization": self._config.wb_api_key}

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._config.content_api_base + CARDS_ENDPOINT
        last_error = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.post(
                    url,
                    json=payload,
                    headers=self._headers,
                    timeout=self._config.http_timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 401:
                    raise WbLogistError(
                        "AUTH_401",
                        "WB отклонил токен (401). Проверьте WB_API_KEY_STANDARD.",
                    )
                if response.status_code == 403:
                    raise WbLogistError(
                        "FORBIDDEN_403",
                        "У токена нет доступа к Content API (403). "
                        "Нужна категория «Контент».",
                    )
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if (response.status_code == 429 or response.status_code >= 500) and (
                    attempt < MAX_ATTEMPTS
                ):
                    self._sleep(_sleep_seconds(attempt, response))
                    continue
                break

            if attempt < MAX_ATTEMPTS:
                self._sleep(_sleep_seconds(attempt, None))

        raise WbLogistError(
            "CONTENT_API_FAILED", f"Content API не ответил успешно: {last_error}"
        )

    def fetch_cards(self) -> list[dict[str, Any]]:
        """Выгружает все карточки постранично (limit = 100)."""

        cards: list[dict[str, Any]] = []
        cursor: dict[str, Any] = {"limit": PAGE_LIMIT}

        for _ in range(500):  # предохранитель от бесконечной пагинации
            data = self._post(
                {
                    "settings": {
                        "sort": {"ascending": True},
                        "cursor": cursor,
                        "filter": {"withPhoto": -1},
                    }
                }
            )
            page = data.get("cards") or []
            cards.extend(page)

            next_cursor = data.get("cursor") or {}
            total = int(next_cursor.get("total") or 0)
            if total < PAGE_LIMIT or not next_cursor.get("nmID"):
                return cards

            cursor = {
                "limit": PAGE_LIMIT,
                "updatedAt": next_cursor.get("updatedAt"),
                "nmID": next_cursor.get("nmID"),
            }

        raise WbLogistError(
            "CONTENT_PAGINATION_LOOP",
            "Content API вернул слишком много страниц — прервано на 500 итерациях.",
        )


# ---------------------------------------------------------------------------
# Кэш и индекс
# ---------------------------------------------------------------------------


def _cache_path(config: Config) -> Path:
    return config.cache_dir / "cards.json"


def load_cards(config: Config, *, refresh: bool = False) -> list[dict[str, Any]]:
    """Карточки из кэша (TTL) или свежие из API."""

    path = _cache_path(config)
    if not refresh and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            fresh = datetime.now(timezone.utc) - fetched_at < timedelta(
                minutes=config.cache_ttl_min
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            fresh = False
        if fresh:
            return payload.get("cards") or []

    cards = ContentClient(config).fetch_cards()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "cards": cards,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # кэш — оптимизация, без него работаем

    return cards


def build_index(cards: Sequence[dict[str, Any]]) -> dict[str, list[CardVariant]]:
    """vendorCode -> все баркоды карточки."""

    index: dict[str, list[CardVariant]] = {}

    for card in cards:
        vendor_code = (card.get("vendorCode") or "").strip()
        if not vendor_code:
            continue

        title = (card.get("title") or "").strip()
        brand = (card.get("brand") or "").strip()
        nm_id = card.get("nmID")
        color = extract_color(vendor_code, card.get("characteristics"))

        for size in card.get("sizes") or []:
            tech_size = (size.get("techSize") or "").strip()
            wb_size = (size.get("wbSize") or "").strip()
            label = tech_size or wb_size
            for sku in size.get("skus") or []:
                barcode = str(sku).strip()
                if not barcode:
                    continue
                index.setdefault(vendor_code, []).append(
                    CardVariant(
                        vendor_code=vendor_code,
                        barcode=barcode,
                        size=label,
                        title=title,
                        brand=brand,
                        nm_id=nm_id,
                        color=color,
                    )
                )

    return index


def resolve_rows(
    rows: Sequence[DistributionRow],
    index: dict[str, list[CardVariant]],
) -> tuple[list[ResolvedItem], list[str]]:
    """Сопоставляет строки распределения с баркодами.

    Возвращает (готовые позиции, список ошибок). Любая ошибка — повод
    остановиться и не создавать поставку.
    """

    folded = {key.casefold(): key for key in index}
    resolved: list[ResolvedItem] = []
    errors: list[str] = []

    for row in rows:
        variants = index.get(row.vendor_code)
        if not variants:
            original = folded.get(row.vendor_code.casefold())
            variants = index.get(original) if original else None
        if not variants:
            errors.append(f"{row.vendor_code}: карточка не найдена в Content API")
            continue

        barcodes = sorted({variant.barcode for variant in variants})
        if len(barcodes) > 1:
            sizes = ", ".join(sorted({variant.size or "—" for variant in variants}))
            errors.append(
                f"{row.vendor_code}: несколько размеров/баркодов ({sizes}); "
                "добавьте колонку «Размер» в таблицу или уточните артикул"
            )
            continue

        variant = variants[0]
        resolved.append(
            ResolvedItem(
                vendor_code=row.vendor_code,
                quantity=row.quantity,
                barcode=variant.barcode,
                size=variant.size,
                title=variant.title,
                brand=variant.brand,
                nm_id=variant.nm_id,
                color=variant.color,
            )
        )

    return resolved, errors

