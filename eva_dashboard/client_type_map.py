"""Existing client type → new reporting group.

Raw Excel / sales labels are remapped so pivots, filters, and chatbot
answers always use the new groupings (never the old long-tail types).
"""

from __future__ import annotations

import re

# Existing (source) → New (display / filter) — from ops mapping table
CLIENT_TYPE_GROUP: dict[str, str] = {
    "Eva Distributors": "Eva Distributors",
    "Direct Customers (Karachi)": "Direct Customers",
    "Oil Clients": "Oil Clients",
    "Bulk Debtors": "Bulk Debtors",
    "Soap Clients": "Soap Clients",
    "CENTRAL LMT": "LMT",
    "Meal Clients": "Meal Clients",
    "BURGER LAB": "Direct Customers",
    "0": "0",
    "Local Seed Suppliers": "Local Seed Suppliers",
    "Whole Seller": "Whole Seller",
    "BOHRI JAMMAT KHANA KARACHI": "Direct Customers",
    "GINSOY GROUP": "Direct Customers",
    "Services Suppliers": "Services Suppliers",
    "Local Dealers": "Dealer",
    "Other Clients": "Other Clients",
    "Brokerage Services": "Brokerage Services",
    "MAAN X MILL-DEALERS": "Dealer",
    "DONATIONS": "DONATIONS",
    "Modren Trade Customers": "LMT",
    "Modern Trade Customers": "LMT",
    "Institutional Sales": "Institutional Sales",
    "X-DEALERS": "Dealer",
    "NORTH LMT": "LMT",
    "Rice Polish": "Rice Polish",
    "Local Rice Polish": "Local Rice Polish",
    "Oil & Meal Clients": "Oil & Meal Clients",
    "Madarsa": "DONATIONS",
    "Lahore Dealers": "Dealer",
    "DGP ARMY": "DGP",
    "SOUTH LMT": "LMT",
    "Canteen Store Department": "IMT",
    "XANDER GROUP": "Direct Customers",
    "CHASE UP": "IMT",
    "Cheezious": "Direct Customers",
    "PAK NAVY": "DGP",
    "Rice Bran Meal": "Rice Bran Meal",
    "Online Customers": "Online Customers",
    "Online Customer": "Online Customers",
    "FOOD PANDA": "FOOD PANDA",
    "Nil": "Nil",
    "Distributor/ Retailer": "Direct Customers",
    "Distributor/Retailer": "Direct Customers",
    "GELANI MART": "LMT",
    "HASHOO GROUP": "Direct Customers",
    "HOTEL ONE": "Direct Customers",
    "MAF Hypermarkets": "IMT",
    "Imtiaz Store": "Imtiaz Store",
    "PIE IN THE SKY": "Direct Customers",
    "ROYAL TAJ": "Direct Customers",
    "AL FATEH": "LMT",
    "Salma Enterprise": "Direct Customers",
    "METRO HABIB": "IMT",
    "SDN DEDUCTION": "SDN DEDUCTION",
    "Store Suppliers": "Store Suppliers",
    "Rice Bran": "Rice Bran",
    "NANDOS PAKISTAN": "Direct Customers",
    "meal": "meal",
    "SPAR - IMT": "IMT",
    "STAR TEXTILE": "Direct Customers",
    "Utility Stores Corporation": "USC",
    "USC M&B": "USC",
    "V E N U S": "Direct Customers",
    "WAH BRAND": "Direct Customers",
    # Keep Maan Distributors as its own group (not in ops remap sheet)
    "Maan Distributors": "Maan Distributors",
}


def _norm_key(value: str | None) -> str:
    if not value:
        return ""
    t = str(value).strip().lower()
    t = t.replace("_", " ").replace("-", " ")
    t = re.sub(r"[/]+", "/", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_GROUP_BY_KEY: dict[str, str] = {
    _norm_key(src): dest for src, dest in CLIENT_TYPE_GROUP.items()
}
_NEW_GROUPS: set[str] = set(CLIENT_TYPE_GROUP.values())
_NEW_GROUP_BY_KEY: dict[str, str] = {_norm_key(g): g for g in _NEW_GROUPS}

# Reverse: new group → source labels (for SQL fallback on raw sales.client_type)
_SOURCES_BY_GROUP: dict[str, list[str]] = {}
for _src, _dest in CLIENT_TYPE_GROUP.items():
    _SOURCES_BY_GROUP.setdefault(_dest, []).append(_src)
for _g in _NEW_GROUPS:
    bucket = _SOURCES_BY_GROUP.setdefault(_g, [])
    if _g not in bucket:
        bucket.append(_g)


def map_client_type(value: str | None) -> str | None:
    """Map a raw / existing client type to its new reporting group.

    Unknown labels pass through unchanged (trimmed). Blank → None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = _norm_key(text)
    if key in _GROUP_BY_KEY:
        return _GROUP_BY_KEY[key]
    if key in _NEW_GROUP_BY_KEY:
        return _NEW_GROUP_BY_KEY[key]
    return text


def is_new_client_type(value: str | None) -> bool:
    if not value:
        return False
    return _norm_key(value) in _NEW_GROUP_BY_KEY


def list_new_client_types() -> list[str]:
    return sorted(_NEW_GROUPS, key=lambda s: s.lower())


def raw_client_types_for_group(group: str | None) -> list[str]:
    """Raw/source labels (plus the group itself) that belong to ``group``."""
    mapped = map_client_type(group)
    if not mapped:
        return []
    sources = list(_SOURCES_BY_GROUP.get(mapped) or [mapped])
    # Dedupe case-insensitively, keep first spelling
    out: list[str] = []
    seen: set[str] = set()
    for s in sources:
        k = _norm_key(s)
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def canonical_raw_client_type(value: str | None) -> str | None:
    """Canonical spelling of an existing/source client type, if known."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    key = _norm_key(text)
    for src in CLIENT_TYPE_GROUP:
        if _norm_key(src) == key:
            return src
    return text


def is_specific_raw_client_type(value: str | None) -> bool:
    """True when ``value`` names an old/source type that rolls up into a broader group.

    E.g. CHASE UP → IMT (specific). Eva Distributors → Eva Distributors (not specific).
    Bare group names like IMT / LMT are not specific.
    """
    if not value:
        return False
    key = _norm_key(value)
    if key in _NEW_GROUP_BY_KEY and key not in _GROUP_BY_KEY:
        return False
    if key not in _GROUP_BY_KEY:
        return False
    dest = _GROUP_BY_KEY[key]
    return _norm_key(dest) != key


def classify_client_type_filter(
    value: str | None,
) -> tuple[str, str] | None:
    """How to apply a client-type filter.

    Returns ``(\"raw\", label)`` for a specific old type (Chase Up, CSD, …)
    or ``(\"group\", label)`` for a new reporting group (IMT, LMT, …).
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if is_specific_raw_client_type(text):
        return ("raw", canonical_raw_client_type(text) or text)
    mapped = map_client_type(text)
    if mapped:
        return ("group", mapped)
    return ("raw", text)


def sql_client_type_values(value: str | None) -> list[str]:
    """Labels to match against raw ``sales.client_type`` / ``clients.type``."""
    classified = classify_client_type_filter(value)
    if not classified:
        return []
    mode, label = classified
    if mode == "raw":
        return [label]
    return raw_client_types_for_group(label) or [label]
