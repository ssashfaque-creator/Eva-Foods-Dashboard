# Eva Foods Dashboard

Terminal app that reads company sales Excel data and generates a PDF sales dashboard.

## Excel layout expected

1. **Sales** sheet — transactional sales (header on row 5)
2. **Category** sheet — product → Category 1 / Category 2 mapping

## Install (Mac / Linux)

```bash
cd Eva-Foods-Dashboard
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Generate a report

```bash
eva-dashboard report /path/to/sales.xlsx
# or
python -m eva_dashboard report /path/to/sales.xlsx -o output/sales_report.pdf
```

By default the **latest date** in the workbook is treated as the current report date.

Optional:

```bash
eva-dashboard report sales.xlsx --date 2026-07-31 -o report.pdf
```

## Report contents (v0.1 — Sales)

**Page 1 — Summary**

| Category | Daily Sales (MT) | Month-to-Date Sales (MT) |
|---|---|---|

(Aggregated by **Category 1** from the mapping sheet.)

**Following pages — Daily sales detail** (landscape)

Category (**Category 2**), Party, Product, Qty, Unit, M.T Qty, Rate, Basic Amount, Incl Gst/Fed, Amount per KG

`Amount per KG = Incl GST/FED ÷ (M.T Qty × 1000)`

### M.T Qty handling

- Uses Excel `M.T Qty` when present and non-zero
- For bulk `Kgs` rows where `M.T Qty` is 0/blank, uses `Qty / 1000`
