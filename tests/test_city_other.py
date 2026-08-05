"""Tests for top-N city tables with Other aggregate."""

from __future__ import annotations

import pandas as pd

from eva_dashboard.report import _top_cities_with_other


def test_top_cities_with_other_aggregates_remainder() -> None:
    frame = pd.DataFrame(
        {
            "city": [f"C{i}" for i in range(12)],
            "Eva Consumer": [float(12 - i) for i in range(12)],
            "total": [float(12 - i) for i in range(12)],
            "avg_30d": [1.0] * 12,
        }
    )
    top, other = _top_cities_with_other(frame, top_n=10)
    assert len(top) == 10
    assert other is not None
    assert other["city"] == "Other"
    assert other["Eva Consumer"] == 2.0 + 1.0  # C10 + C11
    assert other["total"] == 3.0
    assert other["avg_30d"] == 2.0


def test_top_cities_with_other_none_when_within_limit() -> None:
    frame = pd.DataFrame(
        {"city": ["A", "B"], "total": [5.0, 3.0], "avg_30d": [1.0, 1.0]}
    )
    top, other = _top_cities_with_other(frame, top_n=10)
    assert len(top) == 2
    assert other is None
