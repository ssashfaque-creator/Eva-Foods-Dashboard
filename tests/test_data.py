"""Smoke tests for sales data loading and aggregation."""

from pathlib import Path

from eva_dashboard.data import effective_mt_qty, prepare_report_data


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sales.xlsx"


def test_effective_mt_prefers_excel_value():
    assert effective_mt_qty(100, "Ctn", 0.458) == 0.458


def test_effective_mt_converts_kgs_when_blank():
    assert effective_mt_qty(30220, "Kgs", 0) == 30.22


def test_prepare_report_against_sample():
    if not SAMPLE.exists():
        return  # sample workbook is local-only
    data = prepare_report_data(SAMPLE)
    assert data.report_date.isoformat() == "2026-07-31"
    assert len(data.category_summary) == 9
    assert len(data.daily_sales) == 136
    assert abs(data.total_daily_mt - 277.106) < 0.01
    assert abs(data.total_mtd_mt - 15674.312) < 0.01
    assert list(data.daily_sales.columns) == [
        "category",
        "party",
        "product",
        "qty",
        "unit",
        "mt_qty",
        "rate",
        "basic_amount",
        "incl_gst_fed",
        "amount_per_kg",
    ]
    sample = data.daily_sales.iloc[0]
    kg = float(sample["mt_qty"]) * 1000.0
    expected = float(sample["incl_gst_fed"]) / kg if kg else 0.0
    assert abs(float(sample["amount_per_kg"]) - expected) < 1e-6
    cats = set(data.daily_sales["category"])
    # Detail uses Category 2 labels from the mapping sheet
    assert "Canola Meal" in cats or "Soya Meal" in cats
    assert data.daily_sales["party"].astype(str).str.len().gt(0).any()


if __name__ == "__main__":
    test_effective_mt_prefers_excel_value()
    test_effective_mt_converts_kgs_when_blank()
    test_prepare_report_against_sample()
    print("ok")
