# Eva Foods Dashboard — Data Catalog

This document is the source of truth for humans and for the in-app AI chatbot.
Use it when answering questions about sales, costs, clients, categories, or reports.

## Storage layout

Default root: `./data` (override with `EVA_DATA_DIR` / `--data-dir`)

```text
data/
├── eva.db                          # SQLite database
├── uploads/
│   ├── sales/
│   ├── categories/
│   ├── clients/
│   ├── product_costs/
│   └── packing_costs/
└── reports/
    └── sales_report_<date>_<timestamp>.pdf
```

## SQLite tables

### `ingested_files`
Upload ledger. Unique on `(file_type, content_hash)`.

| Column | Meaning |
|---|---|
| `file_type` | `sales`, `categories`, `clients`, `product_costs`, `packing_costs` |
| `original_name` | Filename the user uploaded |
| `stored_path` | Archived copy under `uploads/` |
| `content_hash` | SHA-256 of file bytes |
| `row_count` | Rows imported / replaced |
| `ingested_at` | ISO timestamp |

### `sales` (append-only daily lines)
Key columns: `date`, `party`, `inv_no`, `srno`, `product`, `qty`, `unit`, `mes_qty`, `mes_unit`, `mt_qty`, `rate`, `basic_amount`, `incl_gst_fed_amount`, `client_type`, `payload_json`, `row_hash`.

- Deduped by content hash of the file, and by `row_hash` per line.
- `payload_json` keeps **all** original Excel columns.
- Dates are ISO text `YYYY-MM-DD`.

### `category` (replace-on-upload master)
| Column | Meaning |
|---|---|
| `product` | PK — exact product name as in sales |
| `category_1` | Brand / product type (Eva Consumer, Bulk Oil, …) |
| `category_2` | Sub-type (Oil, Ghee, Meal, …) |

Every category upload **replaces** the whole table.

### `clients` (upsert by ClientID)
| Column | Meaning |
|---|---|
| `client_id` | PK |
| `client` | Party name (join to sales.party via normalize) |
| `type` | Client type (Eva Distributors, …) |
| `city_filter` | **Report geography** (use this, not `city`) |
| `city` | Secondary city field — do **not** use for report city |
| `inactive` | `Y` / blank |
| `payload_json` | Full Excel row (Locality, Zone, CrDays, PaymentType, …) |

### `product_cost_lines` / `packing_cost_lines`
Append-only historical cost source lines.

### `factor_costs` (derived current snapshot)
PK `(client_type, prod_id)`.

| Column | Meaning |
|---|---|
| `product` | Product name used when joining to sales |
| `unit` | `Ltrs` or `Kgs` |
| `product_cost` | Latest product cost sum |
| `packing_cost` | Latest packing cost |
| `total_factor_cost` | product + packing |

## How tables join

```text
sales.product                 = category.product          (exact text)
normalize(sales.party)        = normalize(clients.client) (lower/trim/collapse spaces)
sales client type             = clients.type else sales.client_type else "Unmapped"
factor_costs join to sales    = (resolved client type, sales.product)
                                = (factor_costs.client_type, factor_costs.product)
product_cost_lines.prod_id    = packing_cost_lines.prod_id  (for building factors)
```

## Category 1 order (reports)

1. Eva Consumer  
2. Eva Bulk  
3. Maan Consumer  
4. Maan Bulk  
5. Cusine King *(spelling is intentional)*  
6. Shortening  
7. Bulk Oil  
8. Meal  
9. Byproducts  

Aliases on import: `Maan Consum` → `Maan Consumer`; `Cuisine King` → `Cusine King`.

## Key formulas

### Effective MT
```text
if mt_qty != 0 → mt_qty
else if unit is kg/kgs → qty / 1000
else if unit is MT/ton → qty
else → 0
```

### Time windows (for a report date D)
- **Daily**: date = D  
- **MTD**: month-start(D) … D  
- **Avg 30D / ADS**: inclusive 30 calendar days ending D, sum ÷ 30 (missing days = 0)  
- **AMS**: mean of totals for the 3 full calendar months before D’s month  

### Price Fetch (Rs / maund)
```text
AmountPerKg = Incl GST/FED Amount / (effective_mt × 1000)
CostPerKg   = TotalFactorCost / 0.915   if unit is Ltrs
            = TotalFactorCost          if unit is Kgs
PriceFetch  = (AmountPerKg − CostPerKg) × 37.3246
```
Summary table by client type × (Eva/Maan × Oil/Ghee) is **MT-weighted** average of line Price Fetch.

### Oil / Ghee / brand classification
- Oil Category 2 examples: Eva Cooking, Eva Canola, Eva Sunflower, Maan Oil, Eva Bulk  
- Ghee Category 2 examples: Eva VTF, Eva VTF Bulk, Eva DGP, Maan Ghee  
- Maan Bulk: product starting `Maan Banaspati` → Ghee; else Oil  
- Industrial Bulk Oil (e.g. RBD Palm Olein) is **not** branded Oil/Ghee for Price Fetch  

### City brand columns (summary city tables)
Only: Eva Consumer, Eva Bulk, Maan Consumer, Maan Bulk.  
PDF shows top 10 cities + **Other** (remainder summed).

### Bulk average prices
Categories: Bulk Oil, Byproducts, Meal, Shortening, Cusine King.  
MT-weighted Incl/kg; Bulk Oil × 37.3246 → per maund; others per kg.

## Excel upload formats

| Upload | Sheet / file | Header | Notes |
|---|---|---|---|
| Sales | `Sales` | Excel row 5 | Daily append |
| Categories | first sheet / CSV | `Product`, `Category 1`, `Category 2` | Full replace |
| Clients | `ClientListReport` | Excel row 5 | Upsert by ClientID; needs `City-Filter` |
| Product costs | `ProductCostFactors` | ~row 5 | Latest Date + PCFID per ClientType+ProdID; sum Cost |
| Packing costs | first sheet | ProdID, Cost, … | Latest per ProdID |

## App tabs

1. **Sales data** — sales upload + category upload + browse  
2. **Cost structure** — product/packing costs + factor costs browse  
3. **Client list** — client upload + browse  
4. **Reports** — generate Sales dashboard PDF from DB  
5. **AI Chat** — OpenAI assistant with read-only SQL tools over `eva.db`

## Chatbot guidance

When answering:
1. Prefer SQL / tools against `eva.db` for factual numbers — never invent.  
2. Trust the **live sales date range** in the system prompt (often 2025–2026).  
3. Never mention an OpenAI knowledge cutoff for this app’s data.  
4. Use `city_filter` for geography; never confuse with `city`.  
5. Join parties with normalized names.  
6. State the date range used.  
7. For Price Fetch / AMS / ADS, apply the formulas above.  
8. If category map is empty or products are unmapped, say so clearly.  
9. Never modify the database (read-only).  
10. Currency figures are typically PKR; MT is metric tons.
