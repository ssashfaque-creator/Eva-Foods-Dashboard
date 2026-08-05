"""Hardcoded product → Category 1 / Category 2 map for Eva Foods reports."""

from __future__ import annotations

import pandas as pd

# (Product, Category 1, Category 2)
# Category 1 names match report section order (e.g. Maan Consumer, not truncated labels).
PRODUCT_CATEGORY_ROWS: tuple[tuple[str, str, str], ...] = (
    ("BakeRight Shortening 16 Kgs Ctn", "Shortening", "Shortening"),
    ("CANOLA MEAL", "Meal", "Canola Meal"),
    ("CANOLA OIL", "Bulk Oil", "Canola Oil"),
    ("Canola Oil (CDCO)", "Bulk Oil", "Canola Oil"),
    ("Canola Oil Filter (RS)", "Bulk Oil", "Canola Oil"),
    ("Canola Seed", "Bulk Oil", "Canola Seed"),
    ("Cuisine King (16 Ltr Tin)", "Cusine King", "Cusine King"),
    ("Eva Canola Oil (10 Litrs J/Can)", "Eva Consumer", "Eva Canola"),
    ("Eva Canola Oil (3 Ltr Bottle)", "Eva Consumer", "Eva Canola"),
    ("Eva Canola Oil (5 Ltr Bottle)", "Eva Consumer", "Eva Canola"),
    ("Eva Canola Oil (StandUpPouch)", "Eva Consumer", "Eva Canola"),
    ("Eva Canola Oil 16 Ltr Tin", "Eva Bulk", "Eva Bulk"),
    ("Eva Canola Oil 16 Ltr Tin (Navy)", "Eva Bulk", "Eva Navy"),
    ("Eva Canola Oil 5 Litrs PetBottle (CP)", "Eva Consumer", "Eva Canola"),
    ("Eva Cooking Oil ( 0.5 Ltr Standup Pouch)", "Eva Consumer", "Eva Cooking"),
    ("Eva Cooking Oil (16 Ltr Tin)", "Eva Bulk", "Eva Bulk"),
    ("Eva Cooking Oil (3 Ltr Bottle)", "Eva Consumer", "Eva Cooking"),
    ("Eva Cooking Oil (5 Ltr Bottle)", "Eva Consumer", "Eva Cooking"),
    ("Eva Cooking Oil (StandUpPouch)", "Eva Consumer", "Eva Cooking"),
    ("Eva Cooking Oil 16 kg Tin", "Eva Bulk", "Eva Bulk"),
    ("Eva Cooking Oil 16 Ltr J/Can", "Eva Bulk", "Eva Bulk"),
    ("Eva Cooking Oil 1x5 Pillow Pouch", "Eva Consumer", "Eva Cooking"),
    ("Eva Cooking Oil 5 Litrs PetBottle (CP)", "Eva Consumer", "Eva Cooking"),
    ("Eva Sunflower Oil 1x5 (Standup Pouch)", "Eva Consumer", "Eva Sunflower"),
    ("Eva Sunflower Oil 1X5 Pouch (P.P)", "Eva Consumer", "Eva Sunflower"),
    ("Eva Sunflower Oil 3 Ltr PetBottle", "Eva Consumer", "Eva Sunflower"),
    ("Eva Sunflower Oil 5 Ltr Pet Bottle", "Eva Consumer", "Eva Sunflower"),
    ("Eva VTF Banaspati 1 x 4 Kg Tin", "Eva Consumer", "Eva VTF"),
    ("Eva VTF Banaspati 16 Kg Tin", "Eva Bulk", "Eva VTF Bulk"),
    ("Eva VTF Banaspati 16 kg Tin (DGP)", "Eva Bulk", "Eva DGP"),
    ("Eva VTF Banaspati 1x5 Pouch", "Eva Consumer", "Eva VTF"),
    ("Eva VTF Banaspati 5 Kg Tin", "Eva Consumer", "Eva VTF"),
    ("Fatty Acid", "Byproducts", "Fatty Acid"),
    ("Hydrogenated RBD Palm Olein", "Bulk Oil", "Olein"),
    ("Hydrogenated RBD Palm Olein (Low MCP)", "Bulk Oil", "Olein"),
    ("Liquid Soap", "Byproducts", "Liquid Soap"),
    ("Maan Banaspati 1/2 X 24", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 1/2 X 32", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 10 Kg Bucket", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 16 Kg Bucket", "Maan Bulk", "Maan Bulk"),
    ("Maan Banaspati 16 Kgs Tin", "Maan Bulk", "Maan Bulk"),
    ("Maan Banaspati 1X12", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 1x16 Pouch", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 1X5", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 2.5 Kgs Tin", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 5 Kg Bucket", "Maan Consumer", "Maan Ghee"),
    ("Maan Banaspati 5 Kgs Tin", "Maan Consumer", "Maan Ghee"),
    ("Maan Cooking Oil (10 Litrs J/Can)", "Maan Consumer", "Maan Oil"),
    ("Maan Cooking Oil (10 Ltrs J/Can)", "Maan Consumer", "Maan Oil"),
    ("Maan Cooking Oil 1 X 12 Pouch", "Maan Consumer", "Maan Oil"),
    ("Maan Cooking Oil 1 X 5 Pouch", "Maan Consumer", "Maan Oil"),
    ("Maan Cooking Oil 16 Ltrs. Tin", "Maan Bulk", "Maan Bulk"),
    ("Maan Cooking Oil 3 Ltr Pet Bottle", "Maan Consumer", "Maan Oil"),
    ("Maan Cooking Oil 5 Ltr Pet Bottle", "Maan Consumer", "Maan Oil"),
    ("Processing Charges", "Bulk Oil", "Processing Charges"),
    ("RBD Canola Oil", "Bulk Oil", "Canola Oil"),
    ("RBD Canola Oil (Low MPCD/GE)", "Bulk Oil", "Canola Oil"),
    ("RBD Palm Oil", "Bulk Oil", "Palm RBD"),
    ("RBD Palm Oil (Process)", "Bulk Oil", "Olein"),
    ("RBD Palm Oil (Processed)", "Bulk Oil", "Olein"),
    ("RBD Palm Olein", "Bulk Oil", "Olein"),
    ("RBD Palm Olein (Process)", "Bulk Oil", "Olein"),
    ("RBD Palm Olein (Processed)", "Bulk Oil", "Olein"),
    ("RBD PALM OLIEN (MB)", "Bulk Oil", "Olein"),
    ("RBD Sarso", "Bulk Oil", "Sarsoo"),
    ("RBD Sarso/Rapeseed", "Bulk Oil", "Sarsoo"),
    ("RBD Soyabean Oil", "Bulk Oil", "Soya Oil"),
    ("RBD Sunflower Oil", "Bulk Oil", "Sunflower oil"),
    ("RBD Super Olein (Export)", "Bulk Oil", "Olein"),
    ("RBD Super Olien", "Bulk Oil", "Super Olein"),
    ("Sarsoo Oil", "Bulk Oil", "Sarsoo"),
    ("Scrape", "Byproducts", "Scrap"),
    ("Soap ( C- R )", "Byproducts", "Soap"),
    ("SOLID SOAP", "Byproducts", "Soap"),
    ("Soybean Hull 3mm", "Byproducts", "Hull"),
    ("Soybean Meal Hi Pro", "Meal", "Soya Meal"),
    ("Soybean Oil", "Bulk Oil", "Soya Oil"),
    ("Soybean Oil (CDSO)", "Bulk Oil", "Soya Oil"),
    ("Spent Earth", "Byproducts", "Spent Earth"),
    ("Sunflower Meal", "Meal", "Sun meal"),
    ("Sunflower Wax", "Byproducts", "Sun Wax"),
    ("TAIZI", "Byproducts", "Tazi"),
    ("Tazi", "Byproducts", "Tazi"),
)


def get_category_map() -> pd.DataFrame:
    """Return the hardcoded product category map (product, category1, category2)."""
    frame = pd.DataFrame(
        PRODUCT_CATEGORY_ROWS,
        columns=["product", "category1", "category2"],
    )
    frame["product"] = frame["product"].astype(str).str.strip()
    frame["category1"] = frame["category1"].astype(str).str.strip()
    frame["category2"] = frame["category2"].astype(str).str.strip()
    duplicates = frame["product"][frame["product"].duplicated()].tolist()
    if duplicates:
        raise ValueError(f"Duplicate products in hardcoded category map: {duplicates}")
    return frame.reset_index(drop=True)
