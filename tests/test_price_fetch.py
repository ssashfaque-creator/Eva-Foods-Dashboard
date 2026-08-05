"""Tests for cost factor application and Price Fetch recovery."""

from pathlib import Path

import pandas as pd

from eva_dashboard.costs import compute_total_factor_costs
from eva_dashboard.data import (
    LTR_TO_KG,
    MAUND_FACTOR_BULK_OIL,
    MAUND_FACTOR_PRICE_FETCH,
    classify_oil_ghee,
    classify_price_fetch_segment,
    cost_factor_per_kg,
    prepare_report_data,
    price_fetch_per_maund,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sales.xlsx"
CLIENTS = ROOT / "data" / "clients.xlsx"
PRODUCT_COSTS = ROOT / "data" / "product_costs.xlsx"
PACKING_COSTS = ROOT / "data" / "packing_costs.xlsx"


def test_classify_oil_ghee():
    assert classify_oil_ghee("Eva Cooking", "Eva Cooking Oil (3 Ltr Bottle)") == "Oil"
    assert classify_oil_ghee("Eva Canola", "Eva Canola Oil (StandUpPouch)") == "Oil"
    assert classify_oil_ghee("Eva Sunflower", "Eva Sunflower Oil 5 Ltr Pet Bottle") == "Oil"
    assert classify_oil_ghee("Eva VTF", "Eva VTF Banaspati 1x5 Pouch") == "Ghee"
    assert classify_oil_ghee("Eva VTF Bulk", "Eva VTF Banaspati 16 Kg Tin") == "Ghee"
    assert classify_oil_ghee("Maan Oil", "Maan Cooking Oil 5 Ltr Pet Bottle") == "Oil"
    assert classify_oil_ghee("Maan Ghee", "Maan Banaspati 1X5") == "Ghee"
    assert classify_oil_ghee("Maan Bulk", "Maan Banaspati 16 Kgs Tin") == "Ghee"
    assert classify_oil_ghee("Maan Bulk", "Maan Cooking Oil 16 Ltrs. Tin") == "Oil"
    assert classify_oil_ghee("Eva Bulk", "Eva Cooking Oil (16 Ltr Tin)") == "Oil"
    assert classify_oil_ghee("Bulk Oil", "RBD Palm Olein") is None


def test_classify_price_fetch_segment():
    assert (
        classify_price_fetch_segment(
            "Eva Consumer", "Eva Cooking", "Eva Cooking Oil (3 Ltr Bottle)"
        )
        == "Eva Oil"
    )
    assert (
        classify_price_fetch_segment(
            "Eva Consumer", "Eva VTF", "Eva VTF Banaspati 1x5 Pouch"
        )
        == "Eva Ghee"
    )
    assert (
        classify_price_fetch_segment(
            "Maan Consumer", "Maan Oil", "Maan Cooking Oil 5 Ltr Pet Bottle"
        )
        == "Maan Oil"
    )
    assert (
        classify_price_fetch_segment(
            "Maan Bulk", "Maan Bulk", "Maan Banaspati 16 Kgs Tin"
        )
        == "Maan Ghee"
    )
    assert classify_price_fetch_segment("Bulk Oil", "Olein", "RBD Palm Olein") is None


def test_cost_factor_ltr_to_kg_and_price_fetch():
    per_kg = cost_factor_per_kg(169.79, "Ltrs")
    assert per_kg is not None
    assert abs(per_kg - 169.79 / LTR_TO_KG) < 1e-9
    assert abs(cost_factor_per_kg(50.0, "Kgs") - 50.0) < 1e-9
    pf = price_fetch_per_maund(634.5095, per_kg)
    assert pf is not None
    assert abs(pf - (634.5095 - per_kg) * MAUND_FACTOR_PRICE_FETCH) < 1e-6


def test_prepare_report_with_costs_sample():
    if not (SAMPLE.exists() and CLIENTS.exists() and PRODUCT_COSTS.exists() and PACKING_COSTS.exists()):
        return
    factors = compute_total_factor_costs(PRODUCT_COSTS, PACKING_COSTS).frame
    data = prepare_report_data(SAMPLE, clients_path=CLIENTS, factor_costs=factors)
    assert "cost_factor" in data.daily_sales.columns
    assert "price_fetch" in data.daily_sales.columns
    matched = data.daily_sales["cost_factor"].notna().sum()
    assert matched > 0
    # Known StandUpPouch canola row for Eva Distributors
    sample = data.daily_sales[
        (data.daily_sales["category"] == "Eva Distributors")
        & (data.daily_sales["product"] == "Eva Canola Oil (StandUpPouch)")
    ]
    if len(sample):
        row = sample.iloc[0]
        assert abs(float(row["cost_factor"]) - 150.0) < 1e-6
        expected_pf = (
            float(row["amount_per_kg"]) - (150.0 / LTR_TO_KG)
        ) * MAUND_FACTOR_PRICE_FETCH
        assert abs(float(row["price_fetch"]) - expected_pf) < 0.01

    assert len(data.price_fetch_summary) > 0
    assert any(
        r.eva_oil is not None
        or r.eva_ghee is not None
        or r.maan_oil is not None
        or r.maan_ghee is not None
        for r in data.price_fetch_summary
    )
    assert len(data.bulk_product_prices) > 0
    bulk_oil = [r for r in data.bulk_product_prices if r.category1 == "Bulk Oil"]
    assert bulk_oil
    assert all(r.price_unit == "per Maund" for r in bulk_oil)
    assert any(r.mtd_avg_price is not None for r in bulk_oil)
    meal = [r for r in data.bulk_product_prices if r.category1 == "Meal"]
    assert meal
    assert all(r.price_unit == "per Kg" for r in meal)
    # Report-date meal lines have zero amounts in the sample workbook
    assert all(r.daily_avg_price is None for r in meal)
    assert all(r.mtd_avg_price is not None for r in meal)
    # Maund factor sanity for bulk oil
    assert MAUND_FACTOR_BULK_OIL == 37.3246
    assert MAUND_FACTOR_PRICE_FETCH == 37.3246


def test_price_fetch_summary_is_mt_weighted() -> None:
    """Unequal MT lines must not collapse to a simple mean."""
    from eva_dashboard.data import _build_price_fetch_summary

    daily = pd.DataFrame(
        {
            "detail_category": ["Eva Distributors", "Eva Distributors"],
            "price_fetch_segment": ["Eva Oil", "Eva Oil"],
            "price_fetch": [100.0, 200.0],
            "effective_mt": [9.0, 1.0],
            "mes_qty": [90.0, 10.0],
        }
    )
    rows = _build_price_fetch_summary(daily)
    assert len(rows) == 1
    assert rows[0].eva_oil is not None
    expected = (100.0 * 9.0 + 200.0 * 1.0) / 10.0
    assert abs(rows[0].eva_oil - expected) < 1e-9
    assert abs(rows[0].eva_oil - 150.0) > 1.0  # not simple mean


def test_price_fetch_matches_kg_maund_example():
    """Screenshot example: ~630/kg sell, 150/Ltr cost → ~17,401 / maund."""
    amount_per_kg = 4_799_997 / (7.617375 * 1000.0)
    cost_per_kg = cost_factor_per_kg(150.0, "Ltrs")
    assert cost_per_kg is not None
    pf = price_fetch_per_maund(amount_per_kg, cost_per_kg)
    assert pf is not None
    expected = (amount_per_kg - 150.0 / LTR_TO_KG) * 37.3246
    assert abs(pf - expected) < 0.01
    assert abs(pf - 17400.86) < 1.0


def test_fixture_factor_costs_join():
    factors = compute_total_factor_costs(
        FIXTURES / "product_costs.xlsx",
        FIXTURES / "packing_costs.xlsx",
    ).frame
    assert not factors.empty
    assert set(factors["Unit"]) <= {"Ltrs", "Kgs"}


if __name__ == "__main__":
    test_classify_oil_ghee()
    test_classify_price_fetch_segment()
    test_cost_factor_ltr_to_kg_and_price_fetch()
    test_prepare_report_with_costs_sample()
    test_price_fetch_matches_kg_maund_example()
    test_fixture_factor_costs_join()
    print("ok")
