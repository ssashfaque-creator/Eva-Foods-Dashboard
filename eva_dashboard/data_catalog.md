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
| `category_1` | **Business Unit** — overall division (Eva Consumer, Eva Bulk, Maan Bulk, …) |
| `category_2` | **Oil Type** — brand/variant line (Eva Canola, Eva VTF, Maan Ghee, …) |
| `packing_category` | **Packing Category** — pack form (Tin, Pet bottle, Stand up, Jerry Can, …) |

Upload file headers (required): `Product`, `Business Unit`, `Oil Type`, `Packing Category`.  
Every category upload **replaces** the whole table.

### `clients` (upsert by ClientID)
| Column | Meaning |
|---|---|
| `client_id` | PK |
| `client` | Party name (join to sales.party via normalize) |
| `type` | Raw client type from Excel (remapped for reports — see below) |
| `city_filter` | **Report geography** (use this, not `city`) |
| `city` | Secondary city field — do **not** use for report city |
| `inactive` | `Y` / blank |
| `payload_json` | Full Excel row (Locality, Zone, CrDays, PaymentType, …) |

**Client Type groups (app logic):** raw `type` / `sales.client_type` values
are remapped via `eva_dashboard/client_type_map.py` to NEW groups
(e.g. CHASE UP/METRO/CSD/SPAR → **IMT**; NORTH/CENTRAL/SOUTH LMT → **LMT**;
Local Dealers/X-DEALERS → **Dealer**). Chatbot filters, pivots, and answers
always use the new groups — never the old long-tail labels.

**Zones (app logic, not a DB column):** each City-Filter maps to
`SOUTH` / `CENTRAL` / `NORTH` via `eva_dashboard/geo.py`.
Blank, unmapped, or `undefined` City-Filter → **Karachi** → **SOUTH**.
Chatbot can filter/pivot by `zone` the same way as `city`; after a zone
table, “city wise” nests City under Zone.

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

## Business Unit order (reports)

Formerly called Category 1. PDF summary / city pivots sort as:

1. Eva Consumer  
2. Eva Bulk  
3. Maan Consumer  
4. Maan Bulk  
5. Cusine King *(Excel spelling; "Cuisine King" normalizes to this)*  
6. Shortening  
7. Bulk Oil  
8. Meal  
9. Byproducts  

## Oil Type & Packing (examples)

**Oil Type** (`category_2`): Eva Canola, Eva Cooking, Eva Sunflower, Eva VTF, Eva VTF Bulk,
Eva DGP, Eva Navy, Eva Bulk, Maan Ghee, Maan Bulk, Canola Oil, Olein, Fatty Acid, …

**Packing Category**: Tin, Jerry Can, Pet bottle, Stand up, Pillow, Pouch, Bucket,
`16 ltr / 16 Kg`, or commodity labels for bulk lines.

Price Fetch Oil vs Ghee uses Oil Type:
- Oil examples: Eva Cooking, Eva Canola, Eva Sunflower, Maan Oil, Eva Bulk, Eva Navy  
- Ghee examples: Eva VTF, Eva VTF Bulk, Eva DGP, Maan Ghee  
- Maan Bulk → Ghee if product is Maan Banaspati, else Oil

## Category upload format

| File | Sheet | Headers | Behavior |
|---|---|---|---|
| Categories | first sheet / CSV | `Product`, `Business Unit`, `Oil Type`, `Packing Category` | Full replace |

Legacy `Product` / `Category 1` / `Category 2` files still parse (Packing blank).  

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
Uses **Oil Type** (`category_2`) — see “Oil Type & Packing” above.

### City brand columns (summary city tables)
Only: Eva Consumer, Eva Bulk, Maan Consumer, Maan Bulk.  
PDF shows top 10 cities + **Other** (remainder summed).

### Bulk average prices
Business Units: Bulk Oil, Byproducts, Meal, Shortening, Cusine King.  
MT-weighted Incl/kg; Bulk Oil × 37.3246 → per maund; others per kg.

## Excel upload formats

| Upload | Sheet / file | Header | Notes |
|---|---|---|---|
| Sales | `Sales` | Excel row 5 | Daily append |
| Categories | first sheet / CSV | `Product`, `Business Unit`, `Oil Type`, `Packing Category` | Full replace |
| Clients | `ClientListReport` | Excel row 5 | Upsert by ClientID; needs `City-Filter` |
| Product costs | `ProductCostFactors` | ~row 5 | Latest Date + PCFID per ClientType+ProdID; sum Cost |
| Packing costs | first sheet | ProdID, Cost, … | Latest per ProdID |

## App tabs

1. **Sales data** — sales upload + category upload + browse  
2. **Cost structure** — product/packing costs + factor costs browse  
3. **Client list** — client upload + browse  
4. **Reports** — generate Sales dashboard PDF from DB  
5. **AI Chat** — OpenAI assistant with read-only SQL tools over `eva.db`

## Chatbot sales matrices (query_sales)

For sales questions the assistant should call **`query_sales` once** (not multi-step SQL):

| User specifies | Rows | Columns (default) |
|---|---|---|
| Nothing (e.g. Lahore last month) | Business Unit | Client Type |
| One Business Unit (e.g. Eva Consumer) | **Packing Category** | Client Type |
| Multiple Business Units | Business Unit | (same as prior / requested) |
| Oil Type set | Packing Category | Client Type |
| Packing Category | Product | Client Type |
| Asks “city-wise” | (same row rule) | City |
| Asks “month-wise” / last N months | (same row rule) | Months + **Average** |
| **Client Type** filter (Imtiaz / Distributors / …) | (same row rule) | City (or months) |

All matrices include a **Total** footer row (column totals) and a row **Total** column.

**Client Type aliases:** Imtiaz / store(s) → `Imtiaz Store`; Distributor(s) / Eva distributors → `Eva Distributors`.  
Do not invent a Business Unit when the user only named a client type.

**Row drill-down follow-ups** (keep same filters / months via `prior_spec`):
- “show by product” / “product breakdown” / “product category” → rows = **Packing Category**
- “dissect further” / “SKU wise” / “show by SKU” → rows = **Product** (SKU)
- “by oil type” → rows = **Oil Type**; “by BU” → **Business Unit**

**Other tools:**
- `list_clients` — “Who are my distributors in Lahore?” (City-Filter + Client Type)
- `analyze_parties` — top parties/cities (default AMS), vs AMS / underperformers,
  new/lost parties, packing or SKU mix, invoices, share, YoY, doing-well, geo %
- `lookup_party` — “Who is Al Bari?” → fuzzy client/party matches (name, type, city, MT)
- `query_price` — Rate from sales; optional Price Fetch follow-up on the same scope

**Mode from language (not from filters):**
- “what were / show / breakdown” → **matrix** (one pivot)
- “how were / how are / evaluate / assess / performance / doing / trend” → **analytical**
  (city + client + AMS trend) — works for Business Unit, Oil Type, **or Packing** scope

AMS = mean of the three prior full calendar months (same filters).  
Partial month Expected = `(days_elapsed / days_in_month) × AMS`.  
Completed month: no Expected column (AMS is the baseline).

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
11. Resolve spoken product phrases via `resolve_product_language` before filtering `sales.product`.  
12. Present numeric answers as **markdown tables**, not bullet lists of metrics.  
13. Product speech rules: **16 ltr ≈ oil**, **16 kg ≈ ghee/banaspati**; VTF bulk = Eva VTF 16 Kg Tin only; canola standup pouch is the flagship canola SKU.  
14. Taxonomy: **Business Unit** / **Oil Type** / **Packing Category** — always join `category` and label columns with these names in answers.
15. Filter by **Client Type** when named; use `lookup_party` for individual client names.  
16. Rate / price → `query_price`; “Price Fetch?” follow-up reuses the prior price scope.

## Product language (spoken → exact)

Team shorthand maps to exact `sales.product` / `category.product` names. Key rules:

| Spoken | Means |
|--------|--------|
| shortening / bake right | BakeRight Shortening 16 Kgs Ctn |
| cuisine / cusine king | Cuisine King (16 Ltr Tin) |
| canola standup / flagship | Eva Canola Oil (StandUpPouch) |
| cooking pillow | Eva Cooking Oil 1x5 Pillow Pouch |
| cooking jerry can / 16 jerry | Eva Cooking Oil 16 Ltr J/Can |
| sun / sunflower + pet/pillow/standup | Eva Sunflower Oil packs |
| VTF bulk / VTF 16 kg | Eva VTF Banaspati 16 Kg Tin only |
| VTF pouch / 1x5 / 1x16 / 1x4 / 5 kg tin | other VTF consumer packs |
| maan 16 kg / maan ghee | Maan Banaspati (kg) — not oil |
| eva 16 ltr / maan 16 ltr | oil tins/jerry (not kg ghee) |
| jerry can (maan) | Maan Cooking Oil jerry packs |
| pet / pet bottle | 3 Ltr or 5 Ltr PET bottles |

Always join `category` for Category 1 / Category 2 when answering product questions.

