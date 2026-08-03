# Eva Foods Dashboard

Terminal app that reads company sales + client Excel data and generates a PDF sales dashboard.

## Excel inputs

1. **Sales workbook**
   - **Sales** sheet — transactional sales (header on row 5)
   - **Category** sheet — product → Category 1 / Category 2 mapping
2. **Clients workbook** (`--clients`)
   - **ClientListReport** sheet with `Client`, `Type`, geography, credit fields
   - Report city comes from **`City-Filter`** (last column) — not the `City` column

## Install (Mac / Linux)

```bash
cd Eva-Foods-Dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Generate a report

```bash
eva-dashboard report /path/to/sales.xlsx --clients /path/to/clients.xlsx
# or
python -m eva_dashboard report sales.xlsx --clients clients.xlsx -o output/sales_report.pdf
```

By default the **latest date** in the sales workbook is treated as the current report date.

## Report contents

**Summary pages**

1. Category table (Category 1) — Daily MT + MTD MT
2. **Daily Sales by City** — rows = `City-Filter`, columns = Eva Consumer / Eva Bulk / Maan Consumer / Maan Bulk
3. **MTD Sales by City** — same layout

**Detail pages** (landscape) — grouped customer-wise

Category (**client Type**), City (**City-Filter**), and Party are shown once per customer (vertically merged / centered). SKU lines keep Product, Qty, Unit, M.T Qty, Rate, Basic Amount, Incl Gst/Fed, Amount per KG. Each customer ends with a **Total** row: total M.T Qty, Basic Amount, Incl Gst/Fed, and Rate = total Incl Gst/Fed ÷ total kg (MT × 1000).

`Amount per KG = Incl GST/FED ÷ (M.T Qty × 1000)`

### Credit days (stored from clients file)

- Use `CrDays` when present
- If blank and `PaymentType` is Cash → 0
- If blank and Credit/blank → 30
- `CrLimit` kept when assigned

### M.T Qty handling

- Uses Excel `M.T Qty` when present and non-zero
- For bulk `Kgs` rows where `M.T Qty` is 0/blank, uses `Qty / 1000`
