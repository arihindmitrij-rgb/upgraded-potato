"""Конфигурация: .env + переменные окружения + CLI-оверрайды."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import WbLogistError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

CONTENT_API_BASE = "https://content-api.wildberries.ru"
SUPPLIES_API_BASE = "https://supplies-api.wildberries.ru"
OZON_API_BASE = "https://api-seller.ozon.ru"

SHEETS_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    "?format=csv&gid={gid}"
)
SHEETS_HTMLVIEW_URL = "https://docs.google.com/spreadsheets/d/{spreadsheet_id}/htmlview"

DEFAULT_SHEET_NAME = "Проверочный лист (Дима)"


@dataclass(frozen=True)
class Config:
    """Все настройки скрипта в одном неизменяемом объекте."""

    spreadsheet_id: str | None
    sheet_gid: str | None
    sheet_name: str
    wb_api_key: str | None
    output_dir: Path
    cache_dir: Path
    http_timeout: float
    cache_ttl_min: int
    support_phone: str
    seller_name: str
    brand: str
    destination_marker: str
    report_dir: Path | None = None
    report_prefix: str = "ИП Баранов ВБ"
    report_date_format: str = "%d.%m.%Y"
    telegram_bot_token: str | None = None
    content_api_base: str = CONTENT_API_BASE
    supplies_api_base: str = SUPPLIES_API_BASE
    ozon_client_id: str | None = None
    ozon_api_key: str | None = None
    ozon_api_base: str = OZON_API_BASE
    ozon_drop_off_warehouse_id: int | None = None
    ozon_report_prefix: str = "ИП Баранов Озон"
    ozon_destination_marker: str = "Озон"
    ozon_template_path: Path | None = None
    ozon_report_dir: Path | None = None

    @property
    def ozon_configured(self) -> bool:
        return bool(self.ozon_client_id and self.ozon_api_key)


def parse_env_file(path: Path) -> dict[str, str]:
    """Простой парсер .env: KEY=VALUE, кавычки, комментарии, префикс export.

    Специально без внешних зависимостей — скрипт запускается из n8n,
    и лишние пакеты в venv нам не нужны.
    """

    data: dict[str, str] = {}
    path = Path(path)
    if not path.is_file():
        return data

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value.startswith(("\"", "'")):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing != -1:
                value = value[1:closing]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        data[key] = value
    return data


def _as_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise WbLogistError(
            "CONFIG_ERROR", f"{name} должно быть числом, получено: {raw!r}"
        ) from exc


def _as_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise WbLogistError(
            "CONFIG_ERROR", f"{name} должно быть целым числом, получено: {raw!r}"
        ) from exc


def _as_optional_int(values: Mapping[str, str], name: str) -> int | None:
    raw = values.get(name)
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError as exc:
        raise WbLogistError(
            "CONFIG_ERROR", f"{name} должно быть целым числом, получено: {raw!r}"
        ) from exc


def _as_path(values: Mapping[str, str], name: str, default: str) -> Path:
    raw = values.get(name) or default
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _optional_path(values: Mapping[str, str], name: str) -> Path | None:
    raw = values.get(name)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(
    env_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Собирает конфиг: файл .env -> переменные окружения -> CLI-оверрайды."""

    path = Path(env_path) if env_path else DEFAULT_ENV_PATH
    values: dict[str, str] = parse_env_file(path)

    for key in list(values):
        env_value = os.environ.get(key)
        if env_value:
            values[key] = env_value

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            values[key] = text

    return Config(
        spreadsheet_id=values.get("SPREADSHEET_ID") or None,
        sheet_gid=values.get("SHEET_GID") or None,
        sheet_name=values.get("SHEET_NAME") or DEFAULT_SHEET_NAME,
        wb_api_key=values.get("WB_API_KEY_STANDARD") or None,
        output_dir=_as_path(values, "OUTPUT_DIR", "output"),
        cache_dir=_as_path(values, "CACHE_DIR", ".cache"),
        http_timeout=_as_float(values, "HTTP_TIMEOUT", 30.0),
        cache_ttl_min=_as_int(values, "CACHE_TTL_MIN", 30),
        support_phone=values.get("SUPPORT_PHONE", ""),
        seller_name=values.get("SELLER_NAME", ""),
        brand=values.get("BRAND", ""),
        destination_marker=values.get("DESTINATION_MARKER") or "ВБ",
        report_dir=_optional_path(values, "REPORT_DIR"),
        report_prefix=values.get("REPORT_PREFIX") or "ИП Баранов ВБ",
        report_date_format=values.get("REPORT_DATE_FORMAT") or "%d.%m.%Y",
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or None,
        content_api_base=values.get("CONTENT_API_BASE") or CONTENT_API_BASE,
        supplies_api_base=values.get("SUPPLIES_API_BASE") or SUPPLIES_API_BASE,
        ozon_client_id=values.get("OZON_CLIENT_ID") or None,
        ozon_api_key=values.get("OZON_API_KEY") or None,
        ozon_api_base=values.get("OZON_API_BASE") or OZON_API_BASE,
        ozon_drop_off_warehouse_id=_as_optional_int(values, "OZON_DROP_OFF_WAREHOUSE_ID"),
        ozon_report_prefix=values.get("OZON_REPORT_PREFIX") or "ИП Баранов Озон",
        ozon_destination_marker=values.get("OZON_DESTINATION_MARKER") or "Озон",
        ozon_template_path=_optional_path(values, "OZON_TEMPLATE_PATH"),
        ozon_report_dir=_optional_path(values, "OZON_REPORT_DIR"),
    )
