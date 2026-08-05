"""Tests for PDF table-of-contents helpers."""

from __future__ import annotations

from eva_dashboard.report import _dest_key, _toc_link


def test_dest_key_stable_and_unique() -> None:
    a = _dest_key("cat", "Eva Consumer")
    b = _dest_key("cat", "Eva Consumer")
    c = _dest_key("city", "Eva Consumer", "Karachi")
    assert a == b
    assert a != c
    assert a.startswith("d_")


def test_toc_link_escapes_and_includes_dest() -> None:
    html = _toc_link('A & B <Co>', "d_test_abc", mt=12.5)
    assert 'href="#d_test_abc"' in html
    assert "A &amp; B &lt;Co&gt;" in html
    assert "12.5 MT" in html
    assert "<u>" in html
