"""Client-type aliases, party name search, packing/oil spoken shortcuts."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from eva_dashboard.db import connect, init_db

# Spoken / shorthand → canonical clients.type labels (as used in reports)
CLIENT_TYPE_ALIASES: dict[str, str] = {
    # Imtiaz Store
    "imtiaz": "Imtiaz Store",
    "imtiaz store": "Imtiaz Store",
    "imtiaz stores": "Imtiaz Store",
    "imtiaz's": "Imtiaz Store",
    "imtiaz's store": "Imtiaz Store",
    "store": "Imtiaz Store",
    "stores": "Imtiaz Store",
    # Eva Distributors
    "distributor": "Eva Distributors",
    "distributors": "Eva Distributors",
    "eva distributor": "Eva Distributors",
    "eva distributors": "Eva Distributors",
    "eva dist": "Eva Distributors",
    "dist": "Eva Distributors",
    # Other common types from live data / reports
    "maan distributor": "Maan Distributors",
    "maan distributors": "Maan Distributors",
    "north lmt": "NORTH LMT",
    "central lmt": "CENTRAL LMT",
    "chase up": "CHASE UP",
    "chaseup": "CHASE UP",
    "metro": "METRO HABIB",
    "metro habib": "METRO HABIB",
    "csd": "Canteen Store Department",
    "canteen": "Canteen Store Department",
    "canteen store": "Canteen Store Department",
    "canteen store department": "Canteen Store Department",
    "spar": "SPAR - IMT",
    "spar imt": "SPAR - IMT",
    "food panda": "FOOD PANDA",
    "foodpanda": "FOOD PANDA",
    "gelani": "GELANI MART",
    "gelani mart": "GELANI MART",
    "online": "Online Customer",
    "online customer": "Online Customer",
    "online custome": "Online Customer",
    "other clients": "Other Clients",
    "other client": "Other Clients",
}

PACKING_ALIASES: dict[str, str] = {
    "standup": "Stand up",
    "stand up": "Stand up",
    "stand-up": "Stand up",
    "standuppouch": "Stand up",
    "pet": "Pet bottle",
    "pet bottle": "Pet bottle",
    "pet bottles": "Pet bottle",
    "jerry": "Jerry Can",
    "jerry can": "Jerry Can",
    "j/can": "Jerry Can",
    "jcan": "Jerry Can",
    "pillow": "Pillow",
    "pouch": "Pouch",
    "pouch ghee": "Pouch (ghee)",
    "tin": "Tin",
    "tin ghee": "Tin (Ghee)",
    "bucket": "Bucket",
}

OIL_TYPE_ALIASES: dict[str, str] = {
    "canola": "Eva Canola",
    "eva canola": "Eva Canola",
    "cooking": "Eva Cooking",
    "eva cooking": "Eva Cooking",
    "sunflower": "Eva Sunflower",
    "sun": "Eva Sunflower",
    "eva sunflower": "Eva Sunflower",
    "vtf": "Eva VTF",
    "eva vtf": "Eva VTF",
    "banaspati": "Eva VTF",
}


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = t.replace("-", " ")
    t = re.sub(r"[^\w\s/]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def list_known_client_types() -> list[str]:
    """Distinct client types from clients + sales (canonical spellings)."""
    init_db()
    names: set[str] = set()
    with connect() as conn:
        for row in conn.execute(
            "SELECT DISTINCT type FROM clients WHERE type IS NOT NULL AND trim(type) != ''"
        ):
            names.add(str(row["type"]).strip())
        for row in conn.execute(
            "SELECT DISTINCT client_type FROM sales "
            "WHERE client_type IS NOT NULL AND trim(client_type) != ''"
        ):
            names.add(str(row["client_type"]).strip())
    return sorted(names, key=lambda s: s.lower())


def normalize_client_type(value: str | None) -> str | None:
    """Map spoken client-type text to a canonical label when possible."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = _norm(text)
    if key in CLIENT_TYPE_ALIASES:
        return CLIENT_TYPE_ALIASES[key]

    # Exact case-insensitive match against live types
    known = list_known_client_types()
    for name in known:
        if _norm(name) == key:
            return name

    # Fuzzy against known types (strong match only)
    best_name = None
    best_score = 0.0
    for name in known:
        score = SequenceMatcher(None, key, _norm(name)).ratio()
        if score > best_score:
            best_score = score
            best_name = name
    if best_name and best_score >= 0.82:
        return best_name
    return text


def normalize_packing_category(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return PACKING_ALIASES.get(_norm(text), text)


def normalize_oil_type(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return OIL_TYPE_ALIASES.get(_norm(text), text)


def extract_client_type_from_text(text: str) -> str | None:
    """Pull a client-type mention from free text (longest alias first)."""
    t = _norm(text or "")
    if not t:
        return None

    # Prefer multi-word / longer aliases so "canteen store" beats "store"
    aliases = sorted(CLIENT_TYPE_ALIASES.keys(), key=len, reverse=True)
    for alias in aliases:
        # word-boundary-ish: alias as whole phrase
        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
        if re.search(pattern, t):
            return CLIENT_TYPE_ALIASES[alias]

    # Live type names mentioned verbatim
    for name in list_known_client_types():
        n = _norm(name)
        if len(n) < 3:
            continue
        if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", t):
            return name
    return None


def extract_packing_from_text(text: str) -> str | None:
    t = _norm(text or "")
    aliases = sorted(PACKING_ALIASES.keys(), key=len, reverse=True)
    for alias in aliases:
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", t):
            return PACKING_ALIASES[alias]
    return None


def extract_oil_type_from_text(text: str) -> str | None:
    t = _norm(text or "")
    aliases = sorted(OIL_TYPE_ALIASES.keys(), key=len, reverse=True)
    for alias in aliases:
        if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", t):
            return OIL_TYPE_ALIASES[alias]
    return None


def _payload_fields(payload_json: str | None) -> dict[str, Any]:
    if not payload_json:
        return {}
    try:
        data = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "Locality",
        "locality",
        "Zone",
        "zone",
        "Area",
        "area",
        "Territory",
        "territory",
        "City",
        "Payment Type",
        "payment_type",
    ):
        if key in data and data[key] not in (None, ""):
            out[key.lower().replace(" ", "_")] = data[key]
    return out


def _score_name(query: str, candidate: str) -> float:
    q = _norm(query)
    c = _norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        # Containment bonus scaled by length ratio
        return 0.85 + 0.1 * min(len(q), len(c)) / max(len(q), len(c))
    # Token overlap
    q_toks = set(q.split())
    c_toks = set(c.split())
    if q_toks and q_toks <= c_toks:
        return 0.88
    ratio = SequenceMatcher(None, q, c).ratio()
    return ratio


def lookup_party(query: str, *, limit: int = 10) -> dict[str, Any]:
    """Fuzzy-search clients / sales parties and return close matches with metadata."""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "Empty query", "matches": []}

    # Strip leading "who is" / "who's" / "find"
    cleaned = re.sub(
        r"^(who\s+is|who'?s|who\s+are|find|search|lookup|tell me about)\s+",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip(" ?.,")
    if not cleaned:
        cleaned = q

    init_db()
    candidates: dict[str, dict[str, Any]] = {}

    with connect() as conn:
        client_rows = conn.execute(
            """
            SELECT client_id, client, type, city_filter, city, inactive, payload_json
            FROM clients
            WHERE client IS NOT NULL AND trim(client) != ''
            """
        ).fetchall()
        for row in client_rows:
            name = str(row["client"] or "").strip()
            if not name:
                continue
            score = _score_name(cleaned, name)
            if score < 0.45:
                continue
            extra = _payload_fields(row["payload_json"])
            candidates[name.lower()] = {
                "client": name,
                "client_type": str(row["type"] or "").strip() or None,
                "city_filter": str(row["city_filter"] or "").strip() or None,
                "city": str(row["city"] or "").strip() or None,
                "inactive": str(row["inactive"] or "").strip() or None,
                "client_id": str(row["client_id"] or "").strip() or None,
                "source": "clients",
                "match_score": round(score, 3),
                **extra,
            }

        # Also parties that appear in sales but may be missing from clients
        party_rows = conn.execute(
            """
            SELECT s.party,
                   COALESCE(NULLIF(trim(MAX(cl.type)), ''),
                            NULLIF(trim(MAX(s.client_type)), ''),
                            'Unmapped') AS client_type,
                   NULLIF(trim(MAX(cl.city_filter)), '') AS city_filter,
                   NULLIF(trim(MAX(cl.city)), '') AS city,
                   ROUND(SUM(
                     CASE
                       WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty
                       ELSE 0
                     END
                   ), 3) AS mt_total,
                   COUNT(*) AS sales_lines,
                   MIN(s.date) AS first_sale,
                   MAX(s.date) AS last_sale
            FROM sales s
            LEFT JOIN clients cl
              ON lower(trim(replace(replace(cl.client, '  ', ' '), '  ', ' ')))
               = lower(trim(replace(replace(s.party, '  ', ' '), '  ', ' ')))
            WHERE s.party IS NOT NULL AND trim(s.party) != ''
            GROUP BY s.party
            """
        ).fetchall()

        for row in party_rows:
            name = str(row["party"] or "").strip()
            if not name:
                continue
            score = _score_name(cleaned, name)
            if score < 0.45:
                continue
            key = name.lower()
            existing = candidates.get(key)
            sales_bits = {
                "mt_total": float(row["mt_total"] or 0),
                "sales_lines": int(row["sales_lines"] or 0),
                "first_sale": row["first_sale"],
                "last_sale": row["last_sale"],
            }
            if existing:
                existing.update(sales_bits)
                existing["match_score"] = max(existing["match_score"], round(score, 3))
                if not existing.get("client_type"):
                    existing["client_type"] = row["client_type"]
                if not existing.get("city_filter"):
                    existing["city_filter"] = row["city_filter"]
                if not existing.get("city"):
                    existing["city"] = row["city"]
            else:
                candidates[key] = {
                    "client": name,
                    "client_type": row["client_type"],
                    "city_filter": row["city_filter"],
                    "city": row["city"],
                    "source": "sales",
                    "match_score": round(score, 3),
                    **sales_bits,
                }

    ranked = sorted(
        candidates.values(),
        key=lambda r: (-float(r["match_score"]), -float(r.get("mt_total") or 0)),
    )[: max(1, min(int(limit or 10), 25))]

    strong = [r for r in ranked if float(r["match_score"]) >= 0.72]
    show = strong if strong else ranked[:5]

    lines = [
        f"Client search for **{cleaned}** — {len(show)} close match(es):\n",
        "| Client | Client Type | City-Filter | City | MT (all time) | Score |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in show:
        lines.append(
            "| {client} | {ctype} | {cf} | {city} | {mt} | {score} |".format(
                client=str(r.get("client") or "").replace("|", "/"),
                ctype=str(r.get("client_type") or "—").replace("|", "/"),
                cf=str(r.get("city_filter") or "—").replace("|", "/"),
                city=str(r.get("city") or "—").replace("|", "/"),
                mt=r.get("mt_total") if r.get("mt_total") is not None else "—",
                score=r.get("match_score"),
            )
        )
    if not show:
        lines = [f"No close client matches for **{cleaned}** in sales/clients."]

    return {
        "ok": True,
        "query": cleaned,
        "original_query": q,
        "matches": show,
        "all_ranked_count": len(ranked),
        "answer_markdown": "\n".join(lines) + "\n",
        "response_instructions": (
            "REQUIRED: Reply with `answer_markdown` verbatim (or expand match details "
            "without inventing clients). Report client name, client type, city, etc."
        ),
    }
