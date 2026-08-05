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
git clone -b cursor/sales-dashboard-pdf-8203 https://github.com/ssashfaque-creator/Eva-Foods-Dashboard.git
cd Eva-Foods-Dashboard
```

If you already have the folder, `cd` into it and switch to the app branch:

```bash
cd ~/Eva-Foods-Dashboard
git fetch origin
git checkout cursor/sales-dashboard-pdf-8203
git pull origin cursor/sales-dashboard-pdf-8203
```

No git? Download the branch ZIP instead:  
https://github.com/ssashfaque-creator/Eva-Foods-Dashboard/archive/refs/heads/cursor/sales-dashboard-pdf-8203.zip  
Unzip, then `cd` into that folder for the steps below.

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

### Update to latest (no git / no ZIP each time)

Stop the app (`Ctrl+C`), then:

```bash
cd ~/Eva-Foods-Dashboard
source .venv/bin/activate
eva-dashboard update
eva-dashboard app
```

Your `data/` folder (database + uploads) is kept. Caption under the title should show the new version (currently **v0.2.9**).

**First time only** (if `eva-dashboard update` is not available yet):

```bash
curl -fsSL https://raw.githubusercontent.com/ssashfaque-creator/Eva-Foods-Dashboard/cursor/sales-dashboard-pdf-8203/scripts/update.sh | bash -s -- ~/Eva-Foods-Dashboard
```

Then use `eva-dashboard update` for every future change.

### Next time you open the app

```bash
cd ~/Eva-Foods-Dashboard
source .venv/bin/activate
eva-dashboard app
```

## App tabs

### Sales data
- Upload the daily sales `.xlsx` (same format as before: **Sales** header on row 5).
- New rows are **appended**; the file is renamed/timestamped and stored under `data/uploads/sales/`.
- The same file content is **not imported twice** (SHA-256 hash).
- Upload a **category** Excel/CSV with columns **Product**, **Category 1**, **Category 2**. Each upload **replaces** the previous category map (required for reports).
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

### Reports
- **Sales dashboard PDF** — built from data already in the database (pick a report date).
- Download from the app; files are also saved under `data/reports/`.

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

1. **Sales workbook** — Sales sheet (header on row 5)  
2. **Category file** — columns `Product`, `Category 1`, `Category 2` (upload on Sales data tab; replaces previous map)  
3. **Clients workbook** — `City-Filter` is the report geography  
4. **Product cost factors** — latest `Date` per client type + product; sum `Cost` lines  
5. **Packing costs** — latest per product (`Date` or last row); join on `ProdID`

## Report contents (summary)

- Sales by Category (MT) with 30-day avg, AMS, Δ%  
- Daily / MTD Sales by City (top 10)  
- Price Fetch by Client Type — Oil (Eva), Ghee (Eva), Oil (Maan), Ghee (Maan)  
- Bulk Product Average Prices — Daily Avg + MTD Avg  

Price Fetch = `(Incl GST/FED per kg − cost factor per kg) × 37.3246` (Ltrs costs ÷ 0.915).
