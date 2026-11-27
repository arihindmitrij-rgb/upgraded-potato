"""Тонкий клиент Telegram Bot API: long polling и отправка сообщений."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from .errors import WbLogistError

API_ROOT = "https://api.telegram.org/bot{token}/{method}"


class TelegramClient:
    def __init__(self, token: str, timeout: float = 30.0) -> None:
        if not token:
            raise WbLogistError(
                "CONFIG_ERROR", "TELEGRAM_BOT_TOKEN не задан в .env"
            )
        self._token = token
        self._timeout = timeout

    def _call(
        self,
        method: str,
        *,
        files: dict[str, Any] | None = None,
        **params: Any,
    ) -> Any:
        url = API_ROOT.format(token=self._token, method=method)
        try:
            response = requests.post(
                url, data=params, files=files, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise WbLogistError(
                "TELEGRAM_NETWORK", f"Telegram недоступен: {exc}"
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise WbLogistError(
                "TELEGRAM_BAD_RESPONSE", f"Не JSON: {response.text[:200]}"
            ) from exc

        if not payload.get("ok"):
            raise WbLogistError(
                "TELEGRAM_API_ERROR", str(payload.get("description") or payload)
            )
        return payload.get("result")

    def get_me(self) -> dict:
        return self._call("getMe") or {}

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", **params) or []

    def send_message(self, chat_id: int, text: str) -> dict:
        try:
            return self._call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview="true",
            )
        except WbLogistError as exc:
            if "parse" not in exc.message.lower():
                raise
            # В тексте попались < > — отправляем как обычный текст
            return self._call(
                "sendMessage",
                chat_id=chat_id,
                text=text,
                disable_web_page_preview="true",
            )

    def send_typing(self, chat_id: int) -> None:
        try:
            self._call("sendChatAction", chat_id=chat_id, action="typing")
        except WbLogistError:
            pass  # не критично

    def send_document(self, chat_id: int, path: Path, caption: str) -> dict:
        path = Path(path)
        if not path.is_file():
            raise WbLogistError(
                "TELEGRAM_FILE_MISSING", f"Нет файла для отправки: {path}"
            )
        with path.open("rb") as handle:
            return self._call(
                "sendDocument",
                chat_id=chat_id,
                caption=caption[:1000],
                files={"document": (path.name, handle)},
            )
