"""Off-page SEO for your own site. Two things, both manual-review-only:
  1. Checks whether your homepage links to a Google Business Profile / Bing Places listing
     (the audit tool can only see what your site LINKS to, not the profile's actual
     completeness - that needs Google's/Microsoft's own dashboard, which this has no access to).
  2. A small, hand-picked list of genuine, reputable directories where a real profile helps -
     surfaced as a one-time manual to-do on the portal. Nothing here is ever submitted,
     posted, or created automatically; you create/update each profile yourself."""
from __future__ import annotations
from .agent_utils import domain_of
from .portal import Portal

# Genuine, reputable directories relevant to a marketing/performance-ads consultancy.
# Hand-picked, not scraped or auto-submitted - review each one before creating a profile,
# same as the industry-association list in associations.py.
DIRECTORIES = [
    ("Clutch", "https://clutch.co", "B2B services directory - widely checked by buyers comparing agencies/consultants."),
    ("GoodFirms", "https://www.goodfirms.co", "Similar to Clutch; free profile, client reviews help credibility."),
    ("DesignRush", "https://www.designrush.com", "Agency directory covering marketing/design/dev services."),
    ("UpCity", "https://upcity.com", "Local-marketing-focused directory with a free profile tier."),
]


def run_offpage_check(cfg, portal: Portal, res: dict, today: str) -> dict:
    """Call with the same compact `res` from audit.run(cfg.own_website) used elsewhere -
    no extra audit call needed."""
    out = {"issues": 0, "todo_posted": False}
    month = today[:7]
    if portal.state_list("offpage:month").get("offpage:month", "") == month:
        return out
    domain = domain_of(cfg.own_website)

    if not res.get("gbp_linked"):
        r = portal.add_seo(kind="offpage", page_url=cfg.own_website, title="No Google Business Profile link found on your homepage",
                           details="Your homepage doesn't appear to link to a Google Business Profile / Google Maps listing. "
                                   "This only checks your own page - if you already have a profile, just add a link to it "
                                   "somewhere on your site (footer or contact page is common). If you don't have one yet, "
                                   "create it free at business.google.com - it's one of the highest-impact free things a "
                                   "local/service business can do for both regular search and AI Overviews.")
        out["issues"] += 1 if r.get("created") else 0
    if not res.get("bing_places_linked"):
        r = portal.add_seo(kind="offpage", page_url=cfg.own_website, title="No Bing Places link found on your homepage",
                           details="Bing Places is free and Bing/Copilot both draw on it. Same caveat as Google Business "
                                   "Profile above - this only checks whether your homepage links to one, not whether one exists.")
        out["issues"] += 1 if r.get("created") else 0

    if not portal.state_list("offpage:directories_posted").get("offpage:directories_posted"):
        lines = "\n".join(f"- {name} ({url}): {why}" for name, url, why in DIRECTORIES)
        portal.add_note("Legitimate directories worth a real profile", lines, subject=domain, kind="offpage")
        portal.state_set("offpage:directories_posted", today)
        out["todo_posted"] = True

    portal.state_set("offpage:month", month)
    if out["issues"]:
        portal.log("seo", f"Off-page check for {domain}: {out['issues']} issue(s) found")
    return out
