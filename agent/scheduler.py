"""The agent's daily routine: find leads -> audit -> write messages -> SEO -> brief."""
from __future__ import annotations
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import osm
from .agent_utils import domain_of
from .audit import AuditClient, AuditError, lead_score, summarize_audit
from .cities import CITIES, COUNTRY_NAMES, COUNTRY_WEIGHT
from .logutil import log, safe_exc
from .niches import NICHES, NO_FREE_SOURCE, usable_niches, word_match
from .portal import Portal, PortalError
from .seo import run_seo
from .slack import Slack
from .web import Fetcher, enrich_from_website, normalize_phone, clean_email
from .writer import Gemini, write_messages

RESEARCH_DAYS = 60          # don't repeat the same city+niche search within this many days
PER_SEARCH_MAX = 40         # max new leads taken from one city+niche search


# ------------------------------------------------------------------ small pure helpers
def local_now(cfg) -> datetime:
    try:
        return datetime.now(ZoneInfo(cfg.timezone))
    except Exception:  # noqa: BLE001
        return datetime.utcnow()


def warmup_target(cfg, start: date, today: date) -> int:
    """Outreach per day grows from START to DAILY over WARMUP_DAYS."""
    day = max(0, (today - start).days)
    if day >= cfg.warmup_days:
        return cfg.daily_target
    span = cfg.daily_target - cfg.start_target
    return max(1, round(cfg.start_target + span * day / cfg.warmup_days))


def channel_plan(cfg, target: int) -> dict:
    caps = cfg.channel_caps
    total = sum(caps.values()) or 1
    plan = {k: min(v, round(target * v / total)) for k, v in caps.items()}
    return plan


def pick_searches(cfg, done: dict, today: date, k: int, rng=random) -> list[tuple]:
    names, pri = usable_niches(cfg.blocked_niches, cfg.priority_niches)
    combos = []
    for cc in cfg.countries:
        for rank, (city, lat, lon) in enumerate(CITIES.get(cc, [])):
            for niche in names:
                key = f"search:{cc}:{city}:{niche}"
                if key in done:
                    try:
                        if (today - date.fromisoformat(done[key])).days < RESEARCH_DAYS:
                            continue
                    except ValueError:
                        pass
                w = COUNTRY_WEIGHT.get(cc, 1.0) * (3.0 if niche in pri else 1.0) * (1.6 if rank < 5 else 1.0)
                combos.append((w, cc, city, lat, lon, niche, key))
    picks = []
    for _ in range(min(k, len(combos))):
        total = sum(c[0] for c in combos)
        r = rng.uniform(0, total)
        acc = 0.0
        for i, c in enumerate(combos):
            acc += c[0]
            if acc >= r:
                picks.append(combos.pop(i)[1:])
                break
    return picks


def initial_score(lead: dict, priority: bool, restricted: bool) -> int:
    s = 10
    if lead.get("email"): s += 15
    if lead.get("phone"): s += 10
    if lead.get("facebook_url") or lead.get("linkedin_url") or lead.get("instagram_url"): s += 5
    if priority: s += 10
    if restricted: s -= 25
    return max(0, min(50, s))


class Deadline:
    def __init__(self, minutes: float):
        self.end = time.time() + minutes * 60

    def left(self) -> float: return self.end - time.time()
    def over(self) -> bool: return self.left() <= 0
    def slice(self, fraction: float) -> "Deadline":
        d = Deadline(0)
        d.end = min(self.end, time.time() + max(0.0, self.left()) * fraction)
        return d


# ------------------------------------------------------------------ phases
def phase_find_leads(cfg, portal: Portal, slack: Slack, fetcher: Fetcher, stats: dict, dl: Deadline, dnc: list) -> dict:
    res = {"created": 0, "searches": 0, "skipped": 0, "by_country": {}, "errors": 0}
    room = cfg.max_new_leads - stats.get("leads_today", 0)
    if room <= 0:
        log(f"Lead limit for today reached ({cfg.max_new_leads}).")
        return res
    today = date.fromisoformat(local_now(cfg).strftime("%Y-%m-%d"))
    done = portal.state_list("search:")
    dnc_domains = {i["value"] for i in dnc if i["kind"] == "domain"}
    dnc_emails = {i["value"] for i in dnc if i["kind"] == "email"}
    priority = cfg.priority_niches
    seen_domains: set[str] = set()
    ok_session = 0
    while room > 0 and not dl.over():
        picks = pick_searches(cfg, done, today, 1)
        if not picks:
            log("Every city+niche combination was searched recently. Nothing new to search.")
            break
        cc, city, lat, lon, niche, key = picks[0]
        log(f"Searching {niche} near {city}, {cc}")
        try:
            elements = osm.search(lat, lon, 15000, NICHES[niche], city)
        except osm.OverpassError as e:
            log(f"OpenStreetMap problem: {safe_exc(e, 100)}")
            res["errors"] += 1
            if res["errors"] >= 3:
                break
            time.sleep(15)
            continue
        res["searches"] += 1
        candidates = []
        for el in elements:
            name = el["business_name"]
            if word_match(name, cfg.blocked_niches):
                res["skipped"] += 1
                continue
            dom = domain_of(el["website"])
            if not dom or dom in seen_domains or dom in dnc_domains:
                res["skipped"] += 1
                continue
            seen_domains.add(dom)
            candidates.append(el)
        candidates = candidates[:PER_SEARCH_MAX]

        def work(el):
            info = enrich_from_website(fetcher, el["website"], cc)
            return el, info

        created_here = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(work, el) for el in candidates]
            for fut in as_completed(futures):
                if room <= 0 or dl.over():
                    break
                try:
                    el, info = fut.result()
                except Exception:  # noqa: BLE001
                    res["skipped"] += 1
                    continue
                if not info["reachable"]:
                    res["skipped"] += 1
                    continue
                email = clean_email(el.get("email", "")) or info["email"]
                if email and email in dnc_emails:
                    res["skipped"] += 1
                    continue
                phone = normalize_phone(el.get("phone", ""), cc) or info["phone"]
                lead = {
                    "business_name": el["business_name"], "website": el["website"], "email": email, "phone": phone,
                    "country": cc, "region": el["region"], "address": el["address"], "niche": niche,
                    "facebook_url": el["facebook_url"] or info["facebook_url"],
                    "instagram_url": el["instagram_url"] or info["instagram_url"],
                    "linkedin_url": el["linkedin_url"] or info["linkedin_url"],
                    "contact_page_url": info["contact_page_url"], "source": "osm",
                }
                if not (lead["email"] or lead["phone"] or lead["facebook_url"] or lead["linkedin_url"] or lead["contact_page_url"]):
                    res["skipped"] += 1        # no way to reach them
                    continue
                restricted = bool(word_match(niche + " " + el["business_name"], cfg.restricted_niches))
                is_pri = any(p in niche or niche in p for p in priority) if priority else False
                lead["score"] = initial_score(lead, is_pri, restricted)
                if restricted:
                    lead["notes"] = "Restricted ad category (needs Google/Meta approval) - low priority."
                try:
                    r = portal.upsert_lead(**lead)
                except PortalError as e:
                    log(f"Could not save a lead: {safe_exc(e, 100)}")
                    res["errors"] += 1
                    continue
                if r.get("created"):
                    created_here += 1
                    room -= 1
                    res["created"] += 1
                    res["by_country"][cc] = res["by_country"].get(cc, 0) + 1
        try:
            portal.state_set(key, today.isoformat())
            done[key] = today.isoformat()
        except PortalError:
            pass
        log(f"  -> {created_here} new leads saved from this search")
        time.sleep(3)   # be gentle with the free OpenStreetMap servers
    if res["created"]:
        portal.log("lead_found", f"Found {res['created']} new leads (" + ", ".join(f"{COUNTRY_NAMES.get(c, c)}: {n}" for c, n in res["by_country"].items()) + ")")
    return res


def phase_audit(cfg, portal: Portal, audit: AuditClient, stats: dict, dl: Deadline) -> dict:
    res = {"done": 0, "bad": 0, "transient": 0}
    room = cfg.max_audits - stats.get("audits_today", 0)
    if room <= 0:
        log(f"Audit limit for today reached ({cfg.max_audits}).")
        return res
    failed: set[int] = set()
    consecutive = 0
    while room > 0 and not dl.over() and consecutive < 5:
        batch = [l for l in portal.leads_to_audit(limit=min(10, room) + len(failed)) if int(l["id"]) not in failed]
        if not batch:
            break
        for lead in batch[:min(10, room)]:
            if dl.over() or consecutive >= 5:
                break
            lid = int(lead["id"])
            try:
                r = audit.run(lead["website"])
            except AuditError as e:
                log(f"Audit tool problem: {safe_exc(e, 100)}")
                failed.add(lid); res["transient"] += 1; consecutive += 1
                continue
            if not r.get("ok"):
                if r.get("unreachable"):
                    portal.mark_bad_data(lid, "website unreachable")
                    res["bad"] += 1
                else:
                    failed.add(lid); res["transient"] += 1
                consecutive = 0 if r.get("unreachable") else consecutive + 1
                continue
            consecutive = 0
            ls = lead_score(r, int(lead.get("score") or 0))
            portal.save_audit(id=lid, audit_score=r.get("health_score"), audit_summary=summarize_audit(r), lead_score=ls)
            res["done"] += 1
            room -= 1
            time.sleep(cfg.audit_pause)
    if res["done"] or res["bad"]:
        portal.log("audit", f"Audited {res['done']} websites ({res['bad']} unreachable and set aside)")
    return res


def phase_write(cfg, portal: Portal, gemini: Gemini | None, stats: dict, dl: Deadline, target_today: int) -> dict:
    res = {"written": 0, "ai": 0}
    need = target_today - (stats.get("ready_to_send", 0) + stats.get("contacted_today", 0))
    if need <= 0:
        log(f"Enough ready-to-send leads for today ({target_today}).")
        return res
    while need > 0 and not dl.over():
        batch = portal.leads_to_write(limit=min(10, need))
        if not batch:
            break
        for lead in batch:
            if dl.over() or need <= 0:
                break
            msgs, used_ai = write_messages(lead, cfg, gemini)
            portal.save_messages(id=int(lead["id"]), **msgs)
            res["written"] += 1
            res["ai"] += 1 if used_ai else 0
            need -= 1
    if res["written"]:
        portal.log("messages", f"Prepared {res['written']} ready-to-send messages ({res['ai']} written by AI, {res['written'] - res['ai']} from templates)")
    return res


def phase_brief(cfg, portal: Portal, slack: Slack, target_today: int) -> bool:
    now = local_now(cfg)
    today = now.strftime("%Y-%m-%d")
    if now.hour < cfg.brief_hour:
        return False
    if portal.state_list("brief:last").get("brief:last") == today:
        return False
    st = portal.stats()
    plan = channel_plan(cfg, target_today)
    text = (f"*:robot_face: AI Agent daily brief - {today}*\n"
            f"- New leads found today: *{st['leads_today']}* (total {st['leads_total']})\n"
            f"- Websites audited today: *{st['audits_today']}*\n"
            f"- Ready to send now: *{st['ready_to_send']}* (today's target {target_today})\n"
            f"- Contacted today: *{st['contacted_today']}* | Replies: *{st['replied']}* | Follow-ups due: *{st['followups_due']}*\n"
            f"*Suggested plan for today:* :email: {plan['email']} email, :speech_balloon: {plan['whatsapp']} WhatsApp, "
            f"LinkedIn {plan['linkedin']}, Messenger {plan['messenger']}\n"
            f"Open your portal: {cfg.portal_url}/admin_ai_agent.php")
    if slack.agent(text):
        portal.state_set("brief:last", today)
        return True
    return False


# ------------------------------------------------------------------ one full cycle
def run_cycle(cfg, portal: Portal, slack: Slack, audit: AuditClient | None, gemini: Gemini | None,
              fetcher: Fetcher, minutes: float) -> dict:
    summary: dict = {"errors": []}
    overall = Deadline(minutes)
    stats = portal.stats()
    today_dt = date.fromisoformat(local_now(cfg).strftime("%Y-%m-%d"))
    start_s = portal.state_list("agent:started").get("agent:started")
    if not start_s:
        start_s = today_dt.isoformat()
        portal.state_set("agent:started", start_s)
        portal.log("system", "AI Agent started for the first time")
    target_today = warmup_target(cfg, date.fromisoformat(start_s), today_dt)
    log(f"Today's outreach target: {target_today} (warm-up day {(today_dt - date.fromisoformat(start_s)).days + 1})")
    dnc = portal.get_dnc()

    def guarded(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one broken step must not stop the others
            msg = f"{name} failed: {safe_exc(e)}"
            log(msg)
            summary["errors"].append(msg)
            portal.log("error", msg)
            return None

    r1 = guarded("Lead finder", lambda: phase_find_leads(cfg, portal, slack, fetcher, stats, overall.slice(0.30), dnc))
    if r1:
        summary["leads"] = r1
        if r1["created"]:
            slack.leads(f":sparkles: *{r1['created']} new leads* found: " + ", ".join(f"{COUNTRY_NAMES.get(c, c)} {n}" for c, n in r1["by_country"].items()))
    stats = portal.stats()
    if audit:
        r2 = guarded("Audits", lambda: phase_audit(cfg, portal, audit, stats, overall.slice(0.55)))
        if r2: summary["audits"] = r2
    else:
        log("Audit tool not configured (AUDIT_URL / AUDIT_API_TOKEN) - skipping audits.")
    stats = portal.stats()
    r3 = guarded("Message writer", lambda: phase_write(cfg, portal, gemini, stats, overall.slice(0.9), target_today))
    if r3: summary["messages"] = r3

    last_seo = portal.state_list("seo:last_run").get("seo:last_run", "")
    if audit and (not last_seo or (today_dt - date.fromisoformat(last_seo)).days >= 7):
        r4 = guarded("SEO check", lambda: run_seo(cfg, portal, audit, gemini, today_dt.isoformat()))
        if r4: summary["seo"] = r4
    guarded("Daily brief", lambda: phase_brief(cfg, portal, slack, target_today))

    if summary["errors"]:
        slack.error("\n".join("- " + e for e in summary["errors"][:5]))
    return summary
