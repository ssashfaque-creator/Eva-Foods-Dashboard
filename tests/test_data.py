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
        "category1",
        "product",
        "qty",
        "unit",
        "mt_qty",
        "rate",
        "basic_amount",
        "incl_gst_fed",
    ]


if __name__ == "__main__":
    test_effective_mt_prefers_excel_value()
    test_effective_mt_converts_kgs_when_blank()
    test_prepare_report_against_sample()
    print("ok")
