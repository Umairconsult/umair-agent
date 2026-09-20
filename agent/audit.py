"""Runs your own audit tool (audit/agent_audit.php) on a website."""
from __future__ import annotations
import time
import requests


class AuditError(Exception):
    """Temporary problem (server busy, timeout). Worth trying again later."""


class AuditClient:
    def __init__(self, url: str, token: str, timeout: int = 150):
        self.url, self.token, self.timeout = url, token, timeout
        self.s = requests.Session()

    def _post(self, payload: dict) -> dict:
        last = "unknown"
        for attempt in range(2):
            try:
                r = self.s.post(self.url, json=payload, headers={"X-Agent-Token": self.token}, timeout=self.timeout)
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(4)
                continue
            if r.status_code in (429, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(8)
                continue
            try:
                data = r.json()
            except ValueError:
                last = f"non-JSON reply (HTTP {r.status_code})"
                time.sleep(4)
                continue
            if r.status_code == 401:
                raise AuditError("audit endpoint rejected the token (check AUDIT_API_TOKEN)")
            if r.status_code >= 500:
                last = data.get("error", f"HTTP {r.status_code}")
                time.sleep(4)
                continue
            return data
        raise AuditError(f"audit tool not answering ({last})")

    def ping(self) -> bool:
        return bool(self._post({"action": "ping"}).get("pong"))

    def run(self, website: str, full: bool = False) -> dict:
        """Returns the audit dict. data['ok'] False + data['unreachable'] True = dead website.
        full=True also returns 'blob': the complete audit (compressed) for reports, emails and PDFs."""
        return self._post({"url": website, "full": full})


def summarize_audit(res: dict) -> str:
    """One readable paragraph saved on the lead in your portal (facts only)."""
    parts = [f"Site health {res.get('health_score', '?')}/100 ({res.get('health_label', '')})".strip()]
    gaps = []
    if not res.get("has_ga4") and not res.get("has_gtm"): gaps.append("no analytics")
    if not res.get("has_meta_pixel"): gaps.append("no Meta Pixel")
    if not res.get("has_google_ads_tag"): gaps.append("no Google Ads conversion tag")
    if not res.get("has_schema"): gaps.append("no schema markup")
    if gaps:
        parts.append("Tracking/SEO gaps: " + ", ".join(gaps))
    if res.get("mobile_perf") is not None:
        parts.append(f"Mobile speed {res['mobile_perf']}/100")
    if res.get("ssl") is False:
        parts.append("No SSL")
    if res.get("meta_len") == 0:
        parts.append("no meta description")
    if res.get("cms"):
        parts.append(f"Platform: {res['cms']}")
    issues = [i.get("issue", "") for i in (res.get("top_issues") or [])[:4] if i.get("issue")]
    if issues:
        parts.append("Top issues: " + " ".join(issues))
    return (" · ".join(parts))[:900]


def lead_score(res: dict, initial: int) -> int:
    """0-100: how good a prospect this is (audit opportunity + how reachable they are)."""
    return max(0, min(100, round(int(res.get("opportunity_score", 0)) * 0.5 + initial)))
