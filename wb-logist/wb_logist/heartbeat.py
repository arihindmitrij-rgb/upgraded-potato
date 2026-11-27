"""Heartbeat воркера для Agent Hub: файл с TTL, без Cursor/VPN."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, parse_env_file, DEFAULT_ENV_PATH

DEFAULT_HEARTBEAT = PROJECT_ROOT / ".cache" / "worker_heartbeat.json"
DEFAULT_HUB_HEARTBEAT = Path(
    r"C:\Users\User\.cline\data\workspaces\chat\dashboard"
    r"\agent-hub\agents\wb-logist\heartbeat.json"
)
# Hub считает воркер offline, если heartbeat старше этого TTL
DEFAULT_STALE_SEC = 90


def _paths_from_env(env_path: Path | None = None) -> tuple[Path, Path, int]:
    values = parse_env_file(Path(env_path) if env_path else DEFAULT_ENV_PATH)
    primary = Path(values.get("WORKER_HEARTBEAT_PATH") or DEFAULT_HEARTBEAT)
    hub = Path(values.get("HUB_HEARTBEAT_PATH") or DEFAULT_HUB_HEARTBEAT)
    try:
        stale = int(float(values.get("WORKER_HEARTBEAT_STALE_SEC") or DEFAULT_STALE_SEC))
    except ValueError:
        stale = DEFAULT_STALE_SEC
    return primary, hub, stale


def write_heartbeat(
    *,
    status: str = "ok",
    detail: str = "",
    env_path: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Пишет heartbeat в .cache и зеркало для Agent Hub."""

    primary, hub, stale = _paths_from_env(env_path)
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "status": status,
        "ok": status not in {"stopped", "dead"},
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "detail": detail or "",
        "ts": now.isoformat(),
        "ts_unix": time.time(),
        "stale_after_sec": stale,
    }
    if extra:
        payload.update(extra)

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    for path in (primary, hub):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    return primary


def read_heartbeat(path: Path | None = None) -> dict[str, Any] | None:
    primary, hub, _ = _paths_from_env()
    for candidate in ((path,) if path else (hub, primary)):
        if candidate is None or not candidate.is_file():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def heartbeat_status(
    path: Path | None = None, *, now: float | None = None
) -> dict[str, Any]:
    """Возвращает {ok, status, age_sec, message} для Hub."""

    data = read_heartbeat(path)
    now_ts = time.time() if now is None else now
    if not data:
        return {
            "ok": False,
            "status": "down",
            "age_sec": None,
            "message": "воркер не работает (нет heartbeat)",
        }
    ts = data.get("ts_unix")
    if ts is None and data.get("ts"):
        try:
            ts = datetime.fromisoformat(str(data["ts"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
    if ts is None:
        return {
            "ok": False,
            "status": "down",
            "age_sec": None,
            "message": "воркер не работает (битый heartbeat)",
        }
    age = max(0.0, now_ts - float(ts))
    stale_after = float(data.get("stale_after_sec") or DEFAULT_STALE_SEC)
    if age > stale_after or not data.get("ok", True):
        return {
            "ok": False,
            "status": "down",
            "age_sec": round(age, 1),
            "message": "воркер не работает",
            "heartbeat": data,
        }
    return {
        "ok": True,
        "status": "up",
        "age_sec": round(age, 1),
        "message": "воркер на связи",
        "heartbeat": data,
    }
