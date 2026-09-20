"""Weekly SEO check of YOUR website. Suggests fixes on the portal's SEO page.
The agent never edits your live site - you approve or reject each item."""
from __future__ import annotations
import time

from .audit import AuditClient, AuditError
from .logutil import log, safe_exc
from .portal import Portal
from .writer import Gemini


def _ideas_prompt(cfg, res: dict) -> str:
    return f"""You are an SEO assistant for {cfg.your_name}'s consultancy website {cfg.own_website}. The business offer: {cfg.pitch}.
Current homepage title: "{res.get('title', '')}" ({res.get('title_len', 0)} characters).
Current meta description: "{res.get('meta_desc', '')}" ({res.get('meta_len', 0)} characters).
Give practical, honest suggestions. Return ONLY JSON with these keys:
"new_title": string (max 60 characters, includes the main service),
"new_meta_description": string (120-155 characters, ends with a call to action),
"blog_topics": array of exactly 3 strings (blog post titles that answer real questions a local business owner would search),
"service_page_ideas": array of exactly 2 strings (landing page ideas, each with the target keyword)."""


def run_seo(cfg, portal: Portal, audit: AuditClient | None, gemini: Gemini | None, today: str) -> dict:
    out = {"new_items": 0, "issues": 0, "ideas": 0, "ok": False}
    if not audit:
        return out
    try:
        res = audit.run(cfg.own_website)
    except AuditError as e:
        portal.log("error", f"SEO check could not run: {safe_exc(e, 150)}")
        return out
    if not res.get("ok"):
        portal.log("error", "SEO check: could not open your website: " + str(res.get("error", ""))[:150])
        return out

    for issue in (res.get("top_issues") or [])[:8]:
        title = issue.get("issue", "").strip()
        if not title:
            continue
        r = portal.add_seo(kind="audit", page_url=cfg.own_website, title=title,
                           details=f"Priority: {issue.get('priority', '')} · Area: {issue.get('area', '')}\n\nSuggested fix: {issue.get('fix', '')}")
        out["issues"] += 1
        out["new_items"] += 1 if r.get("created") else 0

    month = today[:7]
    done_month = portal.state_list("seo:ideas_month").get("seo:ideas_month", "")
    if gemini and not gemini.exhausted and done_month != month:
        try:
            ideas = gemini.generate_json(_ideas_prompt(cfg, res))
            items = []
            if ideas.get("new_title"):
                items.append(("Suggested homepage title", f"Current: {res.get('title', '')}\nNew: {ideas['new_title']}"))
            if ideas.get("new_meta_description"):
                items.append(("Suggested homepage meta description", f"Current: {res.get('meta_desc', '')}\nNew: {ideas['new_meta_description']}"))
            for t in (ideas.get("blog_topics") or [])[:3]:
                items.append((f"Blog idea: {t}", "Write a helpful post answering this question for local business owners. Add it to your blog and link it to a service page."))
            for t in (ideas.get("service_page_ideas") or [])[:2]:
                items.append((f"Landing page idea: {t}", "Create a dedicated page for this keyword, with one clear call to action and a short FAQ."))
            for title, details in items:
                r = portal.add_seo(kind="content_idea", page_url=cfg.own_website, title=str(title)[:250],
                                   details=str(details)[:4000], status="suggested")
                out["ideas"] += 1
                out["new_items"] += 1 if r.get("created") else 0
            portal.state_set("seo:ideas_month", month)
        except Exception as e:  # noqa: BLE001
            log(f"SEO ideas skipped: {safe_exc(e, 120)}")

    body = (f"Health score: {res.get('health_score')}/100 ({res.get('health_label')}). "
            f"Critical issues: {res.get('critical_count')}, high: {res.get('high_count')}. "
            f"Mobile speed: {res.get('mobile_perf')}. New suggestions added: {out['new_items']}.")
    portal.add_note(f"SEO check {today}", body)
    portal.log("seo", f"Checked your website: score {res.get('health_score')}/100, {out['new_items']} new suggestions")
    portal.state_set("seo:last_run", today)
    out["ok"] = True
    return out
