"""Display formatting helpers (MT as whole numbers)."""

from __future__ import annotations

from typing import Any


def mt_round(value: Any) -> int:
    """Round metric tons to nearest whole number for display / tables."""
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def mt_str(value: Any) -> str:
    return str(mt_round(value))


def pct_round(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
