"""Tests for product + packing cost factor computation."""

from pathlib import Path

from eva_dashboard.costs import (
    compute_total_factor_costs,
    load_packing_costs,
    load_product_cost_factors,
    save_factor_costs,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"
PRODUCT = FIXTURES / "product_costs.xlsx"
PACKING = FIXTURES / "packing_costs.xlsx"
PACKING_DATED = FIXTURES / "packing_costs_dated.xlsx"
SAMPLE_PRODUCT = Path(__file__).resolve().parents[1] / "data" / "product_costs.xlsx"
SAMPLE_PACKING = Path(__file__).resolve().parents[1] / "data" / "packing_costs.xlsx"


def test_product_costs_use_latest_date_and_sum_centers():
    df = load_product_cost_factors(PRODUCT)
    eva = df[(df["ClientType"] == "Eva Distributors") & (df["ProdID"] == 23)].iloc[0]
    assert abs(eva["ProductCost"] - 15.5) < 1e-9
    assert eva["Unit"] == "Ltrs"
    assert eva["ProductCostDate"].isoformat() == "2026-07-01"

    whole = df[(df["ClientType"] == "Whole Seller") & (df["ProdID"] == 23)].iloc[0]
    assert abs(whole["ProductCost"] - 13.0) < 1e-9

    kg = df[(df["ClientType"] == "Eva Distributors") & (df["ProdID"] == 68)].iloc[0]
    assert abs(kg["ProductCost"] - 56.25) < 1e-9
    assert kg["Unit"] == "Kgs"
    assert kg["ProductCostDate"].isoformat() == "2026-07-15"


def test_packing_costs_last_row_when_no_date():
    df = load_packing_costs(PACKING)
    row23 = df[df["ProdID"] == 23].iloc[0]
    assert abs(row23["PackingCost"] - 46.47) < 1e-9
    row68 = df[df["ProdID"] == 68].iloc[0]
    assert abs(row68["PackingCost"] - 33.98) < 1e-9


def test_packing_costs_prefer_latest_date():
    df = load_packing_costs(PACKING_DATED)
    row23 = df[df["ProdID"] == 23].iloc[0]
    assert abs(row23["PackingCost"] - 46.47) < 1e-9
    assert row23["PackingCostDate"].isoformat() == "2026-07-01"


def test_total_factor_cost_adds_packing():
    result = compute_total_factor_costs(PRODUCT, PACKING)
    frame = result.frame
    eva = frame[(frame["ClientType"] == "Eva Distributors") & (frame["ProdID"] == 23)].iloc[0]
    assert abs(eva["ProductCost"] - 15.5) < 1e-9
    assert abs(eva["PackingCost"] - 46.47) < 1e-9
    assert abs(eva["TotalFactorCost"] - 61.97) < 1e-9
    assert eva["Unit"] == "Ltrs"

    kg = frame[(frame["ClientType"] == "Eva Distributors") & (frame["ProdID"] == 68)].iloc[0]
    assert abs(kg["TotalFactorCost"] - (56.25 + 33.98)) < 1e-9
    assert kg["Unit"] == "Kgs"


def test_save_factor_costs_csv(tmp_path):
    result = compute_total_factor_costs(PRODUCT, PACKING)
    out = save_factor_costs(result, tmp_path / "total_factor_costs.csv")
    assert out.exists()
    text = out.read_text()
    assert "TotalFactorCost" in text
    assert "Eva Distributors" in text


def test_sample_cost_files_if_present():
    if not SAMPLE_PRODUCT.exists() or not SAMPLE_PACKING.exists():
        return
    result = compute_total_factor_costs(SAMPLE_PRODUCT, SAMPLE_PACKING)
    assert len(result.frame) > 0
    assert set(result.frame["Unit"].dropna()) <= {"Ltrs", "Kgs"}
    assert (result.frame["TotalFactorCost"] >= result.frame["ProductCost"]).all()


if __name__ == "__main__":
    test_product_costs_use_latest_date_and_sum_centers()
    test_packing_costs_last_row_when_no_date()
    test_packing_costs_prefer_latest_date()
    test_total_factor_cost_adds_packing()
    from pathlib import Path as P
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        test_save_factor_costs_csv(P(d))
    test_sample_cost_files_if_present()
    print("ok")
