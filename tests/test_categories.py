"""Tests for the hardcoded product category map."""

from eva_dashboard.categories import PRODUCT_CATEGORY_ROWS, get_category_map
from eva_dashboard.data import load_category_map


def test_hardcoded_map_has_core_brands():
    frame = get_category_map()
    assert len(frame) == len(PRODUCT_CATEGORY_ROWS)
    assert set(frame.columns) == {"product", "category1", "category2"}
    products = set(frame["product"])
    assert "Eva Canola Oil (StandUpPouch)" in products
    assert "Maan Banaspati 16 Kgs Tin" in products
    assert "RBD Palm Olein" in products
    # Report uses full Category 1 names
    assert "Maan Consumer" in set(frame["category1"])
    assert "Maan Consum" not in set(frame["category1"])


def test_load_category_map_ignores_path():
    frame = load_category_map("/nonexistent/path.xlsx")
    assert len(frame) == len(PRODUCT_CATEGORY_ROWS)


def test_sample_sales_products_are_mapped():
    from pathlib import Path

    from eva_dashboard.data import load_sales, prepare_report_data

    sample = Path(__file__).resolve().parents[1] / "data" / "sales.xlsx"
    if not sample.exists():
        return
    sales = load_sales(sample)
    cats = get_category_map()
    missing = sorted(set(sales["product"]) - set(cats["product"]))
    assert missing == [], f"Sales products missing from hardcoded map: {missing}"
    # Full prepare still works without relying on Category sheet
    data = prepare_report_data(sample)
    assert len(data.daily_sales) > 0


if __name__ == "__main__":
    test_hardcoded_map_has_core_brands()
    test_load_category_map_ignores_path()
    test_sample_sales_products_are_mapped()
    print("ok")
