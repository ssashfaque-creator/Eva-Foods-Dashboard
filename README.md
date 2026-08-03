# Eva Foods Dashboard

Terminal app that reads company sales + client Excel data and generates a PDF sales dashboard.

## Excel inputs

1. **Sales workbook**
   - **Sales** sheet — transactional sales (header on row 5)
   - **Category** sheet — product → Category 1 / Category 2 mapping
2. **Clients workbook** (`--clients`)
   - **ClientListReport** sheet with `Client`, `Type`, geography, credit fields
   - Report city comes from **`City-Filter`** (last column) — not the `City` column
3. **Product cost factors** — `ProductCostFactors` sheet (header on row 5). Per client type + product, the latest `Date` snapshot is used and all `Cost` lines in that snapshot are summed (Ltrs or Kgs from `Unit`).
4. **Packing costs** — per product, the latest packing cost is used (`Date` when present; otherwise the last row in file order). Matched to products by `ProdID`.

## Install (Mac / Linux)

```bash
cd Eva-Foods-Dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Generate a report

```bash
eva-dashboard report /path/to/sales.xlsx --clients /path/to/clients.xlsx \
  --product-costs /path/to/product_costs.xlsx \
  --packing-costs /path/to/packing_costs.xlsx
```

Or pass a precomputed factor-costs file:

```bash
eva-dashboard report sales.xlsx --clients clients.xlsx \
  --factor-costs output/total_factor_costs.csv
```

## Compute total factor costs

For each client type and product:

`TotalFactorCost = (sum of latest product cost centers) + (latest packing cost)`

Unit stays Ltrs or Kgs from the product cost file.

```bash
eva-dashboard costs /path/to/product_costs.xlsx /path/to/packing_costs.xlsx \
  -o output/total_factor_costs.csv
```

## Report contents

**Summary**

1. **Sales by Category** (fixed order): Eva Consumer → Eva Bulk → Maan Consumer → Maan Bulk → Cusine King → Shortening → Bulk Oil → Meal → Byproducts
2. **Daily / MTD Sales by City** — City-Filter rows sorted by total MT high → low; columns Eva Consumer / Eva Bulk / Maan Consumer / Maan Bulk
3. **Price Fetch by Client Type** — Oil (Eva), Ghee (Eva), Oil (Maan), Ghee (Maan) in Rs/maund. Oil vs Ghee from Category 2 (Eva Cooking/Canola/Sunflower = Oil; Eva VTF = Ghee; Maan Oil/Ghee; Maan Bulk uses product name — `Maan Banaspati*` = Ghee). Always in kg: Price Fetch = (Incl GST/FED per kg − cost factor per kg) × **37.3246**; Ltrs costs ÷ 0.915 to get per kg.
4. **Bulk Product Average Prices** — Daily Avg + MTD Avg; Bulk Oil per maund (× 37.3246); Byproducts / Meal / Shortening / Cusine King per kg

**Detail** (landscape), sectioned by product type

- **Eva Consumer / Eva Bulk / Maan Consumer / Maan Bulk / Cusine King**
  - Product-type heading
  - City subsections (same city order as summary)
  - Customers inside each city sorted by total MT high → low
  - City total + product total
- **Shortening / Bulk Oil / Meal / Byproducts**
  - Product-type heading only (no city sections)
  - Customers sorted by total MT high → low
  - Product total

Customer blocks merge Category (client Type), City, Party; SKU lines plus customer total (MT, Basic, Incl Gst/Fed, blended Rate = Incl ÷ kg). Detail also shows **Cost Factor** and **Price Fetch** when cost files are supplied.
