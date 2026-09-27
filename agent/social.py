"""Writes today's social-media post text - one caption + call-to-action idea per configured
platform - and puts it in front of the owner for manual review: the caption/CTA/status goes to
the portal (same add_content/content_list mechanism blog.py already uses, with kind="social"),
and a short Slack notification lists them.

Text only - no image or video is generated or uploaded anywhere; the owner writes/finds their
own visual and posts it themselves. Progress for "today" is tracked in the portal's own settings
storage (state_set/state_list) so that running this more than once a day is a safety net, not a
duplication risk: if an earlier run already finished everything, a later run sees that and does
nothing; if it only got partway, the later run picks up exactly where it left off.

NOTHING in this file ever posts to a social media platform. A post reaching status
AWAITING_REVIEW only ever means "ready for the owner to look at / copy for manual posting" - see
extras_json['review_status']. This is only ever run when you ask for it (python -m agent
social), separate from the lead-finding/audit/blog cycle in scheduler.py, so it never competes
with that work for time."""
from __future__ import annotations
import json
import re
from datetime import date

from .logutil import log, safe_exc
from .portal import Portal, PortalError
from .writer import Gemini

MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# platform -> display label.
PLATFORM_SPECS = {
    "instagram": {"label": "Instagram"},
    "linkedin":  {"label": "LinkedIn"},
    "facebook":  {"label": "Facebook"},
    "youtube":   {"label": "YouTube"},
    "tiktok":    {"label": "TikTok"},
}


def _prompt(cfg, platform: str, avoid: list[str] | None = None) -> str:
    spec = PLATFORM_SPECS[platform]
    avoid_txt = ""
    if avoid:
        avoid_txt = " Do NOT repeat or closely resemble any of these recent posts: " + " | ".join(a[:90] for a in avoid[-12:]) + "."
    return f"""You are planning one social media post for {cfg.your_name}, who runs UmairConsult. \
Its offer: {cfg.pitch}. Platform: {spec['label']}. Audience: owners of local and small businesses.

Come up with ONE fresh, specific content idea (not a generic ad, not a tired template).{avoid_txt} Then write:
- "caption": the post text for {spec['label']}, natural and non-salesy, at most 3 relevant hashtags, no invented statistics, awards or client stories.
- "cta": one short call to action (a few words).

Return ONLY a JSON object with exactly these keys: caption, cta."""


def _slug(label: str, year: int) -> str:
    """'POST OCT 5' + 2026 -> 'post-oct-5-2026' (the year keeps it unique from one year to the next)."""
    return re.sub(r"[^a-z0-9]+", "-", f"{label} {year}".lower()).strip("-")


def next_post_label(portal: Portal, today: date) -> str:
    """'POST OCT 5' - the counter lives in the portal's own settings storage so it survives
    across runs, and automatically resets to 1 on the 1st of a new month (the label switches to
    the new month's abbreviation on its own)."""
    month_key = today.strftime("%Y-%m")
    state = portal.state_list("social:counter:")
    stored_month = state.get("social:counter:month", "")
    try:
        n = int(state.get("social:counter:n", "0"))
    except ValueError:
        n = 0
    n = 1 if stored_month != month_key else n + 1
    portal.state_set("social:counter:month", month_key)
    portal.state_set("social:counter:n", str(n))
    return f"POST {MONTH_ABBR[today.month - 1]} {n}"


def _progress_key(today: date) -> str:
    return f"social:progress:{today.isoformat()}"


def _load_progress(portal: Portal, today: date) -> dict:
    """{'instagram': true, 'linkedin': true, ...} - which platforms already have a post today.
    Read fresh at the start of every run so a later run the same day knows what an earlier one
    already finished."""
    key = _progress_key(today)
    row = portal.state_list(key)
    raw = row.get(key, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _save_progress(portal: Portal, today: date, progress: dict) -> bool:
    """Returns False (and only logs) if the portal could not store it - the post is already
    listed on the portal by then, so this must never turn a finished post into a "failure"."""
    try:
        portal.state_set(_progress_key(today), json.dumps(progress))
        return True
    except PortalError as e:
        log(f"Could not save today's social progress on the portal: {safe_exc(e, 100)}")
        return False


def _recent_ideas(portal: Portal) -> list[str]:
    """Captions of recent posts, so a new idea doesn't repeat them. Best effort only."""
    try:
        items = portal.content_list(kind="social", include_body=True, limit=12)
    except PortalError:
        return []
    return [str(i.get("excerpt") or i.get("title") or "")[:120] for i in items if i.get("excerpt")]


def _first_dict(obj) -> dict:
    """Gemini occasionally wraps its answer in a list: take the first object inside."""
    if isinstance(obj, list):
        obj = next((x for x in obj if isinstance(x, dict)), {})
    return obj if isinstance(obj, dict) else {}


def run_social(cfg, portal: Portal, gemini: Gemini | None, today: date) -> dict:
    """Runs the pipeline for every configured platform that doesn't already have a post today.
    Never raises - one platform failing is recorded in out['failures'] and everything else still
    gets a chance to run."""
    out = {"done": 0, "already_done_today": 0, "failed": 0, "target": 0, "failures": [], "posts": []}
    if not gemini or gemini.exhausted:
        out["failures"].append("no working Gemini key - cannot write captions")
        return out

    platforms = [p for p in (cfg.social_platforms or list(PLATFORM_SPECS))]
    out["target"] = len(platforms)

    try:
        progress = _load_progress(portal, today)
    except PortalError as e:
        out["failures"].append(f"could not read today's progress from the portal ({safe_exc(e, 100)})")
        return out
    avoid = _recent_ideas(portal)

    for platform in platforms:
        spec = PLATFORM_SPECS.get(platform)
        if not spec:
            out["failures"].append(f"'{platform}' is not a recognised platform (check SOCIAL_PLATFORMS) - skipped")
            out["target"] -= 1
            continue
        if progress.get(platform):
            out["already_done_today"] += 1
            continue
        if gemini.exhausted:
            out["failures"].append(f"{spec['label']}: no Gemini key can write right now - will retry next run")
            continue
        try:
            concept = _first_dict(gemini.generate_json(_prompt(cfg, platform, avoid), max_tokens=1024, temperature=0.9))
            caption = str(concept.get("caption", "")).strip()
            cta = str(concept.get("cta", "")).strip()
            if not caption:
                raise RuntimeError("Gemini did not return a usable caption")
        except Exception as e:  # noqa: BLE001 - one platform failing must not stop the others
            reason = f"{spec['label']}: {safe_exc(e, 200)}"
            out["failures"].append(reason)
            log(f"Social post skipped - {reason}")
            continue

        # ---- the post text exists from here on: never make it "fail" and get made twice ----
        progress[platform] = True
        _save_progress(portal, today, progress)
        avoid.append(caption)
        label = next_post_label(portal, today)
        post_text = caption + (f"\n\n{cta}" if cta else "")
        extras = {"platform": platform, "platform_label": spec["label"], "cta": cta, "post_text": post_text,
                  "review_status": "AWAITING_REVIEW", "generated_on": today.isoformat()}
        try:
            portal.add_content(kind="social", title=label, slug=_slug(label, today.year),
                               excerpt=caption[:2000], body_html="", extras_json=json.dumps(extras))
            # (the portal writes the "ready for review" activity-log line itself when it saves the post)
        except PortalError as e:
            out["failures"].append(f"{label} ({spec['label']}) could not be listed on the portal ({safe_exc(e, 120)})")
            continue
        out["done"] += 1
        out["posts"].append({"label": label, "platform": platform, "platform_label": spec["label"],
                             "caption": caption, "cta": cta})

    out["failed"] = max(0, out["target"] - out["done"] - out["already_done_today"])
    return out
