"""Check Google Ads Transparency Center for a domain's ad activity.

Uses the Transparency Center's internal RPC API (reverse-engineered, no auth needed):
    POST https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives

Returns for a domain:
    - has_ads: bool (True if any ads found pointing to the domain)
    - ad_count: int (ads seen on first page; 40 = "40+")
    - advertiser_name: str (verified advertiser name, if found)
    - error: str (rate_limited, network error, etc.)

Rate limits: Google blocks an IP after ~60-80 requests (redirects to /sorry/).
Callers must pace requests (3-5s apart) and stop on rate_limited.
Results should be cached — re-check a domain at most once per 30 days.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

SEARCH_URL = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser="
SUGGEST_URL = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchSuggestions?authuser="

# Region codes: 2000 + ISO 3166-1 numeric. 2840 = US. Used as a broad default.
DEFAULT_REGION = 2840

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def clean_domain(url_or_domain: str) -> str:
    """Extract a bare domain from a URL or domain string."""
    s = (url_or_domain or "").strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    s = s.split(":")[0]  # strip port
    return s.strip().rstrip(".")


def _rpc_post(url: str, payload: dict, timeout: int = 25) -> dict | None:
    """POST to the Transparency RPC endpoint. Returns parsed JSON or None on rate-limit."""
    data = urllib.parse.urlencode({"f.req": json.dumps(payload)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": BROWSER_UA,
            "Referer": "https://adstransparency.google.com/",
            "Origin": "https://adstransparency.google.com",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if "/sorry/" in resp.url:
            return None  # rate-limited
        body = resp.read().decode("utf-8", errors="replace")
    if body.startswith(")]}'"):
        body = body[4:]
    body = body.strip()
    if not body:
        return {}
    return json.loads(body)


def check_google_ads(domain: str, region: int = DEFAULT_REGION,
                     timeout: int = 25) -> dict:
    """Check Google Ads Transparency for ads pointing to a domain.

    Returns dict: {has_ads, ad_count, advertiser_name, error}
    - has_ads: True/False/None (None = check failed)
    - ad_count: number seen on first page (40 = "40+")
    - advertiser_name: verified advertiser name from first creative
    - error: None, "rate_limited", or error description
    """
    dom = clean_domain(domain)
    if not dom or "." not in dom:
        return {"has_ads": None, "ad_count": 0, "advertiser_name": "",
                "error": f"bad domain: {domain!r}"}
    payload = {
        "2": 40,  # page size
        "3": {"12": {"1": dom, "2": True}},  # domain search
        "7": {"1": 1, "2": 0, "3": region},
    }
    try:
        d = _rpc_post(SEARCH_URL, payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - network errors are expected
        return {"has_ads": None, "ad_count": 0, "advertiser_name": "",
                "error": f"request failed: {type(e).__name__}"}
    if d is None:
        return {"has_ads": None, "ad_count": 0, "advertiser_name": "",
                "error": "rate_limited"}
    creatives = d.get("1") or []
    has_ads = len(creatives) > 0
    advertiser_name = ""
    if creatives:
        # First creative's advertiser info: field "1" = advertiser ID,
        # advertiser name is often in a nested field; try common spots.
        first = creatives[0] if isinstance(creatives[0], dict) else {}
        for key in ("4", "5", "6"):
            v = first.get(key)
            if isinstance(v, str) and v and not v.startswith(("AR", "CR", "http")):
                advertiser_name = v
                break
    return {"has_ads": has_ads, "ad_count": len(creatives),
            "advertiser_name": advertiser_name, "error": None}


def lookup_advertiser(name_or_domain: str, timeout: int = 20) -> dict:
    """Look up a verified advertiser by name or domain via SearchSuggestions.

    Returns dict: {advertiser_name, advertiser_id, country, verified}
    """
    q = clean_domain(name_or_domain)
    payload = {"1": q, "2": 10, "3": 10, "5": {"1": 1}}
    try:
        d = _rpc_post(SUGGEST_URL, payload, timeout=timeout)
    except Exception:  # noqa: BLE001
        return {}
    if not d:
        return {}
    # Results are in field "1" (advertisers) or "2" (domains)
    for field in ("1", "2"):
        items = d.get(field) or []
        if items and isinstance(items[0], dict):
            it = items[0]
            return {
                "advertiser_name": it.get("1", ""),
                "advertiser_id": it.get("2", ""),
                "country": it.get("3", ""),
                "verified": bool(it.get("5")),
            }
    return {}


if __name__ == "__main__":
    import sys
    for dom in sys.argv[1:] or ["nike.com"]:
        r = check_google_ads(dom)
        print(f"{dom}: has_ads={r['has_ads']} count={r['ad_count']} "
              f"advertiser={r['advertiser_name']!r} error={r['error']}")
        time.sleep(2)
