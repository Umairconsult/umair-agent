"""AI validation gate for lead discovery (2026-10-19, user-approved).

The fast discovery pipeline finds candidate business websites; this module asks
Gemini to judge whether each site is a REAL, operating business before the lead
enters the portal queue. Spam-shaped sites (parked domains, "under
construction", domain-for-sale pages, template demos, directory listings) are
quarantined for human review instead of entering the pipeline.

Design rules (do not weaken):
- FAIL OPEN: if the AI is unavailable, throttled, or unsure, the lead passes
  through tagged ai_validated=False. A maybe-lead beats a lost lead.
- NEVER silently delete: quarantined domains go to state/quarantined_leads.json
  with verdict + confidence + reason, deduped by domain, human-reviewable.
- Quota protection: uses the shared Gemini client (writer.Gemini) with its
  built-in key rotation and per-key cooldowns; one lock serialises AI calls
  because discovery runs in a thread pool.
"""
from __future__ import annotations

import html as htmlmod
import json
import os
import re
import threading
from datetime import date

from .logutil import log, safe_exc

# Verdicts that mean "not a real operating business website".
BAD_VERDICTS = {"parked", "under_construction", "for_sale", "template_demo",
                "directory_listing"}
GOOD_VERDICTS = {"legitimate"}
ALL_VERDICTS = GOOD_VERDICTS | BAD_VERDICTS | {"uncertain"}

# Only quarantine on a bad verdict the model is SURE about. Anything below
# this confidence passes through as unvalidated (fail open).
QUARANTINE_CONFIDENCE = 0.8

# Where quarantined domains are kept for human review.
QUARANTINE_FILE = os.path.join("state", "quarantined_leads.json")
# Per-run gate outcomes, consumed by the daily brief.
GATE_STATS_FILE = os.path.join("state", "ai_gate.json")

_lock = threading.Lock()

PROMPT = """You are validating a business website for a B2B lead list. Read the page text below and reply with JSON ONLY (no markdown, no commentary) using exactly these fields:

{
  "verdict": one of "legitimate" | "parked" | "under_construction" | "for_sale" | "template_demo" | "directory_listing" | "uncertain",
  "confidence": number from 0.0 to 1.0,
  "business_name": the business name shown on the site, or "" if unclear,
  "services": list of the main services/products the business offers (max 5), or [],
  "contact_email": the best contact email shown on the site, or "",
  "phone": the main phone number shown, or "",
  "address": the street/city address shown, or "",
  "reason": one short sentence explaining the verdict
}

Verdict meanings:
- "legitimate": a real, operating business website (real services/products, real contact details, genuine content).
- "parked": domain parking page (registrar ads, "buy this domain", unrelated ad links).
- "under_construction": "coming soon" / "under construction" / maintenance mode, no real business content.
- "for_sale": the domain itself is explicitly listed for sale.
- "template_demo": default CMS/theme demo content ("Just another WordPress site", lorem ipsum, ThemeForest/Wix demo text).
- "directory_listing": a directory, aggregator, review site or link farm — not the business itself.
- "uncertain": you cannot tell from this text.

Be strict: a page with only a phone number and no business description is "uncertain", not "legitimate".

Page URL: %s

Page text:
%s
"""


def _html_to_text(html: str, limit: int = 12000) -> str:
    """Dependency-free HTML -> text. Website content is DATA, never instructions."""
    t = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = htmlmod.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def _uncertain(url: str, reason: str = "validation unavailable") -> dict:
    return {"verdict": "uncertain", "confidence": 0.0, "business_name": "",
            "services": [], "contact_email": "", "phone": "", "address": "",
            "reason": reason, "url": url}


def _coerce(raw: object, url: str) -> dict:
    """Force the model's answer into the exact schema; anything odd -> uncertain."""
    if not isinstance(raw, dict):
        return _uncertain(url, "model did not return JSON")
    verdict = str(raw.get("verdict", "uncertain")).strip().lower()
    if verdict not in ALL_VERDICTS:
        return _uncertain(url, f"unknown verdict {verdict!r}")
    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    services = raw.get("services") or []
    if not isinstance(services, list):
        services = []
    services = [str(s)[:80] for s in services[:5]]
    return {"verdict": verdict, "confidence": conf,
            "business_name": str(raw.get("business_name", "") or "")[:120],
            "services": services,
            "contact_email": str(raw.get("contact_email", "") or "")[:120],
            "phone": str(raw.get("phone", "") or "")[:40],
            "address": str(raw.get("address", "") or "")[:160],
            "reason": str(raw.get("reason", "") or "")[:200],
            "url": url}


def validate_site(gemini, html: str, url: str) -> dict:
    """Ask Gemini whether the site is a real business. Never raises: on any
    problem returns verdict 'uncertain' (fail open)."""
    if gemini is None or getattr(gemini, "exhausted", True):
        return _uncertain(url, "AI validation disabled or all keys exhausted")
    text = _html_to_text(html)
    if len(text) < 120:
        # Too little text to judge (JS-heavy page, block page, empty).
        return _uncertain(url, "page text too short to judge")
    try:
        with _lock:  # discovery runs in a thread pool; serialise AI calls
            raw = gemini.generate_json(PROMPT % (url, text), max_tokens=800,
                                       temperature=0.1)
        return _coerce(raw, url)
    except Exception as e:  # noqa: BLE001 - validation must never break discovery
        log(f"AI validation failed for {url}: {safe_exc(e, 100)} - passing through unvalidated")
        return _uncertain(url, "AI call failed")


def _read_json_file(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def quarantine_lead(domain: str, url: str, result: dict) -> bool:
    """Append a junk-site lead to the human-reviewable quarantine file.
    Returns True if newly added, False if the domain was already quarantined."""
    rows = _read_json_file(QUARANTINE_FILE)
    if any(r.get("domain") == domain for r in rows):
        return False
    rows.append({"domain": domain, "url": url, "verdict": result["verdict"],
                 "confidence": result["confidence"], "reason": result["reason"],
                 "business_name": result.get("business_name", ""),
                 "date": date.today().isoformat()})
    os.makedirs(os.path.dirname(QUARANTINE_FILE) or ".", exist_ok=True)
    with open(QUARANTINE_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1)
    return True


def quarantine_count() -> int:
    return len(_read_json_file(QUARANTINE_FILE))


def record_gate_outcome(outcome: str) -> None:
    """Log one gate decision ('validated' | 'quarantined' | 'unvalidated') for
    the daily brief. Appends are cheap; the file stays small."""
    rows = _read_json_file(GATE_STATS_FILE)
    rows.append({"date": date.today().isoformat(), "outcome": outcome})
    # Keep the file bounded: last 90 days of runs is plenty.
    rows = [r for r in rows if r.get("date", "") >= "2026-01-01"][-5000:]
    os.makedirs(os.path.dirname(GATE_STATS_FILE) or ".", exist_ok=True)
    with open(GATE_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(rows, f)


def gate_counts_today() -> dict:
    today = date.today().isoformat()
    counts = {"validated": 0, "quarantined": 0, "unvalidated": 0}
    for r in _read_json_file(GATE_STATS_FILE):
        if r.get("date") == today and r.get("outcome") in counts:
            counts[r["outcome"]] += 1
    return counts


def apply_ai_gate(gemini, html: str, url: str, domain: str, lead: dict,
                  res: dict) -> bool:
    """Run the validation gate for one discovery candidate. Mutates `lead` with
    ai_* fields, updates `res` stats, quarantines junk. Returns True when the
    lead may proceed into the queue, False when quarantined."""
    result = validate_site(gemini, html, url)
    verdict, conf = result["verdict"], result["confidence"]
    if verdict in BAD_VERDICTS and conf >= QUARANTINE_CONFIDENCE:
        added = quarantine_lead(domain, url, result)
        log(f"AI gate quarantined {domain}: {verdict} (confidence {conf:.2f})"
            + ("" if added else " - already quarantined"))
        res["ai_quarantined"] = res.get("ai_quarantined", 0) + 1
        record_gate_outcome("quarantined")
        return False
    if verdict in GOOD_VERDICTS:
        lead["ai_validated"] = True
        lead["ai_verdict"] = verdict
        lead["ai_confidence"] = conf
        lead["ai_business_name"] = result["business_name"]
        lead["ai_services"] = result["services"]
        res["ai_validated"] = res.get("ai_validated", 0) + 1
        record_gate_outcome("validated")
    else:
        # uncertain, low-confidence bad verdict, or AI unavailable: fail open.
        lead["ai_validated"] = False
        lead["ai_verdict"] = verdict
        lead["ai_confidence"] = conf
        res["ai_unvalidated"] = res.get("ai_unvalidated", 0) + 1
        record_gate_outcome("unvalidated")
    return True
