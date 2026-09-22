"""The agent's daily routine: find leads -> audit -> write messages -> follow-ups -> blog -> SEO -> brief."""
from __future__ import annotations
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import associations, osm, registries
from .agent_utils import domain_of
from .audit import AuditClient, AuditError, lead_score, summarize_audit
from .blog import run_blog
from .cities import CITIES, COUNTRY_NAMES, COUNTRY_WEIGHT
from .followups import run_followups
from .geo import run_geo_check
from .hiring import hiring_boost
from .logutil import log, safe_exc
from .niches import osm_filters, ov_keywords, usable_niches, word_match
from .portal import Portal, PortalError
from .seo import apply_approved_seo, run_page_seo, run_seo
from .slack import Slack
from .web import Fetcher, clean_email, enrich_from_website, normalize_phone
from .wordpress import WordPress, publish_approved
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
    return {k: min(v, round(target * v / total)) for k, v in caps.items()}


def _recent(done: dict, key: str, today: date) -> bool:
    if key not in done:
        return False
    try:
        return (today - date.fromisoformat(done[key])).days < RESEARCH_DAYS
    except ValueError:
        return False


def pick_searches(cfg, done: dict, today: date, k: int, overture_on: bool = False, rng=random,
                   registries_on: bool = False, associations_on: bool = False) -> list[tuple]:
    """Choose the next city+niche searches. Returns (cc, city, lat, lon, niche, sources) where
    sources lists which data sources ('osm', 'ov', 'reg', 'assoc') still need to be searched
    for that combination."""
    names, pri = usable_niches(cfg.blocked_niches, cfg.priority_niches, overture_on)
    combos = []
    for cc in cfg.countries:
        for rank, (city, lat, lon) in enumerate(CITIES.get(cc, [])):
            for niche in names:
                due = []
                if osm_filters(niche) and not _recent(done, f"search:{cc}:{city}:{niche}", today):
                    due.append("osm")
                if overture_on and ov_keywords(niche) and not _recent(done, f"ov:{cc}:{city}:{niche}", today):
                    due.append("ov")
                if registries_on and registries.available(cc) and not _recent(done, f"reg:{cc}:{city}:{niche}", today):
                    due.append("reg")
                # association directories are national, not per-city - only attach it to
                # the first city of that country so it's not scheduled once per city
                if (associations_on and rank == 0 and associations.ASSOCIATIONS.get(niche)
                        and not _recent(done, f"assoc:{cc}:{niche}", today)):
                    due.append("assoc")
                if not due:
                    continue
                w = COUNTRY_WEIGHT.get(cc, 1.0) * (3.0 if niche in pri else 1.0) * (1.6 if rank < 5 else 1.0)
                combos.append((w, cc, city, lat, lon, niche, due))
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
def phase_find_leads(cfg, portal: Portal, slack: Slack, fetcher: Fetcher, stats: dict, dl: Deadline, dnc: list, overture=None) -> dict:
    res = {"created": 0, "searches": 0, "skipped": 0, "by_country": {}, "errors": 0, "overture": 0, "osm": 0,
           "registry": 0, "association": 0}
    ctl = portal.state_list("ctl:")
    if ctl.get("ctl:pause_finding") == "1":
        log("Lead finding is PAUSED from the portal - not looking for new leads.")
        res["paused"] = 1
        return res
    try:
        stop_at = int(ctl.get("ctl:auto_pause_at", "0") or 0)
    except ValueError:
        stop_at = 0
    if stop_at > 0 and stats.get("to_contact", 0) >= stop_at:
        log(f"Auto-pause: {stats['to_contact']} leads are waiting to be contacted (limit {stop_at}) - not looking for new leads.")
        res["auto_paused"] = 1
        return res
    room = cfg.max_new_leads - stats.get("leads_today", 0)
    if room <= 0:
        log(f"Lead limit for today reached ({cfg.max_new_leads}).")
        return res
    if stats.get("unaudited", 0) > cfg.max_backlog:
        log(f"{stats['unaudited']} leads are still waiting for an audit - auditing first, finding more later.")
        return res
    today = date.fromisoformat(local_now(cfg).strftime("%Y-%m-%d"))
    done = {**portal.state_list("search:"), **portal.state_list("ov:"), **portal.state_list("reg:"), **portal.state_list("assoc:")}
    dnc_domains = {i["value"] for i in dnc if i["kind"] == "domain"}
    dnc_emails = {i["value"] for i in dnc if i["kind"] == "email"}
    priority = cfg.priority_niches
    seen_domains: set[str] = set()
    ov_failures = 0
    reg_on = cfg.use_registries and bool(cfg.companies_house_key)
    assoc_on = cfg.use_associations
    SRC_LABEL = {"ov": "Overture", "osm": "OpenStreetMap", "reg": "business registry", "assoc": "association directory"}
    while room > 0 and not dl.over():
        overture_on = overture is not None and ov_failures < 3
        picks = pick_searches(cfg, done, today, 1, overture_on, registries_on=reg_on, associations_on=assoc_on)
        if not picks:
            log("Every city+niche combination was searched recently. Nothing new to search.")
            break
        cc, city, lat, lon, niche, sources = picks[0]
        log(f"Searching {niche} near {city}, {cc} ({' + '.join(SRC_LABEL.get(s, s) for s in sources)})")
        elements: list[dict] = []
        finished: list[str] = []
        if "ov" in sources and overture is not None:
            try:
                got = overture.search(lat, lon, 15000, ov_keywords(niche), city)
                elements += got
                finished.append(f"ov:{cc}:{city}:{niche}")
                res["overture"] += len(got)
            except Exception as e:  # noqa: BLE001 - Overture problems must never stop the run
                ov_failures += 1
                log(f"Overture problem: {safe_exc(e, 120)}")
        if "osm" in sources:
            try:
                got = osm.search(lat, lon, 15000, osm_filters(niche), city)
                for g in got:
                    g["source"] = "osm"
                elements += got
                finished.append(f"search:{cc}:{city}:{niche}")
                res["osm"] += len(got)
            except osm.OverpassError as e:
                log(f"OpenStreetMap problem: {safe_exc(e, 100)}")
                res["errors"] += 1
                if res["errors"] >= 3:
                    break
                time.sleep(10)
        if "reg" in sources:
            try:
                got = registries.search(cc, niche, city, cfg.companies_house_key)
                elements += got
                finished.append(f"reg:{cc}:{city}:{niche}")
                res["registry"] += len(got)
            except registries.RegistryError as e:  # noqa: BLE001 - never stops the run
                log(f"Business registry problem: {safe_exc(e, 100)}")
        if "assoc" in sources:
            try:
                got = associations.search(niche, cc)
                elements += got
                finished.append(f"assoc:{cc}:{niche}")
                res["association"] += len(got)
            except Exception as e:  # noqa: BLE001 - a bad directory page must never stop the run
                log(f"Association directory problem: {safe_exc(e, 100)}")
        res["searches"] += 1
        candidates = []
        for el in elements:
            ecc = (el.get("country") or cc).upper()
            dom = domain_of(el["website"])
            if ecc not in cfg.countries or word_match(el["business_name"], cfg.blocked_niches) \
                    or not dom or dom in seen_domains or dom in dnc_domains:
                res["skipped"] += 1
                continue
            seen_domains.add(dom)
            el["_cc"] = ecc
            candidates.append(el)
        candidates = candidates[:PER_SEARCH_MAX]

        def work(el):
            return el, enrich_from_website(fetcher, el["website"], el["_cc"])

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
                ecc = el["_cc"]
                email = clean_email(el.get("email", "")) or info["email"]
                if email and email in dnc_emails:
                    res["skipped"] += 1
                    continue
                lead = {
                    "business_name": el["business_name"], "website": el["website"], "email": email,
                    "phone": normalize_phone(el.get("phone", ""), ecc) or info["phone"],
                    "country": ecc, "region": el.get("region", city), "address": el.get("address", ""), "niche": niche,
                    "facebook_url": el.get("facebook_url") or info["facebook_url"],
                    "instagram_url": el.get("instagram_url") or info["instagram_url"],
                    "linkedin_url": el.get("linkedin_url") or info["linkedin_url"],
                    "contact_page_url": info["contact_page_url"], "source": el.get("source", "osm"),
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
                    res["by_country"][ecc] = res["by_country"].get(ecc, 0) + 1
        for key in finished:
            try:
                portal.state_set(key, today.isoformat())
                done[key] = today.isoformat()
            except PortalError:
                pass
        log(f"  -> {created_here} new leads saved from this search")
        time.sleep(2)   # be gentle with the free servers
    if res["created"]:
        portal.log("lead_found", f"Found {res['created']} new leads (" + ", ".join(f"{COUNTRY_NAMES.get(c, c)}: {n}" for c, n in res["by_country"].items()) + ")")
    return res


def phase_audit(cfg, portal: Portal, audit: AuditClient, stats: dict, dl: Deadline, fetcher: Fetcher | None = None) -> dict:
    res = {"done": 0, "bad": 0, "transient": 0, "hiring": 0}
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
                r = audit.run(lead["website"], full=True)
            except AuditError as e:
                log(f"Audit tool problem: {safe_exc(e, 100)}")
                failed.add(lid); res["transient"] += 1; consecutive += 1
                continue
            if not r.get("ok"):
                if r.get("unreachable"):
                    portal.mark_bad_data(lid, "website unreachable")
                    res["bad"] += 1
                    consecutive = 0
                else:
                    failed.add(lid); res["transient"] += 1; consecutive += 1
                continue
            consecutive = 0
            if "blob" not in r:      # an OLD audit door cannot return full reports - stop instead of doing useless work
                raise RuntimeError("Your audit door (audit/agent_audit.php on Hostinger) is an OLD version and cannot return full reports. "
                                   "Replace it with the file from the update zip (open portal/agent_doctor.php to check).")
            boost, hire_note = (0, "")
            if cfg.use_hiring_signals:
                try:
                    boost, hire_note = hiring_boost(lead["website"], fetcher)
                except Exception:  # noqa: BLE001 - a hiring check must never break an audit
                    boost, hire_note = 0, ""
            ls = lead_score(r, min(50, int(lead.get("score") or 0)), boost)
            summary_txt = summarize_audit(r) + (f" · {hire_note}" if hire_note else "")
            portal.save_audit(id=lid, audit_score=r.get("health_score"), audit_summary=summary_txt,
                              lead_score=ls, audit_blob=r.get("blob") or "TOOBIG")
            if hire_note:
                res["hiring"] += 1
            res["done"] += 1
            room -= 1
            time.sleep(cfg.audit_pause)
    if res["done"] or res["bad"]:
        extra = f", {res['hiring']} showing hiring signals" if res["hiring"] else ""
        portal.log("audit", f"Audited {res['done']} websites with full reports ({res['bad']} unreachable and set aside{extra})")
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


def phase_brief(cfg, portal: Portal, slack: Slack, target_today: int, gemini: Gemini | None = None) -> bool:
    now = local_now(cfg)
    today = now.strftime("%Y-%m-%d")
    if now.hour < cfg.brief_hour:
        return False
    if portal.state_list("brief:last").get("brief:last") == today:
        return False
    st = portal.stats()
    plan = channel_plan(cfg, target_today)
    drafts = len(portal.content_list(status="draft", limit=50))
    ctl = portal.state_list("ctl:")
    lines = [f"*:robot_face: AI Agent daily brief - {today}*"]
    if ctl.get("ctl:pause_finding") == "1":
        lines.append(":pause_button: *Lead finding is PAUSED* (resume it on the portal's AI Agent page)")
    lines += [f"- Leads waiting to be contacted: *{st.get('to_contact', 0)}*",
             f"- New leads found today: *{st['leads_today']}* (total {st['leads_total']})",
             f"- Websites audited today: *{st['audits_today']}* | still waiting for audit: *{st.get('unaudited', 0)}*",
             f"- Ready to send now: *{st['ready_to_send']}* (today's target {target_today})",
             f"- Contacted today: *{st['contacted_today']}* | Replies: *{st['replied']}* | Follow-ups due: *{st['followups_due']}*",
             f"- Blog drafts waiting for your review: *{drafts}*",
             f"*Suggested plan for today:* email {plan['email']}, WhatsApp {plan['whatsapp']}, LinkedIn {plan['linkedin']}, Messenger {plan['messenger']}"]
    if gemini and len(gemini.keys) > 1:
        lines.append(f"- AI keys working: {gemini.live_count()} of {len(gemini.keys)}")
    lines.append(f"Open your portal: {cfg.portal_url}/admin_ai_agent.php")
    if slack.agent("\n".join(lines)):
        portal.state_set("brief:last", today)
        return True
    return False


# ------------------------------------------------------------------ one full cycle
def run_cycle(cfg, portal: Portal, slack: Slack, audit: AuditClient | None, gemini: Gemini | None,
              fetcher: Fetcher, minutes: float, overture=None, wp: WordPress | None = None) -> dict:
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

    r1 = guarded("Lead finder", lambda: phase_find_leads(cfg, portal, slack, fetcher, stats, overall.slice(0.25), dnc, overture))
    if r1:
        summary["leads"] = r1
        if r1["created"]:
            slack.leads(f":sparkles: *{r1['created']} new leads* found: " + ", ".join(f"{COUNTRY_NAMES.get(c, c)} {n}" for c, n in r1["by_country"].items()))
    stats = portal.stats()
    if audit:
        r2 = guarded("Audits", lambda: phase_audit(cfg, portal, audit, stats, overall.slice(0.65), fetcher))
        if r2: summary["audits"] = r2
    else:
        log("Audit tool not configured (AUDIT_URL / AUDIT_API_TOKEN) - skipping audits.")
    stats = portal.stats()
    r3 = guarded("Message writer", lambda: phase_write(cfg, portal, gemini, stats, overall.slice(0.7), target_today))
    if r3: summary["messages"] = r3
    if not overall.over():
        r5 = guarded("Follow-ups", lambda: run_followups(cfg, portal, gemini, overall.slice(0.5)))
        if r5: summary["followups"] = r5
    if not overall.over():
        r6 = guarded("Blog writer", lambda: run_blog(cfg, portal, gemini, today_dt))
        if r6: summary["blog"] = r6
    if wp is not None:
        r7 = guarded("Website publishing", lambda: publish_approved(cfg, portal, wp))
        if r7: summary["website"] = r7
        r8 = guarded("Applying approved SEO fixes", lambda: apply_approved_seo(cfg, portal, wp))
        if r8: summary["seo_applied"] = r8
        if audit and not overall.over():
            r9 = guarded("Page-by-page SEO", lambda: run_page_seo(cfg, portal, audit, gemini, wp, today_dt.isoformat()))
            if r9: summary["page_seo"] = r9

    last_seo = portal.state_list("seo:last_run").get("seo:last_run", "")
    if audit and (not last_seo or (today_dt - date.fromisoformat(last_seo)).days >= 7):
        r4 = guarded("SEO check", lambda: run_seo(cfg, portal, audit, gemini, today_dt.isoformat()))
        if r4: summary["seo"] = r4
    last_geo = portal.state_list("geo:last_run").get("geo:last_run", "")
    if not last_geo or (today_dt - date.fromisoformat(last_geo)).days >= 7:
        r4b = guarded("GEO check", lambda: run_geo_check(cfg, portal, gemini, fetcher, today_dt.isoformat()))
        if r4b:
            summary["geo"] = r4b
            portal.state_set("geo:last_run", today_dt.isoformat())
    guarded("Daily brief", lambda: phase_brief(cfg, portal, slack, target_today, gemini))

    if gemini and gemini.dead:
        summary["gemini_keys_lost"] = len(gemini.dead)
    if summary["errors"]:
        slack.error("\n".join("- " + e for e in summary["errors"][:5]))
    return summary
