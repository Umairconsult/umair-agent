"""Open/free government business registries - best effort, skipped quietly for any
country with no free bulk/API option wired up yet (same idea as niches.NO_FREE_SOURCE).

Mirrors the osm.py / overture.py lead shape: business_name, website, email, phone,
socials, address, region. Registries mostly give a legal name + registered address
and NOT a website or email - those leads still go through the normal "no way to
reach them" filter already in scheduler.py, so a registry-only lead with no website
is skipped there automatically, exactly like any other source. No scraping: every
country below either uses a documented free/open API, or is left out.
"""
from __future__ import annotations
import requests

UA = "UmairConsultAgent/1.0 (+https://umairconsult.com)"

# Countries with a working free/open API wired up below.
WIRED = {"GB"}
# Every other country in cities.COUNTRY_NAMES has no documented free bulk/API
# registry wired up yet (US Secretary-of-State portals differ per state with almost
# none offering a free JSON/CSV API; most EU registries charge or require a
# business-registered account). Add a country here only once a real free API exists
# for it - until then `search()` returns [] for it, same as niches.NO_FREE_SOURCE.


class RegistryError(Exception):
    """Temporary problem (bad key, server busy) - worth trying again later."""


def available(country: str) -> bool:
    return country.upper() in WIRED


def _gb_companies_house(query: str, api_key: str, limit: int) -> list[dict]:
    """UK Companies House public search API - free, needs a free API key from
    https://developer.company-information.service.gov.uk/ (register once, no cost)."""
    try:
        r = requests.get("https://api.company-information.service.gov.uk/search/companies",
                          params={"q": query, "items_per_page": min(limit, 50)},
                          auth=(api_key, ""), headers={"User-Agent": UA}, timeout=30)
    except requests.RequestException as e:
        raise RegistryError(f"Companies House unreachable ({type(e).__name__})") from e
    if r.status_code == 401:
        raise RegistryError("Companies House rejected the API key (check COMPANIES_HOUSE_API_KEY)")
    if r.status_code == 429:
        raise RegistryError("Companies House rate limit hit")
    if r.status_code != 200:
        raise RegistryError(f"Companies House HTTP {r.status_code}")
    try:
        items = r.json().get("items", [])
    except ValueError as e:
        raise RegistryError("Companies House sent a non-JSON reply") from e
    out = []
    for item in items:
        if (item.get("company_status") or "").lower() != "active":
            continue
        addr = item.get("address") or {}
        address = ", ".join(x for x in [addr.get("address_line_1", ""), addr.get("locality", ""),
                                        addr.get("postal_code", "")] if x)
        out.append({
            "business_name": (item.get("title") or "").strip(),
            "website": "", "email": "", "phone": "",
            "facebook_url": "", "instagram_url": "", "linkedin_url": "",
            "address": address, "region": addr.get("locality", ""), "country": "GB",
            "source": "registry",
        })
    return out


def search(country: str, niche: str, city: str, api_key: str = "", limit: int = 20) -> list[dict]:
    """Best-effort: [] where nothing free is wired up for this country, or no key given."""
    cc = country.upper()
    if cc == "GB" and api_key:
        return _gb_companies_house(f"{niche} {city}", api_key, limit)
    return []
