"""Tests for Streamlit display helpers."""

from __future__ import annotations

import pandas as pd

from eva_dashboard.app import _for_display


def test_for_display_stringifies_mixed_object_column() -> None:
    frame = pd.DataFrame({"Type": ["Eva Distributors", 12, None], "Client": ["A", "B", "C"]})
    out = _for_display(frame)
    assert list(out["Type"]) == ["Eva Distributors", "12", ""]
    assert all(isinstance(v, str) for v in out["Type"])
