"""The agent's daily routine: find leads -> audit -> write messages -> follow-ups -> blog -> SEO -> brief."""
from __future__ import annotations
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import associations, osm, registries
from .agent_utils import domain_of
from .audit import AuditClient, lead_score, summarize_audit
from .ads_check import check_google_ads
from .portal_queue import read_queue, remove_queued, queue_size
from .blog import run_blog
from .cities import CITIES, COUNTRY_NAMES, COUNTRY_WEIGHT
from .followups import run_followups
from .geo import run_geo_check
from .hiring import hiring_boost
from .leadqueue import (LeadQueue, load_agent_state, save_agent_state,
                         load_dnc_cache, save_dnc_cache, emails_sent_today,
                         record_emails_sent, utcnow_iso, QUEUE_FILE,
                         STATUS_AUDITED, STATUS_EMAILED)
from .logutil import log, safe_exc
from . import mailer
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
def phase_find_leads(cfg, portal: Portal, queue: LeadQueue, state: dict, slack: Slack, fetcher: Fetcher,
                   stats: dict, dl: Deadline, dnc: list, overture=None) -> dict:
    """Lead finding writes to the LOCAL lead queue (state/leads_queue.json) - never to the
    portal directly. The sync job mirrors new leads to the Sheet and the portal."""
    res = {"created": 0, "searches": 0, "skipped": 0, "by_country": {}, "errors": 0, "overture": 0, "osm": 0,
           "registry": 0, "association": 0}
    try:
        ctl = portal.state_list("ctl:")
    except PortalError:
        ctl = {}
        log("Portal unreachable - pause controls skipped, lead finding continues from the local queue")
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
    room = min(cfg.max_new_leads - stats.get("leads_today", 0), cfg.max_leads_per_run)
    if room <= 0:
        log(f"Lead limit reached (per-run cap {cfg.max_leads_per_run}, daily cap {cfg.max_new_leads}).")
        return res
    if stats.get("unaudited", 0) > cfg.max_backlog:
        log(f"{stats['unaudited']} leads are still waiting for an audit - auditing first, finding more later.")
        return res
    today = date.fromisoformat(local_now(cfg).strftime("%Y-%m-%d"))
    done = state.get("search_done", {})
    if not isinstance(done, dict):
        done = {}
    dnc_domains = {i["value"] for i in dnc if i["kind"] == "domain"}
    dnc_emails = {i["value"] for i in dnc if i["kind"] == "email"}
    priority = cfg.priority_niches
    seen_domains: set[str] = set(queue.domains())  # never re-add a domain the queue already has
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
                if not lead["email"]:
                    res["skipped"] += 1        # no email address found — cannot be emailed
                    continue
                restricted = bool(word_match(niche + " " + el["business_name"], cfg.restricted_niches))
                is_pri = any(p in niche or niche in p for p in priority) if priority else False
                lead["score"] = initial_score(lead, is_pri, restricted)
                if restricted:
                    lead["notes"] = "Restricted ad category (needs Google/Meta approval) - low priority."
                # Durable local queue (dedup by domain) - the sync job uploads to the portal later.
                if queue.add_lead(lead):
                    created_here += 1
                    room -= 1
                    res["created"] += 1
                    res["by_country"][ecc] = res["by_country"].get(ecc, 0) + 1
                else:
                    res["skipped"] += 1
        search_done = state.setdefault("search_done", {})
        for key in finished:
            search_done[key] = today.isoformat()
            done[key] = today.isoformat()
        queue.save(); save_agent_state(state)
        log(f"  -> {created_here} new leads saved from this search (local queue)")
        time.sleep(2)   # be gentle with the free servers
    if res["created"]:
        try:
            portal.log("lead_found", f"Found {res['created']} new leads (" + ", ".join(f"{COUNTRY_NAMES.get(c, c)}: {n}" for c, n in res["by_country"].items()) + ")")
        except PortalError:
            pass
    return res


def phase_audit(cfg, portal: Portal, queue: LeadQueue, audit: AuditClient, stats: dict, dl: Deadline,
              fetcher: Fetcher | None = None) -> dict:
    """Audits run against the LOCAL queue. The audit itself prefers the direct audit
    endpoint (AuditClient -> audit/agent_audit.php, independent of the portal API);
    the portal's audit_run is only a fallback. Results are saved to the queue."""
    res = {"done": 0, "bad": 0, "transient": 0, "hiring": 0}
    room = cfg.max_audits - stats.get("audits_today", 0)
    if room <= 0:
        log(f"Audit limit for today reached ({cfg.max_audits}).")
        return res
    failed: set[str] = set()
    consecutive = 0
    use_direct = audit is not None
    if use_direct:
        log("Audits will use the direct audit endpoint (portal-independent)")
    while room > 0 and not dl.over() and consecutive < 5:
        batch = [l for l in queue.candidates_for_audit(limit=min(10, room) + len(failed))
                 if l["domain"] not in failed]
        if not batch:
            break
        for lead in batch[:min(10, room)]:
            if dl.over() or consecutive >= 5:
                break
            dom = lead["domain"]
            r = None
            if use_direct:
                try:
                    # Independent path: straight to audit/agent_audit.php, no portal API involved.
                    r = audit.run(lead["website"], full=True)
                except Exception as e:  # noqa: BLE001 - e.g. bad AUDIT_API_TOKEN in secrets
                    log(f"Direct audit failed for {dom} ({safe_exc(e, 100)}) - falling back to the portal's audit")
                    use_direct = False  # broken token: don't keep hammering it this run
            if r is None:
                try:
                    # Fallback: the portal's own audit token.
                    r = portal.audit_run(lead["website"])
                except PortalError as e:
                    # The portal is throttling/blocking audits right now (its own
                    # retry waits already burned minutes). Stop the whole phase
                    # instead of retry-sleeping through the run: leads stay queued
                    # (status=new) and are retried on the next run.
                    log(f"Portal audit unavailable ({safe_exc(e, 100)}) - stopping audit phase, will retry next run")
                    res["transient"] += 1
                    room = 0
                    break
            if not r.get("ok"):
                if r.get("unreachable"):
                    queue.mark_bad(dom, "website unreachable")
                    queue.save()
                    res["bad"] += 1
                    consecutive = 0
                else:
                    failed.add(dom); res["transient"] += 1; consecutive += 1
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
            # Keep the full audit blob for the portal mirror (capped: huge blobs
            # would bloat the git-committed queue; "TOOBIG" matches the old convention).
            blob = r.get("blob") or "TOOBIG"
            if isinstance(blob, str) and len(blob) > 200000:
                blob = "TOOBIG"
            queue.update(dom, status=STATUS_AUDITED,
                         audit_score=r.get("health_score"), audit_summary=summary_txt,
                         lead_score=ls, audited_at=utcnow_iso(), audit_blob=blob)
            queue.save()
            if hire_note:
                res["hiring"] += 1
            res["done"] += 1
            room -= 1
            time.sleep(cfg.audit_pause)
    if res["done"] or res["bad"]:
        extra = f", {res['hiring']} showing hiring signals" if res["hiring"] else ""
        try:
            portal.log("audit", f"Audited {res['done']} websites with full reports ({res['bad']} unreachable and set aside{extra})")
        except PortalError:
            pass
    return res



ADS_CHECK_PER_RUN = 20      # max domains checked per run (Google rate-limits ~60-80/IP)
ADS_CHECK_GAP_SECS = 4      # be gentle with the Transparency Center


def phase_ads_check(cfg, portal: Portal, queue: LeadQueue, dl: Deadline) -> dict:
    """Check Google Ads Transparency for audited leads' ad activity. Results are
    saved to the local queue (the sync job mirrors them to the portal)."""
    res = {"checked": 0, "with_ads": 0, "no_ads": 0, "failed": 0, "rate_limited": False}
    cands = [l for l in queue.data["leads"]
             if l.get("status") == STATUS_AUDITED and not l.get("google_ads_checked_at")][:ADS_CHECK_PER_RUN]
    for lead in cands:
        if dl.over():
            break
        domain = lead.get("domain") or ""
        if not domain:
            res["failed"] += 1
            continue
        try:
            r = check_google_ads(domain)
        except Exception as e:  # noqa: BLE001 - one bad check must not stop the phase
            log(f"Ads check failed for {domain}: {safe_exc(e, 80)}")
            res["failed"] += 1
            continue
        if r.get("error") == "rate_limited":
            log("Google Ads Transparency rate-limited us — pausing ads checks until next run")
            res["rate_limited"] = True
            break
        queue.update(domain,
                     google_ads_checked_at=utcnow_iso(),
                     google_ads_count=r["ad_count"] if r["has_ads"] else 0,
                     google_ads_advertiser=r.get("advertiser_name") or "",
                     google_ads_error=r.get("error") or "")
        queue.save()
        res["checked"] += 1
        if r["has_ads"]:
            res["with_ads"] += 1
        else:
            res["no_ads"] += 1
        time.sleep(ADS_CHECK_GAP_SECS)
    if res["checked"]:
        try:
            portal.log("ads_check",
                       f"Checked Google Ads Transparency for {res['checked']} domains: "
                       f"{res['with_ads']} running ads, {res['no_ads']} none found"
                       + (" (rate-limited, will resume)" if res["rate_limited"] else ""))
        except PortalError:
            pass
    return res


DAILY_SEND_CAP = 250          # max outreach emails per day across all runs (user 2026-10-08: min 250 quality sends)
PER_RUN_SEND_MAX = 25       # per-run ceiling so sends pace through the day
SEND_GAP_SECS = 5           # be gentle with Gmail SMTP
MIN_AUDIT_SCORE = 70        # 2026-10-08: only email leads scoring at/above the
                            # quality bar (user's standing accuracy-over-volume
                            # order). Set to 0 to disable.
# Domains that are never a real business website (video/social/link hosts).
# 2026-10-08: the agent emailed Pet Wow (youtu.be) and ARCCADD (anchor.fm).
JUNK_DOMAINS = ("youtu.be", "youtube.com", "anchor.fm", "facebook.com",
                "instagram.com", "tiktok.com", "linkedin.com", "linktr.ee",
                "linktree.com", "podcasts.apple.com", "spotify.com",
                "vimeo.com")


def phase_send(cfg, portal: Portal, queue: LeadQueue, state: dict, dl: Deadline, dnc: list) -> dict:
    """Send personalized outreach emails through Gmail SMTP.

    HARD RULES (user instruction 2026-10-03):
    - An email is sent ONLY for a lead with a completed, successful website
      audit (audit_score present and a usable audit summary). No audit ->
      no email, no exceptions. mailer.build_email() raises instead of
      building an empty/generic email.
    - Never to DNC addresses, bad-channel leads, already-contacted leads,
      or restricted niches.
    - Max 150 emails/day across all runs (tracked in the LOCAL agent state,
      not the portal - so the cap holds even when the portal is unreachable).
    Candidates come from the local lead queue. Portal logging (outreach_log)
    is best-effort: on failure the write is queued by Path B automatically.
    Skips gracefully when GMAIL_APP_PASSWORD is not set in SETTINGS_ENV.
    """
    res = {"sent": 0, "failed": 0, "skipped": 0}
    password = cfg.get("GMAIL_APP_PASSWORD")
    if not password:
        log("Email sending skipped: GMAIL_APP_PASSWORD not set in SETTINGS_ENV")
        return res
    today = local_now(cfg).strftime("%Y-%m-%d")
    sent_today = emails_sent_today(state, today)
    remaining = DAILY_SEND_CAP - sent_today
    if remaining <= 0:
        log(f"Daily email cap reached ({DAILY_SEND_CAP}/day) — no more sends today")
        return res
    budget = min(remaining, PER_RUN_SEND_MAX)

    dnc_emails = {i["value"] for i in dnc if i["kind"] == "email"}
    cands = queue.candidates_for_email(limit=50)

    seen_emails: set[str] = set()
    for lead in cands:
        if res["sent"] >= budget or dl.over():
            break
        dom = lead.get("domain") or ""
        email = clean_email(lead.get("email", ""))
        if email.lower().endswith(".edu"):
            res["skipped"] += 1        # .edu addresses are not ICP — never emailed
            continue
        # ---- HARD RULE: completed successful audit required ----
        if lead.get("audit_score") is None or not (lead.get("audit_summary") or "").strip():
            res["skipped"] += 1
            continue
        # ---- QUALITY BAR (2026-10-08): audit must score at/above the bar ----
        if int(lead.get("audit_score") or 0) < MIN_AUDIT_SCORE:
            log(f"Lead {dom}: not emailed (audit {lead.get('audit_score')}/100 below bar {MIN_AUDIT_SCORE})")
            res["skipped"] += 1
            continue
        # ---- JUNK DOMAIN (2026-10-08): never email a social/video platform ----
        if any(dom == j or dom.endswith("." + j) for j in JUNK_DOMAINS):
            log(f"Lead {dom}: not emailed (junk platform domain, not a business website)")
            res["skipped"] += 1
            continue
        if not email or email in seen_emails or email in dnc_emails:
            res["skipped"] += 1
            continue
        if "email" in (lead.get("bad_channels") or ""):
            res["skipped"] += 1
            continue
        if lead.get("emailed_at"):
            res["skipped"] += 1       # already contacted
            continue
        if word_match((lead.get("niche") or "") + " " + (lead.get("business_name") or ""), cfg.restricted_niches):
            res["skipped"] += 1
            continue
        # USER RULE (2026-10-06): send to ALL emails found for this business.
        import json as _json
        _raw_extra = lead.get("all_emails") or []
        if isinstance(_raw_extra, str):
            try:
                _extra = _json.loads(_raw_extra)
            except Exception:
                _extra = []
        else:
            _extra = list(_raw_extra)
        _send_list = [email] + [e for e in _extra if e and e != email and e not in seen_emails and e not in dnc_emails and not e.lower().endswith(".edu")]
        for _send_to in _send_list:
            if res["sent"] >= budget or dl.over():
                break
            seen_emails.add(_send_to)
            _lead = dict(lead, email=_send_to)
            try:
                subject, html_body = mailer.build_email(_lead)
            except ValueError as e:
                log(f"Lead {dom} ({_send_to}): not emailed ({e})")
                res["skipped"] += 1
                continue
            except Exception as e:  # noqa: BLE001
                log(f"Lead {dom} ({_send_to}): email build failed ({safe_exc(e, 100)})")
                res["skipped"] += 1
                continue
            try:
                mailer.send_email(_send_to, subject, html_body, password)
            except Exception as e:  # noqa: BLE001
                log(f"Lead {dom} ({_send_to}): send failed ({safe_exc(e, 120)})")
                res["failed"] += 1
                # Best-effort portal log (auto-queued by Path B on failure) - only
                # once the sync job has given this lead a portal id.
                if lead.get("portal_id"):
                    try:
                        portal.outreach_log(lead_id=lead["portal_id"], channel="email", result="failed",
                                            sent_by="agent", subject=subject,
                                            summary=f"SMTP send failed to {_send_to}: {safe_exc(e, 120)}")
                    except PortalError:
                        pass
                continue
            # Best-effort portal log (auto-queued by Path B on failure) - only
            # once the sync job has given this lead a portal id.
            if lead.get("portal_id"):
                try:
                    portal.outreach_log(lead_id=lead["portal_id"], channel="email", result="sent",
                                        sent_by="agent", subject=subject,
                                        summary=f"Audit {lead.get('audit_score')}/100 outreach sent to {_send_to}",
                                        email_html=html_body)
                except PortalError as e:
                    log(f"Lead {dom}: email sent but portal logging failed ({safe_exc(e, 100)}) - queued for later")
            emailed_to = list(lead.get("emailed_to") or [])
            if _send_to not in emailed_to:
                emailed_to.append(_send_to)
            queue.update(dom, status=STATUS_EMAILED, emailed_at=utcnow_iso(),
                         emailed_to=emailed_to, contacted_via="email",
                         email_subject=subject, email_html=html_body)
            queue.save()
            res["sent"] += 1
            sent_today += 1
            record_emails_sent(state, today, 1)
            save_agent_state(state)
            time.sleep(SEND_GAP_SECS)
    if res["sent"] or res["failed"]:
        try:
            portal.log("email_sent", f"Sent {res['sent']} outreach emails ({res['failed']} failed, {res['skipped']} skipped)")
        except PortalError:
            pass
    return res


def phase_send_followups(cfg, portal: Portal, queue: LeadQueue, state: dict, dl: Deadline, dnc: list) -> dict:
    """Send follow-up emails to leads who haven't replied.

    USER RULE (2026-10-06): Only send to addresses that have NOT bounced.
    A lead is skipped if bad_channels contains 'email'.
    Candidates and the daily cap come from the LOCAL queue/state; the portal
    log is best-effort (only once the sync job has assigned a portal id).
    """
    res = {"sent": 0, "failed": 0, "skipped": 0}
    password = cfg.get("GMAIL_APP_PASSWORD")
    if not password:
        log("Follow-up sending skipped: GMAIL_APP_PASSWORD not set")
        return res
    today = local_now(cfg).strftime("%Y-%m-%d")
    sent_today = emails_sent_today(state, today)
    remaining = DAILY_SEND_CAP - sent_today
    if remaining <= 0:
        return res
    budget = min(remaining, PER_RUN_SEND_MAX)
    dnc_emails = {i["value"] for i in dnc if i["kind"] == "email"}
    cands = queue.candidates_for_followup_send(limit=50)
    seen_emails: set[str] = set()
    for lead in cands:
        if res["sent"] >= budget or dl.over():
            break
        dom = lead.get("domain") or ""
        if "email" in (lead.get("bad_channels") or ""):
            res["skipped"] += 1
            continue
        email = clean_email(lead.get("email", ""))
        if not email or email.lower().endswith(".edu"):
            res["skipped"] += 1
            continue
        if email in seen_emails or email in dnc_emails:
            res["skipped"] += 1
            continue
        fup_body = (lead.get("followup_email_body") or "").strip()
        if not fup_body:
            res["skipped"] += 1
            continue
        if int(lead.get("followup_count") or 0) >= 2:
            res["skipped"] += 1
            continue
        seen_emails.add(email)
        fup_num = int(lead.get("followup_count") or 0) + 1
        subject = f"Re: {lead.get('email_subject', 'Quick follow-up')}"
        try:
            mailer.send_email(email, subject, fup_body, password)
        except Exception as e:  # noqa: BLE001
            log(f"Lead {dom}: follow-up send failed ({safe_exc(e, 120)})")
            res["failed"] += 1
            continue
        if lead.get("portal_id"):
            try:
                portal.outreach_log(lead_id=lead["portal_id"], channel="email", result="sent",
                                    sent_by="agent", subject=subject,
                                    summary=f"Follow-up #{fup_num} sent",
                                    email_html=fup_body)
            except PortalError:
                pass
        queue.update(dom, followup_count=fup_num, followup_sent_at=utcnow_iso())
        queue.save()
        res["sent"] += 1
        sent_today += 1
        record_emails_sent(state, today, 1)
        save_agent_state(state)
        time.sleep(SEND_GAP_SECS)
    if res["sent"] or res["failed"]:
        try:
            portal.log("followup_sent", f"Sent {res['sent']} follow-ups ({res['failed']} failed)")
        except PortalError:
            pass
    return res


def phase_write(cfg, portal: Portal, queue: LeadQueue, gemini: Gemini | None, dl: Deadline) -> dict:
    """Writes messages for every audited lead that doesn't have one yet, so any lead that's been
    found - today or in the past - becomes 'ready to send' as soon as possible. Messages are
    stored in the LOCAL queue (the sync job mirrors them to the portal). This is deliberately
    NOT throttled by today's outreach pace."""
    res = {"written": 0, "ai": 0, "save_failed": 0}
    skipped: set[str] = set()       # leads whose messages could not be written this run (tried again next run)
    consecutive_fail = 0
    while not dl.over() and consecutive_fail < 3:
        batch = [l for l in queue.candidates_for_message_write(limit=50) if l["domain"] not in skipped]
        if not batch:
            break
        for lead in batch:
            if dl.over() or consecutive_fail >= 3:
                break
            msgs, used_ai = write_messages(lead, cfg, gemini)
            queue.update(lead["domain"], **{k: v for k, v in msgs.items()
                                            if k in ("email_subject", "email_body", "whatsapp_message",
                                                     "social_message")})
            queue.save()
            consecutive_fail = 0
            res["written"] += 1
            res["ai"] += 1 if used_ai else 0
    if res["written"]:
        try:
            portal.log("messages", f"Prepared {res['written']} ready-to-send messages ({res['ai']} written by AI, {res['written'] - res['ai']} from templates)")
        except PortalError:
            pass
    return res


def phase_brief(cfg, portal: Portal, slack: Slack, target_today: int, gemini: Gemini | None = None) -> bool:
    try:
        return _phase_brief_inner(cfg, portal, slack, target_today, gemini)
    except PortalError:
        # Portal unreachable: the brief is a nice-to-have Slack summary, not the pipeline.
        # Stay quiet instead of raising (the guarded() wrapper would spam Slack with errors).
        log("Daily brief skipped: portal unreachable")
        return False


def _phase_brief_inner(cfg, portal: Portal, slack: Slack, target_today: int, gemini: Gemini | None = None) -> bool:
    now = local_now(cfg)
    today = now.strftime("%Y-%m-%d")
    if now.hour < cfg.brief_hour:
        return False
    if portal.state_list("brief:last").get("brief:last") == today:
        return False
    st = portal.stats()
    plan = channel_plan(cfg, target_today)
    drafts = len(portal.content_list(kind="blog", status="draft", limit=50))
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
    try:
        social_waiting = len(portal.content_list(kind="social", status="draft", limit=50))
        if social_waiting:
            lines.append(f"- Social media posts waiting for your review: *{social_waiting}*")
    except PortalError:
        pass
    if gemini and len(gemini.keys) > 1:
        lines.append(f"- AI keys working: {gemini.live_count()} of {len(gemini.keys)}")
    lines.append(f"Open your portal: {cfg.portal_url}/admin_ai_agent.php")
    if slack.agent("\n".join(lines)):
        portal.state_set("brief:last", today)
        return True
    return False


# ------------------------------------------------------------------ one full cycle

def flush_portal_queue(portal: Portal) -> dict:
    """Path B: deliver queued portal writes from previous failed runs.

    Runs at the start of each cycle. Returns {flushed, failed}.
    """
    res = {"flushed": 0, "failed": 0}
    items = read_queue()
    if not items:
        return res
    log(f"Flushing {len(items)} queued portal writes from previous runs...")
    done = []
    for item in items:
        try:
            # Bypass the queue wrapper — call _call directly so failures raise.
            r = portal._call(item["action"], item["payload"])
            if r.get("ok"):
                done.append(item)
                res["flushed"] += 1
            else:
                res["failed"] += 1
        except Exception as e:  # noqa: BLE001 - keep trying the rest
            log(f"Queue flush failed for {item['action']}: {safe_exc(e, 80)}")
            res["failed"] += 1
            break  # portal still down — stop, try again next run
    if done:
        remove_queued(done)
    return res


def run_cycle(cfg, portal: Portal, slack: Slack, audit: AuditClient | None, gemini: Gemini | None,
              fetcher: Fetcher, minutes: float, overture=None, wp: WordPress | None = None) -> dict:
    """One full lead-pipeline cycle, driven by the LOCAL lead queue (state/leads_queue.json).

    The portal API is treated as best-effort throughout: reads that fail fall back to
    cached/local state, writes that fail are queued by Path B. A 429 can no longer
    stop the run - finding, auditing and emailing continue from the queue.
    """
    summary: dict = {"errors": []}
    overall = Deadline(minutes)

    # ---- durable local state (the pipeline's source of truth) ----
    queue = LeadQueue()
    state = load_agent_state()
    today_dt = date.fromisoformat(local_now(cfg).strftime("%Y-%m-%d"))
    today_s = today_dt.isoformat()
    if not state.get("agent_started"):
        state["agent_started"] = today_s
        save_agent_state(state)

    def queue_stats() -> dict:
        return queue.stats(today_s)

    stats = queue_stats()
    start_s = state.get("agent_started", today_s)
    target_today = warmup_target(cfg, date.fromisoformat(start_s), today_dt)
    log(f"Today's outreach target: {target_today} (warm-up day {(today_dt - date.fromisoformat(start_s)).days + 1})")
    log(f"Local lead queue: {len(queue)} leads ({stats['unaudited']} waiting for audit, {stats['to_contact']} ready to contact)")

    # DNC: refresh from the portal when reachable, otherwise use the cached copy.
    try:
        dnc = portal.get_dnc()
        save_dnc_cache(dnc)
    except PortalError as e:
        dnc = load_dnc_cache()
        log(f"Portal unreachable for DNC list - using cached copy ({len(dnc)} entries): {safe_exc(e, 100)}")

    def guarded(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one broken step must not stop the others
            msg = f"{name} failed: {safe_exc(e)}"
            log(msg)
            summary["errors"].append(msg)
            try:
                portal.log("error", msg)
            except PortalError:
                pass
            return None

    # Path B: flush any queued writes from previous runs where the portal was unreachable.
    r0 = guarded("Queue flush", lambda: flush_portal_queue(portal))
    if r0 and (r0["flushed"] or r0["failed"]):
        summary["queue_flush"] = r0

    r1 = guarded("Lead finder", lambda: phase_find_leads(cfg, portal, queue, state, slack, fetcher, queue_stats(), overall.slice(0.25), dnc, overture))
    if r1:
        summary["leads"] = r1
        if r1["created"]:
            slack.leads(f":sparkles: *{r1['created']} new leads* found: " + ", ".join(f"{COUNTRY_NAMES.get(c, c)} {n}" for c, n in r1["by_country"].items()))
    r2 = guarded("Audits", lambda: phase_audit(cfg, portal, queue, audit, queue_stats(), overall.slice(0.65), fetcher))
    if r2: summary["audits"] = r2
    r2a = guarded("Ads check", lambda: phase_ads_check(cfg, portal, queue, overall))
    if r2a: summary["ads_check"] = r2a
    r2b = guarded("Email sending", lambda: phase_send(cfg, portal, queue, state, overall, dnc))
    if r2b: summary["emails"] = r2b
    r2c = guarded("Follow-up sending", lambda: phase_send_followups(cfg, portal, queue, state, overall, dnc))
    if r2c: summary["followups_sent"] = r2c
    r3 = guarded("Message writer", lambda: phase_write(cfg, portal, queue, gemini, overall.slice(0.7)))
    if r3: summary["messages"] = r3
    if not overall.over():
        r5 = guarded("Follow-ups", lambda: run_followups(cfg, portal, queue, gemini, overall.slice(0.5)))
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

    try:
        last_seo = portal.state_list("seo:last_run").get("seo:last_run", "")
    except PortalError:
        last_seo = ""
    if audit and (not last_seo or (today_dt - date.fromisoformat(last_seo)).days >= 7):
        r4 = guarded("SEO check", lambda: run_seo(cfg, portal, audit, gemini, today_dt.isoformat()))
        if r4: summary["seo"] = r4
    try:
        last_geo = portal.state_list("geo:last_run").get("geo:last_run", "")
    except PortalError:
        last_geo = ""
    if not last_geo or (today_dt - date.fromisoformat(last_geo)).days >= 7:
        r4b = guarded("GEO check", lambda: run_geo_check(cfg, portal, gemini, fetcher, today_dt.isoformat()))
        if r4b:
            summary["geo"] = r4b
            try:
                portal.state_set("geo:last_run", today_dt.isoformat())
            except PortalError:
                pass
    guarded("Daily brief", lambda: phase_brief(cfg, portal, slack, target_today, gemini))

    # Final save + the workflow commits state/ at the end of the run.
    queue.save()
    save_agent_state(state)
    log(f"Lead queue saved: {QUEUE_FILE} ({len(queue)} leads)")

    if gemini and gemini.dead:
        summary["gemini_keys_lost"] = len(gemini.dead)
    if summary["errors"]:
        slack.error("\n".join("- " + e for e in summary["errors"][:5]))
    return summary


