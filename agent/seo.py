"""Weekly SEO check of YOUR website. Suggests fixes on the portal's SEO page.
The agent never edits your live site - you approve or reject each item."""
from __future__ import annotations
import time

import json
import re
from datetime import date

from .agent_utils import domain_of
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
    from .agent_utils import domain_of
    site = domain_of(cfg.own_website)
    portal.add_note(f"SEO check {today} - {site}", body, subject=site, kind="seo")
    portal.log("seo", f"Checked {site}: score {res.get('health_score')}/100, {out['new_items']} new suggestions")
    portal.state_set("seo:last_run", today)
    out["ok"] = True
    return out


# ====================================================================== page-by-page SEO (WordPress + Rank Math)
SKIP_SLUGS = ("privacy", "terms", "cookie", "thank", "404", "sample-page", "cart", "checkout", "my-account", "refund", "disclaimer")


def _seo_prompt(cfg, path: str, wp_title: str, cur_title: str, cur_desc: str) -> str:
    return f"""You write SEO titles and meta descriptions for the website of {cfg.your_name}'s consultancy, UmairConsult. Its offer: {cfg.pitch}.
Page: {path or '/'} (page name in WordPress: "{wp_title}")
Current SEO title: "{cur_title}"
Current meta description: "{cur_desc}"
Write natural, specific text a person would click. No hype, no ALL CAPS, no emojis, no invented claims or numbers.
Return ONLY JSON: {{"title": "at most 60 characters, includes the main topic or service", "meta_description": "between 120 and 155 characters, plain sentence, ends with a gentle call to action", "focus_keyword": "2 to 4 words people search for"}}"""


def run_page_seo(cfg, portal: Portal, audit, gemini, wp, today: str) -> dict:
    """Checks a few of your own pages per run and proposes better SEO titles/descriptions (applied after you approve)."""
    out = {"checked": 0, "proposed": 0}
    if wp is None or audit is None:
        return out
    if not wp.seo_ready():
        if not portal.state_list("wp:seo_notice").get("wp:seo_notice"):
            portal.add_note("Let the agent edit your SEO titles and descriptions",
                            "WordPress is connected, but the small WPCode snippet is not active yet, so the agent can only suggest SEO changes. "
                            "Install the snippet from the file 'wpcode_snippet_seo_bridge.txt' (steps are in the chat with Claude) and the agent will be able to apply approved SEO fixes for you.",
                            subject=domain_of(cfg.own_website), kind="system")
            portal.state_set("wp:seo_notice", today)
        return out
    site = domain_of(cfg.own_website)
    done = portal.state_list("seo:page:")
    todays = date.fromisoformat(today)
    idx = wp.index()
    entries = [v for v in idx.values() if domain_of(v["link"]) == site and not any(k in v["link"].lower() for k in SKIP_SLUGS)]
    entries.sort(key=lambda v: (0 if v["link"].rstrip("/") in (cfg.own_website.rstrip("/"), "https://" + site, "http://" + site) else 1,
                                0 if v["kind"] == "page" else 1, v["id"]))
    for v in entries:
        if out["checked"] >= cfg.seo_pages_per_run:
            break
        key = f"seo:page:{v['kind']}{v['id']}"
        try:
            if key in done and (todays - date.fromisoformat(done[key])).days < 30:
                continue
        except ValueError:
            pass
        out["checked"] += 1
        portal.state_set(key, today)
        try:
            res = audit.run(v["link"])
        except AuditError:
            continue
        if not res.get("ok"):
            continue
        tl, ml = int(res.get("title_len") or 0), int(res.get("meta_len") or 0)
        if 30 <= tl <= 60 and 70 <= ml <= 160:
            continue                                            # already fine
        if not gemini or gemini.exhausted:
            continue
        try:
            path = "/" + v["link"].split("://", 1)[-1].split("/", 1)[-1].strip("/") if "/" in v["link"].split("://", 1)[-1] else "/"
            new = gemini.generate_json(_seo_prompt(cfg, path, v["title"], res.get("title", ""), res.get("meta_desc", "")), max_tokens=400)
        except Exception as e:  # noqa: BLE001
            log(f"Page SEO AI skipped: {safe_exc(e, 100)}")
            continue
        t = str(new.get("title", "")).strip()
        d = str(new.get("meta_description", "")).strip()
        if not (10 <= len(t) <= 65 and 70 <= len(d) <= 170):
            continue
        kw = str(new.get("focus_keyword", "")).strip()[:60]
        payload = {"object_type": v["kind"], "object_id": v["id"], "page_url": v["link"], "rank_math_title": t, "rank_math_description": d,
                   "rank_math_focus_keyword": kw, "previous": {"title": res.get("title", ""), "description": res.get("meta_desc", "")}}
        r = portal.add_seo(kind="onpage_fix", page_url=v["link"], title=f"SEO title and description for {path or '/'}",
                           details=f"Current title ({tl} characters): {res.get('title', '')}\nCurrent description ({ml} characters): {res.get('meta_desc', '')}\n\n"
                                   f"New title: {t}\nNew description: {d}\nFocus keyword: {kw}",
                           payload=json.dumps(payload), status="suggested")
        if r.get("created"):
            out["proposed"] += 1
            if cfg.seo_auto_apply and r.get("id"):
                portal.seo_update(id=int(r["id"]), status="approved")
    if out["proposed"]:
        portal.log("seo", f"Checked {out['checked']} pages of your website and proposed SEO title/description fixes for {out['proposed']}")
    return out


def apply_approved_seo(cfg, portal: Portal, wp) -> dict:
    """Applies the SEO title/description fixes you approved to your WordPress site."""
    out = {"applied": 0, "failed": 0}
    if wp is None or not wp.seo_ready():
        return out
    for it in portal.seo_list(kind="onpage_fix", status="approved", include_payload=True, limit=20):
        try:
            pl = json.loads(it.get("payload") or "{}")
            wp.set_seo(pl["object_type"], int(pl["object_id"]), pl["rank_math_title"], pl["rank_math_description"], pl.get("rank_math_focus_keyword", ""))
        except (KeyError, ValueError) as e:
            portal.log("error", f"An approved SEO item had unreadable data and was skipped: {safe_exc(e, 100)}")
            out["failed"] += 1
            continue
        except Exception as e:  # noqa: BLE001 - WordPress problems: leave it approved, try again next run
            portal.log("error", f"Could not apply an SEO fix on your website: {safe_exc(e, 140)}")
            out["failed"] += 1
            continue
        portal.seo_update(id=int(it["id"]), status="done")
        portal.log("seo", f"Applied new SEO title and description to {pl.get('page_url', 'a page')} on your website")
        out["applied"] += 1
    return out
