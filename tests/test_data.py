"""Smoke tests for sales/client data loading and aggregation."""

from pathlib import Path

from eva_dashboard.data import (
    CATEGORY1_ORDER,
    effective_mt_qty,
    load_clients,
    prepare_report_data,
    resolve_credit_days,
)


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sales.xlsx"
CLIENTS = ROOT / "data" / "clients.xlsx"


def test_effective_mt_prefers_excel_value():
    assert effective_mt_qty(100, "Ctn", 0.458) == 0.458


def test_effective_mt_converts_kgs_when_blank():
    assert effective_mt_qty(30220, "Kgs", 0) == 30.22


def test_credit_days_rules():
    assert resolve_credit_days(15, "Cash") == 15.0
    assert resolve_credit_days(None, "Cash") == 0.0
    assert resolve_credit_days(None, "Credit") == 30.0
    assert resolve_credit_days(None, None) == 30.0
    assert resolve_credit_days("", "") == 30.0


def test_clients_use_city_filter_not_city():
    if not CLIENTS.exists():
        return
    clients = load_clients(CLIENTS)
    assert "city" in clients.columns
    cities = set(clients["city"])
    assert "Rawalpindi" in cities or "Sargodha" in cities or "Peshawar" in cities


def test_prepare_report_against_sample():
    if not SAMPLE.exists() or not CLIENTS.exists():
        return
    data = prepare_report_data(SAMPLE, clients_path=CLIENTS)
    assert data.report_date.isoformat() == "2026-07-31"
    assert len(data.category_summary) == 9
    assert len(data.daily_sales) == 136
    assert abs(data.total_daily_mt - 277.106) < 0.01
    assert abs(data.total_mtd_mt - 15674.312) < 0.01

    cat_names = [row.category1 for row in data.category_summary]
    expected = [name for name in CATEGORY1_ORDER if name in cat_names]
    assert cat_names == expected

    assert list(data.daily_sales.columns) == [
        "product_type",
        "category",
        "city",
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
    assert list(data.city_daily.columns) == [
        "city",
        "Eva Consumer",
        "Eva Bulk",
        "Maan Consumer",
        "Maan Bulk",
        "total",
    ]
    # Cities sorted by total MT descending
    totals = data.city_daily["total"].tolist()
    assert totals == sorted(totals, reverse=True)

    assert data.daily_sales["city"].notna().all()
    assert "Eva Consumer" in set(data.daily_sales["product_type"])


if __name__ == "__main__":
    test_effective_mt_prefers_excel_value()
    test_effective_mt_converts_kgs_when_blank()
    test_credit_days_rules()
    test_clients_use_city_filter_not_city()
    test_prepare_report_against_sample()
    print("ok")
