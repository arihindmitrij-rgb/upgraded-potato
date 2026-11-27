"""Sync Agent Hub explain (last_run.json) from User-PC → VPS Hub."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

LOG = logging.getLogger("wb_logist.autodist.hub_sync")

DEFAULT_HOST = "147.45.137.104"
DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/opt/agent-hub/agents/wb-logist"
DEFAULT_KEY = str(Path.home() / ".ssh" / "vps_n8n")
REMOTE_MERGE = "/opt/agent-hub/scripts/merge_last_run_state.py"

_MERGE_PY = r'''import json
from pathlib import Path
agent = Path("/opt/agent-hub/agents/wb-logist")
lr = json.loads((agent / "last_run.json").read_text(encoding="utf-8"))
sp = agent / "state.json"
existing = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
cfg = lr.get("config") or {}
summary = dict(existing.get("summary") or {})
summary["explain"] = {
    "updatedAt": lr.get("updatedAt"),
    "sku_count": lr.get("sku_count"),
    "dry_run": lr.get("dry_run"),
    "history_days": cfg.get("history_days"),
    "cover_ceiling_days": cfg.get("cover_ceiling_days"),
    "ozon_min_cover_days": cfg.get("ozon_min_cover_days"),
    "last_run_file": str(agent / "last_run.json"),
}
metrics = list(existing.get("metrics") or [])
found = False
for m in metrics:
    if m.get("key") == "explain_skus":
        m["value"] = lr.get("sku_count") or 0
        m["hint"] = "Explain " + str(lr.get("updatedAt") or "")[:16]
        found = True
        break
if not found:
    metrics.append({
        "key": "explain_skus",
        "label": "Explain SKU",
        "value": lr.get("sku_count") or 0,
        "unit": "",
        "hint": "Details -> Explain",
    })
dry = lr.get("dry_run")
reco = "Last autodist saved. Open Details -> Explain. "
if dry:
    reco += "(dry-run). "
reco += "Tune autodist_config.json / AUTODIST_* on User-PC"
state = {
    **existing,
    "id": "wb-logist",
    "metrics": metrics,
    "summary": summary,
    "lastRun": {
        "updatedAt": lr.get("updatedAt"),
        "sku_count": lr.get("sku_count"),
        "dry_run": dry,
        "config": cfg,
        "file": "last_run.json",
    },
    "recommendation": reco,
    "updatedAt": lr.get("updatedAt"),
    "view": existing.get("view") or "runs",
}
sp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
ev = agent / "events.jsonl"
with ev.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "ts": lr.get("updatedAt"),
        "level": "ok",
        "text": "Explain sync: %s SKU%s" % (
            lr.get("sku_count"),
            " [dry]" if dry else "",
        ),
    }, ensure_ascii=False) + "\n")
print("ok", lr.get("sku_count"))
'''


def _env(name: str, default: str = "") -> str:
    raw = (os.environ.get(name) or "").strip()
    if raw:
        return raw
    try:
        from ..config import DEFAULT_ENV_PATH, parse_env_file

        file_vals = parse_env_file(DEFAULT_ENV_PATH)
        return (file_vals.get(name) or default).strip()
    except Exception:  # noqa: BLE001
        return default


def sync_enabled() -> bool:
    raw = _env("HUB_VPS_SYNC", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def sync_last_run_to_vps(local_path: Path) -> bool:
    """scp last_run.json to VPS Hub and soft-merge state.json. Returns True on success."""

    if not sync_enabled():
        LOG.info("HUB_VPS_SYNC disabled — skip VPS explain sync")
        return False
    if not local_path.is_file():
        LOG.warning("No local last_run to sync: %s", local_path)
        return False

    host = _env("HUB_VPS_HOST", DEFAULT_HOST)
    user = _env("HUB_VPS_USER", DEFAULT_USER)
    remote_dir = _env("HUB_VPS_AGENT_DIR", DEFAULT_REMOTE_DIR).rstrip("/")
    key = Path(_env("HUB_VPS_SSH_KEY", DEFAULT_KEY)).expanduser()
    scp = shutil.which("scp")
    ssh = shutil.which("ssh")
    if not scp or not ssh:
        LOG.warning("scp/ssh not found — cannot sync explain to VPS")
        return False
    if not key.is_file():
        LOG.warning("SSH key missing for Hub sync: %s", key)
        return False

    remote = f"{user}@{host}:{remote_dir}/last_run.json"
    ssh_base = [
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
    ]
    try:
        subprocess.run(
            [scp, *ssh_base, str(local_path), remote],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        err = getattr(exc, "stderr", None) or str(exc)
        LOG.warning("scp last_run → VPS failed: %s", err.strip()[:300])
        return False

    # Ensure merge helper exists, then soft-merge state on VPS (do not overwrite runs).
    merge_cmd = (
        "mkdir -p /opt/agent-hub/scripts && "
        "cat > /opt/agent-hub/scripts/merge_last_run_state.py << 'PY'\n"
        + _MERGE_PY
        + "\nPY\n"
        "python3 /opt/agent-hub/scripts/merge_last_run_state.py"
    )
    try:
        subprocess.run(
            [ssh, *ssh_base, f"{user}@{host}", merge_cmd],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        err = getattr(exc, "stderr", None) or str(exc)
        LOG.warning("VPS state merge after explain sync failed: %s", err.strip()[:300])
        return False

    LOG.info("Explain synced to VPS Hub %s:%s/last_run.json", host, remote_dir)
    return True
