"""Product language glossary and natural-language → exact product resolver."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

from eva_dashboard.db import connect, init_db

# Canonical product name → spoken aliases used by Eva Foods team
PRODUCT_ALIASES: dict[str, list[str]] = {
    "BakeRight Shortening 16 Kgs Ctn": [
        "shortening",
        "bake right",
        "bakeright",
        "bake-right",
        "bake right shortening",
    ],
    "Cuisine King (16 Ltr Tin)": [
        "cusine king",
        "cuisine king",
        "cuisine",
        "cusine",
        "cuisine king tin",
        "cusine king tin",
    ],
    "Eva Canola Oil (10 Litrs J/Can)": [
        "eva canola jerry can",
        "canola jerry can",
        "canola j/can 10",
        "canola 10 ltr jerry",
        "eva canola 10 jerry",
    ],
    "Eva Canola Oil (3 Ltr Bottle)": [
        "eva canola pet bottle 3 ltr",
        "canola 3 ltr pet",
        "canola 3ltr pet bottle",
        "eva canola 3 ltr bottle",
        "canola 3 pet",
    ],
    "Eva Canola Oil (5 Ltr Bottle)": [
        "eva canola pet bottle 5ltr",
        "eva canola pet bottle 5 ltr",
        "canola 5 ltr pet",
        "canola 5ltr pet bottle",
        "eva canola 5 ltr bottle",
        "canola 5 pet",
    ],
    "Eva Canola Oil (StandUpPouch)": [
        "canola standup pouch",
        "canola stand up pouch",
        "canola stand-up pouch",
        "standup pouch",
        "stand up pouch",
        "canola pouch",
        "eva canola standup",
        "flagship",
        "main canola",
        "canola flagship",
    ],
    "Eva Canola Oil 16 Ltr Tin": [
        "canola oil bulk tin",
        "canola 16 ltr tin",
        "canola 16 liter tin",
        "eva canola 16 tin",
        "eva 16 ltr canola",
    ],
    "Eva Cooking Oil (16 Ltr Tin)": [
        "cooking tin",
        "cooking 16 ltr tin",
        "eva cooking 16 tin",
        "eva cooking oil tin",
        "eva 16 ltr cooking",
        "cooking oil bulk tin",
    ],
    "Eva Cooking Oil (3 Ltr Bottle)": [
        "cooking 3 ltr pet bottle",
        "cooking 3ltr pet",
        "eva cooking 3 ltr",
        "cooking 3 pet",
    ],
    "Eva Cooking Oil (5 Ltr Bottle)": [
        "5 ltr pet bottle eva cooking",
        "cooking 5 ltr pet bottle",
        "cooking 5ltr pet",
        "eva cooking 5 ltr",
        "cooking 5 pet",
    ],
    "Eva Cooking Oil (StandUpPouch)": [
        "cooking stand up pouch",
        "cooking standup pouch",
        "cooking stand-up pouch",
        "eva cooking standup",
        "cooking pouch",
    ],
    "Eva Cooking Oil 16 Ltr J/Can": [
        "cooking jerry can 16 ltr",
        "cooking jerry can",
        "cooking j/can",
        "eva cooking jerry can",
        "cooking 16 jerry",
        "cooking bulk jerry",
    ],
    "Eva Cooking Oil 1x5 Pillow Pouch": [
        "cooking pillow",
        "cooking pillow pouch",
        "eva cooking pillow",
        "cooking 1x5 pillow",
    ],
    "Eva Sunflower Oil 1X5 Pouch (P.P)": [
        "sun pillow pouch",
        "sunflower pillow pouch",
        "sun pillow",
        "sunflower pillow",
        "sun p.p",
        "sunflower pp pouch",
    ],
    "Eva Sunflower Oil 1x5 (Standup Pouch)": [
        "sun standup",
        "sunflower standup",
        "sun stand up",
        "sunflower stand up pouch",
        "sun standup pouch",
    ],
    "Eva Sunflower Oil 3 Ltr PetBottle": [
        "sun 3 ltr pet bottle",
        "sun 3 ltr pet",
        "sunflower 3 ltr pet",
        "sun 3 pet",
        "sunflower 3 pet",
    ],
    "Eva Sunflower Oil 5 Ltr Pet Bottle": [
        "sun 5 ltr pet",
        "sunflower 5 ltr pet",
        "sun 5 pet",
        "sunflower 5 pet bottle",
    ],
    "Eva VTF Banaspati 1 x 4 Kg Tin": [
        "vtf 1 kg tin",
        "vtf 1 by 4",
        "vtf 1x4",
        "vtf 1 x 4",
        "vtf 1x4 tin",
        "vtf consumer 1x4",
    ],
    "Eva VTF Banaspati 16 Kg Tin": [
        "vtf bulk",
        "vtf 16 kg",
        "vtf 16kg tin",
        "vtf 16 kg tin",
        "eva vtf bulk",
        "vtf banaspati bulk",
    ],
    "Eva VTF Banaspati 1x16 Pouch": [
        "vtf pouch 1 x 16",
        "vtf 1x16 pouch",
        "vtf 1x16",
        "vtf pouch 1x16",
    ],
    "Eva VTF Banaspati 1x5 Pouch": [
        "vtf pouch",
        "vtf 1 by 5",
        "vtf 1x5",
        "vtf1 x 5",
        "vtf 1 x 5",
        "vtf pouch 1x5",
    ],
    "Eva VTF Banaspati 5 Kg Tin": [
        "vtf tin 5 kg",
        "vtf 5 kg tin",
        "vtf 5kg tin",
        "vtf 5 tin",
    ],
    "Maan Banaspati 1 X 10 P.Pouch": [
        "maan pillow pouch 1 x 10",
        "maan pillow 1x10",
        "maan 1x10 pillow",
        "maan banaspati pillow",
    ],
    "Maan Banaspati 1/2 X 24": [
        "maan half x 24",
        "maan 1/2 x 24",
        "maan half 24",
    ],
    "Maan Banaspati 1/2 X 32": [
        "maan half x 32",
        "maan 1/2 x 32",
        "maan half 32",
    ],
    "Maan Banaspati 10 Kg Bucket": [
        "maan bucket 10 kg",
        "maan 10 kg bucket",
        "maan 10kg bucket",
    ],
    "Maan Banaspati 16 Kg Bucket": [
        "maan 16 kg bucket",
        "maan 16kg bucket",
        "maan bucket 16",
    ],
    "Maan Banaspati 16 Kgs Tin": [
        "maan 16 kg tin",
        "maan ghee 16 kg tin",
        "maan banaspati 16 kg tin",
        "maan 16kg tin",
        "maan ghee 16",
    ],
    "Maan Banaspati 1X12": [
        "maan ghee 1 x 12",
        "maan banaspati 1 x 12",
        "maan 1x12",
        "maan ghee 1x12",
    ],
    "Maan Banaspati 1X5": [
        "maan ghee 1 x 5",
        "maan ghe 1 x 5",
        "maan 1x5",
        "maan banaspati 1x5",
        "maan ghee 1x5",
    ],
    "Maan Banaspati 1x16 Pouch": [
        "maan banaspati 1x16 pouch",
        "maan 1x16 pouch",
        "maan ghee 1x16 pouch",
    ],
    "Maan Banaspati 2.5 Kgs Tin": [
        "maan 2.5 kg tin",
        "maan 2.5kgs tin",
        "maan banaspati 2.5",
    ],
    "Maan Banaspati 5 Kg Bucket": [
        "maan 5 kg bucket",
        "maan 5kg bucket",
        "maan bucket 5",
    ],
    "Maan Banaspati 5 Kgs Tin": [
        "maan 5 kg tin",
        "maan 5kgs tin",
        "maan banaspati 5 tin",
    ],
    "Maan Cooking Oil (10 Ltrs J/Can)": [
        "maan jerry can",
        "maan jerry can 10",
        "maan cooking jerry can",
        "jerry can maan",
        "maan 10 jerry",
    ],
    "Maan Cooking Oil 1 X 12 Pouch": [
        "maan cooking 1 x 12 pouch",
        "maan oil 1x12 pouch",
        "maan 1x12 oil pouch",
    ],
    "Maan Cooking Oil 1 X 5 Pouch": [
        "maan cooking 1 x 5 pouch",
        "maan oil 1x5 pouch",
        "maan 1x5 oil pouch",
    ],
    "Maan Cooking Oil 16 Ltrs. Tin": [
        "maan 16 ltr tin",
        "maan cooking 16 tin",
        "maan oil 16 ltr tin",
        "maan 16 liter tin",
        "maan 16 ltrs tin",
    ],
    "Maan Cooking Oil 1x10 Pouch": [
        "maan 1 x 10 pouch",
        "maan oil 1x10 pouch",
        "1 x 10 pouch maan",
    ],
    "Maan Cooking Oil 1x16 Pouch": [
        "maan pouches",
        "maan oil pouches",
        "maan 1x16 pouch",
        "maan cooking 1x16 pouch",
    ],
    "Maan Cooking Oil 3 Ltr Pet Bottle": [
        "maan 3 ltr pet bottle",
        "maan 3 pet",
        "maan cooking 3 ltr",
    ],
    "Maan Cooking Oil 5 Ltr Pet Bottle": [
        "maan pet bottle",
        "maan 5 ltr pet bottle",
        "maan 5 pet",
        "maan cooking 5 ltr",
    ],
    "Maan Oil 16 Ltrs. J/Can": [
        "jerry can 16 ltr",
        "maan jerry can 16",
        "maan oil 16 jerry",
        "maan 16 ltr jerry can",
    ],
}

# Global spoken rules (applied when resolving language)
LANGUAGE_RULES = """
PRODUCT LANGUAGE RULES (Eva Foods team speech):
1. "16 ltr" / "16 liter" / "16 litre" almost always means OIL (tin or jerry can).
2. "16 kg" / "16kg" almost always means GHEE / BANASPATI (not oil).
   - "Eva 16 ltr" → Eva cooking/canola oil 16 Ltr pack (ask which if ambiguous).
   - "Maan 16 kg" → Maan Banaspati / ghee 16 kg (tin or bucket).
3. "StandUpPouch" / "standup" / "stand up" for Eva Canola is the FLAGSHIP / main canola
   product: exact name "Eva Canola Oil (StandUpPouch)".
4. "VTF bulk" means ONLY "Eva VTF Banaspati 16 Kg Tin" (Business Unit Eva Bulk,
   Oil Type Eva VTF Bulk). Other VTF SKUs are Eva Consumer packs.
5. "Pet" / "pet bottle" = Packing Category Pet bottle (3 Ltr / 5 Ltr).
6. "Jerry can" / "J/Can" / "jerrycan" = Packing Category Jerry Can.
7. "Pillow" / "P.P" / "PP pouch" = Packing Category Pillow.
8. "Shortening" / "Bake Right" / "BakeRight" = BakeRight Shortening 16 Kgs Ctn
   (Business Unit Shortening).
9. "Cusine King" / "Cuisine King" / "cuisine" = Cuisine King (16 Ltr Tin)
   (Business Unit stored as "Cusine King" in reports).
10. "Sun" usually means Eva Sunflower Oil (Oil Type Eva Sunflower).
11. "Cooking" without brand usually means Eva Cooking Oil (Oil Type Eva Cooking).
12. "Maan ghee" / "maan banaspati" = Maan Banaspati (Oil Type Maan Ghee or Maan Bulk).
13. Always resolve spoken phrases to EXACT `sales.product` names, then join
    `category` for Business Unit / Oil Type / Packing Category.
"""

TAXONOMY_RULES = """
PRODUCT TAXONOMY (category table — three levels):
1. Business Unit (DB column category_1) — overall division, used for PDF summary /
   city brand pivots. Examples: Eva Consumer, Eva Bulk, Maan Consumer, Maan Bulk,
   Cusine King, Shortening, Bulk Oil, Meal, Byproducts.
2. Oil Type (DB column category_2) — brand/variant line. Examples: Eva Canola,
   Eva Cooking, Eva Sunflower, Eva VTF, Eva VTF Bulk, Eva DGP, Eva Navy, Eva Bulk,
   Maan Ghee, Maan Bulk, Canola Oil, Olein, Fatty Acid.
3. Packing Category (DB column packing_category) — pack form. Examples: Tin,
   Jerry Can, Pet bottle, Stand up, Pillow, Pouch, Bucket, 16 ltr / 16 Kg.

Join: sales.product = category.product (exact text).
When the user says "Eva Consumer", filter category_1 / Business Unit.
When they say "Eva Canola" or "canola", prefer Oil Type = Eva Canola (then SKUs).
When they say "pet bottle" / "jerry can" / "standup", filter Packing Category.
"""


def _norm(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("litres", "ltr").replace("liters", "ltr").replace("liter", "ltr")
    t = t.replace("litrs", "ltr").replace("ltrs", "ltr")
    t = t.replace("kilograms", "kg").replace("kilos", "kg").replace("kgs", "kg")
    t = t.replace("stand-up", "standup").replace("stand up", "standup")
    t = t.replace("j/can", "jerry can").replace("jerrycan", "jerry can")
    t = t.replace("petbottle", "pet bottle").replace("pet-bottle", "pet bottle")
    t = t.replace("banaspati", "banaspati").replace("ghee", "ghee")
    t = t.replace("cusine", "cuisine")
    t = re.sub(r"[^a-z0-9./\s]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def load_products_from_db() -> pd.DataFrame:
    """Return product taxonomy from category table (fallback to alias keys)."""
    init_db()
    with connect() as conn:
        frame = pd.read_sql_query(
            """
            SELECT
              product,
              category_1 AS category1,
              category_2 AS category2,
              COALESCE(packing_category, '') AS packing_category
            FROM category
            ORDER BY product
            """,
            conn,
        )
    if frame.empty:
        frame = pd.DataFrame(
            {
                "product": list(PRODUCT_ALIASES.keys()),
                "category1": "",
                "category2": "",
                "packing_category": "",
            }
        )
    frame["business_unit"] = frame["category1"]
    frame["oil_type"] = frame["category2"]
    return frame


@dataclass
class ProductMatch:
    product: str
    category1: str
    category2: str
    packing_category: str
    score: float
    matched_via: str


def resolve_product_language(query: str, limit: int = 8) -> dict[str, Any]:
    """Map spoken product language to exact product names + categories."""
    q = _norm(query)
    products = load_products_from_db()
    # Ensure alias-known products appear even if not currently in DB category map
    known = set(products["product"].astype(str))
    for name in PRODUCT_ALIASES:
        if name not in known:
            products = pd.concat(
                [
                    products,
                    pd.DataFrame(
                        [
                            {
                                "product": name,
                                "category1": "",
                                "category2": "",
                                "packing_category": "",
                                "business_unit": "",
                                "oil_type": "",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )

    matches: list[ProductMatch] = []
    q_tokens = set(q.split())

    def _brand_ok(product_norm: str) -> bool:
        """If the user named a brand, the product must carry it."""
        if "maan" in q_tokens and "maan" not in product_norm:
            return False
        if "eva" in q_tokens and "eva" not in product_norm:
            return False
        if "vtf" in q_tokens and "vtf" not in product_norm:
            return False
        if ("cuisine" in q_tokens or "cusine" in q_tokens) and "cuisine" not in product_norm:
            return False
        return True

    def _row_fields(product: str) -> tuple[str, str, str]:
        row = products.loc[products["product"] == product]
        if not len(row):
            return "", "", ""
        r0 = row.iloc[0]
        return (
            str(r0.get("category1") or ""),
            str(r0.get("category2") or ""),
            str(r0.get("packing_category") or ""),
        )

    # 1) Alias phrase hits
    for product, aliases in PRODUCT_ALIASES.items():
        pn = _norm(product)
        if not _brand_ok(pn):
            continue
        best_alias = None
        best_len = 0
        best_mode = ""
        for alias in aliases:
            an = _norm(alias)
            if not an:
                continue
            if an == q:
                best_len = len(an) + 20
                best_alias = alias
                best_mode = "exact"
                break
            if an in q and len(an) >= best_len:
                best_len = len(an)
                best_alias = alias
                best_mode = "alias_in_query"
            elif q in an and len(q) >= max(8, int(len(an) * 0.85)) and len(an) >= best_len:
                best_len = len(an)
                best_alias = alias
                best_mode = "query_in_alias"
        if best_alias is not None:
            c1, c2, pack = _row_fields(product)
            score = 80.0 + min(best_len, 40)
            if best_mode == "exact":
                score += 25
            if q in {"vtf pouch", "vtf pouches"} and product == "Eva VTF Banaspati 1x5 Pouch":
                score += 30
            if "standup" in _norm(best_alias) and "canola" in pn:
                score += 15
            if "vtf bulk" in q and "16 Kg Tin" in product:
                score += 20
            # Packing-language boosts
            pack_n = _norm(pack)
            if "jerry" in q and "jerry" in pack_n:
                score += 12
            if "pet" in q and "pet" in pack_n:
                score += 12
            if "pillow" in q and "pillow" in pack_n:
                score += 12
            if "standup" in q and ("stand" in pack_n or "standup" in pack_n):
                score += 12
            if "bucket" in q and "bucket" in pack_n:
                score += 12
            if "tin" in q and "tin" in pack_n:
                score += 8
            matches.append(
                ProductMatch(product, c1, c2, pack, score, f"alias:{best_alias}")
            )

    # 2) Token overlap against official names + packing labels
    for _, row in products.iterrows():
        product = str(row["product"])
        pn = _norm(product)
        pack = str(row.get("packing_category") or "")
        pack_n = _norm(pack)
        oil_n = _norm(str(row.get("category2") or ""))
        p_tokens = set(pn.split()) | set(pack_n.split()) | set(oil_n.split())
        if not p_tokens or not _brand_ok(pn):
            continue
        overlap = len(q_tokens & p_tokens) / max(len(q_tokens), 1)
        if overlap >= 0.4 or (q and q in pn):
            score = 40.0 + overlap * 40.0
            if "16 ltr" in q and ("16 ltr" in pn or "16 litr" in pn):
                score += 25
            if "16 kg" in q and "16 kg" in pn:
                score += 25
            if "16 ltr" in q and "16 kg" in pn:
                score -= 30
            if "16 kg" in q and "16 ltr" in pn:
                score -= 30
            if "vtf bulk" in q and product == "Eva VTF Banaspati 16 Kg Tin":
                score += 40
            if "flagship" in q and "StandUpPouch" in product and "Canola" in product:
                score += 40
            matches.append(
                ProductMatch(
                    product,
                    str(row.get("category1") or ""),
                    str(row.get("category2") or ""),
                    pack,
                    score,
                    "name_tokens",
                )
            )

    # Dedupe by product keeping best score
    best: dict[str, ProductMatch] = {}
    for m in matches:
        prev = best.get(m.product)
        if prev is None or m.score > prev.score:
            best[m.product] = m

    ranked = sorted(best.values(), key=lambda m: (-m.score, m.product))[:limit]
    return {
        "query": query,
        "normalized_query": q,
        "language_rules_applied": True,
        "matches": [
            {
                "product": m.product,
                "business_unit": m.category1,
                "oil_type": m.category2,
                "packing_category": m.packing_category,
                "category1": m.category1,
                "category2": m.category2,
                "score": round(m.score, 1),
                "matched_via": m.matched_via,
            }
            for m in ranked
        ],
        "top_product": ranked[0].product if ranked else None,
        "hint": (
            "Use top_product (or ask user to confirm if multiple close scores) "
            "as the exact sales.product filter. Include Business Unit / Oil Type / "
            "Packing Category in markdown table answers."
        ),
    }


def product_sales(
    *,
    product: str | None = None,
    product_query: str | None = None,
    date_from: str,
    date_to: str,
    city: str | None = None,
) -> dict[str, Any]:
    """Sales for one exact product (or resolved from spoken query)."""
    resolution = None
    exact = (product or "").strip()
    if not exact and product_query:
        resolution = resolve_product_language(product_query, limit=5)
        exact = resolution.get("top_product") or ""
    if not exact:
        return {
            "ok": False,
            "error": "Could not resolve a product. Pass product or product_query.",
            "resolution": resolution,
        }

    init_db()
    params: list[Any] = [exact, date_from, date_to]
    sql = """
    SELECT
      s.product,
      COALESCE(c.category_1, '') AS business_unit,
      COALESCE(c.category_2, '') AS oil_type,
      COALESCE(c.packing_category, '') AS packing_category,
      COALESCE(c.category_1, '') AS category1,
      COALESCE(c.category_2, '') AS category2,
      COUNT(*) AS lines,
      COUNT(DISTINCT s.party) AS parties,
      COUNT(DISTINCT s.date) AS days,
      ROUND(SUM(
        CASE
          WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
          WHEN lower(trim(COALESCE(s.unit,''))) IN ('kg','kgs')
            THEN COALESCE(s.qty,0)/1000.0
          WHEN lower(trim(COALESCE(s.unit,''))) IN
               ('mt','m.t','m.t.','ton','tons','tonne','tonnes')
            THEN COALESCE(s.qty,0)
          ELSE 0
        END
      ), 3) AS mt,
      ROUND(SUM(COALESCE(s.qty,0)), 2) AS qty,
      ROUND(SUM(COALESCE(s.incl_gst_fed_amount,0)), 2) AS incl_gst_fed_amount,
      ROUND(SUM(COALESCE(s.basic_amount,0)), 2) AS basic_amount
    FROM sales s
    LEFT JOIN category c ON c.product = s.product
    LEFT JOIN clients cl ON lower(trim(cl.client)) = lower(trim(s.party))
    WHERE s.product = ?
      AND s.date >= ? AND s.date <= ?
    """
    if city:
        sql += " AND lower(trim(COALESCE(cl.city_filter, ''))) = lower(trim(?))"
        params.append(city)
    sql += " GROUP BY s.product, c.category_1, c.category_2, c.packing_category"

    with connect() as conn:
        summary = pd.read_sql_query(sql, conn, params=params)
        by_day = pd.read_sql_query(
            """
            SELECT s.date,
                   COUNT(*) AS lines,
                   ROUND(SUM(COALESCE(s.mt_qty,0)), 3) AS mt,
                   ROUND(SUM(COALESCE(s.incl_gst_fed_amount,0)), 2) AS incl_gst_fed_amount
            FROM sales s
            LEFT JOIN clients cl ON lower(trim(cl.client)) = lower(trim(s.party))
            WHERE s.product = ?
              AND s.date >= ? AND s.date <= ?
            """
            + (
                " AND lower(trim(COALESCE(cl.city_filter,''))) = lower(trim(?))"
                if city
                else ""
            )
            + """
            GROUP BY s.date
            ORDER BY s.date
            """,
            conn,
            params=params,
        )
        top_parties = pd.read_sql_query(
            """
            SELECT s.party,
                   COUNT(*) AS lines,
                   ROUND(SUM(COALESCE(s.mt_qty,0)), 3) AS mt,
                   ROUND(SUM(COALESCE(s.incl_gst_fed_amount,0)), 2) AS incl_gst_fed_amount
            FROM sales s
            LEFT JOIN clients cl ON lower(trim(cl.client)) = lower(trim(s.party))
            WHERE s.product = ?
              AND s.date >= ? AND s.date <= ?
            """
            + (
                " AND lower(trim(COALESCE(cl.city_filter,''))) = lower(trim(?))"
                if city
                else ""
            )
            + """
            GROUP BY s.party
            ORDER BY mt DESC
            LIMIT 15
            """,
            conn,
            params=params,
        )

    return {
        "ok": True,
        "product": exact,
        "date_from": date_from,
        "date_to": date_to,
        "city": city,
        "resolution": resolution,
        "summary": json_records(summary),
        "by_day": json_records(by_day),
        "top_parties": json_records(top_parties),
        "response_format_hint": (
            "Present summary as a markdown table with columns: "
            "Product | Business Unit | Oil Type | Packing | MT | Qty | "
            "Incl GST/FED | Lines | Parties | Days. "
            "Then optional top parties table."
        ),
    }


def json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    import json

    if frame is None or frame.empty:
        return []
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def glossary_for_prompt() -> str:
    lines = [
        TAXONOMY_RULES.strip(),
        "",
        LANGUAGE_RULES.strip(),
        "",
        "PRODUCT ALIAS GLOSSARY (spoken → exact name):",
    ]
    for product, aliases in PRODUCT_ALIASES.items():
        alias_txt = "; ".join(aliases[:6])
        lines.append(f"- {product} ← {alias_txt}")
    return "\n".join(lines)
