# Eva Foods Dashboard

Local app for Eva Foods: upload sales, cost, and client Excel files into a SQLite database, browse/filter the data, and generate PDF sales reports.

## Install on Mac (Terminal)

1. **Install Python 3.10+** (if needed)

```bash
brew install python
```

Or download from [python.org](https://www.python.org/downloads/). Check with:

```bash
python3 --version
```

2. **Get the project**

```bash
cd ~
git clone https://github.com/ssashfaque-creator/Eva-Foods-Dashboard.git
cd Eva-Foods-Dashboard
```

If you already have the folder, `cd` into it instead.

3. **Create a virtual environment and install**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

4. **Launch the app**

```bash
eva-dashboard app
```

Your browser should open to `http://localhost:8501`.  
Leave the Terminal window open while you use the app. Stop with `Ctrl+C`.

Optional:

```bash
eva-dashboard app --port 8502
eva-dashboard app --data-dir ~/Documents/EvaFoodsData
```

Data (database + archived uploads) lives in `./data` by default, or in `EVA_DATA_DIR` / `--data-dir`.

### Next time you open the app

```bash
cd ~/Eva-Foods-Dashboard
source .venv/bin/activate
eva-dashboard app
```

## App tabs

### Sales data
- Upload the daily sales `.xlsx` (same format as before: **Sales** header on row 5, optional **Category** sheet).
- New rows are **appended**; the file is renamed/timestamped and stored under `data/uploads/sales/`.
- The same file content is **not imported twice** (SHA-256 hash).
- Browse all sales **newest first**, with search and date filters.
- **All Excel columns** are stored (in `payload_json`) for future use.

### Cost structure
- Upload **product cost factors** and **packing costs**.
- When both are present, **total factor costs** are refreshed.
- Choose a **client type** to see current Product Cost + Packing + Total Factor Cost per product.

### Client list
- Upload the clients workbook (`ClientListReport`, header row 5).
- Clients are **upserted by ClientID**; all columns are kept.
- Search / filter by type.

## PDF report (CLI)

Still available from Terminal:

```bash
eva-dashboard report /path/to/sales.xlsx --clients /path/to/clients.xlsx \
  --product-costs /path/to/product_costs.xlsx \
  --packing-costs /path/to/packing_costs.xlsx
```

```bash
eva-dashboard costs product_costs.xlsx packing_costs.xlsx -o output/total_factor_costs.csv
```

## Excel inputs

1. **Sales workbook** — Sales + Category sheets  
2. **Clients workbook** — `City-Filter` is the report geography  
3. **Product cost factors** — latest `Date` per client type + product; sum `Cost` lines  
4. **Packing costs** — latest per product (`Date` or last row); join on `ProdID`

## Report contents (summary)

- Sales by Category (MT) with 30-day avg, AMS, Δ%  
- Daily / MTD Sales by City (top 10)  
- Price Fetch by Client Type — Oil (Eva), Ghee (Eva), Oil (Maan), Ghee (Maan)  
- Bulk Product Average Prices — Daily Avg + MTD Avg  

Price Fetch = `(Incl GST/FED per kg − cost factor per kg) × 37.3246` (Ltrs costs ÷ 0.915).
