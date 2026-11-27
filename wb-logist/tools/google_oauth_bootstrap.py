#!/usr/bin/env python3
"""Одноразовый OAuth для записи Google Sheets (браузер на User-PC).

Ожидает Desktop-клиент (тип «Приложение для компьютера») в Google Cloud,
либо Web-клиент с точным Authorized redirect URI:

    http://localhost:8080/

Секреты: GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET в .env
или файл credentials.json / client_secret*.json в корне wb-logist
(скачанный JSON Desktop-клиента). Токен → .cache/google_token.json.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wb_logist.autodist.config import load_autodist_config
from wb_logist.autodist.sheet_writer import SCOPES

# Фиксированный порт: redirect_uri должен совпасть с Console (Web) или
# автоматически разрешён для Desktop. port=0 → случайный URI → 400 mismatch.
OAUTH_HOST = "localhost"
OAUTH_PORT = int(os.environ.get("GOOGLE_OAUTH_LOCAL_PORT", "8080"))
REDIRECT_URI = f"http://{OAUTH_HOST}:{OAUTH_PORT}/"


def _find_client_secrets_file() -> Path | None:
    env_path = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS", "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            ROOT / "credentials.json",
            ROOT / "client_secret.json",
        ]
    )
    candidates.extend(sorted(ROOT.glob("client_secret*.json")))
    for path in candidates:
        if path.is_file():
            return path
    return None


def _load_client_config(ad) -> dict:
    """Desktop JSON с диска или client id/secret из .env как installed."""
    secrets_file = _find_client_secrets_file()
    if secrets_file is not None:
        data = json.loads(secrets_file.read_text(encoding="utf-8"))
        if "installed" in data or "web" in data:
            print(f"OAuth client file: {secrets_file}")
            # Для web-клиента всё равно гоняем local server на фиксированный URI.
            return data
        raise SystemExit(f"Неизвестный формат client secrets: {secrets_file}")

    if not ad.google_oauth_client_id or not ad.google_oauth_client_secret:
        raise SystemExit(
            "Нет GOOGLE_OAUTH_CLIENT_ID/SECRET в .env и нет credentials.json"
        )
    return {
        "installed": {
            "client_id": ad.google_oauth_client_id,
            "client_secret": ad.google_oauth_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI, "http://localhost"],
        }
    }


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _print_console_help() -> None:
    print()
    print("Если браузер показал 400 redirect_uri_mismatch:")
    print("  1) Google Cloud Console → APIs & Services → Credentials")
    print("  2) Откройте OAuth 2.0 Client ID из .env (или создайте новый")
    print("     типа Desktop app / «Приложение для компьютера»).")
    print("  3) Для Desktop — redirect URI настраивать не нужно; подставьте")
    print("     новый Client ID/Secret в .env и повторите скрипт.")
    print("  4) Если оставляете Web client — в Authorized redirect URIs")
    print(f"     добавьте ровно: {REDIRECT_URI}")
    print("     (с trailing slash), Save, подождите ~1 мин, повторите.")
    print()


def main() -> int:
    ad = load_autodist_config()
    client_config = _load_client_config(ad)

    if not _port_free(OAUTH_PORT):
        print(
            f"Порт {OAUTH_PORT} занят. Освободите его или задайте "
            f"GOOGLE_OAUTH_LOCAL_PORT и добавьте тот же "
            f"http://localhost:PORT/ в Console.",
            file=sys.stderr,
        )
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    # web-клиент тоже можно прогнать через InstalledAppFlow + local server,
    # если redirect URI совпадает с Console.
    if "web" in client_config and "installed" not in client_config:
        web = client_config["web"]
        client_config = {
            "installed": {
                "client_id": web["client_id"],
                "client_secret": web["client_secret"],
                "auth_uri": web.get(
                    "auth_uri", "https://accounts.google.com/o/oauth2/auth"
                ),
                "token_uri": web.get(
                    "token_uri", "https://oauth2.googleapis.com/token"
                ),
                "redirect_uris": [REDIRECT_URI],
            }
        }
        print(
            "Внимание: JSON типа web. Нужен Authorized redirect URI:",
            REDIRECT_URI,
        )

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    print(f"Redirect URI (должен совпасть с Console): {REDIRECT_URI}")
    print("Откроется браузер Google — войдите и подтвердите доступ к Sheets.")
    try:
        creds = flow.run_local_server(
            host=OAUTH_HOST,
            port=OAUTH_PORT,
            open_browser=True,
            redirect_uri_trailing_slash=True,
        )
    except Exception as exc:  # noqa: BLE001 — показать человеку понятный хинт
        print(f"OAuth failed: {exc}", file=sys.stderr)
        _print_console_help()
        return 1

    ad.google_token_path.parent.mkdir(parents=True, exist_ok=True)
    ad.google_token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"OK token → {ad.google_token_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
