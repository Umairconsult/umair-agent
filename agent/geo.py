"""GEO ("Generative Engine Optimization") checks for your own site: makes sure AI answer
engines (ChatGPT, Claude, Perplexity, Google's AI Overviews) are actually allowed to read
your site and can find a clear description of what you do. Everything here only reads your
site and suggests - it never edits robots.txt/llms.txt or posts anywhere on your behalf."""
from __future__ import annotations
import re

from .agent_utils import domain_of
from .logutil import log, safe_exc
from .portal import Portal
from .web import Fetcher
from .writer import Gemini

# The AI crawlers worth explicitly allowing - each one feeds a different answer engine.
# (Google-Extended is a control token, not a crawler, but it lives in robots.txt the same way.)
AI_BOTS = {
    "GPTBot": "ChatGPT (OpenAI) uses this to browse/train on your site",
    "OAI-SearchBot": "Powers ChatGPT's live web search citations",
    "ClaudeBot": "Claude (Anthropic) uses this to browse your site",
    "anthropic-ai": "An older Anthropic crawler identifier, still sometimes checked separately",
    "PerplexityBot": "Perplexity's answer engine uses this to browse/cite your site",
    "Google-Extended": "Controls whether Google's Gemini/AI Overviews can use your content (separate from regular Googlebot)",
    "CCBot": "Common Crawl - many AI models are trained on this dataset",
    "Amazonbot": "Powers Amazon's Alexa/AI answers",
}

# A small, hand-picked list of genuine, reputable places worth an accurate, up-to-date
# profile so AI engines can find and cite you - manual to-dos only, never auto-posted.
GEO_PROFILE_TODOS = [
    ("Crunchbase", "https://www.crunchbase.com", "Free company profile - many AI engines treat it as a trusted source for what a business does."),
    ("Google Business Profile", "https://business.google.com", "Also read by AI Overviews, not just Google Maps - keep hours/services/description accurate."),
    ("LinkedIn Company Page", "https://www.linkedin.com/company/setup/new/", "A well-filled-out page is a common citation source for \"who is this company\" questions."),
    ("Relevant Reddit/Quora threads", "", "Answering real questions genuinely and transparently (disclosing who you are) in your niche - "
        "AI engines increasingly cite forum answers. Never post anonymously pretending to be a random customer."),
]


def _parse_robots(body: str) -> dict:
    """Very small robots.txt reader: for each User-agent block, is it disallowed under '/'?"""
    blocks: dict[str, list[str]] = {}
    current: list[str] = []
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            current = blocks.setdefault(val, [])
        elif key == "disallow" and current is not None:
            current.append(val)
    return blocks


def _bot_blocked(blocks: dict[str, list[str]], bot: str) -> bool | None:
    """True if this bot is disallowed from '/' (fully blocked), False if explicitly allowed,
    None if there's no rule naming it at all (falls under the default '*' block, if any)."""
    for name, disallows in blocks.items():
        if name.lower() == bot.lower():
            return any(d.strip() == "/" for d in disallows)
    star = blocks.get("*")
    if star is not None:
        return any(d.strip() == "/" for d in star)
    return None


def _faq_prompt(cfg) -> str:
    return f"""You write content for {cfg.your_name}'s consultancy website {cfg.own_website}. The business offer: {cfg.pitch}.
Write 4 short, plainly-worded FAQ-style question-and-answer pairs that a local business owner would actually ask, and that
directly and factually answer the question in 2-3 sentences (no hype, no invented statistics, no client names or numbers you
weren't given). These are meant to be quoted directly by AI answer engines like ChatGPT and Perplexity, so each answer must
stand alone and be true on its own. Return ONLY JSON: {{"faqs": [{{"q": "...", "a": "..."}}, ...]}}"""


def run_geo_check(cfg, portal: Portal, gemini: Gemini | None, fetcher: Fetcher, today: str) -> dict:
    out = {"issues": 0, "ideas": 0}
    month = today[:7]
    if portal.state_list("geo:month").get("geo:month", "") == month:
        return out
    site = cfg.own_website.rstrip("/")
    domain = domain_of(cfg.own_website)

    # ---- robots.txt: is each AI crawler actually allowed? ----
    got = fetcher.get(site + "/robots.txt")
    if not got:
        r = portal.add_seo(kind="geo", page_url=cfg.own_website, title="No robots.txt found (or it could not be read)",
                           details="AI crawlers default to \"allowed\" with no robots.txt at all, but adding one that explicitly "
                                   "allows GPTBot, ClaudeBot, PerplexityBot and Google-Extended removes any doubt, and lets you "
                                   "block anything else you don't want crawled.")
        out["issues"] += 1 if r.get("created") else 0
    else:
        _, body = got
        blocks = _parse_robots(body)
        blocked = [bot for bot in AI_BOTS if _bot_blocked(blocks, bot)]
        if blocked:
            lines = "\n".join(f"- {b}: {AI_BOTS[b]}" for b in blocked)
            r = portal.add_seo(kind="geo", page_url=cfg.own_website, title=f"{len(blocked)} AI crawler(s) blocked in robots.txt",
                               details=f"Your robots.txt currently blocks:\n{lines}\n\nAdd an explicit \"Allow: /\" block for each "
                                       f"of these user-agents if you want your site included in AI answers and citations.")
            out["issues"] += 1 if r.get("created") else 0

    # ---- llms.txt: does it exist and say anything useful? ----
    got = fetcher.get(site + "/llms.txt")
    if not got:
        r = portal.add_seo(kind="geo", page_url=cfg.own_website, title="No llms.txt found",
                           details="llms.txt is a plain-Markdown file at your site's root that plainly describes what your "
                                   "business does, for AI models to read directly instead of parsing your whole homepage. "
                                   "Not yet supported by every AI engine, but free and low-effort to add. A minimal one is just "
                                   f"a one-line summary of {cfg.your_name}'s consultancy and what it offers, plus links to your "
                                   "main service and contact pages.")
        out["issues"] += 1 if r.get("created") else 0
    else:
        _, body = got
        if len(body.strip()) < 40:
            r = portal.add_seo(kind="geo", page_url=cfg.own_website, title="llms.txt exists but says very little",
                               details=f"Your llms.txt is only {len(body.strip())} characters. Expand it with a clear, "
                                       "plain-language paragraph describing what you do, who you help, and links to your key pages.")
            out["issues"] += 1 if r.get("created") else 0

    # ---- quotable FAQ content ideas (Gemini, same pattern as the existing SEO ideas) ----
    if gemini and not gemini.exhausted:
        try:
            data = gemini.generate_json(_faq_prompt(cfg))
            faqs = (data.get("faqs") or [])[:4]
            if faqs:
                lines = "\n\n".join(f"Q: {f.get('q', '')}\nA: {f.get('a', '')}" for f in faqs if f.get("q") and f.get("a"))
                if lines:
                    r = portal.add_seo(kind="geo_content", page_url=cfg.own_website, title="Quotable FAQ content for AI answer engines",
                                       details="Add these as an FAQPage block (with FAQ schema) on a relevant page. Clear, "
                                               "self-contained Q&A pairs like these are what AI engines tend to quote directly:\n\n" + lines)
                    out["ideas"] += 1 if r.get("created") else 0
        except Exception as e:  # noqa: BLE001 - AI content is a bonus, never blocks the rest of the check
            log(f"GEO FAQ ideas skipped: {safe_exc(e, 120)}")

    # ---- manual profile to-dos, posted once as a note, never repeated ----
    if not portal.state_list("geo:todos_posted").get("geo:todos_posted"):
        lines = "\n".join(f"- {name}{' (' + url + ')' if url else ''}: {why}" for name, url, why in GEO_PROFILE_TODOS)
        portal.add_note("Where else to keep an accurate, citable profile for AI engines", lines, subject=domain, kind="geo")
        portal.state_set("geo:todos_posted", today)

    portal.state_set("geo:month", month)
    if out["issues"] or out["ideas"]:
        portal.log("seo", f"GEO check for {domain}: {out['issues']} robots.txt/llms.txt issue(s), {out['ideas']} content idea(s) added")
    return out
