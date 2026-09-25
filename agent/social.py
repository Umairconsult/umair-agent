"""Generates branded social-media images for each platform, twice a day, and puts them in
front of the owner for manual review: each image goes to Google Drive, the caption/status goes
to the portal (same add_content/content_list mechanism blog.py already uses, with
kind="social"), and a short Slack notification points at the Drive link.

Images only - no video. Two visually/creatively distinct variants are made per platform per
day (see STYLE_ANGLES), using two different professional creative directions rather than two
near-identical images. Progress for "today" is tracked in the portal's own settings storage
(state_set/state_list) so that running this twice a day is a safety net, not a duplication
risk: if the morning run already finished everything, the evening run sees that and does
nothing; if the morning run only got partway (an API hiccup, a slow key), the evening run picks
up exactly where it left off.

NOTHING in this file ever posts to a social media platform. A post reaching status
AWAITING_REVIEW or SELECTED only ever means "ready for the owner to look at / chosen for
manual posting" - see extras_json['review_status']. Runs as its own scheduled job
(python -m agent social), separate from the lead-finding/audit/blog cycle in scheduler.py, so
it never competes with that work for time."""
from __future__ import annotations
import json
import re
from datetime import date

from .logutil import log, safe_exc
from .media.branding import BrandingError, apply_logo, fit_to_size
from .media.drive import DriveError, build_drive
from .media.providers import build_image_generator
from .media.quality import QualityError, check_image
from .portal import Portal, PortalError
from .writer import Gemini

MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# platform -> (display label, target image size, file type). Sizes are sensible defaults for a
# single feed-style image on each platform; adjust freely, this is just configuration. YouTube
# and TikTok are left defined (in case you add them to SOCIAL_PLATFORMS later) even though
# they aren't in today's default lineup.
PLATFORM_SPECS = {
    "instagram": {"label": "Instagram", "size": (1080, 1080), "aspect": "1:1",  "mime": "image/png"},
    "linkedin":  {"label": "LinkedIn",  "size": (1200, 627),  "aspect": "16:9", "mime": "image/png"},
    "facebook":  {"label": "Facebook",  "size": (1200, 630),  "aspect": "16:9", "mime": "image/png"},
    "youtube":   {"label": "YouTube",   "size": (1280, 720),  "aspect": "16:9", "mime": "image/png"},
    "tiktok":    {"label": "TikTok",    "size": (1080, 1920), "aspect": "9:16", "mime": "image/png"},
}

# Two deliberately different professional creative directions, so variant A and variant B for
# a platform are genuinely different images/ideas, not the same picture twice. Add a third
# entry here later if you ever want 3 variants/platform - VARIANTS_PER_PLATFORM below controls
# how many are actually used.
STYLE_ANGLES = [
    {"label": "A", "direction": (
        "a candid, professional photograph: natural lighting, real depth of field, an "
        "authentic small-business setting. It should look like a real photo a good "
        "photographer took, not a staged, overly-polished, obviously-AI-generated scene."
    )},
    {"label": "B", "direction": (
        "a clean, modern flat-design graphic: bold simple shapes, confident use of a small "
        "colour palette, generous negative space, no photorealism - the kind of graphic a "
        "design agency would hand a client, not clip-art."
    )},
]
VARIANTS_PER_PLATFORM = len(STYLE_ANGLES)


def _prompt(cfg, platform: str, variant_index: int, avoid: list[str] | None = None) -> str:
    spec = PLATFORM_SPECS[platform]
    style = STYLE_ANGLES[variant_index % len(STYLE_ANGLES)]
    avoid_txt = ""
    if avoid:
        avoid_txt = " Do NOT repeat or closely resemble any of these recent posts: " + " | ".join(a[:90] for a in avoid[-12:]) + "."
    return f"""You are planning one social media post for {cfg.your_name}, who runs UmairConsult. \
Its offer: {cfg.pitch}. Platform: {spec['label']}. Audience: owners of local and small businesses.

This is variant {style['label']} of {VARIANTS_PER_PLATFORM} for {spec['label']} today - make it \
visually and conceptually distinct from the other variant, not a repeat of the same idea or composition.

Come up with ONE fresh, specific content idea (not a generic ad, not a tired template).{avoid_txt} Then write:
- "caption": the post text for {spec['label']}, natural and non-salesy, at most 3 relevant hashtags, no invented statistics, awards or client stories.
- "cta": one short call to action (a few words).
- "image_prompt": a detailed visual description (composition, mood, colors, setting) for an AI image generator. \
Describe {style['direction']} Use strong visual design principles: one clear focal point, balanced framing \
(e.g. rule of thirds), no clutter, no text baked into the image, no logos of any kind. Leave clean, \
uncluttered empty space in one corner suitable for a small logo to be added afterwards.
- "alt_text": a short accessibility description of the image.

Return ONLY a JSON object with exactly these keys: caption, cta, image_prompt, alt_text."""


def _slug(label: str, year: int) -> str:
    """'POST OCT 5' + 2026 -> 'post-oct-5-2026' (the year keeps it unique from one year to the next)."""
    return re.sub(r"[^a-z0-9]+", "-", f"{label} {year}".lower()).strip("-")


def next_post_label(portal: Portal, today: date) -> str:
    """'POST OCT 5' - the counter lives in the portal's own settings storage so it survives
    across runs and across the two scheduled runs each day, and automatically resets to 1 on
    the 1st of a new month (the label switches to the new month's abbreviation on its own)."""
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
    """{'instagram': [0], 'linkedin': [0, 1], ...} - which variant indexes are already done
    today. Read fresh at the start of every run so the second run of the day knows what the
    first one already finished."""
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
    """Returns False (and only logs) if the portal could not store it - the image is already safe in
    Drive by then, so this must never turn a finished image into a "failure"."""
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
    """Runs the pipeline for every configured platform x variant that isn't already done
    today. Never raises - one image failing is recorded in out['failures'] and everything else
    still gets a chance to run."""
    out = {"done": 0, "already_done_today": 0, "failed": 0, "target": 0, "failures": [], "posts": []}
    if not gemini or gemini.exhausted:
        out["failures"].append("no working Gemini key - cannot write captions or generate images")
        return out

    platforms = [p for p in (cfg.social_platforms or list(PLATFORM_SPECS))]
    out["target"] = len(platforms) * VARIANTS_PER_PLATFORM

    try:
        image_gen = build_image_generator(cfg, gemini)
    except RuntimeError as e:
        out["failures"].append(str(e))
        return out
    try:
        drive = build_drive(cfg)
        drive.ping()          # fail once, clearly, before spending any image quota
    except DriveError as e:
        out["failures"].append(f"Google Drive not usable: {e}")
        return out

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
            out["target"] -= VARIANTS_PER_PLATFORM
            continue
        done_variants = set(progress.get(platform, []))

        for vi in range(VARIANTS_PER_PLATFORM):
            if vi in done_variants:
                out["already_done_today"] += 1
                continue
            if image_gen.unavailable or gemini.exhausted:
                out["failures"].append(f"{spec['label']} variant {STYLE_ANGLES[vi]['label']}: no Gemini key can make images right now "
                                       f"(image models usually need billing switched on for at least one key) - will retry next run")
                continue
            variant_label = STYLE_ANGLES[vi]["label"]
            try:
                concept = _first_dict(gemini.generate_json(_prompt(cfg, platform, vi, avoid), max_tokens=2048, temperature=0.9))
                caption = str(concept.get("caption", "")).strip()
                cta = str(concept.get("cta", "")).strip()
                image_prompt = str(concept.get("image_prompt", "")).strip()
                if not caption or not image_prompt:
                    raise RuntimeError("Gemini did not return a usable caption/image idea")

                raw = image_gen.generate(image_prompt, spec.get("aspect"))
                sized = fit_to_size(raw, spec["size"])
                branded = apply_logo(sized, cfg.logo_path, cfg.logo_position, cfg.logo_margin,
                                     cfg.logo_width, cfg.logo_opacity)
                check_image(branded, expected_size=spec["size"])

                label = next_post_label(portal, today)
                uploaded = drive.upload(branded, f"{label} - {spec['label']} {variant_label}.png", spec["mime"])
            except (BrandingError, QualityError, DriveError, RuntimeError, PortalError) as e:
                reason = f"{spec['label']} variant {variant_label}: {safe_exc(e, 220)}"
                out["failures"].append(reason)
                log(f"Social post skipped - {reason}")
                continue
            except Exception as e:  # noqa: BLE001 - one image failing must not stop the others
                reason = f"{spec['label']} variant {variant_label}: unexpected problem ({safe_exc(e, 150)})"
                out["failures"].append(reason)
                log(f"Social post skipped - {reason}")
                continue

            # ---- the image is safely in Drive from here on: never make it "fail" and get made twice ----
            done_variants.add(vi)
            progress[platform] = sorted(done_variants)
            _save_progress(portal, today, progress)
            avoid.append(caption)
            post_text = caption + (f"\n\n{cta}" if cta else "")
            extras = {"platform": platform, "platform_label": spec["label"], "variant": variant_label, "cta": cta,
                      "post_text": post_text, "alt_text": str(concept.get("alt_text", "")).strip(),
                      "image_size": f"{spec['size'][0]}x{spec['size'][1]}", "drive_link": uploaded["link"],
                      "drive_file_id": uploaded["id"], "review_status": "AWAITING_REVIEW",
                      "generated_on": today.isoformat()}
            try:
                portal.add_content(kind="social", title=label, slug=_slug(label, today.year),
                                   excerpt=caption[:2000], body_html="", extras_json=json.dumps(extras))
                # (the portal writes the "ready for review" activity-log line itself when it saves the post)
            except PortalError as e:
                out["failures"].append(f"{label} ({spec['label']} {variant_label}) is saved in Drive but could not be listed on the "
                                       f"portal ({safe_exc(e, 120)}) - the Drive link is in the Slack message")
            out["done"] += 1
            out["posts"].append({"label": label, "platform": platform, "platform_label": spec["label"],
                                 "variant": variant_label, "caption": caption, "cta": cta, "link": uploaded["link"]})

    out["failed"] = max(0, out["target"] - out["done"] - out["already_done_today"])
    return out
