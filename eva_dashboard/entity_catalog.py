"""Live categorical enums for the Enterprise Semantic Layer.

Injected into ``plan_query`` JSON schema so the LLM cannot invent filter
values (e.g. putting ``Eva Consumer`` into ``client_type``).
"""

from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any

from eva_dashboard.client_language import (
    CLIENT_TYPE_ALIASES,
    OIL_TYPE_ALIASES,
    PACKING_ALIASES,
    list_known_client_types,
    match_client_type_alias,
)
from eva_dashboard.client_type_map import list_new_client_types
from eva_dashboard.db import connect, init_db
from eva_dashboard.geo import CITY_TO_ZONE

# Canonical Business Units (ops + category master). Always included even if
# a local DB is empty / partially loaded.
CANONICAL_BUSINESS_UNITS: tuple[str, ...] = (
    "Eva Consumer",
    "Eva Bulk",
    "Maan Consumer",
    "Maan Bulk",
    "Cusine King",
    "Shortening",
    "Bulk Oil",
    "Meal",
    "Byproducts",
)

CANONICAL_ZONES: tuple[str, ...] = ("SOUTH", "CENTRAL", "NORTH")

# Spoken brand / BU phrases → business_units (Python resolver, not LLM).
BRAND_ENTITY_MAP: dict[str, list[str]] = {
    "eva": ["Eva Consumer", "Eva Bulk"],
    "eva brand": ["Eva Consumer", "Eva Bulk"],
    "maan": ["Maan Consumer", "Maan Bulk"],
    "maan brand": ["Maan Consumer", "Maan Bulk"],
    "consumer": ["Eva Consumer"],
    "eva consumer": ["Eva Consumer"],
    "eva bulk": ["Eva Bulk"],
    "maan consumer": ["Maan Consumer"],
    "maan bulk": ["Maan Bulk"],
    "cusine king": ["Cusine King"],
    "cuisine king": ["Cusine King"],
    "shortening": ["Shortening"],
    "bulk oil": ["Bulk Oil"],
    "meal": ["Meal"],
    "byproducts": ["Byproducts"],
    "by product": ["Byproducts"],
    "by-products": ["Byproducts"],
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().replace("-", " ").split())


def _distinct_category_col(col: str) -> list[str]:
    init_db()
    out: list[str] = []
    try:
        with connect() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT trim({col}) AS v FROM category "
                f"WHERE {col} IS NOT NULL AND trim({col}) != '' "
                f"ORDER BY v COLLATE NOCASE"
            ).fetchall()
        out = [str(r["v"]).strip() for r in rows if str(r["v"]).strip()]
    except Exception:  # noqa: BLE001 — empty DB / missing table during tests
        out = []
    return out


def _distinct_cities() -> list[str]:
    init_db()
    out: list[str] = []
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT trim(city_filter) AS v FROM clients "
                "WHERE city_filter IS NOT NULL AND trim(city_filter) != '' "
                "ORDER BY v COLLATE NOCASE"
            ).fetchall()
        out = [str(r["v"]).strip() for r in rows if str(r["v"]).strip()]
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        out = sorted(CITY_TO_ZONE.keys())
    return out


def _merge_unique(*groups: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for item in group:
            key = _norm(item)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(str(item).strip())
    return out


@lru_cache(maxsize=1)
def load_entity_catalog() -> dict[str, list[str]]:
    """Distinct categorical values for schema enums + resolver."""
    live_bus = _distinct_category_col("category_1")
    live_oil = _distinct_category_col("category_2")
    live_pack = _distinct_category_col("packing_category")

    business_units = _merge_unique(CANONICAL_BUSINESS_UNITS, live_bus)
    oil_types = _merge_unique(sorted(set(OIL_TYPE_ALIASES.values())), live_oil)
    packing = _merge_unique(sorted(set(PACKING_ALIASES.values())), live_pack)

    client_types = _merge_unique(
        list_new_client_types(),
        list_known_client_types(),
    )
    # Cap very large client lists for tool-schema size; keep alphabetically
    # first + ensure Eva Distributors / Imtiaz Store / LMT / IMT present.
    priority = [
        "Eva Distributors",
        "Maan Distributors",
        "Imtiaz Store",
        "LMT",
        "IMT",
        "Dealer",
        "Direct Customers",
        "USC",
        "DGP",
    ]
    if len(client_types) > 120:
        rest = [c for c in client_types if c not in priority]
        client_types = _merge_unique(priority, rest[:100])

    cities = _distinct_cities()
    if len(cities) > 80:
        cities = cities[:80]

    return {
        "business_units": business_units,
        "client_types": client_types,
        "oil_types": oil_types,
        "packing_categories": packing,
        "cities": cities,
        "zones": list(CANONICAL_ZONES),
    }


def clear_entity_catalog_cache() -> None:
    load_entity_catalog.cache_clear()


def is_business_unit_label(value: str | None) -> bool:
    """True when ``value`` is a known Business Unit (not a client channel)."""
    if not value:
        return False
    key = _norm(value)
    if key in BRAND_ENTITY_MAP:
        return True
    catalog = load_entity_catalog()
    bu_keys = {_norm(b) for b in catalog["business_units"]}
    if key in bu_keys:
        return True
    # Prefix match: "eva consumer oils" etc.
    for bu in bu_keys:
        if key.startswith(bu) or bu.startswith(key):
            if len(key) >= 4:
                return True
    return False


def resolve_extracted_entities(entities: list[str] | None) -> dict[str, Any]:
    """Map ambiguous spoken entities → typed filter fields (Python, not LLM)."""
    out: dict[str, Any] = {
        "business_units": [],
        "business_unit": None,
        "client_type": None,
        "oil_type": None,
        "packing_category": None,
        "city": None,
        "zone": None,
        "unresolved": [],
    }
    if not entities:
        return out

    catalog = load_entity_catalog()
    bu_by = {_norm(b): b for b in catalog["business_units"]}
    ct_by = {_norm(c): c for c in catalog["client_types"]}
    oil_by = {_norm(o): o for o in catalog["oil_types"]}
    pack_by = {_norm(p): p for p in catalog["packing_categories"]}
    city_by = {_norm(c): c for c in catalog["cities"]}
    zone_by = {_norm(z): z for z in catalog["zones"]}

    # Also alias maps
    for spoken, canon in OIL_TYPE_ALIASES.items():
        oil_by.setdefault(_norm(spoken), canon)
    for spoken, canon in PACKING_ALIASES.items():
        pack_by.setdefault(_norm(spoken), canon)
    for spoken, canon in CLIENT_TYPE_ALIASES.items():
        ct_by.setdefault(_norm(spoken), canon)

    bus: list[str] = []
    for raw in entities:
        key = _norm(raw)
        if not key:
            continue
        # Brands first (Eva / Consumer)
        if key in BRAND_ENTITY_MAP:
            for b in BRAND_ENTITY_MAP[key]:
                if b not in bus:
                    bus.append(b)
            continue
        if key in bu_by:
            if bu_by[key] not in bus:
                bus.append(bu_by[key])
            continue
        # Channels before oil/packing so "metro" ≠ a product/party guess
        ct_alias = match_client_type_alias(raw)
        if ct_alias and not out["client_type"]:
            out["client_type"] = ct_alias
            continue
        if key in ct_by and not out["client_type"]:
            out["client_type"] = ct_by[key]
            continue
        if key in oil_by and not out["oil_type"]:
            out["oil_type"] = oil_by[key]
            continue
        if key in pack_by and not out["packing_category"]:
            out["packing_category"] = pack_by[key]
            continue
        # Composite oil+packing in one token already handled elsewhere
        from eva_dashboard.client_language import extract_oil_and_packing

        o2, p2 = extract_oil_and_packing(raw)
        if o2 or p2:
            if o2 and not out["oil_type"]:
                out["oil_type"] = o2
            if p2 and not out["packing_category"]:
                out["packing_category"] = p2
            continue
        if key in city_by and not out["city"]:
            out["city"] = city_by[key]
            continue
        if key in zone_by and not out["zone"]:
            out["zone"] = zone_by[key]
            continue
        out["unresolved"].append(str(raw).strip())

    out["business_units"] = bus
    if len(bus) == 1:
        out["business_unit"] = bus[0]
    return out


def validate_categorical_filters(filters: dict[str, Any]) -> list[str]:
    """Strict enum checks with LLM-actionable error messages."""
    errors: list[str] = []
    catalog = load_entity_catalog()
    bu_set = {_norm(b): b for b in catalog["business_units"]}
    ct_set = {_norm(c): c for c in catalog["client_types"]}
    oil_set = {_norm(o): o for o in catalog["oil_types"]}
    pack_set = {_norm(p): p for p in catalog["packing_categories"]}
    city_set = {_norm(c): c for c in catalog["cities"]}
    zone_set = {_norm(z): z for z in catalog["zones"]}

    ct = filters.get("client_type")
    if ct:
        key = _norm(str(ct))
        if is_business_unit_label(str(ct)) or key in bu_set:
            errors.append(
                f"Validation failed: '{ct}' is not a valid client_type. "
                f"Did you mean to place this in business_units? "
                f"Valid client_types include e.g. Eva Distributors, Imtiaz Store, LMT."
            )
        elif ct_set and key not in ct_set:
            errors.append(
                f"Validation failed: '{ct}' is not a valid client_type. "
                f"Use extracted_entities if unsure, or pick from the client_type enum "
                f"(e.g. Eva Distributors, Imtiaz Store, LMT)."
            )

    for field, mapping, label in (
        ("oil_type", oil_set, "oil_type"),
        ("packing_category", pack_set, "packing_category"),
        ("city", city_set, "city"),
        ("zone", zone_set, "zone"),
    ):
        val = filters.get(field)
        if not val:
            continue
        key = _norm(str(val))
        if key not in mapping:
            # Soft for city: allow unknown cities (geo fallback handles blanks)
            if field == "city":
                continue
            if field == "oil_type":
                # Allow alias forms that normalize later
                from eva_dashboard.client_language import normalize_oil_type

                if normalize_oil_type(str(val)):
                    continue
            if field == "packing_category":
                from eva_dashboard.client_language import normalize_packing_category

                if normalize_packing_category(str(val)):
                    continue
            errors.append(
                f"Validation failed: '{val}' is not a valid {label}. "
                f"Use extracted_entities if unsure which column it belongs to."
            )

    bus_vals: list[str] = []
    if filters.get("business_unit"):
        bus_vals.append(str(filters["business_unit"]))
    for b in filters.get("business_units") or []:
        bus_vals.append(str(b))
    for b in bus_vals:
        key = _norm(b)
        if key not in bu_set and key not in BRAND_ENTITY_MAP:
            errors.append(
                f"Validation failed: '{b}' is not a valid business_unit. "
                f"Allowed examples: Eva Consumer, Eva Bulk, Maan Consumer, Maan Bulk."
            )
    return errors


def build_plan_query_tool(base_tool: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy the plan_query tool and inject live categorical enums."""
    tool = copy.deepcopy(base_tool)
    catalog = load_entity_catalog()
    params = tool["function"]["parameters"]
    props = params["properties"]

    # Top-level business_units
    props.setdefault("business_units", {"type": "array", "items": {"type": "string"}})
    props["business_units"]["items"] = {
        "type": "string",
        "enum": catalog["business_units"],
    }
    props["business_units"]["description"] = (
        "Brand / BU filter. Eva→[Eva Consumer, Eva Bulk]. "
        "Consumer alone→[Eva Consumer]. NEVER put these in client_type."
    )

    filt = props.setdefault("filters", {"type": "object", "properties": {}})
    fprops = filt.setdefault("properties", {})
    fprops["business_units"] = {
        "type": "array",
        "items": {"type": "string", "enum": catalog["business_units"]},
        "description": "Same as top-level business_units.",
    }
    fprops["business_unit"] = {
        "type": "string",
        "enum": catalog["business_units"],
        "description": "Single Business Unit (category_1). NOT a client channel.",
    }
    fprops["client_type"] = {
        "type": "string",
        "enum": catalog["client_types"],
        "description": (
            "Sales channel only (Eva Distributors, Imtiaz Store, LMT, …). "
            "NEVER use Business Unit names here (Eva Consumer is NOT a client_type)."
        ),
    }
    fprops["oil_type"] = {
        "type": "string",
        "enum": catalog["oil_types"],
        "description": "Oil / category_2 (e.g. Eva Canola).",
    }
    fprops["packing_category"] = {
        "type": "string",
        "enum": catalog["packing_categories"],
        "description": "Packing (e.g. Stand up, Tin, Pet bottle).",
    }
    if catalog["cities"]:
        fprops["city"] = {
            "type": "string",
            "enum": catalog["cities"],
            "description": "City-Filter.",
        }
        fprops["city_filter"] = {
            "type": "string",
            "enum": catalog["cities"],
            "description": "Alias for city.",
        }
    fprops["zone"] = {
        "type": "string",
        "enum": catalog["zones"],
        "description": "SOUTH | CENTRAL | NORTH — only when user names a zone.",
    }

    props["extracted_entities"] = {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Ambiguous product/brand/packing/city phrases you are NOT 100% sure "
            "how to place. Python resolves them against master tables "
            "(e.g. [\"standup\",\"canola\",\"Eva\"] → packing + oil + BUs). "
            "Prefer this over guessing the wrong filter column."
        ),
    }
    return tool
