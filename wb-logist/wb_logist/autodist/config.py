"""Параметры автораспределения (без мин. партии 100 / короба 50 в v1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..config import PROJECT_ROOT, parse_env_file, DEFAULT_ENV_PATH

# Оверрайды порогов без правки кода / .env (Hub footer ссылается сюда)
AUTODIST_CONFIG_JSON = PROJECT_ROOT / "autodist_config.json"


@dataclass(frozen=True)
class AutodistConfig:
    history_days: int = 35
    cover_ceiling_days: int = 42
    # Кластер Ozon «явно нужен», если локальный cover ниже порога
    ozon_min_cover_days: int = 14
    ozon_min_need: int = 1
    # Сезонный множитель: clamp
    season_min: float = 0.7
    season_max: float = 1.3
    dry_run: bool = False
    wb_warehouse_label: str = "СЦ Домодедово 2"
    moysklad_token: str | None = None
    n8n_base_url: str | None = None
    n8n_api_token: str | None = None
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_token_path: Path = PROJECT_ROOT / ".cache" / "google_token.json"
    apimira_api_key: str | None = None
    config_json_path: Path = AUTODIST_CONFIG_JSON


def _load_json_overrides(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if str(key).startswith("_"):
            continue
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[str(key)] = text
    return out


def load_autodist_config(
    env_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    config_json: Path | None = None,
) -> AutodistConfig:
    """Приоритет: явные overrides > autodist_config.json > .env > дефолты."""

    values = parse_env_file(Path(env_path) if env_path else DEFAULT_ENV_PATH)
    json_path = Path(config_json) if config_json else AUTODIST_CONFIG_JSON
    values.update(_load_json_overrides(json_path))
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            values[key] = text

    def _int(name: str, default: int) -> int:
        raw = values.get(name)
        if not raw:
            return default
        return int(float(raw))

    def _float(name: str, default: float) -> float:
        raw = values.get(name)
        if not raw:
            return default
        return float(raw)

    def _bool(name: str, default: bool = False) -> bool:
        raw = (values.get(name) or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "y", "on"}

    token_path = values.get("GOOGLE_TOKEN_PATH")
    google_token = (
        Path(token_path)
        if token_path
        else PROJECT_ROOT / ".cache" / "google_token.json"
    )
    if not google_token.is_absolute():
        google_token = PROJECT_ROOT / google_token

    return AutodistConfig(
        history_days=_int("AUTODIST_HISTORY_DAYS", 35),
        cover_ceiling_days=_int("AUTODIST_COVER_CEILING_DAYS", 42),
        ozon_min_cover_days=_int("AUTODIST_OZON_MIN_COVER_DAYS", 14),
        ozon_min_need=_int("AUTODIST_OZON_MIN_NEED", 1),
        season_min=_float("AUTODIST_SEASON_MIN", 0.7),
        season_max=_float("AUTODIST_SEASON_MAX", 1.3),
        dry_run=_bool("AUTODIST_DRY_RUN", False),
        wb_warehouse_label=values.get("AUTODIST_WB_WAREHOUSE") or "СЦ Домодедово 2",
        moysklad_token=values.get("MOYSKLAD_TOKEN") or None,
        n8n_base_url=values.get("N8N_BASE_URL") or None,
        n8n_api_token=values.get("N8N_API_TOKEN") or None,
        google_oauth_client_id=values.get("GOOGLE_OAUTH_CLIENT_ID") or None,
        google_oauth_client_secret=values.get("GOOGLE_OAUTH_CLIENT_SECRET") or None,
        google_token_path=google_token,
        apimira_api_key=values.get("APIMIRA_API_KEY") or None,
        config_json_path=json_path,
    )
