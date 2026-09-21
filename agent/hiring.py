"""Is this business actively hiring right now? A growing/investing business is a
better prospect, so this only nudges lead_score in audit.py - it is never used to
create a lead by itself (scheduler.py never calls this during lead finding).

Two free/open, no-account checks:
  1. RemoteOK's public API (https://remoteok.com/api, no key) - matched by domain.
     RemoteOK is mostly remote tech roles, so this mostly helps tech-adjacent niches.
  2. the business's own website, for a visible careers/jobs page - works for any
     niche, costs one extra polite page fetch (reuses web.Fetcher, so it still obeys
     robots.txt and the same private-network/size guards as everything else).
"""
from __future__ import annotations
import re
import requests

from .agent_utils import domain_of

UA = "UmairConsultAgent/1.0 (+https://umairconsult.com)"
CAREERS_PATHS = ["/careers", "/careers/", "/jobs", "/jobs/", "/join-us", "/work-with-us", "/vacancies"]
_HIRE_WORDS = re.compile(r"\b(we'?re hiring|now hiring|join our team|open positions|current vacancies)\b", re.I)

_remoteok_domains_cache: set[str] | None = None


def _remoteok_domains() -> set[str]:
    """Fetched once per run and cached - RemoteOK is one flat JSON list, not per-query."""
    global _remoteok_domains_cache
    if _remoteok_domains_cache is not None:
        return _remoteok_domains_cache
    domains: set[str] = set()
    try:
        r = requests.get("https://remoteok.com/api", headers={"User-Agent": UA}, timeout=20)
        if r.status_code == 200:
            for row in r.json():
                if not isinstance(row, dict):
                    continue
                d = domain_of(row.get("company_url") or row.get("url") or "")
                if d:
                    domains.add(d)
    except (requests.RequestException, ValueError):
        pass   # best-effort signal - never breaks the audit run
    _remoteok_domains_cache = domains
    return domains


def _has_careers_page(fetcher, website: str) -> bool:
    for path in CAREERS_PATHS:
        got = fetcher.get(website.rstrip("/") + path)
        if not got:
            continue
        _, html = got
        if _HIRE_WORDS.search(html[:20000]):
            return True
    return False


def hiring_boost(website: str, fetcher=None, check_remoteok: bool = True) -> tuple[int, str]:
    """(score boost, short note) - best effort, never raises. 0, '' if nothing found
    or the checks are switched off."""
    dom = domain_of(website)
    if not dom:
        return 0, ""
    if check_remoteok and dom in _remoteok_domains():
        return 10, "actively hiring (listed on a job board)"
    if fetcher is not None:
        try:
            if _has_careers_page(fetcher, website):
                return 6, "has an active careers/jobs page"
        except Exception:  # noqa: BLE001 - a hiring check must never break an audit
            pass
    return 0, ""
