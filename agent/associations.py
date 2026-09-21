"""Hand-maintained list of industry-association public member directories - mirrors
niches.py's explicit NICHES dict. This is NOT a generic scraper: every entry below is
one specific page you've checked and approved, using one small shared parser to read
just that page's member rows. Nothing is fetched for a niche/country that has no entry.

BEFORE ADDING AN ENTRY: check that specific directory's robots.txt and Terms of Service.
A page being public does not always mean re-using its listing is allowed - some
associations explicitly restrict scraping/re-use of their member list even though the
page itself has no login. When in doubt, leave it out.
"""
from __future__ import annotations
from urllib.parse import urljoin

import requests

UA = "UmairConsultAgent/1.0 (+https://umairconsult.com)"

# niche -> list of {"country", "url", "row_class"}. "row_class" is the CSS class that
# wraps ONE member's name + link on that page (view the page source once to find it).
# Empty on purpose - add entries only after you've personally checked and approved the
# specific directory (see the docstring above).
ASSOCIATIONS: dict[str, list[dict]] = {
    # EXAMPLE ONLY - do not enable without checking robots.txt/ToS for that real page:
    # "plumber": [{"country": "GB", "url": "https://example-plumbing-association.org/find-a-member",
    #              "row_class": "member-listing"}],
}


class AssociationError(Exception):
    pass


class _RowParser:
    """Very small, dependency-free HTML reader: pulls (name, href) pairs out of the
    <a> tags found inside elements whose class matches row_class. Shared by every
    entry above - the per-directory difference is only the URL and the class hint."""
    def __init__(self, row_class: str):
        from html.parser import HTMLParser

        class _P(HTMLParser):
            def __init__(inner):
                super().__init__()
                inner.depth = 0
                inner.buf: list[str] = []
                inner.href = None
                inner.rows: list[tuple[str, str]] = []

            def handle_starttag(inner, tag, attrs):
                a = dict(attrs)
                if row_class and row_class in (a.get("class") or "").split():
                    inner.depth += 1
                if inner.depth and tag == "a" and a.get("href"):
                    inner.href = a["href"]

            def handle_data(inner, data):
                if inner.depth and inner.href:
                    inner.buf.append(data.strip())

            def handle_endtag(inner, tag):
                if inner.depth and tag == "a" and inner.href:
                    text = " ".join(x for x in inner.buf if x)
                    if text:
                        inner.rows.append((text, inner.href))
                    inner.buf, inner.href = [], None
                if row_class and tag in ("div", "li", "tr", "article", "section") and inner.depth:
                    inner.depth -= 1

        self._impl = _P()

    def feed(self, html: str):
        self._impl.feed(html)
        return self._impl.rows


def fetch_members(entry: dict, session: requests.Session | None = None) -> list[dict]:
    s = session or requests.Session()
    try:
        r = s.get(entry["url"], headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        raise AssociationError(f"could not read {entry['url']} ({type(e).__name__})") from e
    rows = _RowParser(entry.get("row_class", "")).feed(r.text)
    out = []
    for name, href in rows:
        if not name or len(name) > 120:
            continue
        out.append({
            "business_name": name, "website": urljoin(entry["url"], href), "email": "", "phone": "",
            "facebook_url": "", "instagram_url": "", "linkedin_url": "",
            "address": "", "region": "", "country": entry.get("country", ""),
            "source": "association",
        })
    return out


def search(niche: str, country: str, session: requests.Session | None = None) -> list[dict]:
    """[] for any niche/country with no approved directory entry."""
    out: list[dict] = []
    for entry in ASSOCIATIONS.get(niche, []):
        if entry.get("country", country).upper() != country.upper():
            continue
        try:
            out.extend(fetch_members(entry, session))
        except AssociationError:
            continue
    return out
