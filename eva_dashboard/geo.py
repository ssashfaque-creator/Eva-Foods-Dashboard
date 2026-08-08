"""City → Zone (SOUTH / CENTRAL / NORTH) geography for Eva Foods.

Blank / unmapped / undefined City-Filter defaults to Karachi → SOUTH.
"""

from __future__ import annotations

import re
from typing import Iterable

DEFAULT_CITY = "Karachi"
DEFAULT_ZONE = "SOUTH"
ZONES = ("SOUTH", "CENTRAL", "NORTH")

# Canonical display names → zone (from ops city/region map)
CITY_TO_ZONE: dict[str, str] = {
    # SOUTH
    "Sukkur": "SOUTH",
    "Karachi": "SOUTH",
    "Quetta": "SOUTH",
    "Larkana": "SOUTH",
    "Hyderabad": "SOUTH",
    "Dadu": "SOUTH",
    "Badin Thatta": "SOUTH",
    "Badin": "SOUTH",
    "Thatta": "SOUTH",
    "Sindh": "SOUTH",
    "Mir Pur Khas": "SOUTH",
    "Mirpur Khas": "SOUTH",
    "Online": "SOUTH",
    "Jacobabad": "SOUTH",
    "Bahawalpur": "SOUTH",
    "Nawab Shah": "SOUTH",
    "Nawabshah": "SOUTH",
    "Kashmor": "SOUTH",
    "Kashmore": "SOUTH",
    "Haroonabad": "SOUTH",
    # CENTRAL
    "Sialkot": "CENTRAL",
    "Lahore": "CENTRAL",
    "Gujrat": "CENTRAL",
    "Multan": "CENTRAL",
    "Faisalabad": "CENTRAL",
    "Khanewal": "CENTRAL",
    "Sargodha": "CENTRAL",
    "Jhung": "CENTRAL",
    "Jhang": "CENTRAL",
    "Ali Pur": "CENTRAL",
    "Alipur": "CENTRAL",
    "Gujranwala": "CENTRAL",
    "Muzaffar Gar": "CENTRAL",
    "Muzaffargarh": "CENTRAL",
    "Sahiwal": "CENTRAL",
    "Lala Musa": "CENTRAL",
    "Okara": "CENTRAL",
    "Muridke": "CENTRAL",
    "Daska": "CENTRAL",
    "Sheikhapura": "CENTRAL",
    "Sheikhupura": "CENTRAL",
    "Wazirabad": "CENTRAL",
    "Rahiwali": "CENTRAL",
    "Nankana Sah": "CENTRAL",
    "Nankana Sahib": "CENTRAL",
    "Sambrial": "CENTRAL",
    "Hafizabad": "CENTRAL",
    "Mandi Bahau": "CENTRAL",
    "Mandi Bahauddin": "CENTRAL",
    "Eminabad": "CENTRAL",
    "Dinga": "CENTRAL",
    "Kharian": "CENTRAL",
    "Qila Didar Sin": "CENTRAL",
    "Qila Didar Singh": "CENTRAL",
    "Rahim Yar Khan": "CENTRAL",
    "Pasrur": "CENTRAL",
    "Bimber": "CENTRAL",
    "Bhimber": "CENTRAL",
    "Kotri": "CENTRAL",
    "Jalal Pur": "CENTRAL",
    "Jalalpur": "CENTRAL",
    # NORTH
    "Islamabad": "NORTH",
    "Chakwal": "NORTH",
    "HARIPUR": "NORTH",
    "Haripur": "NORTH",
    "Skardu": "NORTH",
    "Wah Cantt": "NORTH",
    "Kotli": "NORTH",
    "Rawalpindi": "NORTH",
    "Peshawar": "NORTH",
    "Jhelum": "NORTH",
    "Rawalakot": "NORTH",
    "Mardan": "NORTH",
    "Mansehra": "NORTH",
    "D.I.Khan": "NORTH",
    "D I Khan": "NORTH",
    "DI Khan": "NORTH",
    "Abbottabad": "NORTH",
    "GILLGITT": "NORTH",
    "Gilgit": "NORTH",
    "Muzaffarabad": "NORTH",
    "Swat": "NORTH",
}

# Extra spoken / DB aliases → canonical city name used in CITY_TO_ZONE
CITY_ALIASES: dict[str, str] = {
    "khi": "Karachi",
    "lhr": "Lahore",
    "isb": "Islamabad",
    "rwp": "Rawalpindi",
    "fsd": "Faisalabad",
    "mux": "Multan",
    "pew": "Peshawar",
    "hyd": "Hyderabad",
    "sheikhupura": "Sheikhupura",
    "sheikhapura": "Sheikhapura",
    "jhang": "Jhang",
    "jhung": "Jhung",
    "muzaffargarh": "Muzaffargarh",
    "muzaffar garh": "Muzaffargarh",
    "nankana sahib": "Nankana Sahib",
    "mandi bahauddin": "Mandi Bahauddin",
    "mandi bahuddin": "Mandi Bahauddin",
    "qila didar singh": "Qila Didar Singh",
    "bhimber": "Bhimber",
    "gilgit": "Gilgit",
    "gillgit": "GILLGITT",
    "gillgitt": "GILLGITT",
    "mirpur khas": "Mirpur Khas",
    "mir purkhas": "Mir Pur Khas",
    "nawabshah": "Nawabshah",
    "d.i khan": "D.I.Khan",
    "d.i. khan": "D.I.Khan",
    "di.khan": "D.I.Khan",
}


def _norm_key(value: str | None) -> str:
    if not value:
        return ""
    t = str(value).strip().lower()
    t = t.replace("_", " ").replace("-", " ")
    t = re.sub(r"[.]+", ".", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_CITY_ZONE_BY_KEY: dict[str, tuple[str, str]] = {}
for _city, _zone in CITY_TO_ZONE.items():
    _CITY_ZONE_BY_KEY[_norm_key(_city)] = (_city, _zone)
for _alias, _canon in CITY_ALIASES.items():
    _zone = CITY_TO_ZONE.get(_canon) or CITY_TO_ZONE.get(_canon.title())
    if not _zone:
        # alias points at a CITY_TO_ZONE key
        for k, z in CITY_TO_ZONE.items():
            if _norm_key(k) == _norm_key(_canon):
                _CITY_ZONE_BY_KEY[_norm_key(_alias)] = (k, z)
                break
    else:
        # prefer canonical CITY_TO_ZONE key spelling
        for k, z in CITY_TO_ZONE.items():
            if z == _zone and _norm_key(k) == _norm_key(_canon):
                _CITY_ZONE_BY_KEY[_norm_key(_alias)] = (k, z)
                break
        else:
            _CITY_ZONE_BY_KEY[_norm_key(_alias)] = (_canon, _zone)


def is_blank_city(value: str | None) -> bool:
    """True for empty / unmapped / undefined city labels."""
    t = _norm_key(value)
    return t in {"", "unmapped", "undefined", "none", "null", "n/a", "na", "-"}


def normalize_zone(value: str | None) -> str | None:
    """Map spoken zone text to SOUTH | CENTRAL | NORTH."""
    if not value:
        return None
    t = _norm_key(value)
    t = re.sub(r"\b(zone|region)\b", "", t).strip()
    aliases = {
        "south": "SOUTH",
        "southern": "SOUTH",
        "central": "CENTRAL",
        "centre": "CENTRAL",
        "center": "CENTRAL",
        "north": "NORTH",
        "northern": "NORTH",
    }
    if t in aliases:
        return aliases[t]
    upper = str(value).strip().upper()
    if upper in ZONES:
        return upper
    return None


def resolve_city_label(value: str | None) -> str:
    """Canonical city for reporting; blank/undefined → Karachi."""
    if is_blank_city(value):
        return DEFAULT_CITY
    raw = str(value).strip()
    hit = _CITY_ZONE_BY_KEY.get(_norm_key(raw))
    if hit:
        return hit[0]
    return raw


def zone_for_city(value: str | None) -> str:
    """Zone for a city label; unknown cities still default to SOUTH."""
    city = resolve_city_label(value)
    hit = _CITY_ZONE_BY_KEY.get(_norm_key(city))
    if hit:
        return hit[1]
    return DEFAULT_ZONE


def resolve_city_zone(value: str | None) -> tuple[str, str]:
    """Return (city, zone) with defaults applied."""
    city = resolve_city_label(value)
    return city, zone_for_city(city)


def extract_zone_from_text(text: str | None) -> str | None:
    """Find SOUTH/CENTRAL/NORTH mentioned in free text."""
    t = (text or "").lower()
    if not t:
        return None
    # Prefer "south zone" / "central region" before bare words
    for needle, zone in (
        (r"\b(south(?:ern)?)\s*(zone|region)?\b", "SOUTH"),
        (r"\b(central|centre|center)\s*(zone|region)?\b", "CENTRAL"),
        (r"\b(north(?:ern)?)\s*(zone|region)?\b", "NORTH"),
    ):
        if re.search(needle, t):
            # Avoid matching NORTH LMT client type as a geography zone when
            # clearly about LMT — still allow "north zone" / "northern".
            if zone == "NORTH" and re.search(r"\bnorth\s*lmt\b", t):
                if not re.search(r"\bnorth(?:ern)?\s*(zone|region)\b", t):
                    continue
            return zone
    return None


def list_known_zones() -> list[str]:
    return list(ZONES)


def cities_in_zone(zone: str | None) -> list[str]:
    z = normalize_zone(zone)
    if not z:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for city, mapped in CITY_TO_ZONE.items():
        if mapped != z:
            continue
        key = _norm_key(city)
        if key in seen:
            continue
        seen.add(key)
        out.append(city)
    return out


def cities_matching_any(cities: Iterable[str]) -> list[str]:
    """Expand a list of city names with known alias spellings (unique)."""
    out: list[str] = []
    seen: set[str] = set()
    for c in cities:
        canon = resolve_city_label(c)
        for candidate in (c, canon):
            key = _norm_key(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(str(candidate).strip())
    return out
