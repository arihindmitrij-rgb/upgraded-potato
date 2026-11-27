"""Автораспределение ИП Баранов: velocity → need → Google sheet."""

from __future__ import annotations

from typing import Any

__all__ = ["run_autodist", "parse_add_skus", "is_autodist_command"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from . import pipeline

        return getattr(pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
