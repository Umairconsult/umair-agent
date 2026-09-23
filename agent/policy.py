"""White-Hat Policy Engine: a hard-coded checklist (not just an AI prompt) that every blog
post must pass BEFORE it goes live, even if you clicked Approve - catches an AI writing
mistake your own review might miss. A few items on your original checklist aren't runtime
checks at all; they're prevented by what this codebase simply doesn't build, listed below
so it's clear the whole checklist is actually covered, not just the parts that are easy to
check in code."""
from __future__ import annotations
import re

# ---- prevented by never building the capability, not by a runtime check ----
STRUCTURALLY_PREVENTED = {
    "Fake backlinks / link schemes / private blog networks": "This agent has no link-building, "
        "backlink-buying, guest-post-swap, or cross-site posting code of any kind.",
    "Automated spam comments / fake engagement": "This agent never posts comments, reviews, likes, "
        "or interacts with any site other than umairconsult.com's own CMS.",
    "Cloaking": "A post is generated once and published as-is - there is no code path that could "
        "serve different content to search engines than to visitors.",
    "Doorway pages / automatically generated mass low-value pages": "BLOG_POSTS_PER_WEEK caps volume "
        "(default 2/week) and every single post requires your Approve click on the portal - there is "
        "no bulk-publish path anywhere in this codebase.",
    "Manipulative structured data": "Schema generation (seo.py) only emits LocalBusiness/Service JSON-LD "
        "built from settings you filled in yourself - Review/AggregateRating schema is never generated, "
        "since no real review data is ever collected.",
}

# Puffery/unverifiable-superlative phrases - each is a WARNING (human judgment), not a hard block,
# since some of these can appear in a genuinely-fine sentence.
PUFFERY = [r"\bindustry[- ]leading\b", r"\b#1\b", r"\bguarantee(d)?\b", r"\bworld[- ]class\b",
           r"\bas seen on\b", r"\baward[- ]winning\b", r"\bbest[- ]in[- ]class\b"]
SUSPICIOUS_STAT = re.compile(r"\b\d{1,3}(\.\d+)?%\b|\$\s?\d[\d,]*\+?\b")
FILLER = re.compile(r"\blorem ipsum\b", re.I)


def _keyword_density(body_text: str, keyword: str) -> float:
    words = re.findall(r"[a-z']+", body_text.lower())
    kw = keyword.lower().split()
    if not words or not kw:
        return 0.0
    n, count = len(kw), 0
    for i in range(len(words) - n + 1):
        if words[i:i + n] == kw:
            count += 1
    return count / max(1, len(words)) * 100


def _near_duplicate(body_text: str, past_bodies: list[str], threshold: float = 0.6) -> str | None:
    """Rough shingled-word-overlap check against your own past posts - not a plagiarism
    detector, just a guard against the agent quietly rewriting the same post twice."""
    words = set(re.findall(r"[a-z']{4,}", body_text.lower()))
    if len(words) < 20:
        return None
    for i, past in enumerate(past_bodies):
        pwords = set(re.findall(r"[a-z']{4,}", past.lower()))
        if not pwords:
            continue
        overlap = len(words & pwords) / max(1, len(words | pwords))
        if overlap >= threshold:
            return f"a recently published post ({overlap:.0%} word overlap)"
    return None


def check_draft(title: str, body_text: str, target_keyword: str = "", past_bodies: list[str] | None = None) -> dict:
    """body_text should be PLAIN text (strip HTML first - see blog.plain_text). Returns
    {"blocked": bool, "reasons": [...], "warnings": [...]}. `reasons` are hard stops: this
    draft cannot go live, even if already marked Approved, until it's fixed and re-approved.
    `warnings` are surfaced for your own judgment and never block publishing on their own."""
    reasons: list[str] = []
    warnings: list[str] = []
    full = f"{title}\n{body_text}"

    if FILLER.search(full):
        reasons.append("Contains placeholder text (e.g. 'lorem ipsum') - looks incomplete.")

    if target_keyword:
        density = _keyword_density(body_text, target_keyword)
        if density > 4.0:
            reasons.append(f'"{target_keyword}" appears at {density:.1f}% density - reads as keyword '
                           f"stuffing. Reword so it appears naturally, well under ~2%.")
        elif density > 2.5:
            warnings.append(f'"{target_keyword}" density is {density:.1f}% - a little high, worth a read-through.')

    for pat in PUFFERY:
        if re.search(pat, full, re.I):
            warnings.append(f"Contains an unverifiable superlative claim (matches /{pat}/) - "
                            f"reads as manipulative puffery rather than real information.")

    stats = SUSPICIOUS_STAT.findall(body_text)
    if stats:
        warnings.append(f"Contains {len(stats)} specific number/stat claim(s) - confirm each is real "
                        f"and something you can back up, not invented by the AI.")

    if past_bodies:
        dup = _near_duplicate(body_text, past_bodies)
        if dup:
            reasons.append(f"Looks like a near-duplicate of {dup} - too similar to something already published.")

    if len(body_text.split()) < 150:
        warnings.append("Under 150 words once rendered as plain text - thin content reads as low-effort.")

    return {"blocked": bool(reasons), "reasons": reasons, "warnings": warnings}
