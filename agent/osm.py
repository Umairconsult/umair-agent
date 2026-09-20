"""Finds businesses on OpenStreetMap (free, no account) via the Overpass API."""
from __future__ import annotations
import time
import requests

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UA = "UmairConsultAgent/1.0 (+https://umairconsult.com)"


class OverpassError(Exception):
    pass


def build_query(lat: float, lon: float, radius_m: int, filters: list[str], limit: int = 150) -> str:
    stmts = []
    for f in filters:
        for website_key in ("website", "contact:website"):
            stmts.append(f'nwr{f}["{website_key}"](around:{radius_m},{lat},{lon});')
    return f"[out:json][timeout:60];({''.join(stmts)});out center tags {limit};"


def _first(tags: dict, *keys) -> str:
    for k in keys:
        v = tags.get(k)
        if v and str(v).strip():
            return str(v).strip().split(";")[0].strip()
    return ""


def parse_elements(data: dict, city: str) -> list[dict]:
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = _first(tags, "name", "brand", "operator")
        website = _first(tags, "website", "contact:website")
        if not name or not website:
            continue
        street = " ".join(x for x in [tags.get("addr:housenumber", ""), tags.get("addr:street", "")] if x).strip()
        address = ", ".join(x for x in [street, tags.get("addr:postcode", ""), tags.get("addr:city", "")] if x)
        out.append({
            "business_name": name,
            "website": website,
            "email": _first(tags, "email", "contact:email"),
            "phone": _first(tags, "phone", "contact:phone", "contact:mobile"),
            "facebook_url": _first(tags, "contact:facebook", "facebook"),
            "instagram_url": _first(tags, "contact:instagram", "instagram"),
            "linkedin_url": _first(tags, "contact:linkedin"),
            "address": address,
            "region": tags.get("addr:city") or city,
            "osm_tags": tags,
        })
    return out


def search(lat: float, lon: float, radius_m: int, filters: list[str], city: str, limit: int = 150,
           session: requests.Session | None = None) -> list[dict]:
    """Try each public Overpass server until one answers."""
    s = session or requests.Session()
    query = build_query(lat, lon, radius_m, filters, limit)
    last = "no server answered"
    for i, ep in enumerate(ENDPOINTS):
        try:
            r = s.post(ep, data={"data": query}, headers={"User-Agent": UA}, timeout=90)
            if r.status_code == 200:
                return parse_elements(r.json(), city)
            last = f"HTTP {r.status_code}"
            if r.status_code in (429, 504):
                time.sleep(8 * (i + 1))
        except (requests.RequestException, ValueError) as e:
            last = type(e).__name__
            time.sleep(3)
    raise OverpassError(f"OpenStreetMap servers busy ({last})")
