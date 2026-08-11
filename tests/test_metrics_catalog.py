"""Governed metrics catalog — synonym resolution and prompt block."""

from __future__ import annotations

from eva_dashboard.chatbot import system_prompt
from eva_dashboard.metrics_catalog import (
    apply_metric_synonyms_to_spec,
    load_metrics_catalog,
    metrics_for_prompt,
    resolve_metrics_from_text,
    resolve_operation_from_text,
)


def test_catalog_loads_canonical_metrics() -> None:
    cat = load_metrics_catalog()
    assert cat.get("version")
    for key in ("volume", "ams", "vs_ams", "avg_price", "price_fetch", "ams_growth"):
        assert key in (cat.get("metrics") or {})


def test_resolve_strong_metric_synonyms() -> None:
    assert "vs_ams" in resolve_metrics_from_text("what % of their AMS is this")
    assert "price_fetch" in resolve_metrics_from_text("Price Fetch for Eva Consumer")
    assert "avg_price" in resolve_metrics_from_text("what's the price?")
    # Weak synonym alone should not fire unless include_weak
    assert resolve_metrics_from_text("Show me Lahore sales") == []
    assert "volume" in resolve_metrics_from_text(
        "Show me Lahore sales", include_weak=True
    )


def test_price_fetch_beats_avg_price() -> None:
    mets = resolve_metrics_from_text("apply the cost factor / Price Fetch")
    assert "price_fetch" in mets
    assert "avg_price" not in mets


def test_apply_synonyms_fills_metrics_and_profile_op() -> None:
    spec = apply_metric_synonyms_to_spec(
        {
            "operation": "pivot",
            "metrics": ["volume"],
            "filters": {"party": "Alpha Dist"},
            "row_dimensions": ["party"],
        },
        "what % of their AMS is this",
    )
    assert "vs_ams" in (spec.get("metrics") or [])

    profile = apply_metric_synonyms_to_spec(
        {
            "operation": "pivot",
            "metrics": ["volume"],
            "extracted_entities": ["Alpha Dist"],
        },
        "tell me about Alpha Dist",
    )
    assert profile.get("operation") == "party_profile"
    assert resolve_operation_from_text("last purchase date") == "party_profile"


def test_prompt_includes_governed_metrics() -> None:
    block = metrics_for_prompt()
    assert "vs_ams" in block
    assert "party_profile" in block
    text = system_prompt()
    assert "GOVERNED METRICS" in text
    assert "vs_ams" in text
