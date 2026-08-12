"""Client-type aliases, party name search, packing/oil spoken shortcuts."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from eva_dashboard.client_type_map import (
    CLIENT_TYPE_GROUP,
    canonical_raw_client_type,
    classify_client_type_filter,
    is_specific_raw_client_type,
    list_new_client_types,
    map_client_type,
)
from eva_dashboard.db import connect, init_db

# Spoken / shorthand → filter label.
# Specific old names stay specific (Chase Up → CHASE UP). Broad group words
# (imt, lmt, dealers) resolve to the NEW reporting group.
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
    # Maan
    "maan distributor": "Maan Distributors",
    "maan distributors": "Maan Distributors",
    # Specific LMT / modern-trade sources (keep narrow when named)
    "north lmt": "NORTH LMT",
    "central lmt": "CENTRAL LMT",
    "south lmt": "SOUTH LMT",
    "gelani": "GELANI MART",
    "gelani mart": "GELANI MART",
    "al fateh": "AL FATEH",
    "modern trade": "Modren Trade Customers",
    "modren trade": "Modren Trade Customers",
    "modern trade customers": "Modren Trade Customers",
    "modren trade customers": "Modren Trade Customers",
    # Broad LMT group
    "lmt": "LMT",
    # Specific IMT sources
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
    "maf": "MAF Hypermarkets",
    "maf hypermarkets": "MAF Hypermarkets",
    # Broad IMT group
    "imt": "IMT",
    # Other
    "food panda": "FOOD PANDA",
    "foodpanda": "FOOD PANDA",
    "online": "Online Customers",
    "online customer": "Online Customers",
    "online customers": "Online Customers",
    "online custome": "Online Customers",
    "other clients": "Other Clients",
    "other client": "Other Clients",
    "direct customers": "Direct Customers",
    "direct customer": "Direct Customers",
    "dealer": "Dealer",
    "dealers": "Dealer",
    "dgp": "DGP",
    "dgp army": "DGP ARMY",
    "pak navy": "PAK NAVY",
    "usc": "USC",
    "utility stores": "Utility Stores Corporation",
    "utility store": "Utility Stores Corporation",
    "utility stores corporation": "Utility Stores Corporation",
    "donations": "DONATIONS",
    "donation": "DONATIONS",
    "donation sales": "DONATIONS",
    "donations sales": "DONATIONS",
    "donation sale": "DONATIONS",
    "madarsa": "Madarsa",
    "madrasa": "Madarsa",
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
    """Known filter labels: NEW groups + specific source types from live data."""
    init_db()
    names: set[str] = set(list_new_client_types())
    names.update(CLIENT_TYPE_GROUP.keys())
    with connect() as conn:
        for row in conn.execute(
            "SELECT DISTINCT type FROM clients WHERE type IS NOT NULL AND trim(type) != ''"
        ):
            raw = str(row["type"]).strip()
            if raw:
                names.add(canonical_raw_client_type(raw) or raw)
                mapped = map_client_type(raw)
                if mapped:
                    names.add(mapped)
        for row in conn.execute(
            "SELECT DISTINCT client_type FROM sales "
            "WHERE client_type IS NOT NULL AND trim(client_type) != ''"
        ):
            raw = str(row["client_type"]).strip()
            if raw:
                names.add(canonical_raw_client_type(raw) or raw)
                mapped = map_client_type(raw)
                if mapped:
                    names.add(mapped)
    return sorted(names, key=lambda s: s.lower())


def match_client_type_alias(value: str | None) -> str | None:
    """Strict channel match — aliases / exact known types only (no fuzzy).

    Use before party ILIKE so \"metro\" / \"metro habib\" / \"lmt\" /
    \"chase up\" become ``filters.client_type``, not customer-name search.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = _norm(text)
    if not key:
        return None
    if key in CLIENT_TYPE_ALIASES:
        return CLIENT_TYPE_ALIASES[key]
    # Exact source spelling from the ops mapping (METRO HABIB, NORTH LMT, …)
    # Do NOT use canonical_raw_client_type — it passes unknown text through.
    for src in CLIENT_TYPE_GROUP:
        if _norm(src) == key:
            return src
    # Exact new group (IMT, LMT, Dealer, …)
    for g in list_new_client_types():
        if _norm(g) == key:
            return g
    return None


def normalize_client_type(value: str | None) -> str | None:
    """Resolve spoken / raw client-type text for filtering.

    Specific old names stay specific (``chase up`` → ``CHASE UP``).
    Broad group words stay as new groups (``imt`` → ``IMT``).
    Use ``map_client_type`` when pivoting/displaying by channel.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = _norm(text)
    if key in CLIENT_TYPE_ALIASES:
        return CLIENT_TYPE_ALIASES[key]

    # Exact match on a known source spelling → keep specific
    raw = canonical_raw_client_type(text)
    if raw and is_specific_raw_client_type(raw):
        return raw

    # Bare new-group name
    classified = classify_client_type_filter(text)
    if classified and classified[0] == "group":
        # Only treat as group when input itself is the group (or identity)
        if not is_specific_raw_client_type(text):
            return classified[1]

    # Exact / fuzzy against known labels (prefer longer / specific)
    known = list_known_client_types()
    for name in known:
        if _norm(name) == key:
            return name

    best_name = None
    best_score = 0.0
    for name in known:
        score = SequenceMatcher(None, key, _norm(name)).ratio()
        if score > best_score:
            best_score = score
            best_name = name
    if best_name and best_score >= 0.82:
        return best_name
    return raw or text


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


def extract_oil_and_packing(text: str | None) -> tuple[str | None, str | None]:
    """Split composite spoken SKUs like ``canola standup`` → oil + packing.

    Longest packing alias wins; remaining tokens resolve via oil aliases.
    Returns ``(oil_type, packing_category)`` — either may be None.
    """
    raw = (text or "").strip()
    if not raw:
        return None, None
    key = _norm(raw)
    if not key:
        return None, None
    if key in OIL_TYPE_ALIASES and key not in PACKING_ALIASES:
        return OIL_TYPE_ALIASES[key], None
    if key in PACKING_ALIASES and key not in OIL_TYPE_ALIASES:
        return None, PACKING_ALIASES[key]

    pack: str | None = None
    tokens = key.split()
    for spoken in sorted(PACKING_ALIASES, key=len, reverse=True):
        spoken_toks = spoken.split()
        if not spoken_toks:
            continue
        for i in range(len(tokens) - len(spoken_toks) + 1):
            if tokens[i : i + len(spoken_toks)] == spoken_toks:
                pack = PACKING_ALIASES[spoken]
                tokens = tokens[:i] + tokens[i + len(spoken_toks) :]
                break
        if pack:
            break

    rest = " ".join(tokens).strip()
    oil: str | None = None
    if rest:
        if rest in OIL_TYPE_ALIASES:
            oil = OIL_TYPE_ALIASES[rest]
        else:
            # Canonical oil labels already in the phrase
            canon_oils = {_norm(v): v for v in OIL_TYPE_ALIASES.values()}
            oil = canon_oils.get(rest) or OIL_TYPE_ALIASES.get(rest)
    return oil, pack


_GENERIC_CLIENT_ALIASES = {
    "store",
    "stores",
    "dist",
    "distributor",
    "distributors",
}

# Bare "distributor(s)" often means party grain ("distributor-wise"), not the
# Eva Distributors channel. Specific names ("Eva Distributors", "Maan
# distributors") still resolve to the channel.
_DISTRIBUTOR_CHANNEL_ALIASES = {
    "distributor",
    "distributors",
    "dist",
}


def is_distributor_party_grain(text: str) -> bool:
    """True when "distributor(s)" means party rows / ranking grain, not channel.

    Grain (do NOT invent Eva Distributors):
      "distributor wise", "by distributor", "party-wise",
      "individual distributors"

    Channel (still Eva Distributors):
      "distributor sales", "top distributors", "distributor performance",
      "which distributors are performing poorly"
    """
    t = (text or "").lower()
    if not t:
        return False
    return bool(
        re.search(
            r"\bdistributors?\s*[- ]?\s*wise\b|"
            r"\bparty\s*[- ]?\s*wise\b|"
            r"\bby\s+distributors?\b|"
            r"\bindividual\s+distributors?\b",
            t,
        )
    )


def extract_all_client_types_from_text(text: str) -> list[str]:
    """All distinct client types mentioned, in order of appearance.

    Longer / specific aliases win on overlapping spans (so ``imtiaz stores``
    is Imtiaz, not a bare ``stores`` hit). Non-overlapping generics still
    count — ``Imtiaz vs distributors`` → both types. Used for multi-type
    compares (Imtiaz vs Metro vs Chase Up).

    Grain language ("distributor wise", "lowest performing distributors") does
    **not** invent the Eva Distributors channel — only explicit channel names
    like "Eva Distributors" / "Maan distributors" do.
    """
    t = _norm(text or "")
    if not t:
        return []

    skip_bare_distributor = is_distributor_party_grain(text)
    hits: list[tuple[int, int, str]] = []  # start, end, canon
    # Longest alias first so specific phrases claim their span before generics
    aliases = sorted(CLIENT_TYPE_ALIASES.keys(), key=len, reverse=True)
    for alias in aliases:
        if skip_bare_distributor and alias in _DISTRIBUTOR_CHANNEL_ALIASES:
            continue
        pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
        for m in re.finditer(pattern, t):
            start, end = m.start(), m.end()
            if any(not (end <= s or start >= e) for s, e, _ in hits):
                continue
            hits.append((start, end, CLIENT_TYPE_ALIASES[alias]))

    # Live type / group names not already covered
    covered = {c for _, _, c in hits}
    try:
        known = list_known_client_types()
    except Exception:
        known = []
    for name in known:
        n = _norm(name)
        if len(n) < 3 or name in covered:
            continue
        for m in re.finditer(r"(?<!\w)" + re.escape(n) + r"(?!\w)", t):
            start, end = m.start(), m.end()
            if any(not (end <= s or start >= e) for s, e, _ in hits):
                continue
            hits.append((start, end, name))
            covered.add(name)

    hits.sort(key=lambda h: h[0])
    out: list[str] = []
    for _, _, canon in hits:
        if canon not in out:
            out.append(canon)
    return out


def extract_client_type_from_text(text: str) -> str | None:
    """Pull a client-type mention from free text (longest alias first)."""
    found = extract_all_client_types_from_text(text)
    return found[0] if found else None


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
    # Token overlap — "al shaheer" vs "AL SHAHEER CORPORATION LIMITED"
    q_toks = [t for t in q.split() if len(t) > 1]
    c_toks = set(c.split())
    if q_toks and set(q_toks) <= c_toks:
        return 0.88
    if q_toks:
        # Each query token that appears as a substring of the candidate
        hits = sum(1 for t in q_toks if t in c)
        if hits == len(q_toks) and hits >= 1:
            return 0.82 + 0.05 * min(hits, 3)
        if hits >= 1:
            partial = 0.40 + 0.40 * (hits / len(q_toks))
            ratio = SequenceMatcher(None, q, c).ratio()
            return max(partial, ratio)
    ratio = SequenceMatcher(None, q, c).ratio()
    return ratio


def _sales_stats_for_parties(
    conn: Any,
    names: list[str],
) -> dict[str, dict[str, Any]]:
    """Cheap per-party MT stats for a small name list (no full sales scan)."""
    clean = [str(n).strip() for n in names if str(n or "").strip()]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    # Anchor AMS window to live max sales date
    max_row = conn.execute("SELECT MAX(date) AS d FROM sales").fetchone()
    max_date = str((max_row["d"] if max_row else None) or "")[:10]
    ams_from = None
    if max_date and len(max_date) >= 7:
        try:
            from datetime import date as _date

            y, m, _ = (int(x) for x in max_date.split("-")[:3])
            # Trailing 3 calendar months ending on max_date's month
            end = _date(y, m, 1)
            start_m = m - 2
            start_y = y
            while start_m <= 0:
                start_m += 12
                start_y -= 1
            ams_from = f"{start_y:04d}-{start_m:02d}-01"
        except (TypeError, ValueError):
            ams_from = None
    rows = conn.execute(
        f"""
        SELECT s.party,
               COALESCE(NULLIF(trim(MAX(cl.type)), ''),
                        NULLIF(trim(MAX(s.client_type)), ''),
                        'Unmapped') AS client_type,
               NULLIF(trim(MAX(cl.city_filter)), '') AS city_filter,
               NULLIF(trim(MAX(cl.city)), '') AS city,
               ROUND(SUM(
                 CASE WHEN COALESCE(s.mt_qty, 0) <> 0 THEN s.mt_qty ELSE 0 END
               ), 3) AS mt_total,
               ROUND(SUM(
                 CASE
                   WHEN ? IS NOT NULL AND s.date >= ? AND s.date <= ?
                        AND COALESCE(s.mt_qty, 0) <> 0
                   THEN s.mt_qty ELSE 0
                 END
               ) / 3.0, 3) AS ams_3m,
               COUNT(*) AS sales_lines,
               MIN(s.date) AS first_sale,
               MAX(s.date) AS last_sale
        FROM sales s
        LEFT JOIN clients cl
          ON lower(trim(replace(replace(cl.client, '  ', ' '), '  ', ' ')))
           = lower(trim(replace(replace(s.party, '  ', ' '), '  ', ' ')))
        WHERE lower(trim(s.party)) IN ({placeholders})
        GROUP BY s.party
        """,
        [ams_from, ams_from, max_date or ams_from] + [n.lower() for n in clean],
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["party"] or "").strip()
        if not name:
            continue
        out[name.lower()] = {
            "mt_total": float(row["mt_total"] or 0),
            "ams_3m": float(row["ams_3m"] or 0) if ams_from else None,
            "sales_lines": int(row["sales_lines"] or 0),
            "first_sale": row["first_sale"],
            "last_sale": row["last_sale"],
            "client_type": str(row["client_type"] or "").strip() or None,
            "city_filter": row["city_filter"],
            "city": row["city"],
        }
    return out


def lookup_party(query: str, *, limit: int = 10) -> dict[str, Any]:
    """Fuzzy-search clients / sales parties and return close matches with metadata.

    Never hangs on a full sales GROUP BY — clients are scored first; sales are
    only queried for token LIKE candidates or to enrich the shortlist.
    """
    q = (query or "").strip()
    if not q:
        return {
            "ok": True,
            "error": None,
            "matches": [],
            "answer_markdown": (
                "Please give a client / distributor name to look up "
                "(e.g. `who is Al Shaheer`).\n"
            ),
            "response_instructions": "REQUIRED: Reply with `answer_markdown` verbatim.",
        }

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
    weak: list[tuple[float, dict[str, Any]]] = []
    tokens = [t for t in _norm(cleaned).split() if len(t) >= 3]
    long_token = max(tokens, key=len) if tokens else _norm(cleaned)

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
            extra = _payload_fields(row["payload_json"])
            raw_type = str(row["type"] or "").strip()
            entry = {
                "client": name,
                "client_type": canonical_raw_client_type(raw_type) or raw_type or None,
                "client_type_group": map_client_type(raw_type) if raw_type else None,
                "city_filter": str(row["city_filter"] or "").strip() or None,
                "city": str(row["city"] or "").strip() or None,
                "inactive": str(row["inactive"] or "").strip() or None,
                "client_id": str(row["client_id"] or "").strip() or None,
                "source": "clients",
                "match_score": round(score, 3),
                **extra,
            }
            if score >= 0.45:
                candidates[name.lower()] = entry
            elif score >= 0.30:
                weak.append((score, entry))

        # Sales parties: token LIKE only (never full-table fuzzy scan)
        if long_token:
            like = f"%{long_token}%"
            party_rows = conn.execute(
                """
                SELECT DISTINCT s.party
                FROM sales s
                WHERE s.party IS NOT NULL AND trim(s.party) != ''
                  AND lower(s.party) LIKE ?
                LIMIT 80
                """,
                (like,),
            ).fetchall()
            for row in party_rows:
                name = str(row["party"] or "").strip()
                if not name:
                    continue
                score = _score_name(cleaned, name)
                if score < 0.45:
                    if score >= 0.30 and name.lower() not in candidates:
                        weak.append(
                            (
                                score,
                                {
                                    "client": name,
                                    "source": "sales",
                                    "match_score": round(score, 3),
                                },
                            )
                        )
                    continue
                key = name.lower()
                if key not in candidates:
                    candidates[key] = {
                        "client": name,
                        "source": "sales",
                        "match_score": round(score, 3),
                    }
                else:
                    candidates[key]["match_score"] = max(
                        float(candidates[key]["match_score"]), round(score, 3)
                    )

        # Enrich shortlist with MT stats (targeted IN list — fast)
        enrich_names = [c["client"] for c in candidates.values()]
        if not enrich_names and weak:
            weak.sort(key=lambda x: -x[0])
            enrich_names = [e["client"] for _, e in weak[:10]]
            for _, e in weak[:5]:
                candidates.setdefault(e["client"].lower(), e)
        stats = _sales_stats_for_parties(conn, enrich_names)
        for key, entry in list(candidates.items()):
            bit = stats.get(key)
            if not bit:
                continue
            entry["mt_total"] = bit["mt_total"]
            entry["ams_3m"] = bit.get("ams_3m")
            entry["sales_lines"] = bit["sales_lines"]
            entry["first_sale"] = bit["first_sale"]
            entry["last_sale"] = bit["last_sale"]
            if not entry.get("client_type") and bit.get("client_type"):
                raw_ct = str(bit["client_type"] or "").strip()
                entry["client_type"] = (
                    canonical_raw_client_type(raw_ct) or raw_ct or None
                )
                entry["client_type_group"] = map_client_type(raw_ct)
            if not entry.get("city_filter"):
                entry["city_filter"] = bit.get("city_filter")
            if not entry.get("city"):
                entry["city"] = bit.get("city")

    try:
        from eva_dashboard.geo import zone_for_city as _zone_for_city
    except Exception:  # noqa: BLE001
        _zone_for_city = None  # type: ignore[assignment]
    for entry in candidates.values():
        if entry.get("zone"):
            continue
        if _zone_for_city:
            z = _zone_for_city(entry.get("city_filter") or entry.get("city"))
            if z:
                entry["zone"] = z

    ranked = sorted(
        candidates.values(),
        key=lambda r: (
            -float(r["match_score"]),
            -float(r.get("ams_3m") or r.get("mt_total") or 0),
        ),
    )[: max(1, min(int(limit or 10), 25))]

    strong = [r for r in ranked if float(r["match_score"]) >= 0.72]
    show = strong if strong else ranked[:8]

    # Still nothing usable — surface weak "did you mean" suggestions
    if not show and weak:
        weak.sort(key=lambda x: -x[0])
        show = []
        for score, entry in weak[:5]:
            entry = dict(entry)
            entry["match_score"] = round(float(score), 3)
            show.append(entry)

    for i, r in enumerate(show, 1):
        r["ordinal"] = i

    lines = [
        f"Client search for **{cleaned}** — {len(show)} close match(es):\n",
        "| # | Client | Client Type | Zone | City-Filter | City | AMS (3m) | Score |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in show:
        ams = r.get("ams_3m")
        if isinstance(ams, (int, float)):
            ams_s = f"{float(ams):.3f}".rstrip("0").rstrip(".")
        else:
            ams_s = "—"
        lines.append(
            "| {n} | {client} | {ctype} | {zone} | {cf} | {city} | {ams} | {score} |".format(
                n=r.get("ordinal"),
                client=str(r.get("client") or "").replace("|", "/"),
                ctype=str(r.get("client_type") or "—").replace("|", "/"),
                zone=str(r.get("zone") or "—").replace("|", "/"),
                cf=str(r.get("city_filter") or "—").replace("|", "/"),
                city=str(r.get("city") or "—").replace("|", "/"),
                ams=ams_s,
                score=r.get("match_score"),
            )
        )
    if not show:
        lines = [
            f"Could not find **{cleaned}** in clients or sales data.\n",
            "Try a longer fragment of the name (e.g. `shaheer` or "
            "`Rubina Shaheen (LHR)`), or add a city / client type.",
        ]
    elif all(float(r.get("match_score") or 0) < 0.45 for r in show):
        lines.insert(
            1,
            "_No strong match — did you mean one of these? Reply with the exact "
            "name or say `first 2` / `#1 and #2`._\n",
        )
    else:
        lines.append("")
        lines.append(
            "_Tip: follow up with `first 2` or `#1 and #2` to pull volumes for "
            "those matches (without unrelated city/channel filters)._"
        )

    return {
        "ok": True,
        "mode": "party_lookup",
        "query": cleaned,
        "original_query": q,
        "matches": show,
        "all_ranked_count": len(ranked),
        "answer_markdown": "\n".join(lines) + "\n",
        "response_instructions": (
            "REQUIRED: Reply with `answer_markdown` verbatim (or expand match details "
            "without inventing clients). Report client name, client type, zone, AMS. "
            "If several matches, ask the user to pick by number (`first 2`) or exact name. "
            "Do NOT call plan_query again for the same who-is ask."
        ),
    }
