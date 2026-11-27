"""Единый тип ошибки: код + человекочитаемое сообщение."""

from __future__ import annotations


class WbLogistError(Exception):
    """Ошибка бизнес-логики с машиночитаемым кодом.

    Код попадает в JSON-ответ (`error_code`) и помогает n8n/Telegram
    показать понятную причину без разбора трейсбека.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"
