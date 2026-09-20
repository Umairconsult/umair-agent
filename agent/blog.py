"""Writes blog drafts for your website in a natural, human voice. Drafts appear on the portal's
AI Agent Blog tab. Nothing goes live until you approve it."""
from __future__ import annotations
import hashlib
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

from .logutil import log, safe_exc
from .portal import Portal
from .writer import Gemini

BANNED = ["delve", "tapestry", "in today's fast-paced", "digital landscape", "game-changer", "game changer", "unlock the",
          "unleash", "elevate your", "seamless", "robust", "leverage", "it's important to note", "it is important to note",
          "in conclusion", "furthermore", "moreover", "navigating the", "ever-evolving", "cutting-edge", "revolutionize",
          "paradigm", "testament to", "at the end of the day", "look no further", "next level", "in the realm of", "harness the power"]
ALLOWED = {"h2", "h3", "p", "ul", "ol", "li", "strong", "em", "a", "blockquote", "br"}
EVERGREEN = [
    "How to tell if your Google Ads are wasting money",
    "Why your website gets visits but no enquiries",
    "Google Business Profile mistakes that cost local businesses calls",
    "What conversion tracking is and why most small businesses get it wrong",
    "A simple weekly SEO routine for a busy business owner",
    "Meta Ads or Google Ads: where should a local business start?",
    "How to read your website's speed report without a developer",
    "Five things to check before you pay an agency for SEO",
]


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "object", "embed"):
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            return
        if tag == "a":
            href = dict(attrs).get("href", "") or ""
            if re.match(r"^(https?://|/|#)", href, re.I):
                self.out.append(f'<a href="{href.replace(chr(34), "%22")}">')
            else:
                self.out.append("<a>")
        elif tag == "br":
            self.out.append("<br>")
        else:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "object", "embed"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip and tag in ALLOWED and tag != "br":
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data.replace("<", "&lt;").replace(">", "&gt;"))

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip:
            self.out.append(f"&#{name};")


def sanitize_html(html: str) -> str:
    c = _Clean()
    c.feed(html or "")
    c.close()
    return "".join(c.out).strip()


def plain_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def humanize(html: str) -> str:
    """Small clean-ups that make AI text read more naturally."""
    html = html.replace(" — ", ", ").replace("—", ", ").replace(" – ", ", ").replace("–", "-")
    html = re.sub(r",\s*,", ",", html)
    return html


def found_banned(text: str) -> list[str]:
    t = text.lower().replace("\u2019", "'")
    return sorted({b for b in BANNED if b in t})


def _prompt(cfg, topic: str, previous: list[str]) -> str:
    return f"""You are ghostwriting a blog post for {cfg.your_name}, who runs UmairConsult, a consultancy. Its offer: {cfg.pitch}.
Audience: owners of local and small businesses in the US, UK, Canada and Australia who are not marketing experts.
Topic: {topic}
Voice: {cfg.blog_voice}.

Write like a real person with hands-on experience, not like an AI:
- Plain words and contractions. Short paragraphs (1 to 4 sentences). Mix short and long sentences. Starting a sentence with "But" or "And" is fine.
- Take a clear point of view: what you would do, what you would skip, and honest trade-offs.
- Use believable examples, but present them as examples ("Say you run a dental clinic in Leeds...") and NEVER as real client stories.
- NEVER invent statistics, studies, quotes, prices, awards or client results. If a number matters, describe it in words or tell the reader how to check it themselves.
- Avoid these words and phrases: {", ".join(BANNED)}. Do not use em dashes (use commas or full stops). Avoid tidy lists of three adjectives and generic openings or closings.
- Open with the reader's real problem in the first two sentences. End with one specific next step, not a summary.
- Do not repeat these existing titles: {"; ".join(previous[:15]) or "none yet"}.

Length: 900 to 1200 words.
Format: HTML using only h2, h3, p, ul, ol, li, strong, em, a. No h1. 4 to 6 h2 sections that are each useful on their own. Finish with <h2>Frequently asked questions</h2> and 3 questions as h3, each followed by a p answer.
Where natural, link to https://umairconsult.com/ using descriptive anchor text (at most 2 links).

Return ONLY a JSON object with these keys:
"title" (max 65 characters, specific, not clickbait), "slug" (lowercase-hyphens), "meta_description" (130 to 155 characters),
"excerpt" (1 to 2 sentence teaser), "body_html", "keywords" (3 to 5 search phrases),
"image_suggestions" (2 to 3 short descriptions of REAL photos or screenshots the owner could add, each with suggested alt text),
"review_notes" (2 to 4 short notes on where the owner should add a REAL example, number or screenshot to make the post truly theirs)."""


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def posts_this_week(portal: Portal, today: date) -> int:
    n = 0
    for it in portal.content_list(kind="blog", limit=60):
        try:
            created = datetime.strptime(it["created_at"][:10], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if created >= _week_start(today):
            n += 1
    return n


def pick_topic(cfg, portal: Portal, gemini: Gemini, previous: list[str], today: date) -> str:
    used = portal.state_list("blog:used:")
    for it in portal.seo_list(title_prefix="Blog idea:", limit=100):
        if it["status"] == "rejected":
            continue
        topic = it["title"].split(":", 1)[1].strip()
        if "blog:used:" + hashlib.md5(topic.lower().encode()).hexdigest()[:12] not in used:
            return topic
    try:
        t = gemini.generate_json(
            f'Suggest ONE new blog topic for {cfg.your_name}\'s consultancy (offer: {cfg.pitch}) that local business owners really search for. '
            f'Existing titles to avoid: {"; ".join(previous[:20]) or "none"}. Return only JSON: {{"topic": "..."}}', max_tokens=200)
        if t.get("topic"):
            return str(t["topic"]).strip()[:150]
    except Exception:  # noqa: BLE001
        pass
    for t in EVERGREEN:
        if "blog:used:" + hashlib.md5(t.lower().encode()).hexdigest()[:12] not in used:
            return t
    return EVERGREEN[today.toordinal() % len(EVERGREEN)]


def run_blog(cfg, portal: Portal, gemini: Gemini | None, today: date) -> dict:
    out = {"written": 0}
    if not gemini or gemini.exhausted or cfg.blog_per_week <= 0:
        return out
    if posts_this_week(portal, today) >= cfg.blog_per_week:
        return out
    previous = [i["title"] for i in portal.content_list(kind="blog", limit=30)]
    topic = pick_topic(cfg, portal, gemini, previous, today)
    log("Writing a blog draft")
    data = gemini.generate_json(_prompt(cfg, topic, previous), max_tokens=8192, temperature=0.85)
    body = humanize(sanitize_html(str(data.get("body_html", ""))))
    words = len(plain_text(body).split())
    title = humanize(str(data.get("title", topic)).strip())[:255]
    if words < 450 or not title:
        portal.log("blog", f"A blog draft was too short ({words} words) and was discarded; the agent will try again on a later run")
        return out
    notes = [str(n)[:300] for n in (data.get("review_notes") or [])][:5]
    bad = found_banned(plain_text(body))
    if bad:
        notes.append("Some phrases here can sound AI-written; consider rewording: " + ", ".join(bad[:8]))
    extras = {"keywords": data.get("keywords") or [], "image_suggestions": data.get("image_suggestions") or [],
              "review_notes": notes, "topic": topic}
    import json
    slug = re.sub(r"[^a-z0-9]+", "-", str(data.get("slug") or title).lower()).strip("-")[:120]
    portal.add_content(kind="blog", title=title, slug=slug, meta_description=humanize(str(data.get("meta_description", "")))[:400],
                       excerpt=humanize(str(data.get("excerpt", "")))[:2000], body_html=body, extras_json=json.dumps(extras),
                       word_count=words)
    portal.state_set("blog:used:" + hashlib.md5(topic.lower().encode()).hexdigest()[:12], today.isoformat())
    out["written"] = 1
    return out
