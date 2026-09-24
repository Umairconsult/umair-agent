"""Google Search Console API - read-only (search analytics + sitemaps). Authenticates as a
service account (GSC_SERVICE_ACCOUNT_JSON in settings.env) - no browser, no OAuth consent
flow, nothing that expires on its own. This module never writes anything to Search Console."""
from __future__ import annotations
import json
from datetime import date, timedelta
from urllib.parse import quote

import requests

from .logutil import log, safe_exc

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
BASE = "https://www.googleapis.com/webmasters/v3"


class SearchConsoleError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _host(site: str) -> str:
    """'sc-domain:example.com' / 'https://www.example.com/path' -> 'example.com' (for matching only)."""
    s = site.strip().lower()
    for pre in ("sc-domain:", "https://", "http://"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.split("/")[0].removeprefix("www.")


class SearchConsole:
    def __init__(self, service_account_json: str, site_url: str):
        if not service_account_json:
            raise SearchConsoleError("GSC_SERVICE_ACCOUNT_JSON is not set")
        if not site_url:
            raise SearchConsoleError("GSC_SITE_URL is not set")
        try:
            info = json.loads(service_account_json)
        except ValueError as e:
            raise SearchConsoleError(f"GSC_SERVICE_ACCOUNT_JSON is not valid JSON ({type(e).__name__})") from e
        try:
            from google.oauth2 import service_account
        except ImportError as e:
            raise SearchConsoleError("the 'google-auth' package isn't installed (see requirements.txt)") from e
        self.site_url = site_url
        self._creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    def _token(self) -> str:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        try:
            if not self._creds.valid:
                self._creds.refresh(Request())
        except GoogleAuthError as e:
            raise SearchConsoleError(f"could not authenticate ({safe_exc(e, 150)})") from e
        return self._creds.token

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            r = requests.request(method, f"{BASE}{path}", headers={"Authorization": f"Bearer {self._token()}"},
                                 json=body, timeout=30)
        except requests.RequestException as e:
            raise SearchConsoleError(f"Search Console unreachable ({type(e).__name__})") from e
        if r.status_code == 403:
            raise SearchConsoleError("Search Console said 'forbidden' - has the service account email "
                                     "(client_email in the JSON key) been added as a user on this property "
                                     "in Search Console > Settings > Users and permissions?")
        if r.status_code != 200:
            raise SearchConsoleError(f"Search Console API HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise SearchConsoleError("Search Console sent a non-JSON reply") from e

    def list_sites(self) -> list[dict]:
        """Every property this service account can see: [{'siteUrl': ..., 'permissionLevel': ...}]."""
        return self._call("GET", "/sites").get("siteEntry", [])

    def ping(self) -> dict:
        """Confirms the key works and the service account can actually see this property -
        raises SearchConsoleError with a clear reason if not. Used by the self-test."""
        try:
            return self._call("GET", f"/sites/{quote(self.site_url, safe='')}")
        except SearchConsoleError as e:
            if e.status != 404:
                raise
            raise SearchConsoleError(self._explain_404()) from e

    def _explain_404(self) -> str:
        """A 404 means: the key is fine, but THIS exact property string isn't one the service
        account has been added to. Look at what it CAN see and say precisely what to change."""
        try:
            sites = self.list_sites()
        except SearchConsoleError:
            return "property not found (HTTP 404) and the list of visible properties could not be read"
        if not sites:
            return ("the key works but the service account can see NO properties. In Search Console > "
                    "Settings > Users and permissions, add the client_email from the JSON key as a user "
                    "(Restricted is enough) on the property")
        mine = _host(self.site_url)
        same = [x["siteUrl"] for x in sites if _host(x["siteUrl"]) == mine]
        if same:
            return ("the service account CAN see this site, but not under the exact text in GSC_SITE_URL. "
                    "Set GSC_SITE_URL to exactly: " + " or ".join(same))
        kinds = sorted({"domain property" if x["siteUrl"].startswith("sc-domain:") else "URL-prefix property"
                        for x in sites})
        return (f"the service account can see {len(sites)} propert{'y' if len(sites) == 1 else 'ies'} "
                f"({', '.join(kinds)}), none for {mine}. Add the client_email from the JSON key as a user "
                f"on the {mine} property in Search Console, then use GSC_SITE_URL=sc-domain:{mine} "
                f"(domain property) or the exact URL-prefix incl. https:// and trailing / (URL-prefix property)")

    def sitemaps(self) -> list[dict]:
        data = self._call("GET", f"/sites/{quote(self.site_url, safe='')}/sitemaps")
        return data.get("sitemap", [])

    def query(self, start_date: str, end_date: str, dimensions=("query", "page"), row_limit: int = 1000,
              search_type: str = "web") -> list[dict]:
        """Raw Search Analytics query. Dates as 'YYYY-MM-DD'. Each row has keys/clicks/
        impressions/ctr/position, in the same order as `dimensions`."""
        body = {"startDate": start_date, "endDate": end_date, "dimensions": list(dimensions),
                "rowLimit": min(row_limit, 25000), "type": search_type}
        data = self._call("POST", f"/sites/{quote(self.site_url, safe='')}/searchAnalytics/query", body)
        return data.get("rows", [])

    def opportunities(self, days: int = 28, min_impressions: int = 50, position_min: float = 5,
                      position_max: float = 30, ctr_max: float = 0.03) -> list[dict]:
        """High impressions + mid position (5-30) + low CTR - pages already being SHOWN a lot
        but barely clicked. The cheapest content wins: no new ranking needed, just a better
        title/meta/intro/FAQ. GSC data usually lags ~2-3 days, so the window ends there."""
        end = date.today() - timedelta(days=3)
        start = end - timedelta(days=days)
        rows = self.query(start.isoformat(), end.isoformat(), dimensions=("query", "page"), row_limit=5000)
        out = []
        for r in rows:
            keys = r.get("keys") or []
            if len(keys) < 2:
                continue
            impressions, position, ctr = r.get("impressions", 0), r.get("position", 0), r.get("ctr", 0)
            if impressions >= min_impressions and position_min <= position <= position_max and ctr <= ctr_max:
                out.append({"query": keys[0], "page": keys[1], "impressions": round(impressions),
                           "clicks": round(r.get("clicks", 0)), "ctr_pct": round(ctr * 100, 2),
                           "position": round(position, 1)})
        out.sort(key=lambda x: -x["impressions"])
        return out
