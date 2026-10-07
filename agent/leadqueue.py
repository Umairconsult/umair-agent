"""Durable local lead store: state/leads_queue.json + state/agent_state.json.

Why this exists (2026-10-07): the portal API (agent_api.php) is intermittently
rate-limited (HTTP 429) from GitHub's cloud IP range, and the self-hosted PC is
offline for days. The agent must keep finding leads, auditing and emailing even
when the portal cannot be reached. So the lead lifecycle now lives HERE:

  * new leads are appended to state/leads_queue.json (deduped by domain),
  * audits / ads checks / messages / email sends update the lead's status
    in the same file,
  * the file is committed to the repo at the end of every run,
  * a separate sync job (Muse's lead-buffer-sync) mirrors new leads to the
    Google Sheet and uploads them to the portal - the agent never writes
    leads to the portal directly anymore.

Statuses: new -> audited -> emailed. bad = website unreachable / unusable.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(BASE_DIR, "state")
QUEUE_FILE = os.path.join(STATE_DIR, "leads_queue.json")
AGENT_STATE_FILE = os.path.join(STATE_DIR, "agent_state.json")
DNC_CACHE_FILE = os.path.join(STATE_DIR, "dnc_cache.json")

QUEUE_VERSION = 1

STATUS_NEW = "new"
STATUS_AUDITED = "audited"
STATUS_EMAILED = "emailed"
STATUS_BAD = "bad"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_domain(website: str) -> str:
    """lowercase, no scheme, no www, no path/query."""
    d = (website or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split("?")[0].split("#")[0].rstrip(".")
    return d


def _atomic_write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class LeadQueue:
    """The agent's local lead ledger. One JSON object, committed to git."""

    def __init__(self):
        self.data = {"version": QUEUE_VERSION, "leads": []}
        self._by_domain: dict[str, dict] = {}
        self.load()

    # ---- persistence ----
    def load(self) -> None:
        try:
            with open(QUEUE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("leads"), list):
                self.data = data
        except (OSError, ValueError):
            pass
        self._reindex()

    def save(self) -> None:
        self.data["updated_at"] = utcnow_iso()
        _atomic_write(QUEUE_FILE, self.data)

    def _reindex(self) -> None:
        self._by_domain = {}
        for lead in self.data["leads"]:
            dom = lead.get("domain") or normalize_domain(lead.get("website", ""))
            if dom:
                lead["domain"] = dom
                self._by_domain.setdefault(dom, lead)

    # ---- reads ----
    def __len__(self) -> int:
        return len(self.data["leads"])

    def domains(self) -> set[str]:
        return set(self._by_domain)

    def get(self, domain: str) -> dict | None:
        return self._by_domain.get(normalize_domain(domain))

    def candidates_for_audit(self, limit: int) -> list[dict]:
        return [l for l in self.data["leads"] if l.get("status") == STATUS_NEW][:limit]

    def candidates_for_email(self, limit: int) -> list[dict]:
        out = []
        for l in self.data["leads"]:
            if l.get("status") != STATUS_AUDITED:
                continue
            if l.get("audit_score") is None or not (l.get("audit_summary") or "").strip():
                continue
            if not (l.get("email") or "").strip():
                continue
            out.append(l)
            if len(out) >= limit:
                break
        return out

    def candidates_for_message_write(self, limit: int) -> list[dict]:
        return [l for l in self.data["leads"]
                if l.get("status") == STATUS_AUDITED and not l.get("email_subject")][:limit]

    def candidates_for_followup_write(self, limit: int, min_days: int = 3) -> list[dict]:
        out = []
        for l in self.data["leads"]:
            if l.get("status") != STATUS_EMAILED:
                continue
            if int(l.get("followup_count") or 0) >= 2:
                continue
            if (l.get("followup_email_body") or "").strip():
                continue
            if "email" in (l.get("bad_channels") or ""):
                continue
            if not _days_since(l.get("emailed_at"), min_days):
                continue
            out.append(l)
            if len(out) >= limit:
                break
        return out

    def candidates_for_followup_send(self, limit: int, min_days: int = 3) -> list[dict]:
        out = []
        for l in self.data["leads"]:
            if l.get("status") != STATUS_EMAILED:
                continue
            if int(l.get("followup_count") or 0) >= 2:
                continue
            if not (l.get("followup_email_body") or "").strip():
                continue
            if "email" in (l.get("bad_channels") or ""):
                continue
            if not _days_since(l.get("emailed_at"), min_days):
                continue
            out.append(l)
            if len(out) >= limit:
                break
        return out

    def stats(self, today: str) -> dict:
        """today = YYYY-MM-DD in the agent's local timezone. Mirrors portal.stats()
        keys the scheduler uses: leads_today, unaudited, to_contact, audits_today."""
        st = {"leads_today": 0, "unaudited": 0, "to_contact": 0, "audits_today": 0,
              "emailed_today": 0, "total": len(self.data["leads"])}
        for l in self.data["leads"]:
            if (l.get("found_at") or "")[:10] == today:
                st["leads_today"] += 1
            if l.get("status") == STATUS_NEW:
                st["unaudited"] += 1
            if l.get("status") == STATUS_AUDITED:
                st["to_contact"] += 1
            if (l.get("audited_at") or "")[:10] == today:
                st["audits_today"] += 1
            if (l.get("emailed_at") or "")[:10] == today:
                st["emailed_today"] += 1
        return st

    # ---- writes ----
    def add_lead(self, lead: dict) -> bool:
        """Append a new lead. Returns True if it was new, False if the domain
        was already in the queue (dedup)."""
        dom = normalize_domain(lead.get("website", ""))
        if not dom or dom in self._by_domain:
            return False
        entry = {
            "domain": dom,
            "website": lead.get("website", ""),
            "business_name": lead.get("business_name", ""),
            "contact_name": lead.get("contact_name", ""),
            "email": lead.get("email", ""),
            "all_emails": lead.get("all_emails") or [],
            "phone": lead.get("phone", ""),
            "country": lead.get("country", ""),
            "region": lead.get("region", ""),
            "address": lead.get("address", ""),
            "niche": lead.get("niche", ""),
            "facebook_url": lead.get("facebook_url", ""),
            "instagram_url": lead.get("instagram_url", ""),
            "linkedin_url": lead.get("linkedin_url", ""),
            "contact_page_url": lead.get("contact_page_url", ""),
            "source": lead.get("source", ""),
            "score": lead.get("score", 0),
            "notes": lead.get("notes", ""),
            "found_at": utcnow_iso(),
            "status": STATUS_NEW,
            # filled later by the audit / message / email phases
            "audit_score": None, "audit_summary": "", "lead_score": 0, "audit_blob": "",
            "audited_at": "", "google_ads_checked_at": "", "google_ads_count": 0,
            "google_ads_advertiser": "", "google_ads_error": "",
            "email_subject": "", "email_body": "", "email_html": "",
            "whatsapp_message": "", "social_message": "",
            "emailed_at": "", "emailed_to": [], "followup_count": 0,
            "followup_email_body": "", "followup_whatsapp": "",
            "followup_sent_at": "", "contacted_via": "", "bad_channels": "",
            "portal_id": None,
        }
        self.data["leads"].append(entry)
        self._by_domain[dom] = entry
        return True

    def update(self, domain: str, **fields) -> bool:
        lead = self.get(domain)
        if not lead:
            return False
        lead.update(fields)
        return True

    def mark_bad(self, domain: str, reason: str = "") -> bool:
        ok = self.update(domain, status=STATUS_BAD)
        if ok and reason:
            lead = self.get(domain)
            lead["notes"] = ((lead.get("notes") or "") + " | bad: " + reason).strip(" |")
        return ok


def _days_since(iso_ts: str | None, min_days: int) -> bool:
    if not iso_ts:
        return False
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() >= min_days * 86400
    except ValueError:
        return False


# ------------------------------------------------------------------ agent state
def load_agent_state() -> dict:
    """Small key/value state the agent used to keep in the portal:
    search_done {key: YYYY-MM-DD}, email_sent {YYYY-MM-DD: n}, agent_started."""
    try:
        with open(AGENT_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_agent_state(state: dict) -> None:
    _atomic_write(AGENT_STATE_FILE, state)


def emails_sent_today(state: dict, today: str) -> int:
    try:
        return int((state.get("email_sent") or {}).get(today, 0))
    except (ValueError, TypeError):
        return 0


def record_emails_sent(state: dict, today: str, count: int) -> None:
    es = state.setdefault("email_sent", {})
    es[today] = int(es.get(today, 0)) + count


# ------------------------------------------------------------------ DNC cache
def load_dnc_cache() -> list:
    """Fallback DNC list when the portal can't be reached. Format matches
    portal.get_dnc(): [{"kind": "email"|"domain", "value": ...}, ...]."""
    try:
        with open(DNC_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
    except (OSError, ValueError):
        pass
    return []


def save_dnc_cache(items: list) -> None:
    _atomic_write(DNC_CACHE_FILE, {"updated_at": utcnow_iso(), "items": items})
